#!/usr/bin/env python3
"""Every tune measured tonight on one F/B scale.

Each tune is read in a round that ALSO contains ident-C, so the round's own
level and room state cancel: the anchored score is
``score(tune) - score(ident-C, same round) + score(ident-C, reference)``.
Raw per-round scores are printed beside the anchored ones so the size of the
correction is visible rather than hidden.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))
sys.path.insert(0, str(SP / "search"))

from fbscore import FRONT_CENTRES, collect, table  # noqa: E402

ANCHOR = "2fc52a19"                    # ident-C, present in every round used
REFERENCE = ("a1", "a2")               # the scale everything is put on

#: tune -> (fingerprint, rounds it is read in). Every round holds ident-C.
PLACE = {
    "N1":      ("5e9afae3", ("h4",)),
    "e-0.35":  ("27a565a9", ("i1b", "i2")),
    "ident-C": ("2fc52a19", ("a1", "a2")),
    "T1":      ("4b0374d1", ("g1m", "g2m")),
    "S1":      ("711b458f", ("a1", "a2")),
    "d-0.12":  ("654e057b", ("h4",)),
    "fb-1":    ("5bdbee3a", ("f1", "f2")),
    "fb-2":    ("76af80b5", ("f1", "f2")),
    "fb-3":    ("60f62a46", ("f1", "f2")),
    "agg-1":   ("bf03e5b6", ("a1", "a2")),
    "agg-2":   ("266e58cb", ("a1", "a2")),
}
ROUND_DIR = {"i1b": "round-d929a1c333a8", "i2": "round-231b37be3851",
             "g1m": "round-d35a332210c5", "g2m": "round-2b1eb4e370e9",
             "h4": "round-8ae2ac84b867", "f1": "round-4e4bc654a331",
             "f2": "round-1bab045aa4e2", "a1": "round-e5f73ee228bc",
             "a2": "round-433113c88326"}


def mean_of(store, cuts, tags, fp):
    rows = [table(store, tag, fp) for tag in tags]
    rows = [r for r in rows if r]
    if not rows:
        return None
    cut = float(np.mean([cuts[(t, fp)] for t in tags if (t, fp) in cuts]))
    total = np.mean([r["front_wide"] for r in rows], axis=0)
    return {"mixed": float(np.mean([r["score_mixed"] for r in rows])),
            "ungated": float(np.mean([r["score_ungated"] for r in rows])),
            "front_total": total, "cut": cut, "n": len(rows)}


def main() -> int:
    import g5lib as g
    tags = sorted({t for _, rounds in PLACE.values() for t in rounds})
    for tag in tags:
        g.ROUNDS[tag] = ROUND_DIR[tag]
    store, cuts = collect(tags)

    reference = mean_of(store, cuts, REFERENCE, ANCHOR)["mixed"]
    reference_u = mean_of(store, cuts, REFERENCE, ANCHOR)["ungated"]

    print("FINAL RANKING — every tune on the ident-C-anchored F/B scale")
    print(f"  {'tune':<9s} {'rounds':<9s} {'mixed':>7s} {'ungat':>7s} "
          f"{'worst front total':>19s} {'cut':>6s} {'400-630 within 1 dB':>21s}")
    table_rows = []
    for name, (fp, rounds) in PLACE.items():
        row = mean_of(store, cuts, rounds, fp)
        if row is None:
            print(f"  {name:<9s} {'/'.join(rounds):<9s}  -- not placeable")
            continue
        anchor = mean_of(store, cuts, rounds, ANCHOR)
        shift = reference - anchor["mixed"]
        shift_u = reference_u - anchor["ungated"]
        worst = float(np.min(row["front_total"]))
        where = FRONT_CENTRES[int(np.argmin(row["front_total"]))]
        top = row["front_total"][FRONT_CENTRES.index(400):]
        flat = bool(np.max(np.abs(top)) <= 1.0)
        table_rows.append((row["mixed"] + shift, name, "/".join(rounds),
                           row["mixed"] + shift, row["ungated"] + shift_u,
                           worst, where, row["cut"], flat,
                           float(np.max(np.abs(top)))))
    for _, name, rounds, mixed, ungated, worst, where, cut, flat, top in sorted(
            table_rows, reverse=True):
        print(f"  {name:<9s} {rounds:<9s} {mixed:>+7.2f} {ungated:>+7.2f} "
              f"{worst:>+13.1f} @{where:<4d} {cut:>+6.2f} "
              f"{('yes' if flat else 'NO') + f' ({top:.1f})':>21s}")
    print(f"\n  anchored on ident-C = {reference:+.2f} mixed / {reference_u:+.2f} ungated "
          f"(its {'/'.join(REFERENCE)} mean); a tune's own round's ident-C is subtracted out")
    print("  'worst front total' is the EQ-back cost: the deepest third octave 63-630 Hz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
