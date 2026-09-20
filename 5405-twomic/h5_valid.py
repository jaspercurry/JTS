#!/usr/bin/env python3
"""Step 1 and 2: build the F/B model, cross-check it, validate it out of sample.

First the pipeline is checked against the coordinator's own ``fb_table.py``
numbers for H4 (all-ungated), so a band-edge or averaging mistake cannot hide
inside the model. Then the model is refitted without the target round and asked
to predict that round's tunes.
"""
from __future__ import annotations

import numpy as np

import g5lib as g
import h5lib as h

SPLITS = (("H4", "h4", {"h4"}, ("5e9afae3", "2fc52a19", "711b458f", "654e057b")),
          ("G1m", "g1m", {"g1m", "g2m"}, ("711b458f", "2fc52a19", "4b0374d1")),
          ("G2m", "g2m", {"g1m", "g2m"}, ("711b458f", "2fc52a19", "4b0374d1")))
HEAD = "        " + "".join(f"{c:>7d}" for c in h.CENTRES)


def main() -> int:
    h.widen()
    store = g.collect()
    chain_of = g.chains()

    print("=== 0. cross-check against graphs/fb_table.py (H4, measured, ALL ungated)")
    model = h.FB(store, chain_of, target="h4")
    print(HEAD)
    for fp in ("5e9afae3", "2fc52a19", "711b458f", "654e057b"):
        front = model.measured_front(fp)
        behind = model.measured_behind_ungated(fp)
        print(f"  {g.label(fp):<8s} front " + h.line(front))
        print(f"  {'':<8s} behind" + h.line(behind))
        print(f"  {'':<8s} F/B   " + h.line(h.gain(front, behind)))

    for name, target, drop, tunes in SPLITS:
        fit_tags = set(g.ALL_TAGS) - drop
        held = h.FB(store, chain_of, target=target, fit_tags=fit_tags)
        print(f"\n=== {name}: fit without {sorted(drop)}, predict {target}"
              "  (behind u<=125/g160-315/u>=400)")
        print(HEAD)
        errors = {"F/B": [], "front": [], "behind": []}
        for fp in tunes:
            if not any(fp in row["cands"] for row in held.front_rows.values()):
                continue
            chain = chain_of[fp]
            got = {"front": held.measured_front(fp), "behind": held.measured_behind(fp)}
            got["F/B"] = h.gain(got["front"], got["behind"])
            pred = {"front": held.front(chain), "behind": held.behind(chain)}
            pred["F/B"] = h.gain(pred["front"], pred["behind"])
            for what in ("front", "behind", "F/B"):
                errors[what].append([pred[what][c] - got[what][c] for c in h.CENTRES])
            print(f"  {g.label(fp):<8s} F/B  meas" + h.line(got["F/B"]))
            print(f"  {'':<8s} F/B  pred" + h.line(pred["F/B"]))
            print(f"  {'':<8s} front meas" + h.line(got["front"])
                  + "   pred" + h.line(pred["front"]))
        print(f"  {'-' * 6} mean |error| / worst, dB")
        for what in ("F/B", "front", "behind"):
            stack = np.abs(np.asarray(errors[what]))
            print(f"  {what:<6s} mean " + "".join(f"{v:7.1f}" for v in stack.mean(axis=0)))
            print(f"  {'':<6s} worst" + "".join(f"{v:7.1f}" for v in stack.max(axis=0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
