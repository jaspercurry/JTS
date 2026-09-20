#!/usr/bin/env python3
"""How far outside the MEASURED tune family does each solution drive the rear?

The forward model is linear in the chain, so any chain is handled exactly --
provided R is right at the frequencies that chain excites. R is only as good as
the drive the measured candidates put there. Per third octave: the solution's
rear drive against the loudest measured candidate's, dB.
"""
from __future__ import annotations

import json

import numpy as np

import g5lib as g
import g5_model as m
from rearpred import section_of

BANDS = [(55.0, 71.0), (71.0, 89.8), (89.1, 112.2), (111.4, 140.3), (142.5, 179.6),
         (178.2, 224.5), (222.7, 280.6), (280.6, 353.6), (353.6, 445.4),
         (445.4, 561.2), (561.2, 650.0)]
NAMES = (63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 600)


def level(chain, band):
    inside = (g.GRID >= band[0]) & (g.GRID < band[1])
    return 10.0 * np.log10(np.mean(np.abs(chain[inside]) ** 2) + 1e-30)


def main() -> int:
    chain_of = g.chains()
    base = section_of(g.load_json(g.SP / "search/D2/doc-e-035.json"))
    solved = json.loads((g.SP / "cands5/summary.json").read_text())["stages"]
    model = None
    best = [max(level(c, b) for c in chain_of.values()) for b in BANDS]
    print("  loudest measured candidate's rear drive, dB, per band:")
    print("  " + "".join(f"{n:>7d}" for n in NAMES))
    print("  " + "".join(f"{v:+7.1f}" for v in best))
    print("\n  each solution's drive MINUS that (positive = outside the measured family)")
    import g5lib
    store = g5lib.collect()
    model = m.Model(store, chain_of)
    for name in ("S1", "S2", "S3"):
        chain = model.chain_of_x(solved[name]["x"], solved[name]["shape"], base)
        cells = [level(chain, b) - v for b, v in zip(BANDS, best)]
        print(f"  {name} " + "".join(f"{v:+7.1f}" for v in cells))
    for label, fp in (("e-0.35", "27a565a9"), ("ident-C", "2fc52a19"), ("T1", "4b0374d1")):
        cells = [level(chain_of[fp], b) - v for b, v in zip(BANDS, best)]
        print(f"  {label:<7s}" + "".join(f"{v:+7.1f}" for v in cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
