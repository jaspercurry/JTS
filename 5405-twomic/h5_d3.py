#!/usr/bin/env python3
"""Add D3's four corner-sweep documents to the index, proved on the MAIN mic.

Same cross-multiplied test as h5_index: D3's side reference is corrupt, its
main takes are not, and the test never touches the side mic.
"""
import json
from itertools import permutations

import numpy as np

import g5lib as g
from h5_index import residual_db
from rearpred import section_of

BAND = (90.0, 700.0)
INSIDE = (g.GRID >= BAND[0]) & (g.GRID < BAND[1])
ADD = {"f7edbfd8": "search/D3/doc-lp250.json", "4afdad08": "search/D3/doc-lp350.json",
       "d7c17985": "search/D3/doc-lp400.json", "613377e2": "search/D3/doc-lp500.json"}
REFS = ("27a565a9", "f299b04c")

store = g.collect()
index = json.loads((g.SP / "search/fp-index.json").read_text())
library = {**index, **ADD}
chains = {fp: g.rear_stage_response(section_of(json.loads((g.SP / p).read_text())), g.GRID)[0]
          for fp, p in library.items() if fp != g.MUTED}
unknowns = tuple(ADD)
pairs = []
for (tag, mic, pose), row in store.items():
    if tag != "d3" or mic != "main":
        continue
    for fp in unknowns:
        for ref in REFS:
            if fp in row["cands"] and ref in row["cands"]:
                pairs.append((fp, ref, row["cands"][fp][INSIDE] - row["muted"][INSIDE],
                              row["cands"][ref][INSIDE] - row["muted"][INSIDE]))
scores = {}
for order in permutations(unknowns):
    mapping = dict(zip(unknowns, order))
    scores[order] = float(np.mean([residual_db(a * chains[ref][INSIDE],
                                               b * chains[mapping[fp]][INSIDE])
                                   for fp, ref, a, b in pairs]))
ranked = sorted(scores.items(), key=lambda kv: kv[1])
best, value = ranked[0]
print(f"  D3 main mic, n={len(pairs)} pairs, {len(scores)} assignments")
print("  best " + " ".join(ADD[f].split('-')[-1][:-5] for f in best)
      + f"   {value:+.1f} dB, next {ranked[1][1] - value:+.1f} dB"
      + ("   == claimed" if best == unknowns else "   <-- DIFFERS FROM THE LOG"))
if best == unknowns:
    index.update(ADD)
    (g.SP / "search/fp-index.json").write_text(json.dumps(dict(sorted(index.items())), indent=1) + "\n")
    print(f"  index extended; it now holds {len(index)} documents")
