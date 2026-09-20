#!/usr/bin/env python3
"""Step 3 and 4: solve the cancellation branch in the GATED domain, emit docs.

Objective: the mean over poses (+-20 weight 1, +0 weight 0.5) of the six band
changes, each capped at -10 dB, with 100/125 Hz read UNGATED and 160-315 Hz
read GATED -- because ungated the room refills 140-320 Hz and no tune can show
a null there at this mic. Front penalties come from the same identified model
on the main mic.
"""
from __future__ import annotations

import json
import time

import numpy as np
from scipy.optimize import differential_evolution

import g5lib as g
import g5_model as m
from rearpred import section_of

from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

SEED = 20260921
OUT = g.SP / "cands5"
ANGLES = (20.0, -20.0, 0.0)


def fit(model, base, shape, angles, *, maxiter, popsize):
    found = differential_evolution(
        lambda x: model.objective(x, shape, base, angles), m.bounds_for(shape),
        seed=SEED, maxiter=maxiter, popsize=popsize, tol=0.01, polish=True,
        init="latinhypercube")
    return found.x


def describe(x, shape) -> str:
    chain = m.cancellation(x, shape)
    parts = [f"lowpass {chain['filters'][1]['parameters']['freq']:.0f} Hz",
             f"delay {chain['delay_ms']:+.3f} ms",
             f"120 Hz {chain['filters'][3]['parameters']['gain']:+.2f} dB"]
    for one in chain["filters"][4:]:
        p = one["parameters"]
        parts.append(f"{p['type']} {p['freq']:.0f} Hz "
                     + (f"{p['gain']:+.2f} dB " if "gain" in p else "") + f"q{p['q']:.2f}")
    return "; ".join(parts)


def table(model, chain) -> dict:
    bands = model.bands_behind(chain)
    a, b, c = model.front_guards(chain)
    total, per = model.score(chain)
    solid = float(np.mean([per[20.0], per[-20.0]]))
    return {"bands": {f"{k:+.0f}": [round(v, 2) for v in vals] for k, vals in bands.items()},
            "capped_mean": {f"{k:+.0f}": round(v, 2) for k, v in per.items()},
            "weighted": round(total, 2), "pm20_mean": round(solid, 2),
            "front": {"100-350_min": round(float(np.min(a)), 2),
                      "71-90_min": round(float(np.min(b)), 2),
                      "71-90_vs_e035": round(float(np.min(b)) - model.e035_guard_b, 2),
                      "350-5k_worst": round(float(np.max(np.abs(c))), 2)}}


def emit(name, x, shape, model, base, rows) -> str:
    section = m.section_with(base, m.cancellation(x, shape))
    figures = rows["table"]
    section["conditions"] = {
        "dataset": "jts3 measured summed rounds D1, D2, CONFIRM, 649a, I1b, I2, L1; "
                   "side mic 0.61 m behind, main mic 0.61 m in front, arm 0/+-20",
        "fit_band_hz": [100.0, 350.0], "fit_tool": "g5_solve.py", "measured": True,
        "timing_reference": "front output of this stage"}
    section["assumptions"] = [
        "The rear acoustic path R(f) is IDENTIFIED from the measured rounds as "
        "(X_i - X_0)/c_i and the prediction is made UNGATED and then windowed "
        "(10 ms after the muted take's own 100-350 Hz first arrival), which is the "
        "only order that is right in principle and validated at least as well as "
        "identifying in the gated domain.",
        "Scored GATED at 160/200/250/315 Hz and UNGATED at 100/125 Hz: ungated, the "
        "room refills 140-320 Hz at this mic position, so an ungated figure there "
        "cannot show a null the speaker really makes.",
        "The +0 (straight behind) arm pose is NOT predictable out of sample -- its "
        "held-out band errors are 5-10 dB after the USB fault moved the rig. Only the "
        "+-20 figures below carry evidence.",
        "The front chain, rear.bass, common_delay_ms 1.06 and valid_band_hz are "
        "doc-e-035's verbatim; only rear.cancellation changed.",
        f"Structure {name}: Butterworth lowpass corner, cancellation delay, the 120 Hz "
        f"Peaking gain, {shape[0]} added Peaking and {shape[1]} added Allpass.",
        "Predicted band change vs rear-muted, dB (100/125 ungated, 160/200/250/315 "
        "gated): " + "; ".join(
            f"{angle} " + " ".join(f"{v:+.1f}" for v in vals)
            for angle, vals in figures["bands"].items()),
        "Predicted front guards, dB: 100-350 Hz "
        f"{figures['front']['100-350_min']:+.2f} (needs >= -2), 71-90 Hz "
        f"{figures['front']['71-90_vs_e035']:+.2f} vs e-0.35 (needs >= -0.5), "
        f"350 Hz-5 kHz {figures['front']['350-5k_worst']:+.2f} (needs |.| <= 0.4).",
    ]
    document = {
        "base": "saved", "kind": "jts_prescription", "schema": 1,
        "rationale": (
            f"Gated rear-null solution {name} (#5405): the cancellation branch solved "
            f"against the measured rear path with the 160-315 Hz bands read through the "
            f"10 ms gate, because ungated the room refills them. {describe(x, shape)}. "
            f"Predicted +-20 capped mean {figures['pm20_mean']:+.2f} dB behind "
            f"(e-0.35 {rows['reference']:+.2f}), held-out across poses "
            f"{rows['held_out']:+.2f} dB. A prediction, not a tune: it has not been "
            f"measured on the speaker."),
        "sections": {"rear_calibration": section}}
    path = OUT / f"gated-{name}.json"
    path.write_text(json.dumps(document, indent=4, sort_keys=True) + "\n")
    try:
        validated = read_rear_calibration(section, sample_rate=48000)
        read_prescription_document(document)
        charge = rear_branch_sum_headroom_db(validated)
        return f"PASS  (rear branch-sum headroom charge {charge:+.3f} dB)"
    except Exception as exc:                      # noqa: BLE001 - the verdict IS the report
        return f"FAIL {type(exc).__name__}: {exc}"


def main() -> int:
    store = g.collect()
    chain_of = g.chains()
    base = section_of(g.load_json(g.SP / "search/D2/doc-e-035.json"))
    model = m.Model(store, chain_of)
    OUT.mkdir(parents=True, exist_ok=True)

    print("=== reference points under the same model (predicted, l1 rig state)")
    reference = {}
    for fp in ("27a565a9", "2fc52a19", "4b0374d1"):
        rows = table(model, chain_of[fp])
        reference[g.label(fp)] = rows
        print(f"  {g.label(fp):<8s} weighted {rows['weighted']:+.2f}  +-20 mean "
              f"{rows['pm20_mean']:+.2f}  per pose "
              + " ".join(f"{k} {v:+.2f}" for k, v in rows["capped_mean"].items()))
    e035_pm20 = reference["e-0.35"]["pm20_mean"]

    results = {}
    for name, shape in m.STAGES.items():
        start = time.time()
        x = fit(model, base, shape, None, maxiter=60, popsize=15)
        rows = table(model, model.chain_of_x(x, shape, base))
        held = []
        for fit_angle, test_angle in ((20.0, -20.0), (-20.0, 20.0)):
            xh = fit(model, base, shape, [fit_angle], maxiter=40, popsize=12)
            _total, per = model.score(model.chain_of_x(xh, shape, base), [test_angle])
            held.append(per[test_angle])
        results[name] = {"x": list(map(float, x)), "shape": shape, "table": rows,
                         "held_out": float(np.mean(held)), "held_pair": held,
                         "reference": e035_pm20, "seconds": round(time.time() - start)}
        print(f"\n=== {name} ({shape[0]} Peaking, {shape[1]} Allpass) "
              f"[{results[name]['seconds']} s]")
        print(f"  {describe(x, shape)}")
        print("  pose   " + "".join(f"{c:>8d}" for c in g.CENTRES) + "   capped mean")
        for angle in ANGLES:
            vals = rows["bands"][f"{angle:+.0f}"]
            print(f"  {angle:+5.0f}  " + "".join(f"{v:+8.1f}" for v in vals)
                  + f"{rows['capped_mean'][f'{angle:+.0f}']:+13.2f}")
        print(f"  weighted {rows['weighted']:+.2f}   +-20 mean {rows['pm20_mean']:+.2f} "
              f"(e-0.35 {e035_pm20:+.2f}, ident-C {reference['ident-C']['pm20_mean']:+.2f})")
        print(f"  held out: fit +20 -> score -20 {held[0]:+.2f}; fit -20 -> score +20 "
              f"{held[1]:+.2f}; mean {np.mean(held):+.2f}")
        print(f"  front: 100-350 {rows['front']['100-350_min']:+.2f} (>= -2), 71-90 "
              f"{rows['front']['71-90_vs_e035']:+.2f} vs e-0.35 (>= -0.5), 350-5k "
              f"{rows['front']['350-5k_worst']:+.2f} (<= 0.4)")

    print("\n=== emitted documents")
    for name in m.STAGES:
        verdict = emit(name, results[name]["x"], results[name]["shape"], model, base,
                       results[name])
        results[name]["verdict"] = verdict
        print(f"  cands5/gated-{name}.json  {verdict}")
    (OUT / "summary.json").write_text(json.dumps(
        {"reference": reference, "stages": results}, indent=1) + "\n")

    print("\n=== stage choice (simpler wins unless the richer gains >= 1 dB held-out)")
    for name in m.STAGES:
        print(f"  {name}: held-out {results[name]['held_out']:+.2f}  "
              f"in-sample +-20 {results[name]['table']['pm20_mean']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
