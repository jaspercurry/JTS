#!/usr/bin/env python3
"""Sanity + speed check on the objective before the search is let loose."""
import json
import time

import numpy as np

import g5lib as g
import g5_model as m
from jasper.active_speaker.branch_chain import rear_stage_response
from rearpred import section_of

store = g.collect()
chain_of = g.chains()
base = section_of(g.load_json(g.SP / "search/D2/doc-e-035.json"))
model = m.Model(store, chain_of)

print(f"guard B reference (e-0.35 predicted 71-90 Hz, worst pose): {model.e035_guard_b:+.2f} dB")
for fp in ("27a565a9", "2fc52a19", "4b0374d1"):
    chain = chain_of[fp]
    total, per = model.score(chain)
    bands = model.bands_behind(chain)
    a, b, c = model.front_guards(chain)
    print(f"\n  {g.label(fp)}: weighted capped mean {total:+.2f}  penalty {model.penalty(chain):.2f}")
    for angle in (20.0, -20.0, 0.0):
        got = g.measured_bands(store[(('l1'), 'side', next(p for (t, mi, p) in store
                                                           if t == 'l1' and mi == 'side'
                                                           and g.angle_of(p) == angle))], fp) \
            if fp in store[('l1', 'side', next(p for (t, mi, p) in store if t == 'l1'
                                               and mi == 'side' and g.angle_of(p) == angle))]["cands"] else None
        print(f"    {angle:+5.0f} pred " + "".join(f"{v:+7.1f}" for v in bands[angle])
              + f"  capped {per[angle]:+.2f}"
              + ("   measured " + "".join(f"{v:+7.1f}" for v in got) if got else ""))
    print(f"    front A {np.min(a):+.2f}  B {np.min(b):+.2f}  C {np.max(np.abs(c)):+.2f}")

x0 = [300.0, -0.35, 3.0, 250.0, 0.0, 1.0]
start = time.time()
for _ in range(20):
    model.objective(x0, (1, 0), base)
print(f"\n  objective: {(time.time() - start) / 20 * 1000:.1f} ms per evaluation")
