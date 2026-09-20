#!/usr/bin/env python3
"""What the behind-mic woofer-band envelope actually looks like near the arrival."""
from __future__ import annotations
import numpy as np
import h4core as hc
from h4load import rows

held = hc.cells(rows())
for (mic, pose), byc in sorted(held.items()):
    muted = byc[hc.MUTED]["transfer"]
    ir = hc.band_limited_impulse(hc.GRID, muted, hc.WOOFER_BAND_HZ)
    env = np.abs(hc.hilbert(ir))
    top = float(np.max(env))
    rolled = np.roll(env, 0)
    print(f"\n{mic} {hc.pose_angle(pose):+.0f}  (100-350 Hz Hilbert envelope, dB rel its own max)")
    marks = list(range(-30, 31, 2))
    line = "   t ms " + "".join(f"{m:>6d}" for m in marks)
    vals = "   dB   " + "".join(
        f"{20 * np.log10(max(rolled[int(round(m * 48)) % hc.N], 1e-30) / top):>6.1f}" for m in marks)
    print(line)
    print(vals)
    ir14 = hc.band_limited_impulse(hc.GRID, muted, hc.MARKER_BAND_HZ)
    e14 = np.abs(hc.hilbert(ir14))
    print(f"   1-4 kHz envelope peak at {np.argmax(e14) / 48.0:.2f} ms; "
          f"100-350 Hz envelope peak at {np.argmax(env) / 48.0:.2f} ms")
