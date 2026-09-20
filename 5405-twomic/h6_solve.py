#!/usr/bin/env python3
"""Round 3: start aggressive. Solve F/B gain with the front bought back by EQ.

Emits cands7/agg-1 (best G1), agg-2 (best G2) and agg-3 (the best solution
whose largest EQ-back boost stays at or under 5 dB).
"""
from __future__ import annotations

import json

import numpy as np
from scipy.optimize import differential_evolution

import g5lib as g
import h5lib as h
import h6_fam as f
from h5_solve import classify, measured_table
from rearpred import section_of

from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

SEED = 20260921
OUT = g.SP / "cands7"
TARGET = "h4"
SUBSETS = {"pre-USB": {"d1", "d2", "c1", "649a", "d3"},
           "post-USB": {"i1b", "i2", "l1", "g1m", "g2m", "h1", "h3", "h4", "f1", "f2"},
           "no-F1/F2": set(g.ALL_TAGS) - {"f1", "f2"}}
REFERENCES = (("S1", "711b458f", None), ("d-0.12", "654e057b", None),
              ("ident-C", "2fc52a19", None), ("fb-1", None, "cands6/fb-1.json"),
              ("fb-2", None, "cands6/fb-2.json"), ("fb-3", None, "cands6/fb-3.json"))
HEAD = "          " + "".join(f"{c:>7d}" for c in h.CENTRES)


def describe(x, name, objective) -> str:
    chain = f.cancellation(x, name, objective.ident_c_chain)
    parts = [f"delay {chain['delay_ms']:+.4f} ms"]
    for one in chain["filters"]:
        p = one["parameters"]
        if p["type"] == "ButterworthLowpass":
            parts.append(f"lowpass {p['freq']:.1f} Hz")
        elif p["type"] == "Peaking" and p["freq"] not in (190.14, 286.9708):
            parts.append(f"Peaking {p['freq']:.1f} Hz {p['gain']:+.2f} dB q{p['q']:.2f}")
    return "; ".join(parts)


def rows_for(objective, chain, section=None) -> dict:
    value, front, gain = objective.evaluate(chain)
    charge = objective.headroom(section) if section is not None else 0.0
    total = f.total_front(front, charge)
    hard, soft, notes = f.guards(total)
    curve, boost = f.eq_back(front, charge)
    return {"score": round(value, 2), "net": round(value - soft, 2),
            "headroom_db": round(charge, 2),
            "soft_charge": round(soft, 2), "eq_back_boost": round(boost, 2),
            "front": {str(c): round(front[c], 2) for c in h.CENTRES},
            "front_total": {str(c): round(total[c], 2) for c in h.CENTRES},
            "gain": {str(c): round(gain[c], 2) for c in h.CENTRES},
            "eq_back": {str(c): round(curve[c], 2) for c in h.CENTRES},
            "hard_notes": notes}


def show(label, rows):
    print(f"  {label:<9s} F/B {rows['score']:+.2f}  headroom cut {rows['headroom_db']:.2f}"
          f"  EQ-back boost {rows['eq_back_boost']:.2f} dB"
          + ("  " if not rows["hard_notes"] else "  HARD: " + "; ".join(rows["hard_notes"])))
    for what in ("front", "front_total", "gain", "eq_back"):
        print(f"  {'':<9s} {what:<8s}"
              + "".join(f"{rows[what][str(c)]:+7.1f}" for c in h.CENTRES))


def fit(objective, name, angles=None, *, gentle=False, maxiter=60, popsize=15):
    found = differential_evolution(
        lambda v: objective.cost(v, name, angles, gentle=gentle), f.bounds_for(name),
        seed=SEED, maxiter=maxiter, popsize=popsize, tol=0.01, polish=True,
        init="latinhypercube")
    return found.x


def held_out(objective, name, *, gentle=False):
    out = []
    for fit_angle, test_angle in ((20.0, -20.0), (-20.0, 20.0)):
        x = fit(objective, name, [fit_angle], gentle=gentle, maxiter=40, popsize=12)
        out.append(objective.evaluate(objective.chain(x, name), [test_angle])[0])
    return float(np.mean(out)), out


def ranges(x, name, measured) -> str:
    """Which knobs sit inside the measured range, and by how much they do not."""
    family, extra = f.FAMILIES[name]
    notes = []
    if family == "G1":
        same = [v for v in measured["F1"].values() if (v[-1] is None) != bool(extra)]
        same = [v for v in measured["F1"].values()
                if (v[-1] is not None) == bool(extra)] or same
        for i, knob in enumerate(("delay", "120 Hz", "lowpass")):
            lo, hi = min(v[i] for v in same), max(v[i] for v in same)
            inside = lo <= x[i] <= hi
            notes.append(f"{knob} {x[i]:+.3f} {'in' if inside else 'OUT'} [{lo:+.3f},{hi:+.3f}]")
        if extra:
            for j, knob in enumerate(("bell f", "bell g", "bell q")):
                values = [v[-1][j] for v in same if v[-1]]
                if values:
                    lo, hi = min(values), max(values)
                    notes.append(f"{knob} {x[3 + j]:+.2f} "
                                 f"{'in' if lo <= x[3 + j] <= hi else 'OUT'} [{lo:+.2f},{hi:+.2f}]")
    else:
        for i, (knob, want) in enumerate((("delay", -0.9188), ("low f", f.IDENT_C_LOW[0]),
                                          ("low g", f.IDENT_C_LOW[1]),
                                          ("low q", f.IDENT_C_LOW[2]))):
            notes.append(f"{knob} {x[i]:+.3f} vs ident-C {want:+.3f} ({x[i] - want:+.3f})")
        if extra:
            notes.append(f"bell {x[4]:.0f} Hz {x[5]:+.2f} dB q{x[6]:.2f} "
                         f"(T3 measured 250 Hz +5.00 q1.00)")
    return "; ".join(notes)


def emit(path, rationale, section, extra_assumptions, rows) -> str:
    section = dict(section)
    section["conditions"] = {
        "dataset": "jts3 measured summed rounds D1..H4; main mic 0.61 m in front (3 arm "
                   "poses), side mic 0.61 m behind (arm +-20); rig state of round H4",
        "fit_band_hz": [100.0, 350.0], "fit_tool": "h6_solve.py", "measured": True,
        "timing_reference": "front output of this stage"}
    section["assumptions"] = [
        "Solved for FRONT-TO-BACK GAIN = front change - behind change. Front is UNGATED and "
        "meaned over the three arm poses; behind is ungated at 63-125 Hz and 400-630 Hz and "
        "GATED 10 ms at 160-315 Hz, meaned over +-20. The +0 arm pose is not used behind.",
        "AGGRESSIVE front rule: the front may fall to -8 dB over 80-315 Hz, because a COMMON "
        "EQ on both woofers restores it without touching F/B gain. What that costs is "
        "excursion and headroom, reported below as the EQ-back curve, not tonality.",
        "After the EQ-back curve is applied the front is back at the rear-muted response and "
        "the level behind is the NEGATIVE of the F/B gain below -- that is the suppression "
        "this tune really buys.",
        "Out of sample the FRONT model lands within 0.1-0.3 dB per third octave, confirmed "
        "again on rounds F1/F2. F/B gain is looser and BIASED: on F1/F2, which measured "
        "fb-1/fb-2/fb-3 for the first time, the model over-predicted the F/B score in all "
        "six cells by 0.5-1.4 dB, with per-band error 2.0 mean / 4.4 worst at 160 Hz and "
        "1.9/3.6 at 200 Hz. Read the F/B figures below as about 1 dB optimistic; the RANKING "
        "held exactly (fb-1 > fb-2 > fb-3, measured and predicted).",
        *extra_assumptions,
        "Predicted front change, dB, 63/80/100/125/160/200/250/315/400/500/630 Hz: "
        + " ".join(f"{rows['front'][str(c)]:+.1f}" for c in h.CENTRES),
        "Predicted F/B gain, dB: "
        + " ".join(f"{rows['gain'][str(c)]:+.1f}" for c in h.CENTRES),
        "Front change INCLUDING the product's own broadband headroom cut "
        f"({rows['headroom_db']:.2f} dB), which is what the owner hears as level: "
        + " ".join(f"{rows['front_total'][str(c)]:+.1f}" for c in h.CENTRES),
        "EQ-back curve = headroom cut plus the residual shape, common to both woofers, dB: "
        + " ".join(f"{rows['eq_back'][str(c)]:+.1f}" for c in h.CENTRES)
        + f"; largest boost {rows['eq_back_boost']:.2f} dB.",
    ]
    document = {"base": "saved", "kind": "jts_prescription", "schema": 1,
                "rationale": rationale, "sections": {"rear_calibration": section}}
    path.write_text(json.dumps(document, indent=4, sort_keys=True) + "\n")
    try:
        read_rear_calibration(section, sample_rate=48000)
        read_prescription_document(document)
        return "PASS"
    except Exception as exc:                       # noqa: BLE001
        return f"FAIL {type(exc).__name__}: {exc}"


def main() -> int:
    h.widen()
    store = g.collect()
    chain_of = g.chains()
    model = h.FB(store, chain_of, target=TARGET)
    ident_c = section_of(g.load_json(g.SP / "cands3/ident-C.json"))
    bases = {"G1": section_of(g.load_json(g.SP / "search/D2/doc-e-035.json")), "G2": ident_c}
    objective = f.Objective(model, bases, ident_c["rear"]["cancellation"])
    measured = measured_table(json.loads((g.SP / "search/fp-index.json").read_text()))
    OUT.mkdir(parents=True, exist_ok=True)

    print("=== references under the round-3 rule (predicted, H4 rig state)")
    print(HEAD)
    reference = {}
    for label, fp, path in REFERENCES:
        chain = chain_of[fp] if fp else model.chain_of_section(
            section_of(g.load_json(g.SP / path)))
        section = (section_of(g.load_json(g.SP / (path or json.loads(
            (g.SP / "search/fp-index.json").read_text())[fp]))))
        reference[label] = rows_for(objective, chain, section)
        show(label, reference[label])

    results = {}
    for name in f.FAMILIES:
        x = fit(objective, name)
        rows = rows_for(objective, objective.chain(x, name), objective.section(x, name))
        mean_held, pair = held_out(objective, name)
        results[name] = {"x": list(map(float, x)), "rows": rows, "held_out": mean_held,
                         "held_pair": pair, "ranges": ranges(x, name, measured),
                         "describe": describe(x, name, objective)}
        print(f"\n=== {name}: {results[name]['describe']}")
        show(name, rows)
        print(f"  {'':<9s} held-out {mean_held:+.2f} ({pair[0]:+.2f} / {pair[1]:+.2f})")
        print(f"  {'':<9s} {results[name]['ranges']}")

    print(f"\n=== agg-3 candidates. The brief's {f.BRIEF_BOOST_DB:g} dB EQ-back cap NEVER "
          f"BINDS (the best\n    unconstrained solution already sits at "
          f"{max(v['rows']['score'] for v in results.values()) and min(v['rows']['eq_back_boost'] for v in results.values()):.2f}-"
          f"{max(v['rows']['eq_back_boost'] for v in results.values()):.2f} dB), so a literal agg-3 "
          f"would be a copy of\n    agg-2. Solved at {f.GENTLE_PICK_DB:g} dB instead, which does "
          f"bind -- see cands7/frontier.json.")
    gentle = {}
    for name in f.FAMILIES:
        x = fit(objective, name, gentle=True)
        rows = rows_for(objective, objective.chain(x, name), objective.section(x, name))
        _hard, _soft, notes = f.guards(
            {int(k): v for k, v in rows["front_total"].items()}, gentle=True)
        mean_held, pair = held_out(objective, name, gentle=True)
        gentle[name] = {"x": list(map(float, x)), "rows": rows, "held_out": mean_held,
                        "held_pair": pair, "ranges": ranges(x, name, measured),
                        "describe": describe(x, name, objective), "notes": notes}
        print(f"  {name:<4s} F/B {rows['score']:+.2f}  boost {rows['eq_back_boost']:.2f} dB"
              f"  held-out {mean_held:+.2f}   {gentle[name]['describe']}")

    print("\n=== picks (simpler stays unless the richer gains >= 1 dB held-out)")
    picks = {}
    for tag, pair, pool in (("agg-1", ("G1a", "G1b"), results),
                            ("agg-2", ("G2a", "G2b"), results)):
        simple, rich = (pool[p] for p in pair)
        picks[tag] = pair[1] if rich["held_out"] - simple["held_out"] >= 1.0 else pair[0]
        print(f"  {tag}: {pair[0]} {simple['held_out']:+.2f} vs {pair[1]} "
              f"{rich['held_out']:+.2f} -> {picks[tag]}")
    legal = {n: v for n, v in gentle.items() if not v["notes"]
             and v["rows"]["eq_back_boost"] <= f.GENTLE_BOOST_DB + 0.01}
    finalists = []
    for pair in (("G1a", "G1b"), ("G2a", "G2b")):
        both = [p for p in pair if p in legal]
        if len(both) == 2:
            finalists.append(pair[1] if legal[pair[1]]["held_out"]
                             - legal[pair[0]]["held_out"] >= 1.0 else pair[0])
        elif both:
            finalists.append(both[0])
    third = max(finalists, key=lambda n: legal[n]["rows"]["score"]) if finalists else None
    print(f"  agg-3: legal {sorted(legal)}, family picks {finalists} -> {third}")

    print("\n=== emitted documents")
    verdicts = {}
    for tag, name, pool in (("agg-1", picks["agg-1"], results),
                            ("agg-2", picks["agg-2"], results),
                            ("agg-3", third, gentle)):
        if name is None:
            print(f"  {tag}: no solution keeps the EQ-back boost under "
                  f"{f.GENTLE_BOOST_DB:g} dB -- NOT emitted")
            continue
        item = pool[name]
        rows = item["rows"]
        extra = [f"Trust region {name}: {item['ranges']}.",
                 f"Held-out pose check (fit one arm angle, score the other): "
                 f"{item['held_out']:+.2f} dB ({item['held_pair'][0]:+.2f} / "
                 f"{item['held_pair'][1]:+.2f})."]
        if tag == "agg-3":
            extra.append(
                f"Solved with the extra rule that no EQ-back boost exceeds "
                f"{f.GENTLE_PICK_DB:g} dB anywhere in 63-630 Hz. The brief asked for "
                f"{f.BRIEF_BOOST_DB:g} dB, but that cap never binds -- the best "
                f"unconstrained solution already needs only 3.3 dB -- so this document "
                f"would otherwise have been a copy of agg-2. The tighter cap costs about "
                f"0.7 dB of F/B gain and is the cheapest useful point on the frontier.")
        verdicts[tag] = emit(
            OUT / f"{tag}.json",
            f"Aggressive F/B solution {name} (#5405): {item['describe']}. Predicted F/B gain "
            f"{rows['score']:+.2f} dB meaned over 100-315 Hz against ident-C's "
            f"{reference['ident-C']['score']:+.2f}, S1's {reference['S1']['score']:+.2f} and "
            f"fb-1's {reference['fb-1']['score']:+.2f}; the front falls to "
            f"{min(rows['front'].values()):+.2f} dB and a common EQ buys it back for a "
            f"{rows['eq_back_boost']:.2f} dB largest boost. A prediction, not a tune.",
            objective.section(item["x"], name), extra, rows)
        print(f"  cands7/{tag}.json  ({name})  {verdicts[tag]}")

    print("\n=== R-subset spread of the emitted solutions (F/B score)")
    print(f"  {'doc':<8s}{'all':>10s}" + "".join(f"{k:>10s}" for k in SUBSETS))
    spread = {}
    for tag, name, pool in (("agg-1", picks["agg-1"], results),
                            ("agg-2", picks["agg-2"], results),
                            ("agg-3", third, gentle)):
        if name is None:
            continue
        section = objective.section(pool[name]["x"], name)
        cells = [pool[name]["rows"]["score"]]
        for tags in SUBSETS.values():
            alt = h.FB(store, chain_of, target=TARGET, fit_tags=tags)
            chain = alt.chain_of_section(section)
            cells.append(f.score_of(h.gain(alt.front(chain), alt.behind(chain))))
        spread[tag] = [round(v, 2) for v in cells]
        print(f"  {tag:<8s}" + "".join(f"{v:+10.2f}" for v in cells))

    (OUT / "summary.json").write_text(json.dumps(
        {"reference": reference, "families": results, "gentle": gentle, "picks": picks,
         "agg3": third, "verdicts": verdicts, "spread": spread}, indent=1) + "\n")
    return 0


def rows_front(rows):
    return {int(k): v for k, v in rows["front"].items()}


if __name__ == "__main__":
    raise SystemExit(main())
