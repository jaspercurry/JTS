#!/usr/bin/env python3
"""What the identified rear path says the rear chain could reach, and its proof.

Three stages: the ideal rear chain against N1's, an OUT-OF-SAMPLE validation
(identify on D1+D2, predict the candidates of CONFIRM and 649a), and a bounded
search on N1's structure at three levels of freedom.

``common_delay_ms`` is held at N1's 1.06 ms throughout. It is not a free knob
here: the compiler adds it to the FRONT chain as well, so moving it would move
the front path too and ``X_0`` -- the measured rear-muted take every score is
read against -- would no longer be the right reference. That is also why the
cancellation delay cannot go below -1.06 ms.
"""
from __future__ import annotations

import argparse
import json
import pickle
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from scipy.optimize import differential_evolution

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

import identlib as il
from rearpred import section_of
from winscore import PRE_MS, SAMPLE_RATE_HZ, window

SEED = 20260920
SCORE_POINTS = np.geomspace(100.0, 350.0, 23)
FRONT_FLOOR_DB = -2.0
PENALTY_WEIGHT = 5.0
COMMON_DELAY_MS = 1.06
#: gain dB, cancellation delay ms. The delay floor is the common delay.
BOUNDS_A = ((-12.0, 0.0), (-1.0, 1.2))
ALLPASS = ((60.0, 500.0), (0.3, 10.0))
PEAKING = ((60.0, 500.0), (-12.0, 6.0), (0.3, 1.0))
BOUNDS_B = (*BOUNDS_A, *ALLPASS)
BOUNDS_C = (*BOUNDS_A, *ALLPASS, *ALLPASS, *PEAKING, *PEAKING)
WINDOW_MS = 25.0


def score_mask() -> np.ndarray:
    grid = il.freqs()
    return (grid >= 90.0) & (grid <= 400.0)


def score_db(transfer: np.ndarray, muted: np.ndarray, *, reduced: bool = False) -> float:
    """``twomic_analyse.score_100_350_db``'s statistic: the mean dB change over a
    log grid of 100-350 Hz, read here on this module's own 1.46 Hz grid.

    ``reduced`` says the two curves are ALREADY cut to :func:`score_mask`. The
    search evaluates ~210 bins instead of 16,385, which is the difference
    between a minute and an hour for a 12-parameter solve; nothing else changes.
    """
    grid = il.freqs()
    inside = score_mask()
    if reduced:
        change = (20.0 * np.log10(np.abs(transfer) + 1e-30)
                  - 20.0 * np.log10(np.abs(muted) + 1e-30))
    else:
        change = (20.0 * np.log10(np.abs(transfer[inside]) + 1e-30)
                  - 20.0 * np.log10(np.abs(muted[inside]) + 1e-30))
    return float(np.mean(np.interp(np.log(SCORE_POINTS), np.log(grid[inside]), change)))


def band_changes(transfer: np.ndarray, muted: np.ndarray, bands) -> list[float]:
    grid = il.freqs()
    out = []
    for low, high in bands:
        inside = (grid >= low) & (grid < high)
        out.append(float(np.mean(
            20.0 * np.log10(np.abs(transfer[inside]) + 1e-30)
            - 20.0 * np.log10(np.abs(muted[inside]) + 1e-30))))
    return out


def cancellation_of(base: Mapping[str, Any], x) -> dict[str, Any]:
    """N1's cancellation branch with this parameter vector's changes."""
    chain = deepcopy(dict(base))
    chain["gain_db"] = round(float(x[0]), 4)
    chain["delay_ms"] = round(float(x[1]), 4)
    extra = []
    if len(x) >= 4:
        extra.append({"type": "Biquad", "parameters": {
            "type": "Allpass", "freq": round(float(x[2]), 4), "q": round(float(x[3]), 4)}})
    if len(x) >= 12:
        extra.append({"type": "Biquad", "parameters": {
            "type": "Allpass", "freq": round(float(x[4]), 4), "q": round(float(x[5]), 4)}})
        for base_index in (6, 9):
            extra.append({"type": "Biquad", "parameters": {
                "type": "Peaking", "freq": round(float(x[base_index]), 4),
                "gain": round(float(x[base_index + 1]), 4),
                "q": round(float(x[base_index + 2]), 4)}})
    chain["filters"] = [*chain["filters"], *extra]
    return chain


def section_with(base_section: Mapping[str, Any], chain: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(dict(base_section))
    out["rear"] = {**out["rear"], "cancellation": deepcopy(dict(chain))}
    out["common_delay_ms"] = COMMON_DELAY_MS
    return out


def windowed(transfer: np.ndarray, marker: int) -> np.ndarray:
    impulse = np.fft.irfft(transfer, n=il.N_FFT)
    return np.fft.rfft(impulse * window(marker, impulse.size, WINDOW_MS), n=il.N_FFT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sp", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--cands", type=Path, required=True)
    args = parser.parse_args()
    with args.model.open("rb") as handle:
        held = pickle.load(handle)
    models, chains, documents, groups = (held[key] for key in
                                         ("models", "chains", "documents", "groups"))
    aligned = held["aligned"]
    bands = il.third_octaves()
    n1 = section_of(json.loads((args.sp / "docs/doc-N1.json").read_text()))
    grid = il.freqs()
    inband = (grid >= 80.0) & (grid <= 400.0)

    print("=== 3. ideal rear chain c_opt = -X_0/R, RELATIVE to N1's rear chain")
    print("  1/6-octave 80-400 Hz; 'ung' is ungated, 'w25' after a common 25 ms window")
    sixths = [(f / 2 ** (1 / 12), f * 2 ** (1 / 12))
              for f in np.geomspace(80.0, 400.0, 15)]
    n1_chain = rear_stage_response(n1, grid)[0]
    for angle_tag, key in (("rear   0", ("c1", "side", "az+0.00_el+0.00_d+1.00")),
                           ("rear +20", ("c1", "side", "az-20.00_el+0.00_d+1.00")),
                           ("rear -20", ("c1", "side", "az+20.00_el+0.00_d+1.00"))):
        if key not in models:
            print(f"  {angle_tag}: no identified model")
            continue
        model = models[key]
        rear_clean = np.nan_to_num(model["R"])
        # One marker for BOTH curves, from R's own main arrival: the window has
        # to sit on the rear path, which is what c_opt is being asked about.
        marker = max(int(np.argmax(np.abs(np.fft.irfft(rear_clean, n=il.N_FFT)))),
                     int(PRE_MS * 1e-3 * SAMPLE_RATE_HZ))
        for tag, muted, rear_path in (("ung", model["muted"], rear_clean),
                                      ("w25", windowed(model["muted"], marker),
                                       windowed(rear_clean, marker))):
            with np.errstate(divide="ignore", invalid="ignore"):
                relative = (-muted / rear_path) / n1_chain
            rows = il.band_stats(np.where(inband, relative, np.nan), sixths)
            print(f"  {angle_tag} {tag}  dB " + "".join(f"{v[0]:+6.1f}" for v in rows))
            print(f"  {'':8s} {'':3s} deg" + "".join(f"{v[1]:+6.0f}" for v in rows))

    print("\n=== 4a. out-of-sample validation: identify on D1+D2 (pose 0), predict c1 and 649a")
    fit_keys = [key for key in models if key[0] in ("d1", "d2")]
    fitted = {}
    for mic in ("side", "main"):
        estimates = [models[key]["R"] for key in fit_keys if key[1] == mic]
        if estimates:
            fitted[mic] = il.robust_mean(estimates)
    print("  round cand      mic  pose   measured  predicted   error   | per third octave error")
    errors = []
    for tag in ("c1", "649a"):
        for (mic, pose), byc in sorted(groups[tag].items()):
            if pose_angle(pose) != 0.0 or "0caaa048" not in byc or mic not in fitted:
                continue
            muted = byc["0caaa048"]["transfer"]
            for fp, row in sorted(byc.items()):
                if fp == "0caaa048" or fp not in chains:
                    continue
                fit = aligned[(tag, mic, pose, fp)]
                if not fit["usable"]:
                    print(f"  {tag:5s} {fp} {mic:4s} {pose_angle(pose):+5.0f}  "
                          f"refused by the alignment gate ({fit['residual_db']:+.1f} dB)")
                    continue
                measured = fit["aligned"]
                predicted = muted + fitted[mic] * chains[fp]
                got, want = score_db(measured, muted), score_db(predicted, muted)
                per = [a - b for a, b in zip(band_changes(measured, muted, bands),
                                             band_changes(predicted, muted, bands))]
                errors.append((mic, got - want, max(per, key=abs)))
                print(f"  {tag:5s} {fp} {mic:4s} {pose_angle(pose):+5.0f}  {got:+8.2f} "
                      f"{want:+10.2f} {got - want:+8.2f}   | worst band {max(per, key=abs):+6.2f}")
    for mic in ("side", "main"):
        values = np.asarray([e[1] for e in errors if e[0] == mic])
        per = np.asarray([e[2] for e in errors if e[0] == mic])
        if values.size:
            print(f"  {mic}: n={values.size} score error mean {values.mean():+.2f} "
                  f"mean|e| {np.abs(values).mean():.2f} worst {values[np.argmax(np.abs(values))]:+.2f} dB"
                  f" | worst third-octave error {per[np.argmax(np.abs(per))]:+.2f} dB")

    print("\n=== 4b. bounds on N1's structure, identified model, 3 rear angles")
    rear_keys = {angle: [key for key in models if key[1] == "side"
                         and pose_angle(key[2]) == angle]
                 for angle in (0.0, 20.0, -20.0)}
    def reference_muted(keys):
        """CONFIRM's muted take where the angle has one -- it is the freshest
        round and the only one measured at all three angles beside 649a."""
        chosen = next((key for key in keys if key[0] == "c1"), keys[0])
        return models[chosen]["muted"]

    rear = {angle: (il.robust_mean([models[key]["R"] for key in keys]), reference_muted(keys))
            for angle, keys in rear_keys.items() if keys}
    front_keys = [key for key in models if key[1] == "main"]
    front_r = il.robust_mean([models[key]["R"] for key in front_keys])
    front_muted = [models[key]["muted"] for key in front_keys if key[0] == "c1"]
    base_chain = n1["rear"]["cancellation"]

    keep = score_mask()
    small_grid = grid[keep]
    small_rear = {angle: (np.nan_to_num(model)[keep], muted[keep])
                  for angle, (model, muted) in rear.items()}
    small_front_r = np.nan_to_num(front_r)[keep]
    small_front_muted = [muted[keep] for muted in front_muted]

    def evaluate(x, angles=None):
        section = section_with(n1, cancellation_of(base_chain, x))
        chain = rear_stage_response(section, small_grid)[0]
        scores = [score_db(muted + model * chain, muted, reduced=True)
                  for angle, (model, muted) in small_rear.items()
                  if angles is None or angle in angles]
        penalty = 0.0
        for muted in small_front_muted:
            value = score_db(muted + small_front_r * chain, muted, reduced=True)
            penalty += max(0.0, FRONT_FLOOR_DB - value)
        return float(np.mean(scores)) + PENALTY_WEIGHT * penalty, scores

    def solve(bounds, angles=None):
        found = differential_evolution(lambda x: evaluate(x, angles)[0], bounds, seed=SEED,
                                       maxiter=60, popsize=15, tol=0.01, polish=True,
                                       init="latinhypercube")
        return found.x

    results = {}
    for name, bounds in (("A", BOUNDS_A), ("B", BOUNDS_B), ("C", BOUNDS_C)):
        x = solve(bounds)
        chain = cancellation_of(base_chain, x)
        total, scores = evaluate(x)
        section = section_with(n1, chain)
        front = [score_db(m + np.nan_to_num(front_r) * rear_stage_response(section, grid)[0], m)
                 for m in front_muted]
        results[name] = {"x": x, "chain": chain, "scores": scores, "front": front}
        print(f"  {name}: mean {np.mean(scores):+.2f} dB  per angle "
              + " ".join(f"{v:+.2f}" for v in scores)
              + f"  front {min(front):+.2f}..{max(front):+.2f}"
              + f"  charge {rear_branch_sum_headroom_db(read_rear_calibration(section, sample_rate=48000)):.3f} dB")
        print(f"     gain {chain['gain_db']:+.3f} dB  delay {chain['delay_ms']:+.3f} ms  filters "
              + " ".join(f"{f['parameters']['type']}@{f['parameters']['freq']:g}"
                         + (f"/q{f['parameters']['q']:g}" if "q" in f["parameters"] else "")
                         + (f"/{f['parameters']['gain']:+g}dB" if "gain" in f["parameters"] else "")
                         for f in chain["filters"]))

    print("\n  held-out angle check (fit on 2 rear angles, score the third)")
    for name, bounds in (("B", BOUNDS_B), ("C", BOUNDS_C)):
        outs = []
        for drop in rear:
            keep = [a for a in rear if a != drop]
            x = solve(bounds, angles=keep)
            _total, _ = evaluate(x, keep)
            held_out = evaluate(x, [drop])[1][0]
            outs.append(held_out)
            print(f"    {name} held out {drop:+.0f} deg: fitted {np.mean(evaluate(x, keep)[1]):+.2f}"
                  f"  held-out {held_out:+.2f}")
        print(f"    {name} mean held-out {np.mean(outs):+.2f} dB")
        results[name]["held_out"] = float(np.mean(outs))

    print("\n=== 5. emitted candidates")
    args.cands.mkdir(parents=True, exist_ok=True)
    for name, row in results.items():
        section = section_with(n1, row["chain"])
        section["conditions"] = {
            "dataset": "jts3 measured summed rounds 3138af96e104 (D1), 815ecfe40241 (D2), "
                       "dea67cbd648d (CONFIRM), 649a313770cb; side mic behind, arm 0/+-20",
            "fit_band_hz": [100.0, 350.0], "fit_tool": "ident_bounds.py", "measured": True,
            "timing_reference": "front output of this stage"}
        section["assumptions"] = [
            "The rear acoustic path R(f) was IDENTIFIED from the measured summed rounds: "
            "R = (X_i - X_0) / c_rear_i over every accepted candidate take, robust mean. "
            "The pair-round forward model is not used -- it was wrong behind the cabinet.",
            "common_delay_ms is held at N1's 1.06 ms: the compiler adds it to the front chain "
            "too, so moving it would invalidate X_0, the measured rear-muted reference.",
            "The front chain and the bass branch are doc-N1's verbatim, so doc-Nm "
            "(fingerprint 0caaa048) stays the measured zero.",
            f"Search stage {name}.",
        ]
        document = {"kind": "jts_prescription", "schema": 1, "base": "saved",
                    "rationale": f"ident-{name}: N1's structure with the cancellation branch "
                                 f"solved against the MEASURED rear path.",
                    "sections": {"rear_calibration": section}}
        path = args.cands / f"ident-{name}.json"
        path.write_text(json.dumps(document, indent=4) + "\n")
        try:
            read_rear_calibration(section, sample_rate=48000)
            read_prescription_document(document)
            verdict = "PASS"
        except Exception as exc:
            verdict = f"FAIL {type(exc).__name__}: {exc}"
        chain = rear_stage_response(read_rear_calibration(section, sample_rate=48000), grid)[0]
        model, muted = rear[0.0]
        per = band_changes(muted + np.nan_to_num(model) * chain, muted, bands)
        print(f"  {path.name} {verdict}  behind 3-angle "
              + " ".join(f"{v:+.2f}" for v in row["scores"])
              + f"  front {min(row['front']):+.2f}")
        print(f"     180 deg per third octave: "
              + " ".join(f"{b[0]:.0f}:{v:+.1f}" for b, v in zip(bands, per)))
    return 0


def pose_angle(pose: str) -> float:
    return float(pose.split("az")[1].split("_")[0])


if __name__ == "__main__":
    raise SystemExit(main())
