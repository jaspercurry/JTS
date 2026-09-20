#!/usr/bin/env python3
"""Prove which document each NEW fingerprint is, from the measurement itself.

No candidate artifact survives in the rounds on disk, so the name in the log is
the only link between a fingerprint and a file -- and a transposed pair would
poison every number downstream. The physics settles it without knowing R:
``X_i - X_0 = R c_i``, so for any two candidates of one pose

    (X_i - X_0) c_j  ==  (X_j - X_0) c_i

CROSS-MULTIPLIED, not divided: the ratio form has the reference's own nulls in
the denominator and reads as noise there. Every unknown fingerprint of a round
is tested against every ASSIGNMENT of that round's claimed documents (all
permutations), and the claimed one must win by a clear margin.
"""
from __future__ import annotations

import json
from itertools import permutations

import numpy as np

import g5lib as g
from rearpred import section_of

BAND = (90.0, 420.0)
MARGIN_DB = 1.0
#: Rounds in dependency order -- each round's references were established by
#: the rounds above it; g1m's ident-C and T1 by six earlier rounds.
CHAIN = (("g1m", ("2fc52a19", "4b0374d1"), ("711b458f", "ed2e86a5")),
         ("g2m", ("2fc52a19", "4b0374d1"), ("711b458f", "ed2e86a5")),
         ("h1", ("711b458f",), ("616bbfd9", "88961484", "a454643e")),
         ("h3", ("616bbfd9",), ("654e057b", "178e3a15", "db96b13f")),
         ("h4", ("711b458f", "2fc52a19", "5e9afae3"), ("654e057b",)))
NEW = {"711b458f": "cands5/gated-S1.json", "ed2e86a5": "cands5/gated-S2.json",
       "616bbfd9": "search/H1/doc-S1-lp400.json", "88961484": "search/H1/doc-S1-lp350.json",
       "a454643e": "search/H1/doc-S1-w3.json", "654e057b": "search/H3/doc-d-012.json",
       "178e3a15": "search/H3/doc-d-025.json", "db96b13f": "search/H3/doc-d-025-w45.json"}
INSIDE = (g.GRID >= BAND[0]) & (g.GRID < BAND[1])


def residual_db(want, have) -> float:
    """Relative residual after the one complex trim the shared R could be."""
    trim = np.vdot(have, want) / max(float(np.vdot(have, have).real), 1e-30)
    return 10.0 * np.log10(max(float(np.sum(np.abs(want - trim * have) ** 2)), 1e-30)
                           / max(float(np.sum(np.abs(want) ** 2)), 1e-30))


def main() -> int:
    store = g.collect()
    index = json.loads((g.SP / "search/fp-index.json").read_text())
    library = {**index, **NEW}
    chains = {fp: g.rear_stage_response(
        section_of(json.loads((g.SP / path).read_text())), g.GRID)[0]
        for fp, path in library.items() if fp != g.MUTED}

    print("  round  assignment                                    residual   vs claimed")
    verdicts = {}
    for tag, references, unknowns in CHAIN:
        pairs = []
        for (row_tag, mic, pose), row in store.items():
            if row_tag != tag or mic != "side" or abs(g.angle_of(pose)) != 20.0:
                continue
            for fp in unknowns:
                for ref in references:
                    if fp in row["cands"] and ref in row["cands"]:
                        pairs.append((fp, ref, row["cands"][fp][INSIDE] - row["muted"][INSIDE],
                                      row["cands"][ref][INSIDE] - row["muted"][INSIDE]))
        if not pairs:
            print(f"  {tag:5s}  -- no usable pose")
            continue
        claimed = tuple(unknowns)
        scores = {}
        for order in permutations(unknowns):
            mapping = dict(zip(unknowns, order))
            cells = [residual_db(a * chains[ref][INSIDE],
                                 b * chains[NEW[mapping[fp]] if False else mapping[fp]][INSIDE])
                     for fp, ref, a, b in pairs]
            scores[order] = float(np.mean(cells))
        ranked = sorted(scores.items(), key=lambda kv: kv[1])
        best, value = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else value + 99.0
        ok = best == claimed and (second - value) > MARGIN_DB
        for fp in unknowns:
            verdicts[fp] = verdicts.get(fp, True) and ok
        print(f"  {tag:5s}  " + " ".join(g.label(f) for f in best)
              + f"{'':<{max(0, 40 - len(' '.join(g.label(f) for f in best)))}s}"
              + f"{value:+8.1f} dB   next {second - value:+.1f} dB"
              + ("" if ok else "   <-- REFUSED"))

    print("\n  best match over the WHOLE library (information, not the test)")
    for tag, references, unknowns in CHAIN:
        for fp in unknowns:
            cells = {}
            for (row_tag, mic, pose), row in store.items():
                if row_tag != tag or mic != "side" or abs(g.angle_of(pose)) != 20.0:
                    continue
                for ref in references:
                    if fp not in row["cands"] or ref not in row["cands"]:
                        continue
                    a = row["cands"][fp][INSIDE] - row["muted"][INSIDE]
                    b = row["cands"][ref][INSIDE] - row["muted"][INSIDE]
                    for name, chain in chains.items():
                        cells.setdefault(name, []).append(
                            residual_db(a * chains[ref][INSIDE], b * chain[INSIDE]))
            ranked = sorted((float(np.mean(v)), k) for k, v in cells.items())
            print(f"  {tag:5s}  {fp} -> {library[ranked[0][1]]:<28s}{ranked[0][0]:+7.1f} dB"
                  f"   runner-up {library[ranked[1][1]].split('/')[-1]:<20s}"
                  f"{ranked[1][0] - ranked[0][0]:+.1f} dB")

    if not all(verdicts.get(fp) for fp in NEW):
        print("\n  index NOT extended: at least one fingerprint did not identify cleanly")
        return 1
    index.update(NEW)
    (g.SP / "search/fp-index.json").write_text(
        json.dumps(dict(sorted(index.items())), indent=1) + "\n")
    print(f"\n  every new fingerprint identified its own document; index holds {len(index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
