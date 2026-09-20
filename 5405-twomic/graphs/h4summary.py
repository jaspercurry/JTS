#!/usr/bin/env python3
"""The headline numbers, and whether they survive a 3 ms error in the marker."""
from __future__ import annotations
import json
from pathlib import Path

SP = Path(__file__).resolve().parent.parent
held = json.loads((SP / "graphs" / "h4-early-late.json").read_text())
bands = ["80-160 Hz", "160-315 Hz", "315-630 Hz", "100-350 Hz"]
tunes = ["N1 (start of day)", "S1", "ident-C (current best)", "d-0.12"]

print("=== early/late ratio CHANGE against cardioid OFF, dB")
for mic, where in (("main", "FRONT (mean of 3 poses)"), ("side", "BEHIND (mean of 160/200 deg)")):
    print(f"\n  {where}")
    print(f"    {'tune':<24s}" + "".join(f"{b:>13s}" for b in bands))
    for name in tunes:
        row = held["early_late_ratio"][mic][name]
        print(f"    {name:<24s}" + "".join(f"{row[b]['change_vs_off_db']:+13.2f}" for b in bands))
    print(f"    {'(pose spread, OFF)':<24s}"
          + "".join(f"{held['early_late_ratio'][mic]['rear muted (cardioid OFF)'][b]['pose_spread_db']:13.2f}"
                   for b in bands))

print("\n=== does a 3 ms marker error move it? change vs OFF at each shift, dB")
for mic in ("main", "side"):
    print(f"\n  {mic}")
    for name in tunes:
        line = f"    {name:<24s}"
        for band in ("100-350 Hz", "160-315 Hz"):
            values = [held["checks"]["marker_sensitivity_db"][mic][s][name][band]
                      for s in ("-3 ms", "+0 ms", "+3 ms")]
            line += f"  {band}: " + "/".join(f"{v:+.2f}" for v in values)
        print(line)

print("\n=== FRONT third-octave change vs OFF, dB (early / late)")
centres = held["front_level_change"][tunes[0]]["third_octave_hz"]
print(f"    {'tune':<24s}" + "".join(f"{c:>14d}" for c in centres))
for name in tunes:
    row = held["front_level_change"][name]
    print(f"    {name:<24s}"
          + "".join(f"{e:+6.1f}/{l:+6.1f}" for e, l in zip(row["early"], row["late"])))

print("\n=== noise: late window over the decayed 260-340 ms tail, dB (worst over poses)")
for mic in ("main", "side"):
    for band in bands:
        values = [v[band]["late_over_decayed_tail_db"]
                  for k, v in held["checks"]["noise"].items() if k.startswith(mic)]
        pre = [v[band]["late_over_pre_arrival_db"]
               for k, v in held["checks"]["noise"].items() if k.startswith(mic)]
        print(f"    {mic:5s} {band:<12s} over decayed tail >= {min(values):5.1f} dB, "
              f"over the pre-arrival region (distortion included) >= {min(pre):5.1f} dB")
print(f"\n  worst back-to-back repeat error: {held['repeat_error_db']['worst_abs_db']:.2f} dB")
