#!/usr/bin/env python3
"""FRONT-TO-BACK GAIN per third octave, the new objective.

F/B gain = front change - behind change, both against the rear-muted take of
the SAME round. Front is the mean of the three main-mic poses, ungated.
Behind is the mean of side +20 and -20 (arm +0 is excluded: its geometry
moved after the USB repair), ungated at 100/125 Hz and gated 10 ms at
160-315 Hz. The all-ungated variant is printed beside it.

Summary = mean over 100/125/160/200/250/315 of min(F/B gain, 12): a band that
is already hugely directional must not buy the score.

Bands, the 10 ms gate and the marker come from the builder's ``g5lib``; the
transfers and the aligner from ``identlib``. Rounds are registered at runtime;
g5lib itself is not edited.

THE ALIGNER'S LEVEL TRIM IS A REAL HEADROOM CUT, and both readings are kept.
``il.align`` fits one broadband magnitude trim over 1-4 kHz. It is NOT rear
branch leakage: the builder refitted it over 4-10 kHz and got the same value,
and across 35 tunes it tracks the product's own ``rear_branch_sum_headroom_db``
at r=0.999. A tune with rear boosts is simply played with a deliberate
broadband headroom cut. So:

  front SHAPE change  = trim kept    -- how the response curve changes
  front TOTAL change  = trim removed -- what a listener gets at one volume
                                        setting, = shape - headroom cut

The largest total loss is the EQ-BACK COST. F/B gain is unaffected by the
choice: a cut common to both microphones cancels in front-minus-behind, which
is what g5lib's SHARE_MAIN_TRIM achieves by a different route. The aligner's
DELAY is always kept -- the 10 ms gate needs the takes on one time base.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))

import g5lib as g          # noqa: E402
import identlib as il      # noqa: E402
from ident import groups   # noqa: E402

#: Third octaves for the FRONT report, 63-630 Hz.
FRONT_CENTRES = (63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630)
EDGE = 2.0 ** (1.0 / 6.0)
SCORE_CAP = 12.0
BEHIND_ANGLES = (20.0, -20.0)


def third(centre: float) -> tuple[float, float]:
    return centre / EDGE, centre * EDGE


def collect(tags) -> tuple[dict, list]:
    """``{(tag, mic, pose): {muted, taper, muted_g, cands}}`` with RAW magnitudes.

    The aligner's delay is applied; its magnitude trim is divided back out.
    """
    held = g.cache(tuple(tags))
    store, trims = {}, {}
    for tag in tags:
        for (mic, pose), byc in groups(held[tag]).items():
            if g.MUTED not in byc:
                continue
            muted = byc[g.MUTED]["transfer"]
            taper = g.taper_of(g.marker_of(muted))
            row = {"muted": muted, "taper": taper, "muted_g": g.gate(muted, taper),
                   "cands": {}}
            for fp, take in byc.items():
                if fp == g.MUTED:
                    continue
                fit = il.align(take["transfer"], muted)
                if fit["residual_db"] > il.ALIGN_RESIDUAL_MAX_DB:
                    continue
                trims.setdefault((tag, fp), []).append(fit["trim_db"])
                row["cands"][fp] = fit["aligned"] / (10.0 ** (fit["trim_db"] / 20.0))
            store[(tag, mic, pose)] = row
    #: the headroom cut, meaned over every accepted take of that tune
    cuts = {key: float(np.mean(values)) for key, values in trims.items()}
    return store, cuts


def change(store, tag, mic, angles, fp, band, *, gated: bool) -> float | None:
    """Mean dB change vs muted over the given poses, for one band."""
    values = []
    for (row_tag, row_mic, pose), row in store.items():
        if row_tag != tag or row_mic != mic or fp not in row["cands"]:
            continue
        if angles is not None and round(g.angle_of(pose)) not in angles:
            continue
        aligned = row["cands"][fp]
        if gated:
            values.append(g.band_db(g.gate(aligned, row["taper"]), band)
                          - g.band_db(row["muted_g"], band))
        else:
            values.append(g.band_db(aligned, band) - g.band_db(row["muted"], band))
    return float(np.mean(values)) if values else None


def table(store, tag, fp) -> dict:
    front, behind_mixed, behind_ung = [], [], []
    for index, centre in enumerate(g.CENTRES):
        band = g.BANDS[index]
        front.append(change(store, tag, "main", None, fp, band, gated=False))
        behind_ung.append(change(store, tag, "side", (20, -20), fp, band, gated=False))
        behind_mixed.append(change(store, tag, "side", (20, -20), fp, band,
                                   gated=index >= g.GATED_FROM))
    front_wide = [change(store, tag, "main", None, fp, third(c), gated=False)
                  for c in FRONT_CENTRES]
    if any(v is None for v in front + behind_mixed + behind_ung):
        return {}
    mixed = [f - b for f, b in zip(front, behind_mixed)]
    ungated = [f - b for f, b in zip(front, behind_ung)]
    return {"front": front, "behind_mixed": behind_mixed, "behind_ungated": behind_ung,
            "fb_mixed": mixed, "fb_ungated": ungated,
            "score_mixed": float(np.mean([min(v, SCORE_CAP) for v in mixed])),
            "score_ungated": float(np.mean([min(v, SCORE_CAP) for v in ungated])),
            "front_wide": front_wide}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", action="append", required=True,
                        metavar="TAG=round-dir", help="e.g. f1=round-abc123")
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text())

    tags = []
    for item in args.round:
        tag, _, directory = item.partition("=")
        g.ROUNDS[tag] = directory
        tags.append(tag)
    g.LAB.update(labels)
    store, cuts = collect(tags)
    print("  headroom cut per tune (dB, the aligner trim; total = shape - cut):")
    for (tag, fp), cut in sorted(cuts.items()):
        if fp in labels:
            print(f"    {tag:4s} {labels[fp]:<9s} {cut:+.2f}")

    held: dict = {}
    for tag in tags:
        for fp, name in labels.items():
            if fp == g.MUTED:
                continue
            row = table(store, tag, fp)
            if row:
                held[(tag, name)] = row

    head = "".join(f"{c:>7d}" for c in g.CENTRES)
    for tag in tags:
        print(f"\n=== round {tag}: FRONT / BEHIND / F-B GAIN per third octave (dB)")
        print(f"  {'tune':<9s} {'view':<14s}{head}{'score':>9s}")
        for fp, name in labels.items():
            row = held.get((tag, name))
            if not row:
                continue
            print(f"  {name:<9s} {'front':<14s}" + "".join(f"{v:>+7.1f}" for v in row["front"]))
            print(f"  {'':<9s} {'behind mixed':<14s}"
                  + "".join(f"{v:>+7.1f}" for v in row["behind_mixed"]))
            print(f"  {'':<9s} {'F/B mixed':<14s}"
                  + "".join(f"{v:>+7.1f}" for v in row["fb_mixed"])
                  + f"{row['score_mixed']:>+9.2f}")
            print(f"  {'':<9s} {'behind ungat':<14s}"
                  + "".join(f"{v:>+7.1f}" for v in row["behind_ungated"]))
            print(f"  {'':<9s} {'F/B ungated':<14s}"
                  + "".join(f"{v:>+7.1f}" for v in row["fb_ungated"])
                  + f"{row['score_ungated']:>+9.2f}")

    if len(tags) > 1:
        print(f"\n=== two-round mean +- half the difference")
        print(f"  {'tune':<9s} {'F/B mixed score':>18s} {'F/B ungated score':>20s}")
        for fp, name in labels.items():
            rows = [held[(t, name)] for t in tags if (t, name) in held]
            if len(rows) != len(tags):
                continue
            a = [r["score_mixed"] for r in rows]
            b = [r["score_ungated"] for r in rows]
            print(f"  {name:<9s} {np.mean(a):>+13.2f} ±{abs(a[0] - a[1]) / 2:<4.2f}"
                  f" {np.mean(b):>+15.2f} ±{abs(b[0] - b[1]) / 2:<4.2f}")

    print(f"\n=== FRONT change per third octave 63-630 Hz (mean of 3 poses, ungated)")
    print(f"  {'tune':<9s} {'view':<6s}" + "".join(f"{c:>7d}" for c in FRONT_CENTRES)
          + f"{'cut':>7s}{'EQ-back':>9s}")
    for fp, name in labels.items():
        rows = [held[(t, name)] for t in tags if (t, name) in held]
        if not rows:
            continue
        total = np.mean([r["front_wide"] for r in rows], axis=0)
        cut = float(np.mean([cuts[(t, fp)] for t in tags if (t, fp) in cuts]))
        shape = total + cut
        worst = float(np.min(total))
        where = FRONT_CENTRES[int(np.argmin(total))]
        print(f"  {name:<9s} {'shape':<6s}" + "".join(f"{v:>+7.1f}" for v in shape))
        print(f"  {'':<9s} {'total':<6s}" + "".join(f"{v:>+7.1f}" for v in total)
              + f"{cut:>+7.2f}" + f"{worst:>+6.1f}@{where}")

    if args.out:
        args.out.write_text(json.dumps(
            {f"{t}|{n}": v for (t, n), v in held.items()}, indent=1) + "\n")
        print(f"\nwrote {args.out}")
    print(f"\n  score = mean over {g.CENTRES} of min(F/B gain, {SCORE_CAP:g})")
    print("  behind excludes arm +0 (geometry moved after the USB repair); front uses all 3 poses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
