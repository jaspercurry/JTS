#!/usr/bin/env python3
"""Can the +0 arm pose be rescued by fitting only the rounds near it in time?

The USB fault sits between {d1,d2,c1,649a} and {i1b,i2,l1}. If +0 moved once
and then held, a post-fault-only fit should predict l1's +0 far better than the
all-rounds fit did.
"""
from __future__ import annotations

import numpy as np

import g5lib as g

TRIALS = (("all rounds but l1", {"649a", "c1", "d1", "d2", "i1b", "i2"}, {"l1"}),
          ("post-fault only (i1b,i2)", {"i1b", "i2"}, {"l1"}),
          ("l1 itself (in-sample)", {"l1"}, {"l1"}))


def main() -> int:
    store = g.collect()
    chain_of = g.chains()
    print("predicting the +0 pose, method B, per band error (pred - measured), dB")
    print(f"  {'fit set':<26s} {'tune':<8s}" + "".join(f"{c:>7d}" for c in g.CENTRES)
          + "    mean|e|")
    for name, fit_tags, target in TRIALS:
        model = g.pooled(g.r_estimates(store, chain_of, fit_tags, "side", 0.0, gated=False))
        if model is None:
            continue
        allerr = []
        for (tag, mic, pose), row in sorted(store.items()):
            if tag not in target or mic != "side" or g.angle_of(pose) != 0.0:
                continue
            for fp in sorted(row["cands"], key=g.label):
                if fp not in chain_of:
                    continue
                got = g.measured_bands(row, fp)
                pred = g.predict_bands(row, chain_of[fp], r_full=model)
                error = [p - m for p, m in zip(pred, got)]
                allerr.append(error)
                print(f"  {name:<26s} {g.label(fp):<8s}"
                      + "".join(f"{v:+7.1f}" for v in error)
                      + f"   {np.mean(np.abs(error)):7.1f}")
        stack = np.abs(np.asarray(allerr))
        print(f"  {name:<26s} {'MEAN':<8s}" + "".join(f"{v:7.1f}" for v in stack.mean(axis=0))
              + f"   {stack.mean():7.1f}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
