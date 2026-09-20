"""Proof: the main-mic replay reproduces the product's banked rear_view.json.

Tolerances the round must meet: band levels and the superposition residual
within 0.05 dB, the arrival gap within 0.005 ms.
"""
import json
import sys
from pathlib import Path

import numpy as np

T = Path("/private/tmp/claude-501/-Users-jaspercurry-Code-JTS--claude-worktrees-speaker-tuning-llm-arch-bb1ff5/f447b743-f8a9-4f10-bce1-5ed46c07c2ff/scratchpad/twomic")
ROUND = T / (sys.argv[1] if len(sys.argv) > 1 else "round-12d0fdb51b0f")
OUT = T / (sys.argv[2] if len(sys.argv) > 2 else "pair-12d0")
DB_TOL, MS_TOL = 0.05, 0.005

want = json.loads((ROUND / "rear_view.json").read_text())["pair"]["positions"]
got = json.loads((OUT / "summary.json").read_text())["mics"]["main"]

print(f"=== replay vs rear_view.json  ({ROUND.name})")
print("  pose                              figure                 replay      product       diff")
worst = {"db": (0.0, ""), "ms": (0.0, "")}
for pose, banked in sorted(want.items()):
    mine = got[pose]
    rows = [("superposition_residual_db", mine["superposition_residual_db"],
             banked["superposition_residual_db"], "db"),
            ("arrival_gap.ms", mine["arrival_gap"]["ms"], banked["arrival_gap"]["ms"], "ms"),
            ("arrival_gap.confidence", mine["arrival_gap"]["confidence"],
             banked["arrival_gap"]["confidence"], "db"),
            ("rear_polarity.phase_deg", mine["rear_polarity"]["phase_deg"],
             banked["rear_polarity"]["phase_deg"], "db")]
    for name, a, b, unit in rows:
        diff = abs(a - b)
        print(f"  {pose:32s} {name:22s} {a:10.5f} {b:12.5f} {diff:10.2e}")
        if diff > worst[unit][0]:
            worst[unit] = (diff, f"{pose} {name}")
    assert mine["rear_polarity"]["state"] == banked["rear_polarity"]["state"], pose
    assert len(mine["bands"]) == len(banked["bands"]), pose
    for a, b in zip(mine["bands"], banked["bands"]):
        assert np.allclose(a["band_hz"], b["band_hz"]), pose
        for key in ("front_db", "rear_db", "pair_sum_db", "level_gap_db"):
            diff = abs(a[key] - b[key])
            if diff > worst["db"][0]:
                worst["db"] = (diff, f"{pose} band {b['band_hz'][0]:.0f} Hz {key}")
    span = f"{banked['bands'][0]['band_hz'][0]:.1f}-{banked['bands'][-1]['band_hz'][1]:.1f} Hz"
    print(f"  {pose:32s} {len(banked['bands'])} bands ({span}): "
          f"front/rear/pair_sum/level_gap all checked")

print(f"\n  worst dB disagreement  {worst['db'][0]:.3e} dB  at {worst['db'][1]}  (tolerance {DB_TOL})")
print(f"  worst ms disagreement  {worst['ms'][0]:.3e} ms  at {worst['ms'][1]}  (tolerance {MS_TOL})")
ok = worst["db"][0] <= DB_TOL and worst["ms"][0] <= MS_TOL
print(f"  PROOF {'PASSES' if ok else 'FAILS'}")

print("\n=== not in the banked file (HEAD adds it, so nothing to compare)")
print("  pair_band_levels now returns a per-band superposition_residual_db;"
      " the banked bands carry none.")
for pose in sorted(got):
    per = [f"{b['band_hz'][0]:.0f}:{b['superposition_residual_db']:.2f}" for b in got[pose]["bands"]]
    print(f"  {pose:32s} {' '.join(per)}")
raise SystemExit(0 if ok else 1)
