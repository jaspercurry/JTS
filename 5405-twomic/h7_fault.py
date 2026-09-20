#!/usr/bin/env python3
"""Confirm the aligner fault before fixing it.

`identlib.align` fits ONE complex trim over 1-4 kHz onto the rear-muted take.
That is only valid if the cancellation branch is silent there. Per document:
the branch's own electrical level at 1-4 kHz (relative to its 80-400 Hz peak)
against the trim the aligner actually charged that take.
"""
import numpy as np
import g5lib as g
import identlib as il
from ident import groups

GRID = g.GRID
HF = (GRID >= 1000.0) & (GRID <= 4000.0)
LF = (GRID >= 80.0) & (GRID <= 400.0)
chains = g.chains()
held = g.cache()
rows = {}
for tag in g.ALL_TAGS:
    for (mic, pose), byc in groups(held[tag]).items():
        if mic != "main" or g.MUTED not in byc:
            continue
        muted = byc[g.MUTED]["transfer"]
        for fp in byc:
            if fp == g.MUTED or fp not in chains:
                continue
            fit = il.align(byc[fp]["transfer"], muted)
            if fit["residual_db"] > il.ALIGN_RESIDUAL_MAX_DB:
                continue
            rows.setdefault(fp, []).append(fit["trim_db"])
print(f"  {'tune':<10s}{'corner':>8s}{'branch 1-4k':>13s}{'aligner trim':>14s}  n")
import json
index = json.loads((g.SP / "search/fp-index.json").read_text())
from rearpred import section_of
for fp, trims in sorted(rows.items(), key=lambda kv: -np.mean(kv[1])):
    chain = chains[fp]
    level = 20 * np.log10(np.mean(np.abs(chain[HF])) / np.max(np.abs(chain[LF])))
    section = section_of(json.loads((g.SP / index[fp]).read_text()))
    corner = next((o["parameters"]["freq"] for o in section["rear"]["cancellation"]["filters"]
                   if o["parameters"].get("type") == "ButterworthLowpass"), float("nan"))
    print(f"  {g.label(fp):<10s}{corner:8.0f}{level:+13.1f}{np.mean(trims):+14.2f}  {len(trims)}")
