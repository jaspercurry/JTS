#!/usr/bin/env python3
"""One compact row per document: the prediction table the report carries.

Every number is :func:`rearpred.score_position`'s, so this file only lays out
what ``predict_null.py`` already computes. The three front poses agree to about
0.3 dB in every document seen so far, so they ride as a range rather than three
columns.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rearpred import load_positions, score_position, section_of

REAR = ("side/az+0.00_el+0.00_d+1.00", "side/az+20.00_el+0.00_d+1.00",
        "side/az-20.00_el+0.00_d+1.00")
FRONT = ("main/az+0.00_el+0.00_d+1.00", "main/az+20.00_el+0.00_d+1.00",
         "main/az-20.00_el+0.00_d+1.00")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--unmute", type=Path, nargs="*", default=(),
                        help="documents shipped rear_muted as a SEED, to score unmuted")
    parser.add_argument("docs", type=Path, nargs="+")
    args = parser.parse_args()
    positions = load_positions(args.out_dir)
    unmute = {path.resolve() for path in args.unmute}
    print("| document | rear-00 | rear-20 | rear+20 | front, 3 poses |")
    print("|---|---|---|---|---|")
    for path in args.docs:
        section = section_of(json.loads(path.read_text()))
        if path.resolve() in unmute:
            section = {**section, "rear_muted": False}
        label = path.stem if path.stem != "doc" else f"fit {path.parent.name}"
        rows = {key: score_position(section, positions[key], early=True)
                for key in (*REAR, *FRONT)}
        for which, field in (("ungated", "null_band_db"), ("early", "early_db")):
            front = [rows[key][field] for key in FRONT]
            print(f"| {label if which == 'ungated' else ''} ({which}) | "
                  + " | ".join(f"{rows[key][field]:+.2f}" for key in REAR)
                  + f" | {min(front):+.2f} .. {max(front):+.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
