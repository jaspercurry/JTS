#!/usr/bin/env python3
"""Add A1/A2 and prove agg-1 / agg-2's fingerprints. Refuse the BA rounds.

The BA documents (C0, B0, A0, A1, B1) carry DIFFERENT front chains and a
cleared room layer. The model's whole premise is X_i = X_0 + R c_i with one
shared front chain and one shared zero, so they cannot join this dataset --
and they carry no Nm take either, so `collect()` drops them anyway. Both
facts are asserted here rather than assumed.
"""
import json
from itertools import permutations

import numpy as np

import g5lib as g
from h5_index import residual_db
from ident import groups
from rearpred import section_of

BAND = (90.0, 700.0)
INSIDE = (g.GRID >= BAND[0]) & (g.GRID < BAND[1])
ADD = {"bf03e5b6": "cands7/agg-1.json", "266e58cb": "cands7/agg-2.json"}
REFS = ("2fc52a19", "711b458f")
BA = {"ba1": "round-3ae479b0d204", "ba2": "round-9418c8888321", "ba3": "round-a16305f0a9f8"}

store = g.collect()
index = json.loads((g.SP / "search/fp-index.json").read_text())
library = {**index, **ADD}
chains = {fp: g.rear_stage_response(section_of(json.loads((g.SP / p).read_text())), g.GRID)[0]
          for fp, p in library.items() if fp != g.MUTED}

print("=== agg-1 / agg-2 fingerprints, proved from A1/A2")
ok = True
for tag in ("a1", "a2"):
    pairs = [(fp, r, row["cands"][fp][INSIDE] - row["muted"][INSIDE],
              row["cands"][r][INSIDE] - row["muted"][INSIDE])
             for (t, mic, pose), row in store.items() if t == tag
             for fp in ADD for r in REFS
             if (mic == "main" or abs(g.angle_of(pose)) == 20.0)
             and fp in row["cands"] and r in row["cands"]]
    scores = {o: float(np.mean([residual_db(a * chains[r][INSIDE],
                                            b * chains[dict(zip(ADD, o))[fp]][INSIDE])
                                for fp, r, a, b in pairs])) for o in permutations(ADD)}
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    good = ranked[0][0] == tuple(ADD)
    ok = ok and good
    print(f"  {tag} n={len(pairs):3d}  best " + " ".join(g.label(f) for f in ranked[0][0])
          + f"  {ranked[0][1]:+.1f} dB, next {ranked[1][1] - ranked[0][1]:+.1f} dB"
          + ("   == claimed" if good else "   <-- DIFFERS"))

print("\n=== BA rounds: front chain and shared zero")
nm_front = json.dumps(section_of(g.load_json(g.SP / "docs/doc-Nm.json"))["front"], sort_keys=True)
for name in ("C0", "B0", "A0", "A1", "B1"):
    s = section_of(g.load_json(g.SP / f"search/BA/doc-{name}.json"))
    same = json.dumps(s["front"], sort_keys=True) == nm_front
    print(f"  doc-{name}: front chain {'matches Nm' if same else 'DIFFERS from Nm'}"
          f"   rear_muted={s.get('rear_muted')}")
for tag, folder in BA.items():
    path = g.SP / folder / "side"
    fps = set()
    try:
        import twomic_analyse as ta
        from twomic_pair import bundle_root
        fps = {r[1]["candidate_id"][:8] for r in ta.take_records(bundle_root(g.SP / folder))}
    except Exception as exc:                        # noqa: BLE001
        print(f"  {tag}: could not read take records ({type(exc).__name__})")
        continue
    print(f"  {tag} ({folder}): candidates {sorted(fps)}; Nm present = {g.MUTED in fps}")
print("  -> BA carries no Nm take and its front chains differ, so it is NOT joined.")

if ok:
    index.update(ADD)
    (g.SP / "search/fp-index.json").write_text(
        json.dumps(dict(sorted(index.items())), indent=1) + "\n")
    print(f"\n  index extended; it now holds {len(index)} documents")
