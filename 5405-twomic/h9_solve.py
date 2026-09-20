#!/usr/bin/env python3
"""Step 2-4: solve for the SMOOTHEST SEAT, and emit two room-cleared documents.

J = 2 x RMS_seat + 1 x RMS_front + 0.3 per dB the seat's wall hole is deeper
than -5 dB. RMS and hole are wall_numbers' own functions, applied to the
MEASURED C0 curve plus this tune's predicted change -- so the product's own
measurement carries all the detail and the model supplies only the difference,
which is the only thing it has ever predicted well.
"""
from __future__ import annotations

import json
from copy import deepcopy

import numpy as np
from scipy.optimize import differential_evolution

import h9lib as w
from ba_lib import POSE0
from wall_numbers import THIRDS

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

SEED = 20260921
OUT = w.SP / "cands9"
HOLE_FLOOR_DB = -5.0
HOLE_RATE = 0.3
FRONT_FLOOR_DB = -8.0
HF_SLACK_DB = 1.0
HARD_WEIGHT = 5.0
MARGIN = 0.05
MEANINGFUL_DJ = 0.5
BOUNDS = ((-0.6, 0.3), (300.0, 450.0), (95.0, 135.0), (0.0, 6.0), (1.0, 3.0),
          (230.0, 280.0), (0.0, 6.0), (1.5, 3.0))
MID = ((130.0, 200.0), (-6.0, 6.0), (1.5, 3.0))
STEP = (0.05, 25.0, 5.0, 0.5, 0.25, 10.0, 0.5, 0.25)
NAMES = ("delay", "lowpass", "low f", "low g", "low q", "bell f", "bell g", "bell q")
MEASURED = {"A0/agg-1": (-0.213, 300.35, 120.0, 5.453, 1.0, 259.46, 5.651, 2.254),
            "wall170-1c": (-0.0456, 364.44, 113.28, 5.54, 2.257, 249.23, 5.773, 2.893)}
LOW = (80, 100, 125, 160, 200, 250, 315)
HF = (400, 500, 630)


def peaking(f, g, q):
    return {"type": "Biquad", "parameters": {"type": "Peaking", "freq": round(float(f), 2),
                                             "gain": round(float(g), 3), "q": round(float(q), 3)}}


def cancellation(x, mid):
    filters = [
        {"type": "BiquadCombo",
         "parameters": {"type": "LinkwitzRileyHighpass", "freq": 80.0, "order": 4}},
        {"type": "BiquadCombo",
         "parameters": {"type": "ButterworthLowpass", "freq": round(float(x[1]), 2),
                        "order": 2}},
        {"type": "Biquad",
         "parameters": {"type": "Peaking", "freq": 190.14, "gain": -6.36, "q": 0.996}},
        peaking(x[2], x[3], x[4]), peaking(x[5], x[6], x[7])]
    if mid:
        filters.append(peaking(x[8], x[9], x[10]))
    return {"delay_ms": round(float(x[0]), 4), "filters": filters,
            "gain_db": 0.0, "inverted": True, "muted": False}


class Seat:
    def __init__(self):
        self.rear, self.front = w.chains()
        self.takes, _ = w.averaged()
        self.base = w.sections()["A0"]
        self.grid = w.result_grid()
        low, high = w.edges_of(self.grid)
        self.cells = [np.flatnonzero((w.GRID >= a) & (w.GRID < b)) for a, b in zip(low, high)]
        self.zero_curve = {m: w.measured_curve(m, w.ZERO)[1] for m in w.MICS}
        self.zero_tf = {m: self.takes[(m, w.ZERO)] for m in w.MICS}
        self.a0_change = {m: self.measured_change(m, "A0") for m in w.MICS}
        # Every candidate of this family wears N1's front chain, so the front
        # ratio is a constant vector; only the rear branch moves per evaluation.
        self.ratio = self.front["A0"] / self.front[w.ZERO]
        self.a0_pred_total = None
        self.flat = np.concatenate(self.cells)
        self.starts = np.cumsum([0] + [c.size for c in self.cells])[:-1]
        self.sizes = np.asarray([c.size for c in self.cells], dtype=float)

    def measured_change(self, mic, name):
        return w.measured_curve(mic, name)[1] - self.zero_curve[mic]

    def section(self, x, mid):
        out = deepcopy(dict(self.base))
        out["rear"] = {**out["rear"], "cancellation": cancellation(x, mid)}
        return out

    def map_change(self, change):
        power = 10.0 ** (change / 10.0)
        sums = np.add.reduceat(power[self.flat], self.starts)
        return 10.0 * np.log10(sums / self.sizes)

    def prepare(self):
        """Cache what does not depend on the candidate's rear branch."""
        live = np.zeros(w.GRID.shape, dtype=bool)
        for mic in w.MICS:
            live |= np.abs(self.model[mic]) > 0
        self.subidx = np.flatnonzero(live)
        self.subgrid = w.GRID[self.subidx]
        self.mdiv = {m: self.model[m] / np.where(np.abs(self.zero_tf[m]) > 0,
                                                 self.zero_tf[m], 1.0) for m in w.MICS}
        self.a0_pred_total = None
        self.a0_pred_total = w.thirds_of(
            self.grid, self.curves(w.sections()["A0"])["main"]["total"])

    def curves(self, section):
        rear_sub = rear_stage_response(section, self.subgrid)[0]
        rear_chain = np.zeros(w.GRID.shape, dtype=complex)
        rear_chain[self.subidx] = rear_sub
        charge = float(rear_branch_sum_headroom_db(section))
        out = {}
        for mic in w.MICS:
            change = 20.0 * np.log10(
                np.abs(self.ratio + self.mdiv[mic] * rear_chain) + 1e-30)
            mapped = self.map_change(change)
            out[mic] = {"change": mapped, "curve": self.zero_curve[mic] + mapped,
                        "total": mapped - charge}
        out["charge"] = charge
        return out

    def figures(self, section):
        out = self.curves(section)
        seat = w.figures(self.grid, out["side"]["curve"])
        front = w.figures(self.grid, out["main"]["curve"])
        hard, notes = 0.0, []
        total = w.thirds_of(self.grid, out["main"]["total"])
        # The 400-630 Hz limit compares like with like: the PREDICTED A0, not
        # the measured one. The front model is ~3.3 dB out at 500 Hz, and
        # charging that shared error to every candidate would make A0 itself
        # illegal against its own measurement.
        a0 = self.a0_pred_total
        for c in LOW:
            short = FRONT_FLOOR_DB + MARGIN - total[c]
            if short > 0:
                hard += short
                if short > MARGIN + 0.005:
                    notes.append(f"front {c} Hz {total[c]:+.1f} < {FRONT_FLOOR_DB:+.0f}")
        for c in HF:
            off = abs(total[c] - a0[c]) - HF_SLACK_DB + MARGIN
            if off > 0:
                hard += off
                if off > MARGIN + 0.005:
                    notes.append(f"front {c} Hz {total[c] - a0[c]:+.1f} vs A0")
        penalty = HOLE_RATE * max(0.0, HOLE_FLOOR_DB - seat["hole_db"])
        j = 2.0 * seat["rms"] + front["rms"] + penalty
        return {"J": round(j, 3), "seat": seat, "front": front, "charge": round(out["charge"], 2),
                "hard": hard, "notes": notes, "penalty": round(penalty, 3),
                "seat_change": {str(c): round(v, 2)
                                for c, v in w.thirds_of(self.grid, out["side"]["change"]).items()},
                "front_total": {str(c): round(v, 2) for c, v in total.items()},
                "eq_back": round(max(0.0, -min(total.values())), 2)}

    def cost(self, x, mid):
        f = self.figures(self.section(x, mid))
        return f["J"] + HARD_WEIGHT * f["hard"]


def distance(x, mid):
    best = None
    for name, ref in MEASURED.items():
        total = 3.0 if mid else 0.0
        parts = []
        for i, knob in enumerate(NAMES):
            d = x[i] - ref[i]
            total += abs(d) / STEP[i]
            if abs(d) > STEP[i] * 0.2:
                parts.append(f"{knob} {d:+.2f}")
        if mid:
            parts.append(f"added {x[8]:.0f} Hz {x[9]:+.2f} dB q{x[10]:.2f}")
        if best is None or total < best[0]:
            best = (total, name, ", ".join(parts) or "identical")
    return best


def describe(x, mid):
    c = cancellation(x, mid)
    out = [f"delay {c['delay_ms']:+.4f} ms",
           f"lowpass {c['filters'][1]['parameters']['freq']:.1f} Hz"]
    for one in c["filters"][3:]:
        p = one["parameters"]
        out.append(f"Peaking {p['freq']:.1f} Hz {p['gain']:+.2f} dB q{p['q']:.2f}")
    return "; ".join(out)


def emit(path, rationale, section, extra, fig) -> str:
    section = dict(section)
    section["conditions"] = {
        "dataset": "jts3 round wall1c (0d0abbb03574), cabinet ~0.2 m off a wall; UMIK-2 "
                   "front arm 0.81 m pose 0; Dayton at the listening seat ~2 m, ~20 deg "
                   "off axis; room layer cleared in every candidate",
        "fit_band_hz": [80.0, 350.0], "fit_tool": "h9_solve.py", "measured": True,
        "timing_reference": "front output of this stage"}
    section["assumptions"] = [
        "Fitted to the SEAT, not to a polar figure: J = 2 x RMS(seat) + RMS(front arm), "
        "where RMS is the 1/6-octave response's deviation from its own 1-octave trend "
        "over 80-350 Hz, plus 0.3 per dB the seat's 120-260 Hz hole is deeper than -5 dB.",
        "The zero is C0 wearing THIS tune's front chain. C0's own front chain is EMPTY "
        "(gain 0, no filters) while every rear-on tune carries N1's (-0.51 dB, Allpass 80, "
        "Peaking 190.14), so X_i - X_C0 is NOT R c_i; the front-chain ratio is applied "
        "explicitly and the 1-4 kHz trim that removes headroom and drift is put back by "
        "the known scalar F_i/F_C0.",
        "Leave-one-out over the four measured rear-on tunes: seat change per third octave "
        "0.78 dB mean / 1.82 dB worst over 100-315 Hz, seat RMS 0.22 dB mean. The hole "
        "depth is predicted about 1.1 dB SHALLOW in all four cases, so read the hole "
        "figure below as optimistic by roughly that much.",
        *extra,
        "Predicted SEAT change vs C0 per third octave "
        + "/".join(str(c) for c in THIRDS) + " Hz: "
        + " ".join(f"{fig['seat_change'][str(c)]:+.1f}" for c in THIRDS),
        f"Predicted seat RMS {fig['seat']['rms']:.2f} dB, wall hole "
        f"{fig['seat']['hole_db']:+.2f} dB at {fig['seat']['hole_hz']:.0f} Hz; front arm "
        f"RMS {fig['front']['rms']:.2f} dB, hole {fig['front']['hole_db']:+.2f} dB.",
        f"Front-arm total change vs C0 (includes this tune's {fig['charge']:.2f} dB "
        f"headroom cut): "
        + " ".join(f"{fig['front_total'][str(c)]:+.1f}" for c in THIRDS)
        + f"; EQ-back cost {fig['eq_back']:.2f} dB.",
    ]
    document = {"base": "saved", "kind": "jts_prescription", "schema": 1,
                "rationale": rationale,
                "sections": {"rear_calibration": section, "room": None}}
    path.write_text(json.dumps(document, indent=4, sort_keys=True) + "\n")
    try:
        read_rear_calibration(section, sample_rate=48000)
        read_prescription_document(document)
        return "PASS"
    except Exception as exc:                        # noqa: BLE001
        return f"FAIL {type(exc).__name__}: {exc}"


def main() -> int:
    seat = Seat()
    seat.model = {m: w.pooled([w.estimate_r(seat.takes, seat.rear, seat.front, m, n)
                               for n in w.REAR_ON]) for m in w.MICS}
    seat.prepare()
    OUT.mkdir(parents=True, exist_ok=True)

    print("=== the model against the measured round (in-sample check)")
    print(f"  {'tune':<11s}{'seat RMS':>10s}{'hole':>8s}{'front RMS':>11s}   (measured / predicted)")
    for name in ("A0", "N1c", "ident-Cc", "wall170-1c"):
        got = {m: w.figures(seat.grid, w.measured_curve(m, name)[1]) for m in w.MICS}
        fig = seat.figures(w.sections()[name])
        print(f"  {name:<11s}{got['side']['rms']:10.2f}{got['side']['hole_db']:8.2f}"
              f"{got['main']['rms']:11.2f}   measured")
        print(f"  {'':<11s}{fig['seat']['rms']:10.2f}{fig['seat']['hole_db']:8.2f}"
              f"{fig['front']['rms']:11.2f}   predicted  J={fig['J']:.2f}")
    a0 = seat.figures(w.sections()["A0"])

    results = {}
    for mid in (False, True):
        bounds = (*BOUNDS, *MID) if mid else BOUNDS
        x = differential_evolution(lambda v: seat.cost(v, mid), bounds, seed=SEED,
                                   maxiter=45, popsize=12, tol=0.01, polish=True,
                                   init="latinhypercube").x
        fig = seat.figures(seat.section(x, mid))
        robust = []
        for held in w.REAR_ON:
            saved = seat.model
            seat.model = {m: w.pooled([w.estimate_r(seat.takes, seat.rear, seat.front, m, n)
                                       for n in w.REAR_ON if n != held]) for m in w.MICS}
            seat.prepare()
            robust.append(seat.figures(seat.section(x, mid))["J"])
            seat.model = saved
            seat.prepare()
        name = "S-b" if mid else "S-a"
        results[name] = {"x": list(map(float, x)), "mid": mid, "fig": fig,
                         "robust": robust, "distance": distance(x, mid),
                         "describe": describe(x, mid)}
        print(f"\n=== {name}: {results[name]['describe']}")
        print(f"  J {fig['J']:.2f} (A0 {a0['J']:.2f})  seat RMS {fig['seat']['rms']:.2f} "
              f"hole {fig['seat']['hole_db']:+.2f} @ {fig['seat']['hole_hz']:.0f} Hz  "
              f"front RMS {fig['front']['rms']:.2f}  EQ-back {fig['eq_back']:.2f} dB"
              + ("" if not fig["notes"] else "  HARD: " + "; ".join(fig["notes"])))
        print(f"  seat change " + "".join(f"{fig['seat_change'][str(c)]:+7.1f}" for c in THIRDS))
        print(f"  J under each 3-tune subset: "
              + " ".join(f"{v:.2f}" for v in robust)
              + f"   nearest measured {results[name]['distance'][1]}: "
                f"{results[name]['distance'][2]} ({results[name]['distance'][0]:.1f} steps)")

    pick = "S-b" if (results["S-a"]["fig"]["J"] - results["S-b"]["fig"]["J"] >= 0.5) else "S-a"
    print(f"\n  seat-1 uses {pick} (S-a J {results['S-a']['fig']['J']:.2f}, "
          f"S-b {results['S-b']['fig']['J']:.2f}; simpler unless 0.5 better)")

    print(f"\n=== seat-2: smallest change from A0 with J at least {MEANINGFUL_DJ} better")
    best = None
    for mid in (False, True):
        bounds = (*BOUNDS, *MID) if mid else BOUNDS

        def penalty(v, mid=mid):
            fig = seat.figures(seat.section(v, mid))
            short = max(0.0, fig["J"] - (a0["J"] - MEANINGFUL_DJ))
            return distance(v, mid)[0] + 20.0 * short + 20.0 * fig["hard"]
        v = differential_evolution(penalty, bounds, seed=SEED, maxiter=35, popsize=10,
                                   tol=0.01, polish=True, init="latinhypercube").x
        fig = seat.figures(seat.section(v, mid))
        d = distance(v, mid)
        good = fig["J"] <= a0["J"] - MEANINGFUL_DJ + 0.01 and not fig["notes"]
        print(f"  {'with' if mid else 'without'} mid bell: J {fig['J']:.2f} "
              f"seat RMS {fig['seat']['rms']:.2f} distance {d[0]:.1f} from {d[1]}"
              + ("" if good else "   <-- does not qualify"))
        if good and (best is None or d[0] < best[0]):
            best = (d[0], list(map(float, v)), mid, fig, d)

    print("\n=== emitted documents")
    item = results[pick]
    verdict = {}
    verdict["seat-1"] = emit(
        OUT / "seat-1.json",
        f"Smoothest-seat solution (#5405, wall1c): {item['describe']}. Predicted seat RMS "
        f"{item['fig']['seat']['rms']:.2f} dB against A0/agg-1's measured "
        f"{w.figures(seat.grid, w.measured_curve('side', 'A0')[1])['rms']:.2f}, wall hole "
        f"{item['fig']['seat']['hole_db']:+.2f} dB. Nearest measured tune "
        f"{item['distance'][1]}: {item['distance'][2]}. A prediction, not a tune.",
        seat.section(item["x"], item["mid"]),
        [f"Nearest measured tune {item['distance'][1]}: {item['distance'][2]} "
         f"({item['distance'][0]:.1f} steps).",
         f"J under R identified from each three-tune subset: "
         + ", ".join(f"{v:.2f}" for v in item["robust"]) + "."],
        item["fig"])
    print(f"  cands9/seat-1.json  ({pick})  {verdict['seat-1']}")
    if best is None:
        print(f"  seat-2: nothing in the family improves J by {MEANINGFUL_DJ} inside the "
              f"front rules -- NOT emitted")
    else:
        _d, v, mid, fig, d = best
        verdict["seat-2"] = emit(
            OUT / "seat-2.json",
            f"Smallest change to the applied tune agg-1 that measurably smooths the seat "
            f"(#5405, wall1c): {d[2]}. Predicted seat RMS {fig['seat']['rms']:.2f} dB "
            f"against A0's predicted {a0['seat']['rms']:.2f}, J {fig['J']:.2f} against "
            f"{a0['J']:.2f}. Chosen by minimising the distance from agg-1, not by "
            f"minimising J. A prediction, not a tune.",
            seat.section(v, mid),
            [f"Distance from {d[1]}: {d[2]} ({d[0]:.1f} steps)."], fig)
        print(f"  cands9/seat-2.json  {verdict['seat-2']}")
        print(f"  {describe(v, mid)}")
        print(f"  J {fig['J']:.2f}  seat RMS {fig['seat']['rms']:.2f} hole "
              f"{fig['seat']['hole_db']:+.2f}  front RMS {fig['front']['rms']:.2f}")
        print("  seat change " + "".join(f"{fig['seat_change'][str(c)]:+7.1f}" for c in THIRDS))
        results["seat-2"] = {"x": v, "mid": mid, "fig": fig, "distance": d}
    (OUT / "summary.json").write_text(json.dumps(
        {"A0": a0, "results": results, "pick": pick, "verdicts": verdict},
        indent=1, default=float) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
