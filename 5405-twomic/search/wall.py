#!/usr/bin/env python3
"""Wall-weighted F/B score, plus 160 and 200 Hz split by arm angle.

The cabinet goes 8 inches from the wall, so the SBIR hole lands near 170 Hz
and the bands around it are what matter. Weights: 100 x1, 125 x2, 160 x3,
200 x3, 250 x1, 315 x0.5, applied to min(F/B, 12).

160 Hz has read differently at +20 and -20 all night, so the two angles are
printed separately, gated and ungated, rather than meaned into one number.
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

import g5lib as g                                   # noqa: E402
from fbscore import collect, change, table          # noqa: E402

WEIGHTS = {100: 1.0, 125: 2.0, 160: 3.0, 200: 3.0, 250: 1.0, 315: 0.5}
SPLIT = (160, 200)


def weighted(fb: list[float]) -> float:
    w = np.array([WEIGHTS[c] for c in g.CENTRES])
    v = np.array([min(x, 12.0) for x in fb])
    return float(np.sum(w * v) / np.sum(w))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", action="append", required=True, metavar="TAG=dir")
    parser.add_argument("--labels", type=Path, required=True)
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text())
    tags = []
    for item in args.round:
        tag, _, directory = item.partition("=")
        g.ROUNDS[tag] = directory
        tags.append(tag)
    store, cuts = collect(tags)

    rows = {}
    for tag in tags:
        for fp, name in labels.items():
            if fp == g.MUTED:
                continue
            row = table(store, tag, fp)
            if row:
                rows[(tag, name)] = row

    print("WALL-WEIGHTED score (100x1 125x2 160x3 200x3 250x1 315x0.5) on min(F/B,12)")
    print(f"  {'tune':<11s}" + "".join(f"{t:>10s}" for t in tags)
          + f"{'mean':>9s}{'spread':>8s}{'plain':>9s}")
    for fp, name in labels.items():
        got = [rows[(t, name)] for t in tags if (t, name) in rows]
        if len(got) != len(tags):
            continue
        ww = [weighted(r["fb_mixed"]) for r in got]
        plain = np.mean([r["score_mixed"] for r in got])
        print(f"  {name:<11s}" + "".join(f"{v:>+10.2f}" for v in ww)
              + f"{np.mean(ww):>+9.2f}{abs(ww[0] - ww[-1]) / 2:>8.2f}{plain:>+9.2f}")

    print(f"\n160 Hz and 200 Hz BEHIND change vs muted, per arm angle "
          f"(two-round mean; g = gated 10 ms, u = ungated)")
    print(f"  {'tune':<11s}" + "".join(
        f"{f'{c}Hz {a:+d} {k}':>13s}" for c in SPLIT for a in (20, -20) for k in ("g", "u")))
    for fp, name in labels.items():
        if fp == g.MUTED:
            continue
        cells = []
        for centre in SPLIT:
            band = g.BANDS[g.CENTRES.index(centre)]
            for angle in (20, -20):
                for gated in (True, False):
                    vals = [change(store, t, "side", (angle,), fp, band, gated=gated)
                            for t in tags]
                    vals = [v for v in vals if v is not None]
                    cells.append(f"{np.mean(vals):>+13.1f}" if vals else f"{'--':>13s}")
        print(f"  {name:<11s}" + "".join(cells))

    print(f"\n  F/B gain at 160 and 200 Hz per angle (front is pose-meaned, so the "
          f"difference between angles is entirely the behind term)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
