#!/usr/bin/env python3
"""Repeat error: the round holds four tunes captured TWICE, both accepted.

Both attempts are aligned onto the SAME rear-muted reference with the SAME
marker, so the difference between their early/late ratios is what the rig can
do to a number that should not have moved. That is the honest bar the cardioid
changes have to clear.
"""
from __future__ import annotations
import numpy as np
import h4core as hc
from h4load import rows


def repeat_pairs():
    held: dict = {}
    for row in rows():
        if not row["ok"]:
            continue
        held.setdefault((row["mic"], row["pose"], row["candidate"]), []).append(row)
    out: dict = {}
    for key, attempts in sorted(held.items()):
        if len(attempts) < 2:
            continue
        mic, pose, fingerprint = key
        cell = {k: v for k, v in held.items() if k[0] == mic and k[1] == pose}
        muted = cell[(mic, pose, hc.MUTED)][-1]["transfer"]
        marker = hc.marker_of(muted)
        ratios = []
        for attempt in attempts:
            fit = hc.il.align(attempt["transfer"], muted)
            if fit["residual_db"] > hc.il.ALIGN_RESIDUAL_MAX_DB:
                ratios.append(None)
                continue
            row_db = []
            for _name, band in hc.OCTAVES:
                ir = hc.rolled_impulse(fit["aligned"], band, marker)
                row_db.append(10 * np.log10(
                    max(hc.window_energy(ir, *hc.EARLY_MS), 1e-30)
                    / max(hc.window_energy(ir, *hc.LATE_MS), 1e-30)))
            ratios.append(row_db)
        if any(r is None for r in ratios):
            continue
        out[f"{mic}|{hc.pose_angle(pose):+.0f}|{hc.TUNES[fingerprint][0]}"] = {
            name: float(ratios[1][i] - ratios[0][i])
            for i, (name, _band) in enumerate(hc.OCTAVES)}
    return out


if __name__ == "__main__":
    table = repeat_pairs()
    names = [n for n, _ in hc.OCTAVES]
    print("=== attempt 2 minus attempt 1, early/late ratio, dB (should be 0)")
    print(f"  {'cell':<44s}" + "".join(f"{n:>13s}" for n in names))
    for key, row in sorted(table.items()):
        print(f"  {key:<44s}" + "".join(f"{row[n]:+13.2f}" for n in names))
    for name in names:
        values = [abs(row[name]) for row in table.values()]
        print(f"  worst |repeat error| {name:<12s} {max(values):.2f} dB   "
              f"(median {np.median(values):.2f} dB, n={len(values)})")
