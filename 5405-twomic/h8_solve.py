#!/usr/bin/env python3
"""Round 4: solve for the SBIR band the wall will put at ~170 Hz.

The cabinet goes 0.2 m off the wall, so the front woofer sits ~0.5 m from it
and the wall hole lands near 170 Hz -- the one band every tune is weakest in
(agg-1's measured F/B there is -0.2 dB against +12.5 at 200). The score is
re-weighted onto 125/160/200 Hz, and agg-1's own structure is kept with its
low lift freed: w3 (S1 with the 120 Hz lift at +3.0 instead of +5.9) measured
-5.0 dB gated behind at 160 Hz against S1's +0.8, so the wide q1.0 lift really
does look like it over-drives the rear at 160.
"""
from __future__ import annotations

import json
from copy import deepcopy

import numpy as np
from scipy.optimize import differential_evolution

import g5lib as g
import h5lib as h
import h6_fam as f6
from rearpred import section_of

from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

SEED = 20260921
OUT = g.SP / "cands8"
TARGET = "a2"
OPTIMISM_DB = 1.0
WEIGHTS = {100: 1.0, 125: 2.0, 160: 3.0, 200: 3.0, 250: 1.0, 315: 0.5}
#: agg-1's structure, with the low lift freed and one optional mid bell.
BOUNDS = ((-0.5, 0.2), (300.0, 400.0), (95.0, 130.0), (0.0, 6.0), (1.0, 3.0),
          (240.0, 280.0), (0.0, 6.0), (1.5, 3.0))
MID = ((150.0, 200.0), (-6.0, 6.0), (1.5, 3.0))
STEP = (0.05, 25.0, 5.0, 0.5, 0.25, 10.0, 0.5, 0.25)
AGG1 = (-0.213, 300.35, 120.0, 5.453, 1.0, 259.46, 5.651, 2.254)
NAMES = ("delay", "lowpass", "low f", "low g", "low q", "bell f", "bell g", "bell q")
SUBSETS = {"pre-USB": {"d1", "d2", "c1", "649a", "d3"},
           "no-A1/A2": set(g.ALL_TAGS) - {"a1", "a2"}}
REF = (("agg-1", "bf03e5b6"), ("ident-C", "2fc52a19"), ("w3", "a454643e"), ("S1", "711b458f"))


def cancellation(x, mid: bool) -> dict:
    filters = [
        {"type": "BiquadCombo",
         "parameters": {"type": "LinkwitzRileyHighpass", "freq": 80.0, "order": 4}},
        {"type": "BiquadCombo",
         "parameters": {"type": "ButterworthLowpass", "freq": round(float(x[1]), 2),
                        "order": 2}},
        {"type": "Biquad",
         "parameters": {"type": "Peaking", "freq": 190.14, "gain": -6.36, "q": 0.996}},
        f6.peaking(x[2], x[3], x[4]), f6.peaking(x[5], x[6], x[7]),
    ]
    if mid:
        filters.append(f6.peaking(x[8], x[9], x[10]))
    return {"delay_ms": round(float(x[0]), 4), "filters": filters,
            "gain_db": 0.0, "inverted": True, "muted": False}


def score_of(gain) -> float:
    return float(sum(w * min(gain[c], f6.SCORE_CAP_DB) for c, w in WEIGHTS.items())
                 / sum(WEIGHTS.values()))


class Objective:
    def __init__(self, model, base):
        self.model, self.base = model, base

    def section(self, x, mid):
        out = deepcopy(dict(self.base))
        out["rear"] = {**out["rear"], "cancellation": cancellation(x, mid)}
        return out

    def rows(self, section, angles=None) -> dict:
        chain = self.model.chain_of_section(section)
        front = self.model.front(chain)
        gain = h.gain(front, self.model.behind(chain, angles))
        charge = float(f6.Objective.headroom(self, section))
        total = f6.total_front(front, charge)
        hard, soft, notes = f6.guards(total)
        curve, boost = f6.eq_back(front, charge)
        value = score_of(gain)
        return {"score": round(value, 2), "expected": round(value - OPTIMISM_DB, 2),
                "headroom_db": round(charge, 2), "eq_back_boost": round(boost, 2),
                "hard": hard, "soft": soft, "notes": notes,
                "front": {str(c): round(front[c], 2) for c in h.CENTRES},
                "front_total": {str(c): round(total[c], 2) for c in h.CENTRES},
                "gain": {str(c): round(gain[c], 2) for c in h.CENTRES},
                "eq_back": {str(c): round(curve[c], 2) for c in h.CENTRES}}

    def cost(self, x, mid, angles=None) -> float:
        chain = self.model.chain_of_section(self.section(x, mid))
        front = self.model.front(chain)
        gain = h.gain(front, self.model.behind(chain, angles))
        section = self.section(x, mid)
        total = f6.total_front(front, float(f6.Objective.headroom(self, section)))
        hard, soft, _ = f6.guards(total, margin=f6.SOLVE_MARGIN_DB)
        return -(score_of(gain) - soft) + f6.HARD_WEIGHT * hard


def bounds_for(mid):
    return (*BOUNDS, *MID) if mid else BOUNDS


def fit(objective, mid, angles=None, *, maxiter=50, popsize=12):
    return differential_evolution(lambda v: objective.cost(v, mid, angles), bounds_for(mid),
                                  seed=SEED, maxiter=maxiter, popsize=popsize, tol=0.01,
                                  polish=True, init="latinhypercube").x


def distance(x, mid) -> tuple[float, str]:
    parts, total = [], 3.0 if mid else 0.0
    for i, name in enumerate(NAMES):
        delta = x[i] - AGG1[i]
        total += abs(delta) / STEP[i]
        if abs(delta) > STEP[i] * 0.2:
            parts.append(f"{name} {delta:+.2f}")
    if mid:
        parts.append(f"added {x[8]:.0f} Hz {x[9]:+.2f} dB q{x[10]:.2f}")
    return total, (", ".join(parts) if parts else "identical") + f" ({total:.1f} steps)"


def describe(x, mid) -> str:
    c = cancellation(x, mid)
    out = [f"delay {c['delay_ms']:+.4f} ms",
           f"lowpass {c['filters'][1]['parameters']['freq']:.1f} Hz"]
    for one in c["filters"][3:]:
        p = one["parameters"]
        out.append(f"Peaking {p['freq']:.1f} Hz {p['gain']:+.2f} dB q{p['q']:.2f}")
    return "; ".join(out)


def show(label, rows, extra=""):
    print(f"  {label:<11s} score {rows['score']:+.2f} (expect {rows['expected']:+.2f})"
          f"  EQ-back {rows['eq_back_boost']:.2f} dB  cut {rows['headroom_db']:.2f}"
          + ("" if not rows["notes"] else "  HARD: " + "; ".join(rows["notes"])) + extra)
    print(f"  {'':<11s} F/B   " + "".join(f"{rows['gain'][str(c)]:+7.1f}" for c in WEIGHTS))
    print(f"  {'':<11s} front " + "".join(f"{rows['front_total'][str(c)]:+7.1f}"
                                          for c in WEIGHTS))


def emit(path, rationale, section, extra, rows) -> str:
    section = dict(section)
    section["conditions"] = {
        "dataset": "jts3 measured summed rounds D1..A2; main mic 0.61 m in front (3 arm "
                   "poses), side mic 0.61 m behind (arm +-20); rig state of round A2",
        "fit_band_hz": [100.0, 350.0], "fit_tool": "h8_solve.py", "measured": True,
        "timing_reference": "front output of this stage"}
    section["assumptions"] = [
        "Scored for the WALL band: the cabinet goes ~0.2 m off the wall, putting the SBIR "
        "hole near 170 Hz, so the F/B weights are 125x2, 160x3, 200x3, 100x1, 250x1, "
        "315x0.5 instead of a flat mean.",
        "F/B gain = front change - behind change. Front ungated, mean of three arm poses; "
        "behind ungated at 63-125 Hz and 400-630 Hz, gated 10 ms at 160-315 Hz, mean of "
        "+-20. The +0 arm pose is not used behind.",
        "The model over-predicts. Measured on A1/A2: agg-1 predicted +7.83, measured +7.39; "
        "agg-2 predicted +8.69, measured +5.96. Simple shapes near measured ones hold, "
        "complex extrapolations do not. Every score here carries a -1.0 dB expectation.",
        *extra,
        "Predicted front change (shape), dB, 63/80/100/125/160/200/250/315/400/500/630 Hz: "
        + " ".join(f"{rows['front'][str(c)]:+.1f}" for c in h.CENTRES),
        f"Front change INCLUDING the product's broadband headroom cut "
        f"({rows['headroom_db']:.2f} dB): "
        + " ".join(f"{rows['front_total'][str(c)]:+.1f}" for c in h.CENTRES),
        "Predicted F/B gain, dB: "
        + " ".join(f"{rows['gain'][str(c)]:+.1f}" for c in h.CENTRES),
        "EQ-back curve (common to both woofers), dB: "
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
    except Exception as exc:                        # noqa: BLE001
        return f"FAIL {type(exc).__name__}: {exc}"


def main() -> int:
    h.widen()
    store = g.collect()
    chain_of = g.chains()
    model = h.FB(store, chain_of, target=TARGET)
    base = section_of(g.load_json(g.SP / "cands7/agg-1.json"))
    objective = Objective(model, base)
    OUT.mkdir(parents=True, exist_ok=True)

    print("=== references, wall-weighted (predicted at the A2 rig state)")
    print(f"  {'':<11s}       " + "".join(f"{c:>7d}" for c in WEIGHTS))
    reference = {}
    index = json.loads((g.SP / "search/fp-index.json").read_text())
    for label, fp in REF:
        section = section_of(g.load_json(g.SP / index[fp]))
        reference[label] = objective.rows(section)
        show(label, reference[label])
    agg1_160 = reference["agg-1"]["gain"]["160"]

    results = {}
    for mid in (False, True):
        name = "W-b" if mid else "W-a"
        x = fit(objective, mid)
        rows = objective.rows(objective.section(x, mid))
        held = []
        for a, b in ((20.0, -20.0), (-20.0, 20.0)):
            xh = fit(objective, mid, [a], maxiter=35, popsize=10)
            held.append(score_of(h.gain(
                model.front(model.chain_of_section(objective.section(xh, mid))),
                model.behind(model.chain_of_section(objective.section(xh, mid)), [b]))))
        results[name] = {"x": list(map(float, x)), "mid": mid, "rows": rows,
                         "held_out": float(np.mean(held)), "held": held,
                         "distance": distance(x, mid)}
        print(f"\n=== {name}: {describe(x, mid)}")
        show(name, rows)
        print(f"  {'':<11s} held-out {np.mean(held):+.2f} ({held[0]:+.2f}/{held[1]:+.2f})"
              f"   from agg-1: {results[name]['distance'][1]}")

    print(f"\n=== wall170-2: smallest change from agg-1 with F/B at 160 Hz >= "
          f"{agg1_160 + 3.0:+.1f} (agg-1 {agg1_160:+.1f})")
    best = None
    for mid in (False, True):
        def penalty(v, mid=mid):
            rows = objective.rows(objective.section(v, mid))
            short = max(0.0, agg1_160 + 3.0 - rows["gain"]["160"])
            return (distance(v, mid)[0] + 20.0 * short
                    + 20.0 * f6.guards(f6.total_front(
                        {int(k): val for k, val in rows["front"].items()},
                        rows["headroom_db"]), margin=f6.SOLVE_MARGIN_DB)[0])
        v = differential_evolution(penalty, bounds_for(mid), seed=SEED, maxiter=40,
                                   popsize=12, tol=0.01, polish=True,
                                   init="latinhypercube").x
        rows = objective.rows(objective.section(v, mid))
        d = distance(v, mid)
        good = rows["gain"]["160"] >= agg1_160 + 2.99 and not rows["notes"]
        print(f"  {'with' if mid else 'without'} mid bell: 160 Hz {rows['gain']['160']:+.1f}"
              f"  score {rows['score']:+.2f}  distance {d[0]:.1f}"
              + ("" if good else "   <-- does not qualify"))
        if good and (best is None or d[0] < best[0]):
            best = (d[0], list(map(float, v)), mid, rows, d)
    print("\n=== emitted documents")
    verdicts = {}
    pick = max(results, key=lambda n: results[n]["held_out"])
    if results["W-b"]["held_out"] - results["W-a"]["held_out"] < 1.0:
        pick = "W-a"
    item = results[pick]
    print(f"  wall170-1 uses {pick} (W-a held-out {results['W-a']['held_out']:+.2f}, "
          f"W-b {results['W-b']['held_out']:+.2f}; simpler unless +1.0)")
    verdicts["wall170-1"] = emit(
        OUT / "wall170-1.json",
        f"Wall-band (~170 Hz SBIR) F/B solution (#5405): {describe(item['x'], item['mid'])}. "
        f"Wall-weighted F/B {item['rows']['score']:+.2f} dB predicted, "
        f"{item['rows']['expected']:+.2f} expected after this model's measured -1 dB "
        f"optimism, against agg-1's {reference['agg-1']['score']:+.2f} predicted. "
        f"From agg-1: {item['distance'][1]}. A prediction, not a tune.",
        objective.section(item["x"], item["mid"]),
        [f"Distance from the applied tune agg-1: {item['distance'][1]}.",
         f"Held-out pose check: {item['held_out']:+.2f} dB "
         f"({item['held'][0]:+.2f}/{item['held'][1]:+.2f})."],
        item["rows"])
    print(f"  cands8/wall170-1.json  ({pick})  {verdicts['wall170-1']}")
    if best is None:
        print("  wall170-2: NOTHING in the family gains 3 dB at 160 Hz inside the front "
              "rules -- not emitted")
    else:
        _d, v, mid, rows, dd = best
        verdicts["wall170-2"] = emit(
            OUT / "wall170-2.json",
            f"Smallest change to the applied tune agg-1 that gains 3 dB of F/B at 160 Hz "
            f"(#5405): {dd[1]}. 160 Hz goes {agg1_160:+.1f} -> {rows['gain']['160']:+.1f}; "
            f"wall-weighted F/B {rows['score']:+.2f} predicted, {rows['expected']:+.2f} "
            f"expected. Chosen by minimising the distance from agg-1, not by maximising "
            f"the score. A prediction, not a tune.",
            objective.section(v, mid),
            [f"Distance from agg-1: {dd[1]}. Chosen as the smallest edit that buys 3 dB "
             f"at 160 Hz while keeping the front rules."], rows)
        print(f"  cands8/wall170-2.json  {verdicts['wall170-2']}")
        show("wall170-2", rows)
        results["wall170-2"] = {"x": v, "mid": mid, "rows": rows, "distance": dd}

    print("\n=== R-subset spread (wall-weighted score)")
    print(f"  {'doc':<11s}{'A2':>9s}" + "".join(f"{k:>11s}" for k in SUBSETS))
    for tag, item in (("wall170-1", results[pick]),
                      ("wall170-2", results.get("wall170-2"))):
        if item is None:
            continue
        section = objective.section(item["x"], item["mid"])
        cells = [item["rows"]["score"]]
        for tags in SUBSETS.values():
            alt = h.FB(store, chain_of, target=TARGET, fit_tags=tags)
            chain = alt.chain_of_section(section)
            cells.append(score_of(h.gain(alt.front(chain), alt.behind(chain))))
        print(f"  {tag:<11s}" + "".join(f"{v:+11.2f}" for v in cells))
    (OUT / "summary.json").write_text(json.dumps(
        {"reference": reference, "results": results, "pick": pick,
         "verdicts": verdicts}, indent=1, default=float) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
