#!/usr/bin/env python3
"""F1/F2 landed: did fb-1/fb-2/fb-3 measure the way the model said?

Same cross-multiplied fingerprint proof as before, then measured vs predicted
F/B gain and front change per third octave. This is a clean out-of-sample test
of the very model round 3's solutions were built on, so it decides how much
anyone should trust them.
"""
import json
from itertools import permutations

import numpy as np

import g5lib as g
import h5lib as h
from h5_index import residual_db
from rearpred import section_of

BAND = (90.0, 700.0)
INSIDE = (g.GRID >= BAND[0]) & (g.GRID < BAND[1])
ADD = {"5bdbee3a": "cands6/fb-1.json", "76af80b5": "cands6/fb-2.json",
       "60f62a46": "cands6/fb-3.json"}
REFS = ("2fc52a19",)
NEW = ("f1", "f2")

h.widen()
store = g.collect()
index = json.loads((g.SP / "search/fp-index.json").read_text())
library = {**index, **ADD}
chains = {fp: g.rear_stage_response(section_of(json.loads((g.SP / p).read_text())), g.GRID)[0]
          for fp, p in library.items() if fp != g.MUTED}

print("=== fingerprint -> document, proved from the measurement")
ok = True
for tag in NEW:
    pairs = [(fp, ref, row["cands"][fp][INSIDE] - row["muted"][INSIDE],
              row["cands"][ref][INSIDE] - row["muted"][INSIDE])
             for (t, mic, pose), row in store.items() if t == tag
             for fp in ADD for ref in REFS
             if (mic == "main" or abs(g.angle_of(pose)) == 20.0)
             and fp in row["cands"] and ref in row["cands"]]
    if not pairs:
        print(f"  {tag}: no usable takes"); ok = False; continue
    scores = {o: float(np.mean([residual_db(a * chains[r][INSIDE],
                                            b * chains[dict(zip(ADD, o))[fp]][INSIDE])
                                for fp, r, a, b in pairs]))
              for o in permutations(ADD)}
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    good = ranked[0][0] == tuple(ADD)
    ok = ok and good
    print(f"  {tag} n={len(pairs):3d}  best " + " ".join(g.label(f) for f in ranked[0][0])
          + f"  {ranked[0][1]:+.1f} dB, next {ranked[1][1] - ranked[0][1]:+.1f} dB"
          + ("   == claimed" if good else "   <-- DIFFERS FROM THE LOG"))

print("\n=== measured vs predicted, F/B gain and front change (fit WITHOUT f1/f2)")
print("          " + "".join(f"{c:>7d}" for c in h.CENTRES))
errors = {"F/B": [], "front": []}
for tag in NEW:
    held = h.FB(store, chains, target=tag, fit_tags=set(g.ALL_TAGS) - set(NEW))
    for fp in ADD:
        if not any(fp in r["cands"] for r in held.front_rows.values()):
            continue
        chain = chains[fp]
        got = {"front": held.measured_front(fp), "behind": held.measured_behind(fp)}
        got["F/B"] = h.gain(got["front"], got["behind"])
        pred = {"front": held.front(chain), "behind": held.behind(chain)}
        pred["F/B"] = h.gain(pred["front"], pred["behind"])
        for what in ("F/B", "front"):
            errors[what].append([pred[what][c] - got[what][c] for c in h.CENTRES])
        print(f"  {tag} {g.label(fp):<5s} F/B meas" + h.line(got["F/B"])
              + f"   score {np.mean([min(got['F/B'][c], 12.0) for c in h.SCORE]):+.2f}")
        print(f"  {'':<10s} F/B pred" + h.line(pred["F/B"])
              + f"   score {np.mean([min(pred['F/B'][c], 12.0) for c in h.SCORE]):+.2f}")
        print(f"  {'':<10s} front m " + h.line(got["front"]) + "  p" + h.line(pred["front"]))
for what in ("F/B", "front"):
    s = np.abs(np.asarray(errors[what]))
    print(f"  {what:<5s} mean |err|" + "".join(f"{v:7.1f}" for v in s.mean(axis=0)))
    print(f"  {'':<5s} worst     " + "".join(f"{v:7.1f}" for v in s.max(axis=0)))
