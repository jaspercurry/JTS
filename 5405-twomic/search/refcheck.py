#!/usr/bin/env python3
"""Is this round's rear-muted reference take sound?

Every side score is read against ONE muted take, so a bad muted take shifts
every candidate in the round together (round d3, 310c37cd0445: the side
reference came in 1.9 dB quiet with a 14.7 dB hole at 143 Hz, and all seven
candidates moved +2.0 to +2.9 dB). The main microphone is the control: it
watches the same playbacks from the front, so if the MAIN reference is steady
across rounds and the SIDE one is not, the fault is the side take and not the
speaker or the room.

Prints the reference's absolute in-band level and its shape (per band, minus
its own in-band mean, so a plain level offset does not hide a spectral defect).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

BAND_HZ = (89.0, 111.0, 143.0, 178.0, 224.0, 283.0, 356.0)
POINTS = np.geomspace(100.0, 350.0, 23)


def read(node: dict) -> tuple[float, list[float]]:
    row = node["candidates"][node["muted"]]
    grid = np.asarray(row["freqs_hz"], dtype=float)
    curve = np.asarray(row["magnitude_db"], dtype=float)
    level = float(np.interp(np.log(POINTS), np.log(grid), curve).mean())
    shape = [float(np.interp(np.log(hz), np.log(grid), curve)) - level for hz in BAND_HZ]
    return level, shape


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    print("rear-muted reference, per round / mic / pose")
    print(f"  {'round':<16s} {'mic':<5s} {'pose':<10s} {'abs dB':>8s}   "
          + "".join(f"{hz:>7.0f}" for hz in BAND_HZ))
    for path in args.result:
        result = json.loads(path.read_text())
        name = path.parent.name
        for mic in ("main", "side"):
            for pose, node in sorted(result["by_pose"][mic].items()):
                level, shape = read(node)
                angle = pose.split("az")[1].split("_")[0]
                print(f"  {name:<16s} {mic:<5s} {angle:<10s} {level:>8.2f}   "
                      + "".join(f"{value:>+7.1f}" for value in shape))
    print("\n  'abs dB' is the absolute in-band level; the band columns are that curve")
    print("  minus its own in-band mean, so they show SHAPE only.")
    print("  A side reference that departs from the other rounds while the main one")
    print("  holds steady is a bad take, not a change in the speaker or the room.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
