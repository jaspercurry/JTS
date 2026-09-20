#!/usr/bin/env python3
"""Identify the rear acoustic path R(f) from the measured summed rounds.

Stages, each printed: alignment quality, agreement between the per-candidate
estimates of R, the ideal rear chain against N1's, and an out-of-sample
validation of the identified model. Writes everything to a cache so the bound
search does not re-decode the WAVs.
"""
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np

import identlib as il

#: ``era`` splits the rounds at the USB host-controller failure and re-bind.
#: The rig was handled during that repair, so a model identified before it is
#: not automatically the same system as one identified after.
ROUNDS = {"d1": "round-3138af96e104", "d2": "round-815ecfe40241",
          "c1": "round-dea67cbd648d", "649a": "round-649a313770cb",
          "i1b": "round-d929a1c333a8", "i2": "round-231b37be3851"}
ERA = {"d1": "pre", "d2": "pre", "c1": "pre", "649a": "pre", "i1b": "post", "i2": "post"}


def pose_angle(pose: str) -> float:
    return float(pose.split("az")[1].split("_")[0])


def collect(sp: Path, names) -> dict:
    cache = sp / "ident-cache.pkl"
    if cache.exists():
        with cache.open("rb") as handle:
            held = pickle.load(handle)
    else:
        held = {}
    for tag in names:
        if tag in held:
            continue
        held[tag] = il.take_transfers(
            sp / ROUNDS[tag],
            main_cal=sp / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
            side_cal=sp / "dayton-CMM31555.txt")
        with cache.open("wb") as handle:
            pickle.dump(held, handle)
        print(f"  decoded {tag} ({ROUNDS[tag]}): {len(held[tag])} take rows")
    return held


def groups(rows):
    """``{(mic, pose): {candidate: row}}`` keeping the LAST accepted attempt."""
    out: dict = {}
    for row in rows:
        if not row["ok"]:
            continue
        out.setdefault((row["mic"], row["pose"]), {})[row["candidate"]] = row
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sp", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sp = args.sp
    documents = il.load_documents(sp / "search" / "fp-index.json", sp)
    chains = {fp: il.rear_chain(doc) for fp, doc in documents.items()}
    print("decoding rounds (cached)")
    rounds = collect(sp, ROUNDS)

    print("\n=== 1. alignment onto the muted take, 1-4 kHz only")
    print("  round mic  pose   n   delay ms (min..max)   trim dB (min..max)   residual dB (worst)")
    aligned: dict = {}
    dropped: list = []
    for tag, rows in rounds.items():
        for (mic, pose), byc in sorted(groups(rows).items()):
            if "0caaa048" not in byc:
                print(f"  {tag:5s} {mic:4s} {pose_angle(pose):+5.0f}  -- no accepted muted take")
                continue
            muted = byc["0caaa048"]["transfer"]
            delays, trims, residuals = [], [], []
            for fp, row in byc.items():
                fit = il.align(row["transfer"], muted)
                fit["usable"] = fit["residual_db"] <= il.ALIGN_RESIDUAL_MAX_DB
                aligned[(tag, mic, pose, fp)] = fit
                if fp == "0caaa048":
                    continue
                if not fit["usable"]:
                    dropped.append((tag, mic, pose_angle(pose), fp, row["take_id"][-9:],
                                    fit["residual_db"], fit["trim_db"]))
                    continue
                delays.append(fit["delay_ms"]); trims.append(fit["trim_db"])
                residuals.append(fit["residual_db"])
            if not residuals:
                print(f"  {tag:5s} {mic:4s} {pose_angle(pose):+5.0f}  -- every candidate refused")
                continue
            print(f"  {tag:5s} {mic:4s} {pose_angle(pose):+5.0f} {len(residuals):3d}   "
                  f"{min(delays):+6.3f}..{max(delays):+6.3f}      "
                  f"{min(trims):+5.2f}..{max(trims):+5.2f}       {max(residuals):+6.1f}")

    print(f"  gate: a candidate take whose 1-4 kHz residual is worse than "
          f"{il.ALIGN_RESIDUAL_MAX_DB:g} dB is refused")
    for row in dropped:
        print(f"    refused {row[0]:5s} {row[1]:4s} {row[2]:+5.0f} {row[3]} {row[4]} "
              f"residual {row[5]:+.1f} dB, trim {row[6]:+.1f} dB")
    print(f"    {len(dropped)} of {len(aligned)} take rows refused")

    print("\n=== 2. agreement of the per-candidate estimates of R")
    bands = il.third_octaves()
    print("  R spread across candidates (peak-to-peak), per third octave 80-400 Hz")
    print("  round mic  pose  n  " + "".join(f"{b[0]:>7.0f}" for b in bands) + "   worst")
    models: dict = {}
    for tag, rows in rounds.items():
        for (mic, pose), byc in sorted(groups(rows).items()):
            if "0caaa048" not in byc:
                continue
            muted = byc["0caaa048"]["transfer"]
            estimates, used = [], []
            for fp in byc:
                if fp == "0caaa048" or fp not in chains:
                    continue
                if not aligned[(tag, mic, pose, fp)]["usable"]:
                    continue
                estimates.append(il.estimate_r(
                    aligned[(tag, mic, pose, fp)]["aligned"], muted, chains[fp]))
                used.append(fp)
            if len(estimates) < 2:
                continue
            model = il.robust_mean(estimates)
            models[(tag, mic, pose)] = {"R": model, "muted": muted, "n": len(estimates)}
            stack = np.asarray(estimates)
            spread_db, spread_deg = [], []
            for low, high in bands:
                inside = (il.freqs() >= low) & (il.freqs() < high)
                cell = stack[:, inside]
                ok = np.isfinite(cell)
                if not ok.any():
                    spread_db.append(np.nan); spread_deg.append(np.nan); continue
                with np.errstate(invalid="ignore"):
                    level = 20.0 * np.log10(np.abs(cell))
                    spread_db.append(float(np.nanmax(np.nanmean(level, axis=1))
                                           - np.nanmin(np.nanmean(level, axis=1))))
                    unit = np.nanmean(cell / np.abs(cell), axis=1)
                    spread_deg.append(il.circular_spread_deg(np.degrees(np.angle(unit))))
            print(f"  {tag:5s} {mic:4s} {pose_angle(pose):+5.0f} {len(estimates):2d}  "
                  + "".join(f"{v:7.1f}" for v in spread_db)
                  + f"   {np.nanmax(spread_db):.1f} dB / {np.nanmax(spread_deg):.0f} deg")
    with args.out.open("wb") as handle:
        pickle.dump({"models": models, "aligned": aligned, "chains": chains,
                     "documents": documents,
                     "groups": {tag: groups(rows) for tag, rows in rounds.items()}}, handle)
    print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
