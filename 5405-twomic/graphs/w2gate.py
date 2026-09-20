#!/usr/bin/env python3
"""Do the takes the 1-4 kHz gate refuses actually disagree about early/late?

The gate compares whole 1-4 kHz spectra. The figures here need two much weaker
things: that the ratio inside a take is stable, and (for level figures) that the
trim is a real level match. This prints both, for every take, gate-passing or not.
"""
from __future__ import annotations
import numpy as np
import w2core as wc
from w2load import rows

nodes = wc.cells(rows())
names = [n for n, _ in wc.OCTAVES]

print("=== take 2 minus take 1 of the SAME tune: early/late ratio (dB) and 100-350 level (dB)")
print("    'gate' is the aligner's verdict on take 2")
print(f"  {'round':7s} {'mic':5s} {'tune':<38s} {'gate':>6s}"
      + "".join(f"{n:>12s}" for n in names) + f"{'level':>9s}")
ratio_gap, level_gap = {True: [], False: []}, {True: [], False: []}
for (tag, mic), node in sorted(nodes.items()):
    for tune in sorted(node["tunes"], key=wc.label):
        takes, fits = node["tunes"][tune], node["fits"][tune]
        if len(takes) < 2:
            continue
        passed = fits[1]["gate_pass"]
        row = []
        for _name, band in wc.OCTAVES:
            pairs = wc.band_energies(node, tune, band, 20.0)
            row.append(10 * np.log10(pairs[1][0] / pairs[1][1])
                       - 10 * np.log10(pairs[0][0] / pairs[0][1]))
        pairs = wc.band_energies(node, tune, wc.BAND_100_350, 20.0)
        level = 10 * np.log10(sum(pairs[1]) / sum(pairs[0]))
        ratio_gap[passed].extend(abs(v) for v in row)
        level_gap[passed].append(abs(level))
        print(f"  {tag:7s} {mic:5s} {wc.label(tune):<38s} {'pass' if passed else 'FAIL':>6s}"
              + "".join(f"{v:+12.2f}" for v in row) + f"{level:+9.2f}")

for passed in (True, False):
    if not ratio_gap[passed]:
        continue
    print(f"\n  takes the gate {'PASSES' if passed else 'REFUSES'} "
          f"(n={len(level_gap[passed])} pairs):")
    print(f"    ratio  worst {max(ratio_gap[passed]):.2f} dB, "
          f"median {np.median(ratio_gap[passed]):.2f} dB")
    print(f"    level  worst {max(level_gap[passed]):.2f} dB, "
          f"median {np.median(level_gap[passed]):.2f} dB")
