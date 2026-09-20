#!/usr/bin/env python3
"""The same cross-multiplied identification, on the MAIN mic and wider.

H1's three tunes differ only by a low-pass corner (472/400/350 Hz) and H3's by
a delay (-0.12/-0.25 ms). Before asking a measurement to tell them apart, ask
how far apart they ARE: the separation between two documents is the ceiling on
any test's margin. Printed first, so a thin margin can be read as "these are
nearly the same filter", not as "the test failed".
"""
from __future__ import annotations

import json
from itertools import permutations

import numpy as np

import g5lib as g
from h5_index import CHAIN, NEW, residual_db
from rearpred import section_of

BAND = (90.0, 700.0)
INSIDE = (g.GRID >= BAND[0]) & (g.GRID < BAND[1])


def main() -> int:
    store = g.collect()
    index = json.loads((g.SP / "search/fp-index.json").read_text())
    library = {**index, **NEW}
    chains = {fp: g.rear_stage_response(
        section_of(json.loads((g.SP / path).read_text())), g.GRID)[0]
        for fp, path in library.items() if fp != g.MUTED}

    print("  how separable are the rivals at all? (relative residual of the best fit")
    print("   of one document onto another over 90-700 Hz -- the margin ceiling)")
    for _tag, _refs, unknowns in CHAIN:
        if len(unknowns) < 2:
            continue
        for i, a in enumerate(unknowns):
            for b in unknowns[i + 1:]:
                sep = residual_db(chains[a][INSIDE], chains[b][INSIDE])
                print(f"   {g.label(a):<10s} vs {g.label(b):<10s} {sep:+7.1f} dB")

    print("\n  assignment test, both mics, 90-700 Hz")
    verdicts = {}
    for tag, references, unknowns in CHAIN:
        pairs = []
        for (row_tag, mic, pose), row in store.items():
            if row_tag != tag:
                continue
            if mic == "side" and abs(g.angle_of(pose)) != 20.0:
                continue
            for fp in unknowns:
                for ref in references:
                    if fp in row["cands"] and ref in row["cands"]:
                        pairs.append((fp, ref, mic,
                                      row["cands"][fp][INSIDE] - row["muted"][INSIDE],
                                      row["cands"][ref][INSIDE] - row["muted"][INSIDE]))
        scores = {}
        for order in permutations(unknowns):
            mapping = dict(zip(unknowns, order))
            scores[order] = float(np.mean(
                [residual_db(a * chains[ref][INSIDE], b * chains[mapping[fp]][INSIDE])
                 for fp, ref, _mic, a, b in pairs]))
        ranked = sorted(scores.items(), key=lambda kv: kv[1])
        best, value = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else value + 99.0
        ok = best == tuple(unknowns)
        for fp in unknowns:
            verdicts[fp] = verdicts.get(fp, True) and ok
        print(f"  {tag:5s} n={len(pairs):3d}  best " + " ".join(g.label(f) for f in best)
              + f"   {value:+.1f} dB, next {second - value:+.1f} dB"
              + ("   == claimed" if ok else "   <-- DIFFERS FROM THE LOG"))
    print("\n  " + ("every round's best assignment is the one the log claims"
                    if all(verdicts.values()) else "a round disagrees with the log"))
    if all(verdicts.values()):
        index.update(NEW)
        (g.SP / "search/fp-index.json").write_text(
            json.dumps(dict(sorted(index.items())), indent=1) + "\n")
        print(f"  index extended; it now holds {len(index)} documents")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
