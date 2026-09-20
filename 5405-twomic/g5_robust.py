#!/usr/bin/env python3
"""How fragile are the three solutions?

  1  re-score them with R identified from DIFFERENT round subsets, and against
     a different round's muted reference -- the held-out-pose check cannot see
     a model that is simply wrong about the rig.
  2  perturb each solved parameter and read the cost -- a design that only
     works at exactly one delay is a fit to noise, not a null.
  3  refit S1 on +-20 only, to see whether the untrustworthy +0 pose (weight
     0.5) is steering the answer.
"""
from __future__ import annotations

import json

import numpy as np
from scipy.optimize import differential_evolution

import g5lib as g
import g5_model as m
from g5_solve import SEED, describe, table
from rearpred import section_of

SUBSETS = {"all": set(g.ALL_TAGS), "pre-USB": {"d1", "d2", "c1", "649a"},
           "post-USB": {"i1b", "i2", "l1"}, "no-l1": set(g.ALL_TAGS) - {"l1"}}
STEPS = {"lowpass Hz": (0, 40.0), "delay ms": (1, 0.10), "120 Hz dB": (2, 1.0),
         "pk freq Hz": (3, 20.0), "pk gain dB": (4, 1.0), "pk q": (5, 0.4)}


def main() -> int:
    store = g.collect()
    chain_of = g.chains()
    base = section_of(g.load_json(g.SP / "search/D2/doc-e-035.json"))
    solved = json.loads((g.SP / "cands5/summary.json").read_text())["stages"]

    print("=== 1. +-20 capped mean under R from other round subsets / other reference")
    print(f"  {'design':<9s}" + "".join(f"{k:>12s}" for k in SUBSETS)
          + f"{'i2 as ref':>12s}")
    for name in ("S1", "S2", "S3", "e-0.35", "ident-C"):
        cells = []
        for tags in SUBSETS.values():
            model = m.Model(store, chain_of, fit_tags=tags)
            chain = (chain_of[{"e-0.35": "27a565a9", "ident-C": "2fc52a19"}[name]]
                     if name in ("e-0.35", "ident-C")
                     else model.chain_of_x(solved[name]["x"], solved[name]["shape"], base))
            _t, per = model.score(chain)
            cells.append(np.mean([per[20.0], per[-20.0]]))
        model = m.Model(store, chain_of, score_round="i2")
        chain = (chain_of[{"e-0.35": "27a565a9", "ident-C": "2fc52a19"}[name]]
                 if name in ("e-0.35", "ident-C")
                 else model.chain_of_x(solved[name]["x"], solved[name]["shape"], base))
        _t, per = model.score(chain)
        cells.append(np.mean([per[20.0], per[-20.0]]))
        print(f"  {name:<9s}" + "".join(f"{v:+12.2f}" for v in cells))

    model = m.Model(store, chain_of)
    print("\n=== 2. S1 parameter sensitivity: +-20 capped mean when one knob moves")
    x = list(solved["S1"]["x"])
    print(f"  solved: {describe(x, (1, 0))}")
    print(f"  {'knob':<12s} {'step':>7s}   {'minus':>8s} {'solved':>8s} {'plus':>8s}")
    base_score = np.mean([v for k, v in model.score(
        model.chain_of_x(x, (1, 0), base))[1].items() if k != 0.0])
    for knob, (index, step) in STEPS.items():
        row = []
        for sign in (-1, +1):
            moved = list(x)
            moved[index] += sign * step
            _t, per = model.score(model.chain_of_x(moved, (1, 0), base))
            row.append(np.mean([per[20.0], per[-20.0]]))
        print(f"  {knob:<12s} {step:>7.2f}   {row[0]:+8.2f} {base_score:+8.2f} {row[1]:+8.2f}")

    print("\n=== 3. S1 refitted on +-20 only (the +0 pose left out entirely)")
    found = differential_evolution(
        lambda v: model.objective(v, (1, 0), base, [20.0, -20.0]), m.bounds_for((1, 0)),
        seed=SEED, maxiter=60, popsize=15, tol=0.01, polish=True, init="latinhypercube")
    rows = table(model, model.chain_of_x(found.x, (1, 0), base))
    print(f"  {describe(found.x, (1, 0))}")
    print(f"  +-20 mean {rows['pm20_mean']:+.2f} (S1 with +0 at weight 0.5: "
          f"{solved['S1']['table']['pm20_mean']:+.2f})")
    print("  pose   " + "".join(f"{c:>8d}" for c in g.CENTRES))
    for angle in (20.0, -20.0, 0.0):
        print(f"  {angle:+5.0f}  "
              + "".join(f"{v:+8.1f}" for v in rows["bands"][f"{angle:+.0f}"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
