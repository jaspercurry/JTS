#!/usr/bin/env python3
"""Checks that have to pass before the wall early/late figures mean anything."""
from __future__ import annotations
import numpy as np
import w2core as wc
from w2load import rows

held = rows()
nodes = wc.cells(held)

print("=== 1. repeat slots kept, and what was dropped")
for (tag, mic, tune), takes in sorted(wc.slots(held).items()):
    print(f"  {tag:7s} {mic:5s} {wc.label(tune):<38s} {len(takes)} slot(s): "
          + " ".join(t["take_id"][-8:] for t in takes))

print("\n=== 2. alignment onto B0 over 1-4 kHz, gate <= "
      f"{wc.il.ALIGN_RESIDUAL_MAX_DB:g} dB")
print(f"  {'round':7s} {'mic':5s} {'tune':<38s} {'delay ms':>9s} {'trim dB':>8s} "
      f"{'residual dB':>12s} {'own 1-4k marker ms':>19s}")
worst = 0.0
for (tag, mic), node in sorted(nodes.items()):
    for tune in sorted(node["fits"], key=wc.label):
        for fit in node["fits"][tune]:
            worst = max(worst, fit["residual_db"])
            flag = "" if fit["accepted"] else "   REFUSED"
            print(f"  {tag:7s} {mic:5s} {wc.label(tune):<38s} {fit['delay_ms']:+9.3f} "
                  f"{fit['trim_db']:+8.2f} {fit['residual_db']:+12.1f} "
                  f"{fit['own_marker_ms']:19.2f}{flag}")
print(f"  worst residual over every take: {worst:+.1f} dB")

print("\n=== 3. shared direct-arrival marker (B0's 1-4 kHz envelope peak) and the")
print("        spread of each take's OWN marker inside a cell")
for (tag, mic), node in sorted(nodes.items()):
    own = [f["own_marker_ms"] for fits in node["fits"].values() for f in fits]
    print(f"  {tag:7s} {mic:5s} shared {node['marker'] / 48.0:6.2f} ms; own markers "
          f"{min(own):.2f}..{max(own):.2f} ms (spread {max(own) - min(own):.2f} ms, n={len(own)})")

print("\n=== 4. noise floor: is the +20..+250 ms late window real sound?")
print(f"  floor = the decayed tail {wc.TAIL_MS[0]:.0f}..{wc.TAIL_MS[1]:.0f} ms; "
      "levels are power per ms, dB")
print(f"  {'round':7s} {'mic':5s} {'band':<12s} {'early':>8s} {'late':>8s} {'tail':>8s} "
      f"{'late-tail':>10s} {'env >=tail+10 to':>17s}")
for (tag, mic), node in sorted(nodes.items()):
    for name, band in wc.OCTAVES:
        ir = wc.rolled_impulse(node["tunes"][wc.REFERENCE][0], band, node["marker"])
        per = lambda lo, hi: 10 * np.log10(  # noqa: E731
            max(wc.window_energy(ir, lo, hi), 1e-30) / (hi - lo))
        env = wc.envelope_db(ir)
        floor = float(np.mean(env[wc.slice_ms(*wc.TAIL_MS)]))
        look = 10 * np.log10(np.maximum(env[:int(0.400 * wc.FS)], 1e-30) / max(floor, 1e-30))
        up = np.flatnonzero(look >= 10.0)
        print(f"  {tag:7s} {mic:5s} {name:<12s} {per(0, 20):8.1f} "
              f"{per(20, wc.LATE_END_MS):8.1f} {per(*wc.TAIL_MS):8.1f} "
              f"{per(20, wc.LATE_END_MS) - per(*wc.TAIL_MS):10.1f} "
              f"{(up[-1] / 48.0 if up.size else 0.0):15.0f} ms")

print("\n=== 5. within-round repeat: take 2 minus take 1, early/late ratio, dB")
print(f"  {'round':7s} {'mic':5s} {'tune':<38s}" + "".join(f"{n:>13s}" for n, _ in wc.OCTAVES))
spread = []
for (tag, mic), node in sorted(nodes.items()):
    for tune in sorted(node["tunes"], key=wc.label):
        if len(node["tunes"][tune]) < 2:
            continue
        row = []
        for _name, band in wc.OCTAVES:
            pairs = wc.band_energies(node, tune, band, 20.0)
            row.append(10 * np.log10(pairs[1][0] / pairs[1][1])
                       - 10 * np.log10(pairs[0][0] / pairs[0][1]))
        spread.extend(abs(v) for v in row)
        print(f"  {tag:7s} {mic:5s} {wc.label(tune):<38s}" + "".join(f"{v:+13.2f}" for v in row))
print(f"  worst |take-to-take| {max(spread):.2f} dB, median {np.median(spread):.2f} dB")
