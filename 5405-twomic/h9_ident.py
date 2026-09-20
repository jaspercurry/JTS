#!/usr/bin/env python3
"""Step 1: identify R at the seat and at the front arm, and LEAVE ONE OUT.

The leave-one-out table is the whole decision: identify from three tunes,
predict the fourth's seat change against C0 per third octave, and read the
error. Nothing downstream is worth anything if that fails.
"""
from __future__ import annotations

import json

import numpy as np

import h9lib as w
import identlib as il
from wall_numbers import THIRDS

BANDS = [(c / 2 ** (1 / 6), c * 2 ** (1 / 6)) for c in (80, 100, 125, 160, 200, 250, 315, 400)]
CENTRES = (80, 100, 125, 160, 200, 250, 315, 400)
SCORED = (100, 125, 160, 200, 250, 315)


def spread(estimates, band):
    inside = (w.GRID >= band[0]) & (w.GRID < band[1])
    cell = np.asarray([e[inside] for e in estimates])
    if not np.isfinite(cell).any():
        return float("nan"), float("nan")
    with np.errstate(invalid="ignore", divide="ignore"):
        level = 20.0 * np.log10(np.abs(cell))
        db = float(np.nanmax(np.nanmean(level, axis=1)) - np.nanmin(np.nanmean(level, axis=1)))
        unit = np.nanmean(cell / np.abs(cell), axis=1)
    return db, il.circular_spread_deg(np.degrees(np.angle(unit)))


def main() -> int:
    rear, front = w.chains()
    takes, refused = w.averaged()
    print("=== takes kept per tune (2 repeats planned)")
    for mic in w.MICS:
        row = " ".join(f"{n}:{takes[(mic, n, 'n')]}" for n in w.DOCS)
        print(f"  {'seat ' if mic == 'side' else 'front'}  {row}")
    for item in refused:
        print(f"  refused {item[0]} {item[1]} {item[2]} residual {item[3]:+.1f} dB")
    print(f"  front-chain ratio at 1-4 kHz, A0/C0 = "
          f"{20 * np.log10(abs(w.rho_of(front, 'A0'))):+.2f} dB "
          f"(the -0.51 dB front gain the trim removed and this puts back)")

    print("\n=== agreement of the four R estimates, per third octave (dB / deg spread)")
    print(f"  {'mic':<6s}" + "".join(f"{c:>9d}" for c in CENTRES))
    models = {}
    for mic in w.MICS:
        estimates = [w.estimate_r(takes, rear, front, mic, n) for n in w.REAR_ON]
        models[mic] = w.pooled(estimates)
        rows = [spread(estimates, b) for b in BANDS]
        label = "seat" if mic == "side" else "front"
        print(f"  {label:<6s}" + "".join(f"{v[0]:9.1f}" for v in rows))
        print(f"  {'':<6s}" + "".join(f"{v[1]:9.0f}" for v in rows))

    print("\n=== LEAVE ONE OUT at the SEAT: fit on three, predict the fourth")
    print("  change vs C0 per third octave, measured then predicted, then error")
    print(f"  {'tune':<11s}" + "".join(f"{c:>7d}" for c in THIRDS) + "   RMS   hole")
    grid = w.result_grid()
    errors, figure_rows = [], []
    for mic in w.MICS:
        if mic != "side":
            continue
        for held in w.REAR_ON:
            model = w.pooled([w.estimate_r(takes, rear, front, mic, n)
                              for n in w.REAR_ON if n != held])
            predicted = w.predict(takes, front, model, mic, rear[held], front[held])
            zero = takes[(mic, w.ZERO)]
            change = 20.0 * np.log10(np.abs(predicted) + 1e-30) - 20.0 * np.log10(
                np.abs(zero) + 1e-30)
            mapped = w.to_result_grid(change, grid)
            f0, c0_curve = w.measured_curve(mic, w.ZERO)
            _, own = w.measured_curve(mic, held)
            measured_change = own - c0_curve
            got = w.thirds_of(f0, measured_change)
            pred = w.thirds_of(f0, mapped)
            error = [pred[c] - got[c] for c in THIRDS]
            errors.append(error)
            fig_pred = w.figures(f0, c0_curve + mapped)
            fig_meas = w.figures(f0, own)
            figure_rows.append((held, fig_meas, fig_pred))
            print(f"  {held:<11s}" + "".join(f"{got[c]:+7.1f}" for c in THIRDS)
                  + f"  {fig_meas['rms']:5.2f} {fig_meas['hole_db']:+6.2f} meas")
            print(f"  {'':<11s}" + "".join(f"{pred[c]:+7.1f}" for c in THIRDS)
                  + f"  {fig_pred['rms']:5.2f} {fig_pred['hole_db']:+6.2f} pred")
            print(f"  {'':<11s}" + "".join(f"{v:+7.1f}" for v in error)
                  + f"  {fig_pred['rms'] - fig_meas['rms']:+5.2f} "
                    f"{fig_pred['hole_db'] - fig_meas['hole_db']:+6.2f} err")
    stack = np.abs(np.asarray(errors))
    print(f"  {'mean |e|':<11s}" + "".join(f"{v:7.1f}" for v in stack.mean(axis=0)))
    print(f"  {'worst':<11s}" + "".join(f"{v:7.1f}" for v in stack.max(axis=0)))
    scored = [i for i, c in enumerate(THIRDS) if c in SCORED]
    inband = stack[:, scored]
    print(f"\n  100-315 Hz: mean |error| {inband.mean():.2f} dB, worst {inband.max():.2f} dB")
    print(f"  RMS figure: mean |error| "
          f"{np.mean([abs(p['rms'] - m['rms']) for _n, m, p in figure_rows]):.2f} dB; "
          f"hole depth mean |error| "
          f"{np.mean([abs(p['hole_db'] - m['hole_db']) for _n, m, p in figure_rows]):.2f} dB")
    verdict = "USABLE" if inband.mean() <= 2.5 else "NOT USABLE"
    print(f"  VERDICT: leave-one-out at the seat is {verdict} "
          f"against the ~2.5 dB per band bar")
    np.save(w.SP / "wall1c-models.npy",
            np.asarray([models[m] for m in w.MICS]))
    (w.SP / "wall1c-loo.json").write_text(json.dumps(
        {"mean_inband": float(inband.mean()), "worst_inband": float(inband.max()),
         "per_band_mean": [float(v) for v in stack.mean(axis=0)],
         "figures": [[n, m, p] for n, m, p in figure_rows]}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
