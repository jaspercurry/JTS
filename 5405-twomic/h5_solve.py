#!/usr/bin/env python3
"""Steps 3 and 4: solve for FRONT-TO-BACK GAIN inside the measured trust
region, report how far each solution sits from the nearest measured tune, and
emit the three documents.
"""
from __future__ import annotations

import json

import numpy as np
from scipy.optimize import differential_evolution

import g5lib as g
import h5_fam as f
import h5lib as h
from rearpred import section_of

from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

SEED = 20260921
OUT = g.SP / "cands6"
TARGET = "h4"
STEP = {"F1": (0.05, 0.5, 25.0, (20.0, 0.5, 0.3)), "F2": (0.05, 0.5, (20.0, 0.5, 0.3))}
IDENT_C_FREQS = (80.0, 300.0, 190.14, 120.0, 365.6582, 391.4937, 64.3614, 286.9708)


def classify(section):
    """(family, parameter vector) for a measured document, or None.

    Read from the document, never transcribed: a hand-typed corner or delay in
    a comparison table would quietly misreport how far a solution sits from
    the nearest measured tune.
    """
    chain = section["rear"]["cancellation"]
    freqs = tuple(one["parameters"]["freq"] for one in chain["filters"])
    delay = float(chain["delay_ms"])
    if freqs[:len(IDENT_C_FREQS)] == IDENT_C_FREQS:
        gain64 = next(one["parameters"]["gain"] for one in chain["filters"]
                      if one["parameters"]["freq"] == 64.3614)
        extra = _extra_of(chain["filters"][len(IDENT_C_FREQS):])
        return None if extra is False else ("F2", (delay, float(gain64), extra))
    if freqs[:4] == (80.0, freqs[1], 190.14, 120.0):
        gain120 = next(one["parameters"]["gain"] for one in chain["filters"]
                       if one["parameters"]["freq"] == 120.0)
        extra = _extra_of(chain["filters"][4:])
        return None if extra is False else (
            "F1", (delay, float(gain120), float(freqs[1]), extra))
    return None


def _extra_of(filters):
    """The one added Peaking, ``None`` for none, ``False`` for anything else.

    ``False`` marks a document that is not in either family -- an added
    Allpass, or more than one added filter -- so it is left out of the
    comparison rather than being described as a Peaking it does not have.
    """
    if not filters:
        return None
    p = filters[0]["parameters"]
    if len(filters) > 1 or p.get("type") != "Peaking":
        return False
    return (float(p["freq"]), float(p["gain"]), float(p["q"]))


def measured_table(chain_paths) -> dict:
    out = {"F1": {}, "F2": {}}
    for fp, path in chain_paths.items():
        if fp == g.MUTED:
            continue
        got = classify(section_of(g.load_json(g.SP / path)))
        if got:
            out[got[0]][g.label(fp)] = got[1]
    return out


def vector_of(x, name):
    family, extra = f.FAMILIES[name]
    core = list(map(float, x[:3 if family == "F1" else 2]))
    tail = tuple(map(float, x[len(core):])) if extra else None
    return (*core, tail)


def nearest(vec, family, measured) -> tuple[str, str]:
    """The measured tune this solution is closest to, and the per-knob deltas."""
    steps = STEP[family]
    best = None
    for tune, ref in measured[family].items():
        distance, parts = 0.0, []
        for i, (got, want) in enumerate(zip(vec[:-1], ref[:-1])):
            distance += abs(got - want) / steps[i]
            parts.append(f"{got - want:+.3f}")
        if vec[-1] is None and ref[-1] is None:
            pass
        elif vec[-1] is None or ref[-1] is None:
            distance += 3.0
            parts.append("added Peaking" if vec[-1] else "dropped Peaking")
        else:
            for j, (got, want) in enumerate(zip(vec[-1], ref[-1])):
                distance += abs(got - want) / steps[-1][j]
                parts.append(f"{got - want:+.2f}")
        if best is None or distance < best[0]:
            best = (distance, tune, parts)
    return best[1], ", ".join(best[2]) + f"  (distance {best[0]:.1f} steps)"


def describe(x, name, objective) -> str:
    chain = f.cancellation(x, name, objective.ident_c_chain)
    parts = [f"delay {chain['delay_ms']:+.4f} ms"]
    for one in chain["filters"]:
        p = one["parameters"]
        if p["type"] == "ButterworthLowpass":
            parts.append(f"lowpass {p['freq']:.1f} Hz")
        elif p["type"] == "Peaking" and p["freq"] in (120.0, 64.3614):
            parts.append(f"{p['freq']:.0f} Hz {p['gain']:+.3f} dB")
        elif p["type"] == "Peaking" and p["freq"] not in (190.14, 286.9708):
            parts.append(f"Peaking {p['freq']:.1f} Hz {p['gain']:+.2f} dB q{p['q']:.2f}")
    return "; ".join(parts)


def rows_for(objective, chain) -> dict:
    value, front, behind, gain = objective.evaluate(chain)
    cost, notes = f.guard_violation(front, objective.e035_80)
    return {"score": round(value, 2),
            "front": {str(c): round(front[c], 2) for c in h.CENTRES},
            "behind": {str(c): round(behind[c], 2) for c in h.CENTRES},
            "gain": {str(c): round(gain[c], 2) for c in h.CENTRES},
            "guard_cost": round(cost, 2), "guard_notes": notes}


def show(label, rows):
    print(f"  {label:<10s} score {rows['score']:+.2f}"
          + ("  guards PASS" if not rows["guard_notes"]
             else "  guards FAIL: " + "; ".join(rows["guard_notes"])))
    for what in ("front", "behind", "gain"):
        print(f"  {'':<10s} {what:<6s}"
              + "".join(f"{rows[what][str(c)]:+7.1f}" for c in h.CENTRES))


def fit(objective, name, angles=None, *, maxiter=60, popsize=15):
    found = differential_evolution(lambda v: objective.cost(v, name, angles),
                                   f.bounds_for(name), seed=SEED, maxiter=maxiter,
                                   popsize=popsize, tol=0.01, polish=True,
                                   init="latinhypercube")
    return found.x


def emit(path, rationale, section, extra_assumptions, rows) -> str:
    section = dict(section)
    section["conditions"] = {
        "dataset": "jts3 measured summed rounds D1..H4; main mic 0.61 m in front (3 arm "
                   "poses), side mic 0.61 m behind (arm +-20); rig state of round H4",
        "fit_band_hz": [100.0, 350.0], "fit_tool": "h5_solve.py", "measured": True,
        "timing_reference": "front output of this stage"}
    section["assumptions"] = [
        "Solved for FRONT-TO-BACK GAIN = front change - behind change, not for level "
        "behind: a tune that makes the whole speaker quieter scores well behind and buys "
        "nothing, and a common EQ can undo it. F/B gain is what the cardioid really did.",
        "Front is UNGATED and meaned over the three arm poses; behind is ungated at "
        "63-125 Hz and 400-630 Hz and GATED 10 ms at 160-315 Hz, meaned over +-20. The +0 "
        "arm pose is not used behind -- it is not predictable out of sample.",
        "Out of sample the front model lands within 0.1-0.3 dB per third octave and F/B "
        "gain within 0.4-1.6 dB mean (worst 2.6) over the scored bands.",
        *extra_assumptions,
        "Predicted front change, dB, 63/80/100/125/160/200/250/315/400/500/630 Hz: "
        + " ".join(f"{rows['front'][str(c)]:+.1f}" for c in h.CENTRES),
        "Predicted behind change (u/u/u/u/g/g/g/g/u/u/u), dB: "
        + " ".join(f"{rows['behind'][str(c)]:+.1f}" for c in h.CENTRES),
        "Predicted F/B gain, dB: "
        + " ".join(f"{rows['gain'][str(c)]:+.1f}" for c in h.CENTRES),
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
    base = section_of(g.load_json(g.SP / "search/D2/doc-e-035.json"))
    ident_c = section_of(g.load_json(g.SP / "cands3/ident-C.json"))
    objective = f.Objective(model, base, ident_c, chain_of[f.E035])
    measured = measured_table(json.loads((g.SP / "search/fp-index.json").read_text()))
    print("  measured tunes classified into the families: F1 "
          + ", ".join(sorted(measured["F1"])) + " | F2 " + ", ".join(sorted(measured["F2"])))
    OUT.mkdir(parents=True, exist_ok=True)

    print("=== measured tunes under the same model (predicted, H4 rig state)")
    reference = {}
    for fp in (f.E035, "5e9afae3", f.IDENT_C, "711b458f", "654e057b", "4b0374d1"):
        reference[g.label(fp)] = rows_for(objective, chain_of[fp])
        show(g.label(fp), reference[g.label(fp)])

    results = {}
    for name in f.FAMILIES:
        x = fit(objective, name)
        rows = rows_for(objective, objective.chain(x, name))
        held = []
        for fit_angle, test_angle in ((20.0, -20.0), (-20.0, 20.0)):
            xh = fit(objective, name, [fit_angle], maxiter=40, popsize=12)
            held.append(objective.evaluate(objective.chain(xh, name), [test_angle])[0])
        vec = vector_of(x, name)
        tune, deltas = nearest(vec, f.FAMILIES[name][0], measured)
        results[name] = {"x": list(map(float, x)), "rows": rows,
                         "held_out": float(np.mean(held)), "nearest": tune,
                         "deltas": deltas}
        print(f"\n=== {name}: {describe(x, name, objective)}")
        show(name, rows)
        print(f"  {'':<10s} held-out {np.mean(held):+.2f} ({held[0]:+.2f} / {held[1]:+.2f})"
              f"   nearest measured: {tune} -> {deltas}")

    print("\n=== fb-3: the SMALLEST edit of measured ident-C that clears the front guards")
    print("  ident-C's own score is higher, but it FAILS them -- fb-3 is the nearest")
    print("  point to it that does not, so the least of its F/B gain is given up.")
    for knob, lo, hi, index in (("delay alone", *f.F2_CORE[0], 0),
                                ("64 Hz gain alone", *f.F2_CORE[1], 1)):
        passing = []
        for value in np.linspace(lo, hi, 81):
            x = [-0.9188, 3.9525]
            x[index] = value
            if not rows_for(objective, objective.chain(x, "F2a"))["guard_notes"]:
                passing.append(value)
        print(f"  {knob:<17s} " + (f"passes for {len(passing)} of 81 points"
                                   if passing else "no point in range passes"))
    steps, anchor = STEP["F2"], (-0.9188, 3.9525)
    third = None
    for delay in np.linspace(*f.F2_CORE[0], 61):
        for gain in np.linspace(*f.F2_CORE[1], 61):
            x = [float(delay), float(gain)]
            if f.guard_violation(objective.model.front(objective.chain(x, "F2a")),
                                 objective.e035_80, margin=f.SOLVE_MARGIN_DB)[0] > 0.0:
                continue
            distance = (abs(delay - anchor[0]) / steps[0] + abs(gain - anchor[1]) / steps[1])
            rows = rows_for(objective, objective.chain(x, "F2a"))
            key = (round(distance, 3), -rows["score"])
            if third is None or key < third[0]:
                third = (key, (f"delay {delay - anchor[0]:+.3f} ms and 64 Hz gain "
                               f"{gain - anchor[1]:+.3f} dB from measured ident-C",
                               (None, rows, x)))
    third = third[1]
    print(f"  smallest passing edit: {third[0]}  score {third[1][1]['score']:+.2f}"
          f"  (ident-C {reference['ident-C']['score']:+.2f}, guards failed)")

    print("\n=== emitted documents")
    picks = {}
    for tag, pair in (("fb-1", ("F1a", "F1b")), ("fb-2", ("F2a", "F2b"))):
        simple, rich = (results[p] for p in pair)
        chosen = pair[1] if rich["held_out"] - simple["held_out"] >= 1.0 else pair[0]
        picks[tag] = chosen
        print(f"  {tag}: {pair[0]} held-out {simple['held_out']:+.2f}, "
              f"{pair[1]} {rich['held_out']:+.2f} -> {chosen}")
    verdicts = {}
    for tag, name in picks.items():
        item = results[name]
        verdicts[tag] = emit(
            OUT / f"{tag}.json",
            f"Front-to-back gain solution {name} (#5405): {describe(item['x'], name, objective)}. "
            f"Predicted F/B gain {item['rows']['score']:+.2f} dB meaned over 100-315 Hz "
            f"against ident-C's {reference['ident-C']['score']:+.2f} and S1's "
            f"{reference['S1']['score']:+.2f}, with the front inside every guard. Nearest "
            f"measured tune {item['nearest']} ({item['deltas']}). A prediction, not a tune.",
            objective.section(item["x"], name),
            [f"Trust region {name}: only shapes the rig has measured. Nearest measured tune "
             f"{item['nearest']}, per-knob offsets {item['deltas']}.",
             f"Held-out pose check (fit one arm angle, score the other): "
             f"{item['held_out']:+.2f} dB."],
            item["rows"])
        print(f"  cands6/{tag}.json  ({name})  {verdicts[tag]}")
    rows3 = third[1][1]
    verdicts["fb-3"] = emit(
        OUT / "fb-3.json",
        f"Smallest edit of measured ident-C that clears the front guards (#5405): "
        f"{third[0]}; every other parameter is ident-C's own measured value. Measured "
        f"ident-C scores {reference['ident-C']['score']:+.2f} dB of F/B gain but FAILS the "
        f"front guards (100 Hz -3.8 dB); this gives up "
        f"{reference['ident-C']['score'] - rows3['score']:.2f} dB of that to pass them, and "
        f"still beats N1 -- the only measured tune that passes them -- by "
        f"{rows3['score'] - reference['N1']['score']:+.2f} dB. A prediction, not a tune.",
        objective.section(third[1][2], "F2a"),
        [f"Trust region: measured ident-C with {third[0]}. Chosen by minimising the "
         f"distance from measured ident-C subject to the front guards, NOT by maximising "
         f"F/B gain -- cands6/fb-2.json is the ident-C-family solution that does that.",
         "Neither knob alone clears the guards: ident-C's 64 Hz Peaking has q 0.68, so it "
         "reaches 100 Hz, and it is what pulls the front down 3.8 dB there."],
        rows3)
    print(f"  cands6/fb-3.json  ({third[0]})  {verdicts['fb-3']}")
    show("fb-3", rows3)
    (OUT / "summary.json").write_text(json.dumps(
        {"reference": reference, "families": results, "picks": picks,
         "fb3": {"knob": third[0], "x": third[1][2], "rows": rows3},
         "verdicts": verdicts}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
