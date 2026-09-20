#!/usr/bin/env python3
"""What did "start aggressive" actually buy, and what is the real frontier?

  1  the EQ-back frontier: best F/B score against a cap on the largest boost
  2  the front floor with the soft charge switched OFF -- does the solver even
     want a deep front notch, or is the -8 dB allowance dead weight?
  3  round 2's hard front box, with round 3's WIDER families -- separates the
     gain that came from opening the front from the gain that came from
     freeing the filters
  4  rear drive against the envelope of every measured chain, per band: S2 was
     predicted -8.2 and measured -0.6 because it drove where nothing had
"""
from __future__ import annotations

import json

import numpy as np

import g5lib as g
import h5lib as h
import h6_fam as f
from h6_solve import TARGET, describe, fit, rows_for
from rearpred import section_of

BANDS = {c: (c / 2 ** (1 / 6), c * 2 ** (1 / 6)) for c in h.CENTRES}
CAPS = (5.0, 4.0, 3.0, 2.5, 2.0, 1.5)


def level(chain, centre):
    low, high = BANDS[centre]
    inside = (g.GRID >= low) & (g.GRID < high)
    return 10.0 * np.log10(np.mean(np.abs(chain[inside]) ** 2) + 1e-30)


def main() -> int:
    h.widen()
    store = g.collect()
    chain_of = g.chains()
    model = h.FB(store, chain_of, target=TARGET)
    ident_c = section_of(g.load_json(g.SP / "cands3/ident-C.json"))
    bases = {"G1": section_of(g.load_json(g.SP / "search/D2/doc-e-035.json")), "G2": ident_c}
    objective = f.Objective(model, bases, ident_c["rear"]["cancellation"])

    print("=== 1. EQ-back frontier: cap on the largest boost vs best F/B score")
    print(f"  {'cap dB':>7s}" + "".join(f"{n:>9s}" for n in f.FAMILIES) + "     best")
    frontier = {}
    held = f.GENTLE_BOOST_DB
    for cap in CAPS:
        f.GENTLE_BOOST_DB = cap
        row = {}
        for name in f.FAMILIES:
            x = fit(objective, name, gentle=True)
            rows = rows_for(objective, objective.chain(x, name))
            _hard, _soft, notes = f.guards({int(k): v for k, v in rows["front"].items()},
                                           gentle=True)
            row[name] = (rows["score"], rows["eq_back_boost"], list(map(float, x)),
                         bool(notes))
        frontier[cap] = row
        best = max((v for v in row.values() if not v[3]), default=None,
                   key=lambda v: v[0])
        print(f"  {cap:7.1f}" + "".join(f"{row[n][0]:+9.2f}" for n in f.FAMILIES)
              + (f"     {best[0]:+.2f} at {best[1]:.2f} dB" if best else "     none legal"))
    f.GENTLE_BOOST_DB = held

    print("\n=== 2. is the -8 dB front floor doing anything? (soft charge off)")
    rate = f.SOFT_RATE
    for value in (f.SOFT_RATE, 0.0):
        f.SOFT_RATE = value
        for name in ("G1b", "G2b"):
            x = fit(objective, name)
            rows = rows_for(objective, objective.chain(x, name))
            print(f"  soft {value:.2f} dB/dB  {name}  F/B {rows['score']:+.2f}"
                  f"  worst front {min(rows['front'].values()):+.2f}"
                  f"  boost {rows['eq_back_boost']:.2f}")
    f.SOFT_RATE = rate

    print("\n=== 3. round 2's hard front box (>= -3, <= +3, 400-630 within +-0.7),")
    print("        with round 3's WIDER families")
    floor, hf, soft = f.FRONT_FLOOR_DB, f.HF_LIMIT_DB, f.SOFT_RATE
    f.FRONT_FLOOR_DB, f.HF_LIMIT_DB, f.SOFT_RATE = -3.0, 0.7, 0.0
    for name in f.FAMILIES:
        x = fit(objective, name)
        rows = rows_for(objective, objective.chain(x, name))
        print(f"  {name}  F/B {rows['score']:+.2f}  worst front "
              f"{min(rows['front'].values()):+.2f}   {describe(x, name, objective)}")
    f.FRONT_FLOOR_DB, f.HF_LIMIT_DB, f.SOFT_RATE = floor, hf, soft

    print("\n=== 4. rear drive vs the loudest MEASURED chain, per band (dB)")
    best_measured = {c: max(level(chain, c) for chain in chain_of.values())
                     for c in h.CENTRES}
    print("  " + "".join(f"{c:>7d}" for c in h.CENTRES))
    solved = json.loads((g.SP / "cands7/summary.json").read_text())
    for tag, pool, key in (("agg-1", "families", solved["picks"]["agg-1"]),
                           ("agg-2", "families", solved["picks"]["agg-2"]),
                           ("agg-3", "gentle", solved["agg3"])):
        x, name = solved[pool][key]["x"], key
        chain = objective.chain(x, name)
        print(f"  " + "".join(f"{level(chain, c) - best_measured[c]:+7.1f}"
                              for c in h.CENTRES) + f"   {tag} ({name})")
    (g.SP / "cands7/frontier.json").write_text(json.dumps(
        {str(k): {n: [v[0], v[1], v[3]] for n, v in row.items()}
         for k, row in frontier.items()}, indent=1) + "\n")
    (g.SP / "cands7/frontier-x.json").write_text(json.dumps(
        {str(k): {n: v[2] for n, v in row.items() if not v[3]}
         for k, row in frontier.items()}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
