#!/usr/bin/env python3
"""Is the aligner's trim branch LEAKAGE, or the product's own headroom cut?

If it were the cancellation branch still playing at 1-4 kHz, restricting the
trim fit to 4-10 kHz would remove it. It does not. The other candidate is
`rear_branch_sum_headroom_db`: the product attenuates the whole stage so the
branch sum cannot clip, which is a BROADBAND level cut on both woofers -- and
a trim is then exactly the right correction for it.
"""
import json

import numpy as np

import g5lib as g
import identlib as il
from ident import groups
from rearpred import section_of

from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.rear_calibration import read_rear_calibration

index = json.loads((g.SP / "search/fp-index.json").read_text())
held = g.cache()
GRID, rows = g.GRID, {}
for tag in g.ALL_TAGS:
    for (mic, pose), byc in groups(held[tag]).items():
        if mic != "main" or g.MUTED not in byc:
            continue
        muted = byc[g.MUTED]["transfer"]
        for fp in byc:
            if fp == g.MUTED or fp not in index:
                continue
            fit = il.align(byc[fp]["transfer"], muted)
            if fit["residual_db"] <= il.ALIGN_RESIDUAL_MAX_DB:
                rows.setdefault(fp, []).append(fit["trim_db"])

print(f"  {'tune':<10s}{'headroom charge':>17s}{'aligner trim':>14s}{'difference':>12s}  n")
pairs = []
for fp, trims in sorted(rows.items(), key=lambda kv: -np.mean(kv[1])):
    section = read_rear_calibration(
        section_of(json.loads((g.SP / index[fp]).read_text())), sample_rate=48000)
    charge = rear_branch_sum_headroom_db(section)
    trim = float(np.mean(trims))
    pairs.append((charge, trim))
    if len(pairs) <= 14 or abs(trim) > 0.3:
        print(f"  {g.label(fp):<10s}{charge:+17.2f}{trim:+14.2f}{trim - charge:+12.2f}"
              f"  {len(trims)}")
c = np.asarray([p[0] for p in pairs]); t = np.asarray([p[1] for p in pairs])
print(f"\n  n={c.size}  correlation {np.corrcoef(c, t)[0, 1]:+.3f}  "
      f"slope {np.polyfit(c, t, 1)[0]:+.3f}  mean |trim - charge| {np.abs(t - c).mean():.2f} dB")
