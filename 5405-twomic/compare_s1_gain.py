#!/usr/bin/env python3
"""Measured against predicted, one table, for the s1-gain trial round.

Left side: what ``twomic_analyse.py`` read off the measured round. Right side:
what ``predict_windowed.py`` predicted from the pair round through the product's
forward model. Same window, same band, same reference (the rear-muted
candidate), so the two columns are the same number twice.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

#: The measured round's candidate fingerprints, and the document each one is.
CANDIDATES = (("5e9afae3", "doc-N1", "N1"),
              ("df4cc782", "doc-N1-g-2", "N1 g-2"),
              ("28d32bac", "doc-N1-g-4", "N1 g-4"))
SPANS = ("6", "12", "25")


def pose_label(mic: str, pose: str) -> str:
    degrees = float(pose.split("az")[1].split("_")[0])
    return ("rear" if mic == "side" else "front") + (
        f"{-degrees:+03.0f}" if mic == "side" else f"{degrees:+03.0f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measured", type=Path, required=True)
    parser.add_argument("--predicted", type=Path, required=True)
    args = parser.parse_args()
    measured = json.loads(args.measured.read_text())
    predicted = json.loads(args.predicted.read_text())
    rule = measured["by_candidate"]["main"]["marker_rule"]
    forecast = {name: row["win_score_db_by_marker"][rule]
                for name, row in predicted["candidates"].items()}
    forecast_ung = {name: row["ungated_db"] for name, row in predicted["candidates"].items()}

    columns = [(mic, pose) for mic in ("side", "main")
               for pose in sorted(measured["by_pose"][mic], key=lambda key: -float(
                   key.split("az")[1].split("_")[0]))]
    labels = [pose_label(mic, pose) for mic, pose in columns]
    print(f"s1-gain: MEASURED vs PREDICTED, dB against the rear-muted candidate "
          f"(marker rule {rule!r})")
    print("  candidate  win   " + "".join(f"{one:>17s}" for one in labels))
    errors: dict[str, list[float]] = {}
    for fingerprint, document, name in CANDIDATES:
        for span in (*SPANS, "ung"):
            cells = []
            for mic, pose in columns:
                table = measured["by_pose"][mic][pose]["candidates"]
                key = next((cid for cid in table if cid.startswith(fingerprint)), None)
                got = None if key is None else (
                    table[key]["score_100_350_db"] if span == "ung"
                    else table[key]["win_score_db"][span])
                want = (forecast_ung[document][pose_label(mic, pose)] if span == "ung"
                        else forecast[document][pose_label(mic, pose)][span])
                if got is None:
                    cells.append(f"{'--':>7s} {want:+8.2f}")
                    continue
                cells.append(f"{got:+7.2f} {want:+8.2f}")
                errors.setdefault(span, []).append(got - want)
            print(f"  {name if span == SPANS[0] else '':10s} {span:>4s}  " + " ".join(cells))
    print("  each cell is  measured  predicted ; '--' is a take the window refused")
    print("\n  prediction error (measured minus predicted), dB")
    for span in (*SPANS, "ung"):
        values = np.asarray(errors.get(span, []))
        if not values.size:
            continue
        worst = values[int(np.argmax(np.abs(values)))]
        print(f"    T={span:>4s}  n={values.size:2d}  mean {values.mean():+6.2f}  "
              f"mean|err| {np.abs(values).mean():5.2f}  worst {worst:+6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
