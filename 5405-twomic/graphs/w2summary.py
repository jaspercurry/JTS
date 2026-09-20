#!/usr/bin/env python3
"""The headline tables for the wall rounds."""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent.parent
held = json.loads((SP / "graphs" / "wall2-early-late.json").read_text())
bands = ["80-160 Hz", "160-315 Hz", "315-630 Hz", "100-350 Hz"]
tunes = ["C0 rear simply off", "A0 agg-1", "seat-1 (applied)", "seat-2", "wall170-1"]

for mic, where in (("side", "AT THE SEAT"), ("main", "FRONT ARM 0.81 m")):
    print(f"\n=== {where}: early/late ratio CHANGE vs B0, dB   [20 ms split | 30 ms split]")
    print(f"  {'tune':<20s}" + "".join(f"{b:>22s}" for b in bands))
    for name in tunes:
        row = held["early_late_ratio"][mic]
        line = f"  {name:<20s}"
        for band in bands:
            a = row["20 ms split"][name][band]["change_vs_B0_db"]
            b = row["30 ms split"][name][band]["change_vs_B0_db"]
            line += f"{a:+11.2f} |{b:+9.2f}"
        print(line)
    print(f"  {'B0 own ratio':<20s}"
          + "".join(f"{held['early_late_ratio'][mic]['20 ms split']['B0 fair off (rear off, front matched)'][b]['early_late_db']:+11.2f} |"
                   f"{held['early_late_ratio'][mic]['30 ms split']['B0 fair off (rear off, front matched)'][b]['early_late_db']:+9.2f}"
                   for b in bands))

print("\n=== B0 vs C0 sanity (front EQ only; a pure per-band gain would give 0.00)")
for key, row in held["b0_vs_c0_ratio_change_db"].items():
    print(f"  {key:<22s}" + "".join(f"{row[b]:+13.2f}" for b in bands))

print("\n=== between-round repeat, wall2 minus wall1c, 20 ms split, dB")
for key, row in sorted(held["between_round_repeat_db"]["per_tune"].items()):
    print(f"  {key:<52s}" + "".join(f"{row[b]:+13.2f}" for b in bands))
print(f"  worst |repeat| {held['between_round_repeat_db']['worst_abs_db']:.2f} dB")

print("\n=== seat third-octave change vs B0, dB (early / late)")
centres = held["seat_level_change"]["A0 agg-1"]["third_octave_hz"]
print(f"  {'tune':<20s}" + "".join(f"{c:>14d}" for c in centres))
for name in ("C0 rear simply off", "A0 agg-1", "seat-1 (applied)"):
    row = held["seat_level_change"][name]
    print(f"  {name:<20s}" + "".join(f"{e:+6.1f}/{l:+6.1f}"
                                     for e, l in zip(row["early"], row["late"])))

print("\n=== noise: late window (+20..+250 ms) over the decayed 260..340 ms tail, dB")
for key, row in sorted(held["checks"]["noise"].items()):
    print(f"  {key:<16s}" + "".join(f"{row[b]:13.1f}" for b in bands))

print("\n=== cumulative share of the first 250 ms that has arrived by 20 and 40 ms, %")
for mic in ("side", "main"):
    node = held["energy_time"][mic]
    time = np.asarray(node["time_ms"])
    for name, series in node["cumulative_pct"].items():
        values = np.asarray(series)
        at20 = float(np.interp(20.0, time, values))
        at40 = float(np.interp(40.0, time, values))
        print(f"  {mic:5s} {name:<40s} {at20:6.1f} % by 20 ms, {at40:6.1f} % by 40 ms")
