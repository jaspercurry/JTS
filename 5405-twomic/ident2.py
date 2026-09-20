#!/usr/bin/env python3
"""Round 5: the incoherent floor, the corrected validation, and the refit.

The coherent model ``X = X_0 + R c`` promises a null as deep as the filters can
make it. The measurement cannot: street noise, the part of the room that does
not repeat between takes and the dynamic bass block's non-linearity all add
POWER that no pressure cancellation removes. So the forward model here is

    |X|^2 = |X_0 + R c|^2 + N^2

and ``N`` is estimated from the residuals of the takes R was identified from.
"""
from __future__ import annotations

import argparse
import json
import pickle
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.optimize import differential_evolution

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

import identlib as il
from ident import ERA
from ident_bounds import (
    COMMON_DELAY_MS, FRONT_FLOOR_DB, PENALTY_WEIGHT, SCORE_POINTS, band_changes, score_mask,
)
from rearpred import section_of

SEED = 20260921
HANDOVER_BANDS = ((44.54, 56.12), (56.13, 70.72), (71.27, 89.80))
HANDOVER_SLACK_DB = 0.5
GUARD_BAND = (350.0, 5000.0)
ALLPASS = ((60.0, 500.0), (0.3, 10.0))
PEAKING = ((60.0, 500.0), (-12.0, 6.0), (0.3, 1.0))
#: gain dB, cancellation delay ms, common delay ms
CORE = ((-12.0, 0.0), (-3.0, 1.2), (1.06, 3.5))
BOUNDS_C = (*CORE, *ALLPASS, *ALLPASS, *PEAKING, *PEAKING)
BOUNDS_D = (*CORE, *ALLPASS, *ALLPASS, *ALLPASS, *PEAKING, *PEAKING, *PEAKING)


def pose_angle(pose: str) -> float:
    return float(pose.split("az")[1].split("_")[0])


def chain_of(base, x, n_allpass: int, n_peaking: int) -> dict:
    chain = deepcopy(dict(base))
    chain["gain_db"] = round(float(x[0]), 4)
    chain["delay_ms"] = round(float(x[1]), 4)
    extra, cursor = [], 3
    for _ in range(n_allpass):
        extra.append({"type": "Biquad", "parameters": {
            "type": "Allpass", "freq": round(float(x[cursor]), 4),
            "q": round(float(x[cursor + 1]), 4)}})
        cursor += 2
    for _ in range(n_peaking):
        extra.append({"type": "Biquad", "parameters": {
            "type": "Peaking", "freq": round(float(x[cursor]), 4),
            "gain": round(float(x[cursor + 1]), 4), "q": round(float(x[cursor + 2]), 4)}})
        cursor += 3
    chain["filters"] = [*chain["filters"], *extra]
    return chain


def section_with(base_section, chain, common_ms: float) -> dict:
    out = deepcopy(dict(base_section))
    out["rear"] = {**out["rear"], "cancellation": deepcopy(dict(chain))}
    out["common_delay_ms"] = round(float(common_ms), 4)
    return out


def score_with_floor(muted, model, chain, floor, mask, grid_in) -> float:
    """``10log10`` of the mean change, with the incoherent power added back.

    The reference carries its own floor too, so both sides get it: otherwise a
    quiet muted take would read as a deeper null than it is.
    """
    coherent = np.abs(muted + model * chain) ** 2 + floor
    reference = np.abs(muted) ** 2 + floor
    change = 10.0 * np.log10(np.maximum(coherent, 1e-30) / np.maximum(reference, 1e-30))
    return float(np.mean(np.interp(np.log(SCORE_POINTS), np.log(grid_in), change)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sp", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--cands", type=Path, required=True)
    args = parser.parse_args()
    with args.model.open("rb") as handle:
        held = pickle.load(handle)
    models, chains, groups, aligned = (held[k] for k in
                                       ("models", "chains", "groups", "aligned"))
    grid = il.freqs()
    keep = score_mask()
    grid_in = grid[keep]
    bands = il.third_octaves()
    n1 = section_of(json.loads((args.sp / "docs/doc-N1.json").read_text()))
    base_chain = n1["rear"]["cancellation"]

    def residuals_of(tags, mic, angle):
        """Every ``X_i - X_0 - R c_i`` available for one (mic, angle)."""
        out, keys = [], [k for k in models if k[0] in tags and k[1] == mic
                         and pose_angle(k[2]) == angle]
        if not keys:
            return out, None
        model = il.robust_mean([models[k]["R"] for k in keys])
        for tag, _mic, pose in keys:
            byc = groups[tag][(mic, pose)]
            muted = byc["0caaa048"]["transfer"]
            for fp in byc:
                if fp == "0caaa048" or fp not in chains:
                    continue
                fit = aligned[(tag, mic, pose, fp)]
                if not fit["usable"]:
                    continue
                out.append(fit["aligned"] - muted - np.nan_to_num(model) * chains[fp])
        return out, model

    print("=== 1. arm +0 across the USB event: did R move, or X_0?")
    print("  era   n   R level 80-400 Hz (dB)   X_0 level 100-350 Hz (dB)   R agreement with pre")
    pre_r = None
    for era in ("pre", "post"):
        tags = [t for t, value in ERA.items() if value == era]
        keys = [k for k in models if k[0] in tags and k[1] == "side" and pose_angle(k[2]) == 0.0]
        if not keys:
            print(f"  {era:5s}  -- no identified model at arm +0"); continue
        model = il.robust_mean([models[k]["R"] for k in keys])
        muted = [models[k]["muted"] for k in keys]
        level = 10.0 * np.log10(np.nanmean(np.abs(np.nan_to_num(model)[keep]) ** 2))
        zero = np.mean([10.0 * np.log10(np.mean(np.abs(one[keep]) ** 2)) for one in muted])
        agree = ""
        if pre_r is not None:
            ratio = np.nan_to_num(model)[keep] / np.where(np.abs(np.nan_to_num(pre_r)[keep]) > 0,
                                                          np.nan_to_num(pre_r)[keep], 1.0)
            agree = (f"{10 * np.log10(np.mean(np.abs(ratio) ** 2)):+.2f} dB, "
                     f"{np.degrees(np.angle(np.mean(ratio / np.abs(ratio)))):+.0f} deg")
        else:
            pre_r = model
        print(f"  {era:5s} {len(keys):2d}   {level:+12.2f}          {zero:+14.2f}          {agree}")

    print("\n=== 2. the incoherent floor N, per third octave (side mic)")
    print("  angle  n  " + "".join(f"{b[0]:>9.0f}" for b in bands) + "   (N minus the muted level, dB)")
    floors = {}
    for angle in (0.0, 20.0, -20.0):
        rows, model = residuals_of(set(ERA), "side", angle)
        if not rows:
            continue
        floor = il.residual_floor(rows, bands)
        keys = [k for k in models if k[1] == "side" and pose_angle(k[2]) == angle]
        muted = models[keys[0]]["muted"]
        cells = []
        for band in bands:
            inside = (grid >= band[0]) & (grid < band[1])
            level = float(np.mean(np.abs(muted[inside]) ** 2))
            cells.append(10.0 * np.log10(max(floor[band], 1e-30) / max(level, 1e-30)))
        floors[angle] = il.floor_curve(floor)
        print(f"  {angle:+5.0f} {len(rows):3d}  " + "".join(f"{v:+9.1f}" for v in cells))
    front_rows, front_model = residuals_of(set(ERA), "main", 0.0)
    front_floor = il.floor_curve(il.residual_floor(front_rows, bands)) if front_rows else None

    print("\n=== 3. out-of-sample validation WITH the floor (fit <= CONFIRM, predict i1b/i2)")
    fit_tags = {"d1", "d2", "c1", "649a"}
    fitted, fit_floor = {}, {}
    for mic in ("side", "main"):
        for angle in (0.0, 20.0, -20.0):
            keys = [k for k in models if k[0] in fit_tags and k[1] == mic
                    and pose_angle(k[2]) == angle]
            if not keys:
                continue
            fitted[(mic, angle)] = np.nan_to_num(il.robust_mean([models[k]["R"] for k in keys]))
            rows, _ = residuals_of(fit_tags, mic, angle)
            fit_floor[(mic, angle)] = il.floor_curve(il.residual_floor(rows, bands)) if rows else 0.0
    print("  round tune      mic  angle  measured  no floor   with floor   error(floor)")
    errors = {}
    for tag in ("i1b", "i2"):
        for (mic, pose), byc in sorted(groups[tag].items()):
            angle = pose_angle(pose)
            if (mic, angle) not in fitted or "0caaa048" not in byc:
                continue
            muted = byc["0caaa048"]["transfer"][keep]
            model = fitted[(mic, angle)][keep]
            floor = np.asarray(fit_floor[(mic, angle)])[keep]
            for fp in sorted(byc):
                if fp == "0caaa048" or fp not in chains:
                    continue
                fit = aligned[(tag, mic, pose, fp)]
                if not fit["usable"]:
                    continue
                chain = chains[fp][keep]
                got = score_with_floor(muted, model, chain, 0.0, keep, grid_in) * 0 + float(
                    np.mean(np.interp(np.log(SCORE_POINTS), np.log(grid_in),
                                      20 * np.log10(np.abs(fit["aligned"][keep]) + 1e-30)
                                      - 20 * np.log10(np.abs(muted) + 1e-30))))
                plain = score_with_floor(muted, model, chain, 0.0, keep, grid_in)
                withf = score_with_floor(muted, model, chain, floor, keep, grid_in)
                errors.setdefault(mic, []).append((got - withf, got - plain, angle))
                print(f"  {tag:5s} {fp} {mic:4s} {angle:+5.0f} {got:+9.2f} {plain:+9.2f} "
                      f"{withf:+11.2f} {got - withf:+13.2f}")
    for mic, rows in errors.items():
        with_floor = np.asarray([r[0] for r in rows]); plain = np.asarray([r[1] for r in rows])
        print(f"  {mic}: n={with_floor.size}  WITH floor mean {with_floor.mean():+.2f} "
              f"mean|e| {np.abs(with_floor).mean():.2f} worst {with_floor[np.argmax(np.abs(with_floor))]:+.2f}"
              f"  | no floor mean {plain.mean():+.2f} worst {plain[np.argmax(np.abs(plain))]:+.2f}")

    print("\n=== 4. refit with the floor, common_delay free in 1.06..3.5 ms")
    rear = {}
    for angle in (0.0, 20.0, -20.0):
        keys = [k for k in models if k[1] == "side" and pose_angle(k[2]) == angle]
        if not keys:
            continue
        chosen = next((k for k in keys if k[0] == "i2"), keys[0])
        rear[angle] = (np.nan_to_num(il.robust_mean([models[k]["R"] for k in keys]))[keep],
                       models[chosen]["muted"][keep], np.asarray(floors[angle])[keep])
    front_keys = [k for k in models if k[1] == "main"]
    front_r = np.nan_to_num(il.robust_mean([models[k]["R"] for k in front_keys]))[keep]
    front_muted = [models[k]["muted"][keep] for k in front_keys if k[0] == "i2"]
    front_floor_in = np.asarray(front_floor)[keep] if front_floor is not None else 0.0

    reference_handover = None

    def evaluate(x, shape, angles=None):
        n_ap, n_pk = shape
        chain_doc = chain_of(base_chain, x, n_ap, n_pk)
        section = section_with(n1, chain_doc, x[2])
        chain = rear_stage_response(section, grid_in)[0]
        scores = [score_with_floor(muted, model, chain, floor, keep, grid_in)
                  for angle, (model, muted, floor) in rear.items()
                  if angles is None or angle in angles]
        penalty = 0.0
        for muted in front_muted:
            value = score_with_floor(muted, front_r, chain, front_floor_in, keep, grid_in)
            penalty += max(0.0, FRONT_FLOOR_DB - value)
            if reference_handover is not None:
                full = rear_stage_response(section, grid)[0]
                got = band_changes(models[front_keys[0]]["muted"] + np.nan_to_num(
                    il.robust_mean([models[k]["R"] for k in front_keys])) * full,
                    models[front_keys[0]]["muted"], HANDOVER_BANDS)
                penalty += sum(max(0.0, reference_handover[i] - HANDOVER_SLACK_DB - got[i])
                               for i in range(len(HANDOVER_BANDS)))
        return float(np.mean(scores)) + PENALTY_WEIGHT * penalty, scores

    ident_c = section_of(json.loads((args.sp / "cands3/ident-C.json").read_text()))
    full_front_r = np.nan_to_num(il.robust_mean([models[k]["R"] for k in front_keys]))
    ref_muted = models[front_keys[0]]["muted"]
    reference_handover = band_changes(
        ref_muted + full_front_r * rear_stage_response(ident_c, grid)[0], ref_muted, HANDOVER_BANDS)
    print(f"  ident-C's modelled front hand-over (45-56/56-71/71-90 Hz): "
          + " ".join(f"{v:+.2f}" for v in reference_handover)
          + f"; new tunes must stay within {HANDOVER_SLACK_DB:g} dB of it")

    results = {}
    for name, bounds, shape in (("C", BOUNDS_C, (2, 2)), ("D", BOUNDS_D, (3, 3))):
        found = differential_evolution(lambda x: evaluate(x, shape)[0], bounds, seed=SEED,
                                       maxiter=60, popsize=15, tol=0.01, polish=True,
                                       init="latinhypercube")
        _total, scores = evaluate(found.x, shape)
        results[name] = {"x": found.x, "shape": shape, "scores": scores}
        print(f"  {name}: mean {np.mean(scores):+.2f} dB  per angle "
              + " ".join(f"{v:+.2f}" for v in scores)
              + f"  common {found.x[2]:.2f} ms  delay {found.x[1]:+.3f} ms  gain {found.x[0]:+.2f} dB")

    print("\n  held-out angle and USB-era robustness")
    for name in ("C", "D"):
        shape = results[name]["shape"]
        outs = []
        for drop in rear:
            keys_keep = [a for a in rear if a != drop]
            found = differential_evolution(lambda x: evaluate(x, shape, keys_keep)[0],
                                           BOUNDS_C if name == "C" else BOUNDS_D, seed=SEED,
                                           maxiter=40, popsize=12, tol=0.02, polish=True,
                                           init="latinhypercube")
            outs.append(evaluate(found.x, shape, [drop])[1][0])
        results[name]["held_out"] = float(np.mean(outs))
        print(f"    {name} held-out mean {np.mean(outs):+.2f} dB "
              + "(" + " ".join(f"{v:+.2f}" for v in outs) + ")")

    for name in ("C", "D"):
        shape, x = results[name]["shape"], results[name]["x"]
        section = section_with(n1, chain_of(base_chain, x, *shape), x[2])
        chain = rear_stage_response(section, grid_in)[0]
        per_era = {}
        for era in ("pre", "post"):
            tags = {t for t, v in ERA.items() if v == era}
            scores = []
            for angle in rear:
                keys = [k for k in models if k[0] in tags and k[1] == "side"
                        and pose_angle(k[2]) == angle]
                if not keys:
                    continue
                model = np.nan_to_num(il.robust_mean([models[k]["R"] for k in keys]))[keep]
                muted = models[keys[0]]["muted"][keep]
                scores.append(score_with_floor(muted, model, chain, rear[angle][2], keep, grid_in))
            per_era[era] = scores
        results[name]["era"] = per_era
        print(f"    {name} pre-USB " + " ".join(f"{v:+.2f}" for v in per_era["pre"])
              + f" (mean {np.mean(per_era['pre']):+.2f})  post-USB "
              + " ".join(f"{v:+.2f}" for v in per_era["post"])
              + f" (mean {np.mean(per_era['post']):+.2f})")

    print("\n=== 5. emitted candidates")
    args.cands.mkdir(parents=True, exist_ok=True)
    robust = max(results, key=lambda name: min(
        min(results[name]["era"]["pre"]), min(results[name]["era"]["post"])))
    for name, label in (("C", "ident2-C"), ("D", "ident2-D"), (robust, "ident2-R")):
        shape, x = results[name]["shape"], results[name]["x"]
        section = section_with(n1, chain_of(base_chain, x, *shape), x[2])
        section["conditions"] = {
            "dataset": "jts3 measured summed rounds D1, D2, CONFIRM, 649a, I1b, I2; "
                       "side mic behind, arm 0/+-20",
            "fit_band_hz": [100.0, 350.0], "fit_tool": "ident2.py", "measured": True,
            "timing_reference": "front output of this stage"}
        section["assumptions"] = [
            "The rear path R(f) is IDENTIFIED from the measured rounds, and the forward model "
            "carries an INCOHERENT floor: |X|^2 = |X_0 + R c|^2 + N^2. Without that term the "
            "predictor over-promised depth by about 1.9 dB on ident-B and ident-C.",
            "common_delay_ms may differ from N1's 1.06 ms. A pure common delay moves the whole "
            "stage, front chain included, so it does not change the rear-muted level and the "
            ">= 1 kHz alignment removes it; doc-Nm stays the measured zero.",
            "The front chain and rear.bass are doc-N1's verbatim (bass delay_ms -1.06, "
            "front delay_ms 0).",
            f"Structure {name}: {shape[0]} Allpass and {shape[1]} Peaking added to N1's "
            "cancellation branch.",
        ]
        document = {"kind": "jts_prescription", "schema": 1, "base": "saved",
                    "rationale": f"{label}: N1's structure, cancellation branch solved against the "
                                 f"measured rear path with an incoherent-noise floor.",
                    "sections": {"rear_calibration": section}}
        path = args.cands / f"{label}.json"
        path.write_text(json.dumps(document, indent=4) + "\n")
        try:
            validated = read_rear_calibration(section, sample_rate=48000)
            read_prescription_document(document)
            verdict = "PASS"
        except Exception as exc:
            verdict = f"FAIL {type(exc).__name__}: {exc}"
            validated = section
        print(f"  {path.name} ({name}) {verdict}  charge "
              f"{rear_branch_sum_headroom_db(validated):.3f} dB  "
              f"behind (floor) " + " ".join(f"{v:+.2f}" for v in results[name]["scores"])
              + f"  mean {np.mean(results[name]['scores']):+.2f}"
              f"  held-out {results[name]['held_out']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
