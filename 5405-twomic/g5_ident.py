#!/usr/bin/env python3
"""Step 1: identify R in the GATED domain and report its agreement.

Per pose and per third octave: the spread of the per-candidate estimates of
``R_g`` in magnitude (dB, peak to peak) and phase (deg, about the circular
mean), pooled two ways -- within a round, and across every round. The ungated
identification is printed beside it so the two can be compared directly.
"""
from __future__ import annotations

import numpy as np

import g5lib as g
import identlib as il

REPORT = [(142.5, 179.6), (178.2, 224.5), (222.7, 280.6), (280.6, 353.6)]
CENTRES = (160, 200, 250, 315)


def spread(estimates, band) -> tuple[float, float]:
    inside = (g.GRID >= band[0]) & (g.GRID < band[1])
    cell = np.asarray([one[inside] for one in estimates])
    if not np.isfinite(cell).any():
        return float("nan"), float("nan")
    with np.errstate(invalid="ignore", divide="ignore"):
        level = 20.0 * np.log10(np.abs(cell))
        db = float(np.nanmax(np.nanmean(level, axis=1)) - np.nanmin(np.nanmean(level, axis=1)))
        unit = np.nanmean(cell / np.abs(cell), axis=1)
    return db, il.circular_spread_deg(np.degrees(np.angle(unit)))


def main() -> int:
    store = g.collect()
    chain_of = g.chains()

    print("=== takes per (round, mic, pose) after the 1-4 kHz aligner gate")
    for key in sorted(store):
        row = store[key]
        if key[1] != "side":
            continue
        names = " ".join(sorted(g.label(fp) for fp in row["cands"]))
        print(f"  {key[0]:5s} {g.angle_of(key[2]):+5.0f}  {len(row['cands'])}  {names}")

    for mic in ("side", "main"):
        print(f"\n=== agreement of R across candidates AND rounds -- {mic} mic")
        print("  domain  angle   n  " + "".join(f"{c:>8d}" for c in CENTRES)
              + "     worst dB / deg")
        for gated in (True, False):
            for angle in (20.0, -20.0, 0.0):
                estimates = g.r_estimates(store, chain_of, set(g.ALL_TAGS), mic, angle,
                                          gated=gated)
                if len(estimates) < 2:
                    continue
                rows = [spread(estimates, b) for b in REPORT]
                print(f"  {'gated ' if gated else 'ungated'} {angle:+5.0f} {len(estimates):3d}  "
                      + "".join(f"{v[0]:8.1f}" for v in rows)
                      + f"   {np.nanmax([v[0] for v in rows]):5.1f} / "
                        f"{np.nanmax([v[1] for v in rows]):.0f}")

    print("\n=== round-to-round agreement of the POOLED gated R (side mic), dB / deg")
    print("  angle  pair                " + "".join(f"{c:>8d}" for c in CENTRES))
    for angle in (20.0, -20.0, 0.0):
        per_round = {}
        for tag in g.ALL_TAGS:
            estimates = g.r_estimates(store, chain_of, {tag}, "side", angle, gated=True)
            if len(estimates) >= 2:
                per_round[tag] = g.pooled(estimates)
        tags = sorted(per_round)
        if len(tags) < 2:
            continue
        base = tags[-1]
        for tag in tags[:-1]:
            cells = []
            for band in REPORT:
                inside = (g.GRID >= band[0]) & (g.GRID < band[1])
                ratio = per_round[tag][inside] / np.where(
                    np.abs(per_round[base][inside]) > 0, per_round[base][inside], 1.0)
                ratio = ratio[np.isfinite(ratio) & (np.abs(ratio) > 0)]
                cells.append((10.0 * np.log10(np.mean(np.abs(ratio) ** 2)),
                              np.degrees(np.angle(np.mean(ratio / np.abs(ratio))))))
            print(f"  {angle:+5.0f}  {tag:>5s} vs {base:<5s}     "
                  + "".join(f"{c[0]:+5.1f}/{c[1]:+4.0f}" if abs(c[1]) < 1000 else "   --"
                           for c in cells))

    print("\n=== is the GATED muted take smooth? |X_0,g| band level vs |X_0| (side)")
    print("  round angle  " + "".join(f"{c:>8d}" for c in CENTRES))
    for key in sorted(store):
        if key[1] != "side":
            continue
        row = store[key]
        cells = [g.band_db(row["muted_g"], b) - g.band_db(row["muted"], b) for b in REPORT]
        print(f"  {key[0]:5s} {g.angle_of(key[2]):+5.0f}  "
              + "".join(f"{v:+8.1f}" for v in cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
