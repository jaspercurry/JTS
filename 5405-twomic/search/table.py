#!/usr/bin/env python3
"""Small table printer for a twomic_analyse.py result.

SCORE = side mic ``score_100_350_db`` (ungated change vs the rear-muted
candidate over 100-350 Hz; more negative = deeper null behind the box).
The per-band columns are the same ungated difference, averaged over the
1/3-octave band around each of 89/111/143/178/224/283/356 Hz. (A single grid
point is far too spiky to read a trend off.)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

BAND_HZ = (89.0, 111.0, 143.0, 178.0, 224.0, 283.0, 356.0)


def tags(path: Path | None) -> dict[str, str]:
    return {} if path is None or not path.exists() else json.loads(path.read_text())


def name(fingerprint: str, labels: dict[str, str]) -> str:
    for prefix, tag in labels.items():
        if fingerprint.startswith(prefix):
            return tag
    return fingerprint[:8]


EDGE = 2.0 ** (1.0 / 6.0)   # 1/3-octave half-width


def per_band(node: dict, fingerprint: str) -> list[float]:
    """Ungated dB change, meaned over the 1/3-octave band at each BAND_HZ."""
    row = node["candidates"][fingerprint]
    reference = node["candidates"][node["muted"]]
    grid = np.asarray(row["freqs_hz"], dtype=float)
    delta = np.asarray(row["magnitude_db"], dtype=float) - np.asarray(
        reference["magnitude_db"], dtype=float)
    out = []
    for centre in BAND_HZ:
        inside = (grid >= centre / EDGE) & (grid <= centre * EDGE)
        # The analysis grid is linear, so a low band can hold a single point.
        out.append(float(delta[inside].mean()) if inside.any()
                   else float(delta[int(np.argmin(np.abs(grid - centre)))]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--labels", type=Path, default=None,
                        help="JSON {fingerprint-prefix: tag}")
    parser.add_argument("--mic", default="both", choices=("side", "main", "both"))
    args = parser.parse_args()
    result = json.loads(args.result.read_text())
    labels = tags(args.labels)
    mics = ("side", "main") if args.mic == "both" else (args.mic,)

    for mic in mics:
        where = "BEHIND the box" if mic == "side" else "IN FRONT"
        for pose, node in sorted(result["by_pose"][mic].items()):
            print(f"\n{mic} ({where})  pose {pose}   muted={node['muted'][:8]}")
            print(f"  {'tag':<12s} {'score':>7s}  " + "".join(
                f"{hz:>7.0f}" for hz in BAND_HZ))
            rows = []
            for fingerprint, row in node["candidates"].items():
                score = row["score_100_350_db"]
                rows.append((score if score is not None else 99.0,
                             name(fingerprint, labels), fingerprint, score))
            for _, tag, fingerprint, score in sorted(rows):
                cells = "".join(f"{value:>+7.1f}" for value in per_band(node, fingerprint))
                shown = "   --  " if score is None else f"{score:>+7.2f}"
                print(f"  {tag:<12s} {shown}  {cells}   {fingerprint[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
