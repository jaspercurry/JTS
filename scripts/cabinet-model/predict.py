#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Predicted woofer-pair response without a room, and at a seat in front of a back wall; with
--farfield, the model checked against gated far-field takes first.

    .venv/bin/python scripts/cabinet-model/predict.py --transfer transfer.npz --nearfield nearfield_view.json \\
        --dsp live.yml --dsp prescription.json --png compare.png --xmax-mm 14.7

--dsp takes the speaker's live CamillaDSP graph or a rear-calibration or prescription JSON. Only the
rear stage's rear/front ratio is modelled: its front chain, the woofer chain, room cuts and the bass
boost are left out, as rear-design.py scores it. Levels are per unit front drive, 0 dB = the front
woofer alone on axis at 400-600 Hz. Trust about 30-600 Hz.

--farfield takes the `jasper-round-views nearfield` view of a drivers/each round that took each
woofer alone, gated, in front (bearing poses) and behind (behind poses) at the model's microphone
distance (bem-transfer.py --mic-m). It prints measured - model for each woofer and side over [the
take's gate floor 1/T, 600 Hz], 2.5/T beside it, with the model gated like the take. One anchor sets
0 dB = the front woofer alone in front at 400-600 Hz (the median difference, ADR-0358). The model
passes within 1 dB, max |measured - model|, on each front row. Not for near-field or ungated takes,
nor with a transfer saved before --mic-m existed: re-run bem-transfer.py.

    .venv/bin/python scripts/cabinet-model/predict.py --transfer transfer.npz --nearfield nearfield_view.json \\
        --farfield farfield_view.json

Exit codes: 0 when every row compared (the verdict is printed, not encoded); 1 when a row is missing
(no_placement, no_raw, not_gated, no_band), or, before printing, when the transfer has no microphone
points or the front woofer's front row cannot anchor 400-600 Hz; 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from _cabinet import WOOFERS, Cabinet, db, farfield_takes, gated_db, rear_ratio, roughness_db, sealed_fit
from jasper.audio_measurement.series_stats import curve_difference, deviation_summary

TABLE_HZ = (30, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 600)
TRAVEL_HZ = (20, 25, 30, 40, 50)
FB_BAND = (130, 450)
COLORS = ("#2a78d6", "#eb6834", "#1baf7a")
TRUSTED_HZ = (30.0, 600.0)
ANCHOR_HZ = (400.0, 600.0)
WITHIN_DB = 1.0


def model_check(cab: Cabinet, view: dict[str, Any]) -> dict[str, Any]:
    """measured - model for each woofer alone in front and behind over [its gate's 1/T floor, 600 Hz],
    the model gated like the take. One offset, the front woofer's median difference in front at
    400-600 Hz (ADR-0358), comes off every row, so each row keeps its level against that one."""
    if not cab.at_mic:
        raise SystemExit("the transfer has no microphone points: re-run bem-transfer.py (--mic-m, 0.5 m by default)")
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for (woofer, side), take in farfield_takes(view, cab.mic_m).items():
        row = rows[woofer, side] = {"driver": WOOFERS[woofer], "side": side,
                                    **{key: take.get(key) for key in ("missing", "distance_m", "take_ids", "gate")}}
        if take["missing"] is None:
            freqs, measured = (np.asarray(take["raw"][key], float) for key in ("freqs_hz", "level_db"))
            band = [float(max(take["gate"]["validity_floor_hz"], freqs[0], TRUSTED_HZ[0])),
                    float(min(TRUSTED_HZ[1], freqs[-1], take["high_hz"] or np.inf, cab.solved_to_hz))]
            model = gated_db(cab.at_mic[woofer, side], take["gate"]["window_ms"], freqs)
            row.update(band_hz=band, diff=curve_difference(freqs, measured, freqs, model, band_hz=band, remove_level=False))
            row["missing"] = None if row["diff"] else "no_band"
    front, anchor = rows["front", "front"], None
    if front.get("diff"):
        anchor_band = [max(ANCHOR_HZ[0], front["band_hz"][0]), min(ANCHOR_HZ[1], front["band_hz"][1])]
        anchor = curve_difference(front["diff"].freqs_hz, front["diff"].curve_db, front["diff"].freqs_hz,
                                  front["diff"].against_db, band_hz=anchor_band)
    if anchor is None:
        raise SystemExit(f"the front woofer's front row cannot anchor {ANCHOR_HZ[0]:g}-{ANCHOR_HZ[1]:g} Hz "
                         f"({front['missing'] or 'its band misses it'})")
    for row in rows.values():
        if diff := row.pop("diff", None):
            delta = diff.delta_db - anchor.level_offset_db
            row.update(deviation_summary(diff.freqs_hz, delta), level_db=float(np.median(delta)),
                       freqs_hz=diff.freqs_hz.tolist(), delta_db=delta.tolist())
            if row["side"] == "front":
                row["within_1_db"] = row["max_abs_db"] <= WITHIN_DB
    return {"mic_m": cab.mic_m, "anchor_offset_db": anchor.level_offset_db, "anchor_band_hz": anchor_band,
            "nearfield_take_ids": cab.nearfield_take_ids, "rows": list(rows.values())}


def print_check(check: dict[str, Any]) -> None:
    rows, names = check["rows"], [f"{row['driver']}/{row['side']}" for row in check["rows"]]
    print(f"model check at {check['mic_m']:g} m, measured - model (dB): 0 dB = the front woofer alone in front at "
          f"{check['anchor_band_hz'][0]:.0f}-{check['anchor_band_hz'][1]:.0f} Hz, anchor_offset_db "
          f"{check['anchor_offset_db']:+.2f} | near-field takes "
          + "; ".join(f"{driver} {', '.join(ids)}" for driver, ids in check["nearfield_take_ids"].items()))
    print("   Hz" + "".join(f"{name:>20}" for name in names))
    for hz in TABLE_HZ:
        cells = [f"{row['delta_db'][int(np.argmin(np.abs(np.asarray(row['freqs_hz']) - hz)))]:+.1f}"
                 if not row["missing"] and row["band_hz"][0] <= hz <= row["band_hz"][1] else "" for row in rows]
        if any(cells):
            print(f"{hz:5d}" + "".join(f"{cell:>20}" for cell in cells))
    for name, row in zip(names, rows):
        if row["missing"]:
            print(f"{name}: missing, {row['missing']}")
            continue
        gate = row["gate"]
        verdict = f" | within {WITHIN_DB:g} dB: {'yes' if row['within_1_db'] else 'NO'}" if "within_1_db" in row else ""
        print(f"{name}: {row['band_hz'][0]:.0f}-{row['band_hz'][1]:.0f} Hz | gate {gate['window_ms']:.1f} ms, 1/T "
              f"{gate['validity_floor_hz']:.0f} Hz, 2.5/T {gate['trusted_floor_hz']:.0f} Hz, {gate['floor_source']} | "
              f"take at {row['distance_m']:g} m, model {check['mic_m']:g} m | takes {', '.join(row['take_ids'])} | level "
              f"{row['level_db']:+.2f}, rms {row['rms_db']:.2f}, max |d| {row['max_abs_db']:.2f} dB at "
              f"{row['max_abs_hz']:.0f} Hz, {row['bins']} bins{verdict}")


def loudest_db(cab: Cabinet, r: np.ndarray, xmax_mm: float) -> np.ndarray:
    """Loudest level at 1 m on axis, free field, before either cone travels xmax_mm (peak)."""
    per_pa = np.maximum(np.abs(cab.v["front"]), np.abs(r * cab.v["rear"])) / np.abs(cab.radius * cab.at_angle(r, 0))
    travel = per_pa / (2 * np.pi * cab.grid)
    return 20 * np.log10(xmax_mm / 1000 / (travel * np.sqrt(2) * 20e-6))


def plot(curves: dict, grid: np.ndarray, png: Path, listener_m: float, gap_m: float) -> None:
    import matplotlib  # optional output; the plots extra installs it

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

    fig, (free, seat) = plt.subplots(2, 1, figsize=(9.5, 8.6), sharex=True)
    for (name, c), color in zip(curves.items(), COLORS):
        free.semilogx(grid, c["front"], color=color, lw=2, label=f"{name}: in front")
        free.semilogx(grid, c["behind"], color=color, lw=2, ls="--", label=f"{name}: behind")
        seat.semilogx(grid, c["seat"], color=color, lw=2, label=name)
    free.set_title("No room; rear stage only, per unit front drive", loc="left")
    seat.set_title(f"Seat {listener_m:g} m in front, back wall {gap_m:g} m behind the cabinet (image model)", loc="left")
    for ax in (free, seat):
        ax.axvspan(20, 30, color="#eeede8")
        ax.axvspan(600, 1000, color="#eeede8")
        ax.grid(True, color="#e4e3de")
        ax.set_ylabel("dB (0 = front woofer alone, 400-600 Hz)")
        ax.legend(frameon=False, fontsize=9, loc="lower right")
        ax.set_xlim(20, 1000)
        ax.xaxis.set_major_locator(FixedLocator([20, 30, 50, 100, 200, 300, 500, 1000]))
        ax.xaxis.set_minor_locator(NullLocator())
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}" if v < 1000 else f"{v / 1000:g}k"))
    seat.set_xlabel("Frequency (Hz); shaded = not trusted")
    fig.tight_layout()
    fig.savefig(png, dpi=120)
    print(f"saved {png}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transfer", type=Path, required=True, help="bem-transfer.py output")
    ap.add_argument("--nearfield", type=Path, action="append", required=True,
                    help="jasper-round-views nearfield output of a near-field round (nearfield_view.json); repeat for "
                         "a re-run, whose curve replaces an earlier view's for each woofer it has")
    ap.add_argument("--farfield", type=Path,
                    help="jasper-round-views nearfield output of the drivers/each round to check the model against")
    ap.add_argument("--dsp", type=Path, action="append", default=[],
                    help="live graph YAML or rear-calibration/prescription JSON; repeat to compare (up to 3)")
    ap.add_argument("--listener-m", type=float, default=2.0)
    ap.add_argument("--wall-gap-m", type=float, default=0.2, help="cabinet back to the wall behind it")
    ap.add_argument("--xmax-mm", type=float, help="also print the loudest level before this cone travel")
    ap.add_argument("--png", type=Path)
    args = ap.parse_args(argv)
    if not (args.dsp or args.farfield):
        ap.error("give --dsp, --farfield or both")
    grid = np.geomspace(20, 1000, 400)
    cab = Cabinet(args.transfer, args.nearfield, grid)
    missing = False
    if args.farfield:
        check = model_check(cab, json.loads(args.farfield.read_text()))
        print_check(check)
        missing = any(row["missing"] for row in check["rows"])
    room = {"listener_m": args.listener_m, "wall_gap_m": args.wall_gap_m}
    ref = np.mean(db(cab.at_angle(np.zeros_like(grid), 0))[(grid >= 400) & (grid <= 600)])
    fb_band = (grid >= FB_BAND[0]) & (grid <= FB_BAND[1])
    rows = [int(np.argmin(np.abs(grid - f))) for f in TABLE_HZ]
    curves = {}
    for path in args.dsp[:len(COLORS)]:
        r = rear_ratio(path, grid)
        c = {"front": db(cab.at_angle(r, 0)) - ref, "side": db(cab.at_angle(r, 90)) - ref,
             "behind": db(cab.at_angle(r, 180)) - ref, "seat": db(cab.seat(r, **room)) - ref}
        curves[path.name] = c
        fb = c["front"] - c["behind"]
        rough = " / ".join(f"{roughness_db(db(cab.seat(r, d, **room)), grid):.2f}" for d in (0, 15, 30))
        f0, q, rms = sealed_fit(grid, c["front"], 30, 150)
        print(f"\n{path.name}: seat roughness 0/15/30 deg {rough} dB | front minus behind {FB_BAND[0]}-{FB_BAND[1]} Hz "
              f"mean {np.mean(fb[fb_band]):+.1f}, min {np.min(fb[fb_band]):+.1f} dB | on-axis low end "
              f"~ 2nd-order high-pass {f0:.0f} Hz, Q {q:.2f} (fit rms {rms:.1f} dB)")
        print("   Hz   front    side  behind  front-behind    seat")
        for f, i in zip(TABLE_HZ, rows):
            print(f"{f:5d} {c['front'][i]:+7.1f} {c['side'][i]:+7.1f} {c['behind'][i]:+7.1f} {fb[i]:+13.1f} {c['seat'][i]:+7.1f}")
        if args.xmax_mm:
            loud = loudest_db(cab, r, args.xmax_mm)
            print(f"   loudest level at 1 m on axis (free field) before {args.xmax_mm:g} mm cone travel: " + ", ".join(
                f"{f} Hz {loud[int(np.argmin(np.abs(grid - f)))]:.0f} dB" for f in TRAVEL_HZ))
    if args.png and curves:
        plot(curves, grid, args.png, args.listener_m, args.wall_gap_m)
    return int(missing)


if __name__ == "__main__":
    raise SystemExit(main())
