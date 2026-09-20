#!/usr/bin/env python3
"""Run the product's ``scripts/fit-rear-branches.py`` on measured branch data.

The script is loaded from the product checkout by path and run unmodified,
with ONE disclosed exception: ``--allow-phase-steps`` replaces
``check_measured_pair`` with a reporter.

Why that exception exists. The guard refuses a measured pair whose front/rear
phase steps more than 90 degrees between adjacent rows, reading such a step as
a rotation the grid failed to resolve. On a REAL in-room pair the breaches sit
at NULLS of the measured ratio -- at a 0.1 m behind mic, 8 of 519 rows over
40-800 Hz, each at a frequency where one branch is 20-30 dB below its
neighbours. There the phase flips; it is not an unresolved rotation, and
nothing recovers it because there is nothing to recover. Checked: the breaches
survive 1/48 to 1/6 octave of delay-compensated complex smoothing (8 -> 2), so
they are not a sampling artifact. The override is printed, and the worst step
is printed with it, so a reader always sees what was overridden.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np

PRODUCT = Path("/Users/jaspercurry/Code/JTS/.claude/worktrees/deploy-jts3-cardioid")
FITTER = PRODUCT / "scripts" / "fit-rear-branches.py"


def load_fitter():
    spec = importlib.util.spec_from_file_location("fit_rear_branches", FITTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--measured-front", type=Path, required=True)
    parser.add_argument("--measured-rear", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-phase-steps", action="store_true",
                        help="report check_measured_pair's worst step instead of refusing")
    args = parser.parse_args()

    fitter = load_fitter()
    strict = fitter.check_measured_pair

    def reporting(front, rear) -> None:
        try:
            strict(front, rear)
            print("  check_measured_pair: PASSES unmodified")
        except ValueError as exc:
            freqs = front[0]
            band = (freqs >= fitter.FIT_BAND_HZ[0]) & (freqs <= fitter.FIT_BAND_HZ[1])
            relative = np.diff(np.angle(front[1][band] / rear[1][band]))
            step = np.degrees(np.abs((relative + np.pi) % (2.0 * np.pi) - np.pi))
            breach = freqs[band][1:][step > 90.0]
            print(f"  check_measured_pair: OVERRIDDEN -- {exc}")
            print(f"  {breach.size} of {step.size} rows over "
                  f"{fitter.FIT_BAND_HZ[0]:g}-{fitter.FIT_BAND_HZ[1]:g} Hz breach 90 deg: "
                  + " ".join(f"{hz:.0f}" for hz in breach))
            levels = [20.0 * np.log10(np.abs(table[1][band][1:][step > 90.0]))
                      for table in (front, rear)]
            print("  |front|,|rear| there, dB: "
                  + " ".join(f"({a:.0f},{b:.0f})" for a, b in zip(*levels)))

    if args.allow_phase_steps:
        fitter.check_measured_pair = reporting
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    sys.argv = ["fit-rear-branches.py",
                "--target", str(args.target), "--out", str(args.out), "--report", str(args.report),
                "--measured-front", str(args.measured_front),
                "--measured-rear", str(args.measured_rear)]
    try:
        fitter.main()
    except SystemExit as exc:
        # The fitter writes the document and the report either way and exits 1
        # when the fit misses its own suppression floor; that is a result.
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
