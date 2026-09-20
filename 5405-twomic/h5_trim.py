#!/usr/bin/env python3
"""Does the per-mic aligner trim belong in a FRONT-MINUS-BEHIND figure?

One take is ONE playback heard by two mics, so a playback-level drift hits
both equally and cancels in front-minus-behind by itself. The aligner
estimates a trim per mic independently, so the part that does NOT agree
between the mics is injected straight into F/B gain. Measure that part.
"""
import numpy as np
import g5lib as g
import identlib as il
from ident import groups

held = g.cache()
rows = []
for tag in g.ALL_TAGS:
    if tag in g.SIDE_EXCLUDE:
        continue
    byk = groups(held[tag])
    for (mic, pose), byc in byk.items():
        if mic != "main" or g.MUTED not in byc:
            continue
        side = byk.get(("side", pose))
        if not side or g.MUTED not in side:
            continue
        for fp in byc:
            if fp == g.MUTED or fp not in side:
                continue
            a = il.align(byc[fp]["transfer"], byc[g.MUTED]["transfer"])
            b = il.align(side[fp]["transfer"], side[g.MUTED]["transfer"])
            if max(a["residual_db"], b["residual_db"]) > il.ALIGN_RESIDUAL_MAX_DB:
                continue
            rows.append((tag, g.label(fp), g.angle_of(pose), a["trim_db"], b["trim_db"]))
d = np.asarray([r[3] - r[4] for r in rows])
print(f"  n={d.size} takes with both mics accepted")
print(f"  main trim: mean {np.mean([r[3] for r in rows]):+.2f} dB, "
      f"sd {np.std([r[3] for r in rows]):.2f}")
print(f"  side trim: mean {np.mean([r[4] for r in rows]):+.2f} dB, "
      f"sd {np.std([r[4] for r in rows]):.2f}")
print(f"  main-minus-side trim (this lands directly in F/B gain): "
      f"mean {d.mean():+.2f} dB, sd {d.std():.2f}, worst {d[np.argmax(np.abs(d))]:+.2f}")
worst = sorted(rows, key=lambda r: -abs(r[3] - r[4]))[:6]
for r in worst:
    print(f"    {r[0]:5s} {r[1]:<9s} {r[2]:+5.0f}  main {r[3]:+.2f}  side {r[4]:+.2f}  "
          f"difference {r[3] - r[4]:+.2f}")
