#!/usr/bin/env python3
"""The real floor, and how far out the late window can honestly run.

An exponential-sweep deconvolution puts the harmonic-distortion impulses at
NEGATIVE times, so the pre-arrival region is a distortion+noise bound, not the
quietest part of the record. The quietest part is the tail after the room has
decayed, and that is what a late window has to beat.
"""
from __future__ import annotations
import numpy as np
import h4core as hc
from h4load import rows

held = hc.cells(rows())
PRE = (-300.0, -100.0)
TAIL = (300.0, 450.0)


def per_ms(ir, lo, hi):
    return 10 * np.log10(max(hc.window_energy(ir, lo, hi), 1e-30) / (hi - lo))


print("=== power per ms, rear-muted take, dB (relative levels only)")
print(f"  {'mic':5s} {'arm':>4s} {'band':<12s} {'pre -300..-100':>15s} {'tail 300..450':>14s} "
      f"{'early 0..20':>12s} {'late 20..200':>13s} {'late/tail':>10s}")
for (mic, pose), byc in sorted(held.items()):
    muted = byc[hc.MUTED]["transfer"]
    marker = int(np.argmax(np.abs(hc.hilbert(
        hc.band_limited_impulse(hc.GRID, muted, hc.MARKER_BAND_HZ)))))
    for name, band in hc.OCTAVES:
        ir = hc.rolled_impulse(muted, band, marker)
        pre, tail = per_ms(ir, *PRE), per_ms(ir, *TAIL)
        early, late = per_ms(ir, 0, 20), per_ms(ir, 20, 200)
        print(f"  {mic:5s} {hc.pose_angle(pose):+4.0f} {name:<12s} {pre:15.1f} {tail:14.1f} "
              f"{early:12.1f} {late:13.1f} {late - tail:10.1f}")

print("\n=== where the 3 ms-smoothed envelope drops to 10 dB above the 300..450 ms tail")
print(f"  {'mic':5s} {'arm':>4s}" + "".join(f"{n:>14s}" for n, _ in hc.OCTAVES))
for (mic, pose), byc in sorted(held.items()):
    muted = byc[hc.MUTED]["transfer"]
    marker = int(np.argmax(np.abs(hc.hilbert(
        hc.band_limited_impulse(hc.GRID, muted, hc.MARKER_BAND_HZ)))))
    row = []
    for _name, band in hc.OCTAVES:
        ir = hc.rolled_impulse(muted, band, marker)
        env = hc.envelope_db(ir)
        floor = float(np.mean(env[hc.slice_ms(*TAIL)]))
        look = env[:int(0.300 * hc.FS)]
        level = 10 * np.log10(np.maximum(look, 1e-30) / max(floor, 1e-30))
        # last time the envelope is still 10 dB up, so a single dip cannot end it
        up = np.flatnonzero(level >= 10.0)
        row.append(up[-1] / 48.0 if up.size else 0.0)
    print(f"  {mic:5s} {hc.pose_angle(pose):+4.0f}" + "".join(f"{v:13.0f}ms" for v in row))

print("\n=== fraction of the 0..200 ms energy that the 20..200 window would gain")
print("    by running to 300 ms instead (rear muted) -- how much the cut-off costs")
print(f"  {'mic':5s} {'arm':>4s}" + "".join(f"{n:>14s}" for n, _ in hc.OCTAVES))
for (mic, pose), byc in sorted(held.items()):
    muted = byc[hc.MUTED]["transfer"]
    marker = int(np.argmax(np.abs(hc.hilbert(
        hc.band_limited_impulse(hc.GRID, muted, hc.MARKER_BAND_HZ)))))
    row = []
    for _name, band in hc.OCTAVES:
        ir = hc.rolled_impulse(muted, band, marker)
        row.append(100.0 * hc.window_energy(ir, 200, 300) / max(hc.window_energy(ir, 0, 300), 1e-30))
    print(f"  {mic:5s} {hc.pose_angle(pose):+4.0f}" + "".join(f"{v:13.2f}%" for v in row))
