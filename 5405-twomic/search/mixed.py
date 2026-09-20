#!/usr/bin/env python3
"""The MIXED objective, per angle, measured beside the builder's prediction.

Mixed = UNGATED change at 100 and 125 Hz, GATED 10 ms change at 160/200/250/315
Hz, each band capped at -10 dB, meaned over the six. The gate is what the new
model predicts, and below 160 Hz a 10 ms window is too short to read.

Bands, gate, marker rule and the aligner all come from the builder's own
``diag.py`` / ``identlib`` by import; nothing is re-derived here. The +0 pose is
printed but is DECORATION: its geometry keeps moving between rounds.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))
sys.path.insert(0, str(SP / "search"))

import identlib as il                 # noqa: E402
from ident import groups, pose_angle  # noqa: E402
from lscore import (BANDS, CENTRES, CAP_DB, MUTED, band_db, gated,  # noqa: E402
                    marker_of)

GATED_FROM = 2                        # 160 Hz and up are read gated
FRONT_THIRDS = ((280.6, 353.6, 315), (353.6, 445.4, 400),
                (445.4, 561.2, 500), (561.2, 707.1, 630))
FRONT_GUARDS = (("100-350", (100.0, 350.0)), ("71-90", (71.0, 90.0)),
                ("350-5k", (350.0, 5000.0)))
CALS = {"main_cal": SP / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
        "side_cal": SP / "dayton-CMM31555.txt"}


def capped_mean(values):
    """Mean of the six bands with each floored at CAP_DB, so one very deep band
    cannot buy the score."""
    return float(np.mean([max(v, CAP_DB) for v in values]))


def rows_of(round_dir: Path, labels: dict[str, str]):
    """{(mic, angle, name): {'ungated': [...6], 'gated': [...6], 'mixed': float}}"""
    out: dict = {}
    for (mic, pose), byc in groups(il.take_transfers(round_dir, **CALS)).items():
        if MUTED not in byc:
            continue
        muted = byc[MUTED]["transfer"]
        marker = marker_of(muted)
        muted_gated = gated(muted, marker)
        for fingerprint, row in byc.items():
            if fingerprint == MUTED:
                continue
            fit = il.align(row["transfer"], muted)
            name = labels.get(fingerprint, fingerprint)
            key = (mic, pose_angle(pose), name)
            if fit["residual_db"] > il.ALIGN_RESIDUAL_MAX_DB:
                out[key] = {"refused": fit["residual_db"]}
                continue
            curve = fit["aligned"]
            curve_gated = gated(curve, marker)
            ungated = [band_db(curve, b) - band_db(muted, b) for b in BANDS]
            gate = [band_db(curve_gated, b) - band_db(muted_gated, b) for b in BANDS]
            mixed = ungated[:GATED_FROM] + gate[GATED_FROM:]
            out[key] = {"ungated": ungated, "gated": gate, "mixed": mixed,
                        "score": capped_mean(mixed)}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-dir", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--predicted", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text())
    held = rows_of(args.round_dir, labels)
    names = sorted({k[2] for k in held})
    predicted = {}
    if args.predicted:
        summary = json.loads(args.predicted.read_text())
        for tag, node in summary.get("stages", {}).items():
            predicted[tag] = node["table"]["bands"]

    head = "".join(f"{c:>8d}" for c in CENTRES)
    for angle in (20.0, -20.0, 0.0):
        note = "   (DECORATION: this pose's geometry keeps moving)" if angle == 0 else ""
        print(f"\n=== arm {angle:+.0f}{note}")
        print(f"  {'tune':<9s} {'view':<9s}{head}{'mixed':>9s}")
        for name in names:
            row = held.get(("side", angle, name))
            if row is None:
                continue
            if "refused" in row:
                print(f"  {name:<9s} REFUSED by the aligner ({row['refused']:+.1f} dB)")
                continue
            print(f"  {name:<9s} {'ungated':<9s}" + "".join(f"{v:>+8.1f}" for v in row["ungated"]))
            print(f"  {'':<9s} {'gated':<9s}" + "".join(f"{v:>+8.1f}" for v in row["gated"]))
            print(f"  {'':<9s} {'MIXED':<9s}" + "".join(f"{v:>+8.1f}" for v in row["mixed"])
                  + f"{row['score']:>+9.2f}")
            want = predicted.get(name, {}).get({20.0: "+20", -20.0: "-20", 0.0: "+0"}[angle])
            if want:
                print(f"  {'':<9s} {'predicted':<9s}" + "".join(f"{v:>+8.1f}" for v in want)
                      + f"{capped_mean(want):>+9.2f}")
                print(f"  {'':<9s} {'ERROR':<9s}" + "".join(
                    f"{m - w:>+8.1f}" for m, w in zip(row["mixed"], want)))
    if args.out:
        args.out.write_text(json.dumps(
            {f"{m}|{a:+.0f}|{n}": v for (m, a, n), v in held.items()}, indent=1) + "\n")
        print(f"\nwrote {args.out}")

    print("\n=== FRONT (main mic) guards vs muted, dB")
    print(f"  {'tune':<9s} {'angle':>6s}" + "".join(f"{n:>10s}" for n, _ in FRONT_GUARDS))
    front: dict = {}
    for (mic, pose), byc in groups(il.take_transfers(args.round_dir, **CALS)).items():
        if mic != "main" or MUTED not in byc:
            continue
        muted = byc[MUTED]["transfer"]
        for fingerprint, row in byc.items():
            if fingerprint == MUTED:
                continue
            fit = il.align(row["transfer"], muted)
            if fit["residual_db"] > il.ALIGN_RESIDUAL_MAX_DB:
                continue
            name = labels.get(fingerprint, fingerprint)
            front[(name, pose_angle(pose))] = (
                [band_db(fit["aligned"], b) - band_db(muted, b) for _, b in FRONT_GUARDS],
                [band_db(fit["aligned"], (lo, hi)) - band_db(muted, (lo, hi))
                 for lo, hi, _ in FRONT_THIRDS])
    for (name, angle), (guards, _) in sorted(front.items()):
        print(f"  {name:<9s} {angle:>+6.0f}" + "".join(f"{v:>+10.2f}" for v in guards))
    print(f"\n=== FRONT per third octave (the S1 472 Hz low-pass check), dB vs muted")
    print(f"  {'tune':<9s} {'angle':>6s}" + "".join(f"{c:>9d}" for *_, c in FRONT_THIRDS))
    for (name, angle), (_, thirds) in sorted(front.items()):
        print(f"  {name:<9s} {angle:>+6.0f}" + "".join(f"{v:>+9.2f}" for v in thirds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
