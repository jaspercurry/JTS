#!/usr/bin/env python3
"""Stage 4a on its own: out-of-sample validation of the identified rear path.

Identify R on D1+D2 (pose 0 only), then predict every ACCEPTED candidate take of
CONFIRM and 649a at pose 0 and compare with what was measured. Takes the
alignment gate refused are named and left out -- they are broken cuts, and
scoring a model against a broken take measures the take.
"""
from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np

import identlib as il
from ident_bounds import band_changes, score_db


def pose_angle(pose: str) -> float:
    return float(pose.split("az")[1].split("_")[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    with args.model.open("rb") as handle:
        held = pickle.load(handle)
    models, chains, groups, aligned = (held[k] for k in
                                       ("models", "chains", "groups", "aligned"))
    bands = il.third_octaves()
    fitted = {}
    for mic in ("side", "main"):
        estimates = [models[key]["R"] for key in models if key[0] in ("d1", "d2") and key[1] == mic]
        if estimates:
            fitted[mic] = il.robust_mean(estimates)
    print("identified on D1+D2 pose 0; predicting CONFIRM and 649a pose 0")
    print("  round cand     mic  measured predicted   error  | worst 1/3-oct error")
    errors = {}
    for tag in ("c1", "649a"):
        for (mic, pose), byc in sorted(groups[tag].items()):
            if pose_angle(pose) != 0.0 or "0caaa048" not in byc or mic not in fitted:
                continue
            muted = byc["0caaa048"]["transfer"]
            for fp in sorted(byc):
                if fp == "0caaa048" or fp not in chains:
                    continue
                fit = aligned[(tag, mic, pose, fp)]
                if not fit["usable"]:
                    print(f"  {tag:5s} {fp} {mic:4s}  refused by the alignment gate "
                          f"({fit['residual_db']:+.1f} dB, trim {fit['trim_db']:+.1f} dB)")
                    continue
                predicted = muted + fitted[mic] * chains[fp]
                got, want = score_db(fit["aligned"], muted), score_db(predicted, muted)
                per = [a - b for a, b in zip(band_changes(fit["aligned"], muted, bands),
                                             band_changes(predicted, muted, bands))]
                errors.setdefault(mic, []).append((got - want, max(per, key=abs)))
                print(f"  {tag:5s} {fp} {mic:4s} {got:+9.2f} {want:+9.2f} {got - want:+8.2f}  "
                      f"| {max(per, key=abs):+.2f}")
    for mic, rows in errors.items():
        values = np.asarray([r[0] for r in rows]); per = np.asarray([r[1] for r in rows])
        print(f"  {mic}: n={values.size} mean {values.mean():+.2f} "
              f"mean|e| {np.abs(values).mean():.2f} worst {values[np.argmax(np.abs(values))]:+.2f} dB"
              f" | worst band {per[np.argmax(np.abs(per))]:+.2f} dB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
