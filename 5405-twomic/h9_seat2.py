#!/usr/bin/env python3
"""Re-derive seat-2 with the distance measured from agg-1 ONLY.

The brief asks for the most conservative change FROM AGG-1, the applied tune.
The general search reports the nearest of the two same-structure measured
tunes, and picked one 3.2 steps from wall170-1c but ~19 from agg-1 -- a
conservative edit of the wrong tune.
"""
import json

import numpy as np
from scipy.optimize import differential_evolution

import h9_solve as s
import h9lib as w
from wall_numbers import THIRDS

A0 = s.MEASURED["A0/agg-1"]


def distance_from_a0(x, mid):
    total, parts = (3.0 if mid else 0.0), []
    for i, knob in enumerate(s.NAMES):
        d = x[i] - A0[i]
        total += abs(d) / s.STEP[i]
        if abs(d) > s.STEP[i] * 0.2:
            parts.append(f"{knob} {d:+.2f}")
    if mid:
        parts.append(f"added {x[8]:.0f} Hz {x[9]:+.2f} dB q{x[10]:.2f}")
    return total, ", ".join(parts) or "identical"


def main() -> int:
    seat = s.Seat()
    seat.model = {m: w.pooled([w.estimate_r(seat.takes, seat.rear, seat.front, m, n)
                               for n in w.REAR_ON]) for m in w.MICS}
    seat.prepare()
    a0 = seat.figures(w.sections()["A0"])
    target = a0["J"] - s.MEANINGFUL_DJ
    print(f"  A0 J {a0['J']:.2f}; seat-2 must reach J <= {target:.2f}")
    best = None
    for mid in (False, True):
        bounds = (*s.BOUNDS, *s.MID) if mid else s.BOUNDS

        def penalty(v, mid=mid):
            fig = seat.figures(seat.section(v, mid))
            return (distance_from_a0(v, mid)[0] + 20.0 * max(0.0, fig["J"] - target)
                    + 20.0 * fig["hard"])
        v = differential_evolution(penalty, bounds, seed=s.SEED, maxiter=40, popsize=12,
                                   tol=0.01, polish=True, init="latinhypercube").x
        fig = seat.figures(seat.section(v, mid))
        d = distance_from_a0(v, mid)
        ok = fig["J"] <= target + 0.01 and not fig["notes"]
        print(f"  {'with' if mid else 'without'} mid bell: J {fig['J']:.2f} "
              f"seat RMS {fig['seat']['rms']:.2f} hole {fig['seat']['hole_db']:+.2f}  "
              f"{d[0]:.1f} steps from agg-1: {d[1]}" + ("" if ok else "   <-- does not qualify"))
        if ok and (best is None or d[0] < best[0]):
            best = (d[0], list(map(float, v)), mid, fig, d)
    if best is None:
        print("  nothing qualifies -- seat-2 left as emitted by the general search")
        return 1
    _d, v, mid, fig, d = best
    verdict = s.emit(
        s.OUT / "seat-2.json",
        f"Smallest change to the APPLIED tune agg-1 that measurably smooths the seat "
        f"(#5405, wall1c): {d[1]}. Predicted seat RMS {fig['seat']['rms']:.2f} dB against "
        f"A0's predicted {a0['seat']['rms']:.2f} (measured 3.81), wall hole "
        f"{fig['seat']['hole_db']:+.2f} dB, J {fig['J']:.2f} against {a0['J']:.2f}. Chosen "
        f"by minimising the distance from agg-1, not by minimising J. A prediction, not a tune.",
        seat.section(v, mid),
        [f"Distance from agg-1: {d[1]} ({d[0]:.1f} steps). Every other parameter is "
         f"agg-1's own measured value."], fig)
    print(f"  cands9/seat-2.json  {verdict}")
    print(f"  {s.describe(v, mid)}")
    print("  seat change " + "".join(f"{fig['seat_change'][str(c)]:+7.1f}" for c in THIRDS))
    print("  front total " + "".join(f"{fig['front_total'][str(c)]:+7.1f}" for c in THIRDS))
    robust = []
    for held in w.REAR_ON:
        saved = seat.model
        seat.model = {m: w.pooled([w.estimate_r(seat.takes, seat.rear, seat.front, m, n)
                                   for n in w.REAR_ON if n != held]) for m in w.MICS}
        seat.prepare()
        robust.append(seat.figures(seat.section(v, mid))["J"])
        seat.model = saved
        seat.prepare()
    print("  J under each 3-tune subset: " + " ".join(f"{x:.2f}" for x in robust))
    path = s.OUT / "summary.json"
    data = json.loads(path.read_text())
    data["seat2_from_agg1"] = {"x": v, "mid": mid, "fig": fig, "distance": list(d),
                               "robust": robust}
    path.write_text(json.dumps(data, indent=1, default=float) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
