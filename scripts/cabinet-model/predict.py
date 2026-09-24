#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Predicted woofer-pair response without a room, and at a seat in front of a back wall.

    .venv/bin/python scripts/cabinet-model/predict.py --transfer transfer.npz --nearfield nearfield_view.json \\
        --dsp live.yml --dsp prescription.json --png compare.png --xmax-mm 14.7

--dsp takes the speaker's live CamillaDSP graph or a rear-calibration or prescription JSON. Only the
rear stage's rear/front ratio is modelled: its front chain, the woofer chain, room cuts and the bass
boost are left out, as rear-design.py scores it. Levels are per unit front drive, 0 dB = the front
woofer alone on axis at 400-600 Hz. Trust about 30-600 Hz.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from _cabinet import Cabinet, db, rear_ratio, roughness_db, sealed_fit

TABLE_HZ = (30, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 600)
TRAVEL_HZ = (20, 25, 30, 40, 50)
FB_BAND = (130, 450)
COLORS = ("#2a78d6", "#eb6834", "#1baf7a")


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transfer", type=Path, required=True, help="bem-transfer.py output")
    ap.add_argument("--nearfield", type=Path, required=True, help="jasper-round-views nearfield output (nearfield_view.json)")
    ap.add_argument("--dsp", type=Path, action="append", required=True,
                    help="live graph YAML or rear-calibration/prescription JSON; repeat to compare (up to 3)")
    ap.add_argument("--listener-m", type=float, default=2.0)
    ap.add_argument("--wall-gap-m", type=float, default=0.2, help="cabinet back to the wall behind it")
    ap.add_argument("--xmax-mm", type=float, help="also print the loudest level before this cone travel")
    ap.add_argument("--png", type=Path)
    args = ap.parse_args()
    grid = np.geomspace(20, 1000, 400)
    cab = Cabinet(args.transfer, args.nearfield, grid)
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
    if args.png:
        plot(curves, grid, args.png, args.listener_m, args.wall_gap_m)


if __name__ == "__main__":
    main()
