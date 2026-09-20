#!/usr/bin/env python3
"""Step 2: out-of-sample, per band, gated. Does the model deserve any trust?

Two held-out splits (fit on every round but the target, predict the target),
two prediction methods (A: identify gated; B: identify ungated and window the
prediction), and a do-nothing baseline (predict 0 dB) so the model has to beat
"the tune changes nothing" before anyone builds on it.
"""
from __future__ import annotations

import numpy as np

import g5lib as g

SPLITS = (("L1", {"l1"}), ("I1b+I2", {"i1b", "i2"}))
ANGLES = (20.0, -20.0, 0.0)


def main() -> int:
    store = g.collect()
    chain_of = g.chains()
    summary = {}

    for name, target in SPLITS:
        fit_tags = set(g.ALL_TAGS) - target
        models = {}
        for angle in ANGLES:
            models[angle] = {
                "full": g.pooled(g.r_estimates(store, chain_of, fit_tags, "side", angle,
                                               gated=False)),
                "gated": g.pooled(g.r_estimates(store, chain_of, fit_tags, "side", angle,
                                                gated=True)),
            }
        print(f"\n=== split {name}: fit on {sorted(fit_tags)}, predict {sorted(target)}")
        print(f"  {'round':5s} {'tune':<8s} {'angle':>5s} {'method':<8s}"
              + "".join(f"{c:>7d}" for c in g.CENTRES) + "   (100/125 ungated)")
        rows = {}
        for (tag, mic, pose), row in sorted(store.items()):
            if tag not in target or mic != "side":
                continue
            angle = g.angle_of(pose)
            if models[angle]["full"] is None:
                continue
            for fp in sorted(row["cands"], key=g.label):
                if fp not in chain_of:
                    continue
                chain = chain_of[fp]
                got = g.measured_bands(row, fp)
                print(f"  {tag:5s} {g.label(fp):<8s} {angle:+5.0f} {'measured':<8s}"
                      + "".join(f"{v:+7.1f}" for v in got))
                for method, kwargs in (("A gated", {"r_full": models[angle]["full"],
                                                    "r_gated": models[angle]["gated"]}),
                                       ("B window", {"r_full": models[angle]["full"]})):
                    pred = g.predict_bands(row, chain, **kwargs)
                    error = [p - m for p, m in zip(pred, got)]
                    print(f"  {'':5s} {'':<8s} {'':>5s} {method:<8s}"
                          + "".join(f"{v:+7.1f}" for v in pred)
                          + "   err " + "".join(f"{v:+6.1f}" for v in error))
                    rows.setdefault((method, angle), []).append(error)
                rows.setdefault(("C none", angle), []).append([-v for v in got])

        print(f"\n  -- {name}: per-band |error| (mean / worst), dB")
        print(f"  {'method':<8s} {'angle':>5s}  n  " + "".join(f"{c:>13d}" for c in g.CENTRES))
        for method in ("A gated", "B window", "C none"):
            for angle in ANGLES:
                cell = rows.get((method, angle))
                if not cell:
                    continue
                stack = np.abs(np.asarray(cell))
                print(f"  {method:<8s} {angle:+5.0f} {stack.shape[0]:2d}  "
                      + "".join(f"{m:6.1f}/{w:<6.1f}" for m, w in
                                zip(stack.mean(axis=0), stack.max(axis=0))))
            cell = [v for (m, a), rr in rows.items() if m == method for v in rr]
            stack = np.abs(np.asarray(cell))
            summary[(name, method)] = stack
            solid = [v for (m, a), rr in rows.items() if m == method and a != 0.0 for v in rr]
            solid = np.abs(np.asarray(solid))
            print(f"  {method:<8s}  ALL  {stack.shape[0]:2d}  "
                  + "".join(f"{m:6.1f}/{w:<6.1f}" for m, w in
                            zip(stack.mean(axis=0), stack.max(axis=0))))
            print(f"  {method:<8s} +-20  {solid.shape[0]:2d}  "
                  + "".join(f"{m:6.1f}/{w:<6.1f}" for m, w in
                            zip(solid.mean(axis=0), solid.max(axis=0))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
