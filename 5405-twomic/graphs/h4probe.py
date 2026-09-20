#!/usr/bin/env python3
"""Where the behind-mic arrival really is, and where the room decay reaches the floor."""
from __future__ import annotations
import numpy as np
import h4core as hc
from h4load import rows

held = hc.cells(rows())

print("=== side-mic rear-muted take, 100-350 Hz |IR| around t=0 (raw, un-rolled)")
for (mic, pose), byc in sorted(held.items()):
    muted = byc[hc.MUTED]["transfer"]
    ir = hc.band_limited_impulse(hc.GRID, muted, hc.WOOFER_BAND_HZ)
    env = np.abs(hc.hilbert(ir))
    top = int(np.argmax(env))
    order = np.argsort(env)[::-1][:6]
    print(f"  {mic:5s} {hc.pose_angle(pose):+5.0f} peak at {top / 48.0:7.2f} ms; "
          f"|IR| at 0 ms = {20 * np.log10(env[0] / env[top]):+6.1f} dB rel peak; "
          f"top-6 peaks ms " + " ".join(f"{i / 48.0:.2f}" for i in sorted(order)))
    first = np.flatnonzero(env >= env[top] * 10 ** (-6.0 / 20.0))
    print(f"        within -6 dB of peak: {first.size} samples, first at "
          f"{first[0] / 48.0:.2f} ms, last at {first[-1] / 48.0:.2f} ms")
    # how the energy is distributed in the first 20 ms vs the whole record
    for lo, hi in ((-50.0, -5.0), (-5.0, 0.0), (0.0, 20.0), (20.0, 200.0), (200.0, 400.0)):
        keep = np.roll(ir, 0)
        print(f"        {lo:+7.1f}..{hi:+7.1f} ms energy "
              f"{10 * np.log10(max(hc.window_energy(keep, lo, hi), 1e-30)):7.1f} dB")

print("\n=== where the 100-350 Hz smoothed envelope of the rear-muted take falls to")
print("    10 dB above the pre-arrival floor (measured from the marker)")
for (mic, pose), byc in sorted(held.items()):
    muted = byc[hc.MUTED]["transfer"]
    marker = hc.marker_of(muted, mic)
    for name, band in hc.OCTAVES:
        ir = hc.rolled_impulse(muted, band, marker)
        env = hc.envelope_db(ir)
        floor = float(np.mean(env[hc.slice_ms(*hc.NOISE_MS)]))
        time = np.arange(0, int(0.400 * hc.FS))
        level = 10 * np.log10(np.maximum(env[time], 1e-30) / max(floor, 1e-30))
        below = np.flatnonzero(level < 10.0)
        cross = below[0] / 48.0 if below.size else 400.0
        print(f"  {mic:5s} {hc.pose_angle(pose):+5.0f} {name:<12s} "
              f"env reaches floor+10 dB at {cross:6.1f} ms")
