#!/usr/bin/env python3
"""Checks that have to pass before the H4 early/late figures mean anything."""
from __future__ import annotations
import numpy as np
import h4core as hc
from h4load import rows

held = rows()
nodes = hc.aligned_transfers(held)

print("=== 1. alignment onto the rear-muted take (1-4 kHz only), gate <= "
      f"{hc.il.ALIGN_RESIDUAL_MAX_DB:g} dB")
print(f"  {'mic':5s} {'arm':>5s} {'tune':<24s} {'delay ms':>9s} {'trim dB':>8s} {'residual dB':>12s}")
for (mic, pose), node in sorted(nodes.items()):
    for fp, fit in sorted(node["fits"].items(), key=lambda kv: hc.TUNES[kv[0]][0]):
        flag = "" if fit["residual_db"] <= hc.il.ALIGN_RESIDUAL_MAX_DB else "   REFUSED"
        print(f"  {mic:5s} {hc.pose_angle(pose):+5.0f} {hc.TUNES[fp][0]:<24s} "
              f"{fit['delay_ms']:+9.3f} {fit['trim_db']:+8.2f} {fit['residual_db']:+12.1f}{flag}")

print("\n=== 2. direct-arrival marker per cell, ms into the deconvolved impulse")
print("  USED, both mics: peak of the 1-4 kHz Hilbert envelope of the rear-muted take")
print("  cross-checks: the 100-350 Hz envelope peak, and the folder's behind-the-box rule")
print("  (first 100-350 Hz peak within 6 dB of the maximum)")
print(f"  {'mic':5s} {'arm':>5s} {'USED ms':>8s} {'100-350 peak ms':>16s} {'6 dB rule ms':>13s}")
for (mic, pose), node in sorted(nodes.items()):
    muted = node["tunes"][hc.MUTED]
    woofer = np.abs(hc.hilbert(hc.band_limited_impulse(hc.GRID, muted, hc.WOOFER_BAND_HZ)))
    print(f"  {mic:5s} {hc.pose_angle(pose):+5.0f} {node['marker'] / 48.0:8.2f} "
          f"{float(np.argmax(woofer)) / 48.0:16.2f} "
          f"{hc.marker_of(muted, 'woofer_first') / 48.0:13.2f}")
print("  the 6 dB rule returns 0.00 ms behind the box: the woofer band there is")
print("  room-dominated and never falls 6 dB below its own maximum in 683 ms")

print("\n  per-tune marker spread inside a cell (each tune's OWN marker, ms) --")
print("  they must agree, or the shared marker is hiding a real time shift")
for (mic, pose), node in sorted(nodes.items()):
    own = [hc.marker_of(t) / 48.0 for t in node["tunes"].values()]
    print(f"  {mic:5s} {hc.pose_angle(pose):+5.0f} n={len(own)} "
          f"min {min(own):.2f}  max {max(own):.2f}  spread {max(own) - min(own):.2f} ms")

print("\n=== 3. noise floor against the late window, per mic and band")
print("  floor = mean power/sample in the pre-arrival region "
      f"{hc.NOISE_MS[0]:.0f}..{hc.NOISE_MS[1]:.0f} ms (circular)")
print("  'late over noise' = late-window ENERGY minus the noise energy the same")
print("  number of samples would hold; 'envelope margin' = smoothed envelope at")
print("  +200 ms over the pre-arrival envelope, dB")
print(f"  {'mic':5s} {'arm':>5s} {'band':<12s} {'early dB':>9s} {'late dB':>8s} "
      f"{'noise-in-late dB':>17s} {'margin@200ms dB':>16s}")
late_n = int(np.sum(hc.slice_ms(*hc.LATE_MS)))
summary = {}
for (mic, pose), node in sorted(nodes.items()):
    muted = node["tunes"][hc.MUTED]
    for name, band in hc.OCTAVES:
        ir = hc.rolled_impulse(muted, band, node["marker"])
        early = hc.window_energy(ir, *hc.EARLY_MS)
        late = hc.window_energy(ir, *hc.LATE_MS)
        noise = hc.noise_power(ir) * late_n
        env = hc.envelope_db(ir)
        at200 = float(np.mean(env[hc.slice_ms(190.0, 210.0)]))
        pre = float(np.mean(env[hc.slice_ms(*hc.NOISE_MS)]))
        margin = 10.0 * np.log10(max(at200, 1e-30) / max(pre, 1e-30))
        summary.setdefault((mic, name), []).append(
            (10 * np.log10(late / max(noise, 1e-30)), margin))
        print(f"  {mic:5s} {hc.pose_angle(pose):+5.0f} {name:<12s} "
              f"{10 * np.log10(max(early, 1e-30)):9.1f} {10 * np.log10(max(late, 1e-30)):8.1f} "
              f"{10 * np.log10(max(noise, 1e-30)):17.1f} {margin:16.1f}")

print("\n  worst case over poses, per mic and band:")
for key in sorted(summary):
    values = summary[key]
    print(f"    {key[0]:5s} {key[1]:<12s} late/noise >= {min(v[0] for v in values):5.1f} dB, "
          f"envelope margin at 200 ms >= {min(v[1] for v in values):5.1f} dB")

print("\n=== 4. how much band-pass energy lands BEFORE the marker (pre-ringing)")
print("  fraction of the -20..+200 ms energy that sits in -20..0 ms, rear muted")
print(f"  {'mic':5s} {'arm':>5s}" + "".join(f"{n:>13s}" for n, _ in hc.OCTAVES))
for (mic, pose), node in sorted(nodes.items()):
    muted = node["tunes"][hc.MUTED]
    row = []
    for _name, band in hc.OCTAVES:
        ir = hc.rolled_impulse(muted, band, node["marker"])
        pre = hc.window_energy(ir, -20.0, 0.0)
        total = hc.window_energy(ir, -20.0, 200.0)
        row.append(100.0 * pre / max(total, 1e-30))
    print(f"  {mic:5s} {hc.pose_angle(pose):+5.0f}" + "".join(f"{v:12.1f}%" for v in row))
