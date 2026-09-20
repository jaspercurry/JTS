#!/usr/bin/env python3
"""The three front guards, main microphone, against the rear-muted take.

  A  100-350 Hz   must be >= -2 dB
  B  71-90 Hz     must be no more than 0.5 dB worse than e-0.35
  C  350 Hz-5 kHz must be within 0.4 dB of muted

e-0.35 is not a candidate in every round, so guard B's reference is read from
whichever round holds it and the cross-round caveat is printed with it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))

import identlib as il              # noqa: E402
from ident import groups, pose_angle  # noqa: E402

GUARDS = (("100-350", (100.0, 350.0)), ("71-90", (71.0, 90.0)), ("350-5k", (350.0, 5000.0)))
MUTED = "0caaa048"
GRID = il.freqs()
CALS = {"main_cal": SP / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
        "side_cal": SP / "dayton-CMM31555.txt"}


def band_db(values, band):
    inside = (GRID >= band[0]) & (GRID < band[1])
    return 10.0 * np.log10(np.mean(np.abs(values[inside]) ** 2) + 1e-30)


def measure(round_dir: Path, labels: dict[str, str]) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = {}
    grouped = groups(il.take_transfers(round_dir, **CALS))
    for (mic, pose), byc in grouped.items():
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
            values = [band_db(fit["aligned"], b) - band_db(muted, b) for _, b in GUARDS]
            out.setdefault(name, {})[f"{pose_angle(pose):+.0f}"] = values
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-dir", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--reference-round", type=Path, nargs="*", default=[],
                        help="round(s) holding e-0.35, for guard B's reference")
    parser.add_argument("--reference-labels", type=Path)
    args = parser.parse_args()

    held = measure(args.round_dir, json.loads(args.labels.read_text()))
    reference: list[float] = []
    for path in args.reference_round:
        rows = measure(path, json.loads(args.reference_labels.read_text()))
        reference += [v[1] for v in rows.get("e-0.35", {}).values()]
    e035 = float(np.mean(reference)) if reference else None

    print("FRONT guards, main mic vs rear-muted, dB")
    print(f"  {'tune':<11s} {'angle':>6s}" + "".join(f"{n:>11s}" for n, _ in GUARDS)
          + "   verdict")
    for name, byangle in sorted(held.items()):
        for angle, values in sorted(byangle.items(), key=lambda kv: -float(kv[0])):
            notes = []
            if values[0] < -2.0:
                notes.append("FAIL 100-350 < -2")
            if e035 is not None and values[1] < e035 - 0.5:
                notes.append(f"FAIL 71-90 {values[1] - e035:+.2f} vs e-0.35")
            if abs(values[2]) > 0.4:
                notes.append(f"FAIL 350-5k {values[2]:+.2f}")
            print(f"  {name:<11s} {angle:>6s}" + "".join(f"{v:>+11.2f}" for v in values)
                  + "   " + ("; ".join(notes) if notes else "pass"))
    if e035 is not None:
        print(f"\n  guard B reference: e-0.35's 71-90 Hz = {e035:+.2f} dB, "
              f"meaned over {len(reference)} pose(s) of the reference round(s).")
        print("  e-0.35 is not in this round, so guard B is a CROSS-ROUND comparison.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
