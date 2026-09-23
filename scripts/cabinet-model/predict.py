#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Predicted woofer-pair response without a room, and at a seat in front of a back wall.

    .venv/bin/python scripts/cabinet-model/predict.py --transfer transfer.npz --nearfield nearfield.npz \\
        --dsp live.yml --dsp prescription.json --png compare.png --xmax-mm 14.7

--dsp takes the speaker's live CamillaDSP graph (its rear_out2_* chains) or a rear-calibration or
prescription JSON. Only the rear stage is modelled: the common woofer chain and the bass boost are
left out. Levels are per unit drive, 0 dB = the front woofer alone on axis at 400-600 Hz. Seat
roughness is scored on the rear stage's ratio alone (as rear-design.py scores it), so EQ common to
both woofers does not count. Trust about 30-600 Hz.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

from _cabinet import Cabinet, db, rear_ratio, roughness_db

TABLE_HZ = (30, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 600)
TRAVEL_HZ = (20, 25, 30, 40, 50)
FB_BAND = (130, 450)
COLORS = ("#2a78d6", "#eb6834", "#1baf7a")


def low_end_fit(grid: np.ndarray, h: np.ndarray) -> tuple[float, float, float]:
    """2nd-order high-pass fit of a response over 30-150 Hz: (f0 Hz, Q, rms dB)."""
    sel = (grid >= 30) & (grid <= 150)

    def model(p):
        s = 1j * grid[sel] / p[0]
        return p[2] + 20 * np.log10(np.abs(s * s / (s * s + s / p[1] + 1)))

    fit = least_squares(lambda p: model(p) - db(h)[sel], x0=(80.0, 0.9, float(np.median(db(h)[sel]))),
                        bounds=((20, 0.3, -300), (200, 3.0, 300)))
    return float(fit.x[0]), float(fit.x[1]), float(np.sqrt(np.mean(fit.fun ** 2)))


def loudest_db(cab: Cabinet, r: np.ndarray, xmax_mm: float) -> np.ndarray:
    """Loudest level at 1 m on axis, free field, before either cone travels xmax_mm (peak)."""
    per_pa = np.maximum(np.abs(cab.v["front"]), np.abs(r * cab.v["rear"])) / np.abs(10 * cab.at_angle(r, 0))
    travel = per_pa / (2 * np.pi * cab.grid)
    return 20 * np.log10(xmax_mm / 1000 / (travel * np.sqrt(2) * 20e-6))


def plot(curves: dict, grid: np.ndarray, ref: float, png: Path, listener_m: float, gap_m: float) -> None:
    import matplotlib  # optional output; the plots extra installs it

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

    fig, (free, seat) = plt.subplots(2, 1, figsize=(9.5, 8.6), sharex=True)
    for (name, c), color in zip(curves.items(), COLORS):
        free.semilogx(grid, db(c["front"]) - ref, color=color, lw=2, label=f"{name}: in front")
        free.semilogx(grid, db(c["behind"]) - ref, color=color, lw=2, ls="--", label=f"{name}: behind")
        seat.semilogx(grid, db(c["seat"]) - ref, color=color, lw=2, label=name)
    free.set_title("No room; rear stage only, no bass boost", loc="left")
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
    ap.add_argument("--nearfield", type=Path, required=True, help="nearfield-analyze.py output")
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
    curves = {}
    for path in args.dsp[:len(COLORS)]:
        r, front = rear_ratio(path, grid)
        c = {"front": front * cab.at_angle(r, 0), "side": front * cab.at_angle(r, 90),
             "behind": front * cab.at_angle(r, 180), "seat": front * cab.seat(r, **room)}
        curves[path.name] = c
        fb = db(c["front"]) - db(c["behind"])
        rough = " / ".join(f"{roughness_db(db(cab.seat(r, d, **room)), grid):.2f}" for d in (0, 15, 30))
        f0, q, rms = low_end_fit(grid, c["front"])
        print(f"\n{path.name}: seat roughness 0/15/30 deg {rough} dB | front minus behind {FB_BAND[0]}-{FB_BAND[1]} Hz "
              f"mean {np.mean(fb[fb_band]):+.1f}, min {np.min(fb[fb_band]):+.1f} dB | on-axis low end "
              f"~ 2nd-order high-pass {f0:.0f} Hz, Q {q:.2f} (fit rms {rms:.1f} dB)")
        print("   Hz   front    side  behind  front-behind    seat")
        for f in TABLE_HZ:
            i = int(np.argmin(np.abs(grid - f)))
            print(f"{f:5d} {db(c['front'])[i] - ref:+7.1f} {db(c['side'])[i] - ref:+7.1f} "
                  f"{db(c['behind'])[i] - ref:+7.1f} {fb[i]:+13.1f} {db(c['seat'])[i] - ref:+7.1f}")
        if args.xmax_mm:
            loud = loudest_db(cab, r, args.xmax_mm)
            print(f"   loudest level at 1 m on axis (free field) before {args.xmax_mm:g} mm cone travel: " + ", ".join(
                f"{f} Hz {loud[int(np.argmin(np.abs(grid - f)))]:.0f} dB" for f in TRAVEL_HZ))
    if args.png:
        plot(curves, grid, ref, args.png, args.listener_m, args.wall_gap_m)


if __name__ == "__main__":
    main()
