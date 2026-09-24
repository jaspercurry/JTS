#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Fit the rear stage for the smoothest response at the seat, back wall included.

fit-rear-branches.py's branch structure and bounds (bass low-pass + cancellation band-pass, no
all-pass) with a seat objective in place of a cardioid target, per unit front drive:
  - seat 45-650 Hz at 2 m (0/20/30 deg) and 1.5/2.5 m (0 deg): deviation from its own 1-octave
    trend, holes weighted x2;
  - 30-80 Hz: at most 1 dB under both woofers in phase;
  - front minus behind at least 8 dB over 130-450 Hz (soft);
  - seat at most 3 dB under the front woofer alone over 60-400 Hz;
  - rear at least 20 dB down above 1 kHz, and a soft pull on the rear/front ratio to +6 dB or less
    below 900 Hz.
The result leans on the wall: set --wall-gap-m from the room and read the robustness rows.

    .venv/bin/python scripts/cabinet-model/rear-design.py --transfer transfer.npz --nearfield nearfield_view.json \\
        --live live.yml --out prescription.json

Writes a jts_prescription with the rear unmuted, for a listening trial through the speaker's
jasper-crossover-prescriber (README.md has the steps). A fitted branch gain above unity is written
as the same flat boost on both rear branches with the front chain at 0 dB (ADR-0327), so the
woofer/tweeter balance holds and the headroom charge pays for the boost.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

from _cabinet import Cabinet, db, rear_ratio, roughness_db, seat_deviation
from jasper.active_speaker.branch_chain import camilla_filter_response, rear_branch_sum_headroom_db, rear_stage_response
from jasper.active_speaker.crossover_v2.prescription_document import DOCUMENT_KIND, read_prescription_document
from jasper.active_speaker.rear_calibration import compile_rear_stage, read_rear_calibration

_spec = importlib.util.spec_from_file_location("fit_rear_branches", Path(__file__).resolve().parents[1] / "fit-rear-branches.py")
_fitrb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fitrb)
branch_model, build_document, target_delay_ms = _fitrb._model, _fitrb.build_document, _fitrb.target_delay_ms
combo, group_delay_ms = _fitrb._combo, _fitrb._group_delay_ms
LOWER, UPPER, SAMPLE_RATE = np.array(_fitrb.PARAM_LOWER[:7]), np.array(_fitrb.PARAM_UPPER[:7]), _fitrb.DEFAULT_SAMPLE_RATE
ORDERS = (_fitrb.BASS_ORDER, _fitrb.HIGHPASS_ORDER, _fitrb.LOWPASS_ORDER)
SEED_HZ = (_fitrb.BASS_SEED_HZ, float(np.mean(_fitrb.DELAY_SLOPE_BAND_HZ)))

SEATS = ((2.0, 0), (2.0, 20), (2.0, 30), (1.5, 0), (2.5, 0))  # (listener m, bearing deg)
TABLE_HZ = (30, 40, 50, 63, 80, 100, 125, 160, 200, 315, 500, 800)
PARAMS = ("bass corner Hz", "bass delay ms", "cancel HP Hz", "cancel LP Hz", "cancel delay ms", "bass gain dB",
          "cancel gain dB")
FLAT_BOOST_HZ = 16000.0  # a Lowshelf this high is flat to 0.001 dB and 0.7 degrees below 1.2 kHz


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transfer", type=Path, required=True, help="bem-transfer.py output")
    ap.add_argument("--nearfield", type=Path, required=True, help="jasper-round-views nearfield output (nearfield_view.json)")
    ap.add_argument("--out", type=Path, required=True, help="prescription JSON to write")
    ap.add_argument("--live", type=Path, help="the live graph YAML (or a document) to compare against")
    ap.add_argument("--wall-gap-m", type=float, default=0.2, help="cabinet back to the wall behind it")
    args = ap.parse_args()
    grid = np.geomspace(30, 1200, 300)
    cab = Cabinet(args.transfer, args.nearfield, grid)
    band = {name: (grid >= lo) & (grid <= hi) for name, (lo, hi) in
            {"lf": (30, 80), "fb": (130, 450), "eff": (60, 400)}.items()}

    def seat(r, deg=0.0, listener_m=2.0, gap=args.wall_gap_m):
        return db(cab.seat(r, deg, listener_m=listener_m, wall_gap_m=gap))

    front_only, both = seat(0 * grid), seat(1 + 0 * grid)

    def residuals(p):
        r = branch_model(np.asarray(p, float), grid, False)
        at_seat = seat(r)
        fb = db(cab.at_angle(r, 0)) - db(cab.at_angle(r, 180))
        return np.concatenate([
            *(seat_deviation(seat(r, deg, listener_m), grid) for listener_m, deg in SEATS),
            2.0 * np.maximum(0.0, both - 1.0 - at_seat)[band["lf"]],
            0.3 * np.maximum(0.0, 8.0 - fb[band["fb"]]),
            0.5 * np.maximum(0.0, front_only - at_seat - 3.0)[band["eff"]],
            np.maximum(0.0, db(r)[grid >= 1000] + 20.0),
            0.5 * np.maximum(0.0, db(r)[grid <= 900] - 6.0),
        ])

    behind = cab.index(180)
    cardioid_delay = target_delay_ms(-cab.A["front"][:, behind] / cab.A["rear"][:, behind], grid)

    def start(bass, hp, lp):
        """A start whose branch delays cancel each branch's own group delay, as fit-rear-branches seeds."""
        low = camilla_filter_response([combo("ButterworthLowpass", bass, ORDERS[0])], grid)
        band = -camilla_filter_response([combo("ButterworthHighpass", hp, ORDERS[1]),
                                         combo("ButterworthLowpass", lp, ORDERS[2])], grid)
        return np.clip([bass, -group_delay_ms(low, grid, SEED_HZ[0]), hp, lp,
                        cardioid_delay - group_delay_ms(band, grid, SEED_HZ[1]), 0.0, 0.0], LOWER + 1e-6, UPPER - 1e-6)

    fits = [least_squares(residuals, start(bass, hp, lp), bounds=(LOWER, UPPER), x_scale="jac", max_nfev=400)
            for bass in (70.0, 90.0, 120.0) for hp in (60.0, 80.0, 110.0) for lp in (350.0, 600.0, 900.0)]
    best = min(fits, key=lambda fit: fit.cost).x
    print("fit: " + ", ".join(f"{name} {value:.3g}" for name, value in zip(PARAMS, best)))

    document = build_document(
        best, allpass=False,
        conditions={"dataset": f"{args.nearfield.name} x {args.transfer.name}", "fit_band_hz": [45.0, 650.0],
                    "fit_tool": "cabinet-model/rear-design.py", "measured": True,
                    "timing_reference": "front output of this stage"},
        assumptions=[f"Model design: measured near-field x Boundary Lab cabinet transfer; wall {args.wall_gap_m:g} m "
                     "behind the cabinet as an image source (R 0.9); room modes not modelled; both woofer amplifier "
                     "channels assumed to share one latency.",
                     "Listening trial: compare against the previously applied candidate."])
    shift = -document["front"]["gain_db"]
    boost = {"type": "Biquad", "parameters": {"type": "Lowshelf", "freq": FLAT_BOOST_HZ, "gain": round(shift, 4), "q": 0.7071}}
    if shift > 0:
        document["front"]["gain_db"] = 0.0
        for branch in ("bass", "cancellation"):
            document["rear"][branch]["filters"].append(boost)
    document = read_rear_calibration({**document, "rear_muted": False,
                                      "geometry": {**document["geometry"], "cabinet_back_wall_m": args.wall_gap_m}},
                                     sample_rate=SAMPLE_RATE)
    compile_rear_stage(document, front_channel=0, rear_channel=2, channel_count=3, tweeter_channel=1)
    summed, front = rear_stage_response(document, grid)
    r_new = summed / front
    flat = camilla_filter_response([boost], grid) * 10 ** (-shift / 20) if shift > 0 else 1.0
    if not np.allclose(r_new / flat, branch_model(best, grid, False), rtol=1e-3, atol=1e-5):
        raise SystemExit("the written document does not realize the fitted ratio")
    prescription = read_prescription_document({
        "kind": DOCUMENT_KIND, "schema": 1, "base": "saved", "sections": {"rear_calibration": document},
        "rationale": "Cabinet-model design of the rear stage for the smoothest seat response with the back wall."})
    with open(args.out, "w") as fh:
        fh.write(json.dumps(prescription, indent=2) + "\n")
    print(f"wrote {args.out}; front chain 0 dB, both rear branches +{max(shift, 0.0):.2f} dB (ADR-0327); "
          f"headroom charge {rear_branch_sum_headroom_db(document):.2f} dB")

    designs = {"front woofer only": 0 * grid, "new design": r_new}
    if args.live:
        designs["live"] = rear_ratio(args.live, grid)
    for name, r in designs.items():
        fb = db(cab.at_angle(r, 0)) - db(cab.at_angle(r, 180))
        rough = "/".join(f"{roughness_db(seat(r, deg), grid):.2f}" for deg in (0, 15, 30))
        peak = f"{np.max(db(r)[grid <= 900]):+.1f} dB" if np.any(r) else "none"
        print(f"{name:18s} seat roughness 0/15/30 deg {rough} dB | front minus behind 130-450 Hz mean "
              f"{np.mean(fb[band['fb']]):+.1f}, min {np.min(fb[band['fb']]):+.1f} dB | rear peak {peak}")
    compared = {n: r for n, r in designs.items() if n != "front woofer only"}
    print(" f Hz | new r dB / deg | seat vs front woofer only: " + " / ".join(compared))
    seats = {n: seat(r) - front_only for n, r in compared.items()}
    for f in TABLE_HZ:
        i = int(np.argmin(np.abs(grid - f)))
        print(f"{f:5d} | {db(r_new)[i]:+6.1f} {np.degrees(np.angle(r_new[i])):+5.0f} | "
              + " / ".join(f"{y[i]:+5.1f}" for y in seats.values()))
    for label, kwargs in [(f"listener {m} m", {"listener_m": m}) for m in (1.5, 2.5, 3.0)] + \
                         [(f"wall gap {g} m", {"gap": g}) for g in (0.1, 0.3)]:
        print(f"robustness, {label}: roughness " + ", ".join(
            f"{n} {roughness_db(seat(r, **kwargs), grid):.2f} dB" for n, r in compared.items()))


if __name__ == "__main__":
    main()
