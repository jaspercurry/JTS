#!/usr/bin/env python3
"""Does the headline survive a different early window?

A zero-phase band-pass puts part of the arrival BEFORE the marker, and the
owner's early window starts AT the marker, so that part is dropped. Here the
early window is widened backwards to catch it. If the sign of the change
survives, the window choice is not what produced the result.
"""
from __future__ import annotations
import numpy as np
import h4core as hc
import h4figs as hf

BANDS = [("100-350 Hz", hc.BAND_100_350), ("160-315 Hz", (160.0, 315.0))]
STARTS = (0.0, -6.0, -12.0)

print("=== early/late change vs OFF, dB, for three early-window starts")
print("    (late window is always +20..+200 ms)")
for mic, poses, where in (("main", hf.FRONT, "FRONT"), ("side", hf.BEHIND, "BEHIND")):
    print(f"\n  {where}")
    print(f"    {'tune':<24s}" + "".join(
        f"{name} start {s:+.0f} ms".rjust(24) for name, _ in BANDS for s in STARTS[:1]))
    header = f"    {'':<24s}"
    for name, _band in BANDS:
        header += f"  {name}: " + "/".join(f"{s:+.0f}ms" for s in STARTS)
    print(header)
    for fp in hf.ORDER:
        if fp == hc.MUTED or fp not in hf.present(mic, poses):
            continue
        line = f"    {hc.TUNES[fp][0]:<24s}"
        for name, band in BANDS:
            values = []
            for start in STARTS:
                per_pose = {}
                for key in (fp, hc.MUTED):
                    ratios = []
                    for pose in poses:
                        ir = hf.impulse(mic, pose, key, band)
                        early = hc.window_energy(ir, start, hc.EARLY_MS[1])
                        late = hc.window_energy(ir, *hc.LATE_MS)
                        ratios.append(10 * np.log10(max(early, 1e-30) / max(late, 1e-30)))
                    per_pose[key] = float(np.mean(ratios))
                values.append(per_pose[fp] - per_pose[hc.MUTED])
            line += f"  {name}: " + "/".join(f"{v:+.2f}" for v in values)
        print(line)
