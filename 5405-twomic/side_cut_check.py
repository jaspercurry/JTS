#!/usr/bin/env python3
"""Is the side mic's cut time-consistent with the main mic's own capture?

The side recording is one long WAV cut by the speaker journal, so a bad cut or
a slipped clock shows up as INCOHERENCE: the pair's measured sum stops matching
the sum of its two solo segments. ``superposition_residual_db`` is exactly that
disagreement, so the test is that the side mic's per-band residual is the same
ORDER as the main mic's at the same pose -- not equal, because the two mics
stand in different places, but not several times larger either.

Also printed: the arrival gap both mics read. The main mic stands in front and
the side mic behind, so the two gaps should carry OPPOSITE signs and similar
magnitudes; a side gap that does not is a cut landing on the wrong playback.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

CHECK_BAND_HZ = (80.0, 250.0)
#: A MEDIAN side residual this many times the main mic's, over the bands above,
#: is a cut fault. The median, not the mean: a cut fault is broadband (every
#: band loses coherence at once), while one band several times worse than its
#: neighbours at EVERY pose is the side position's own physics or its lower
#: SNR, and a mean cannot tell those two apart. The worst single band is
#: printed beside the verdict rather than folded into it.
RATIO_CEILING = 3.0
#: The schedule says where each playback started; the correlator says where it
#: found the sweep. Over one round those two may disagree by the journal's own
#: ``action=start`` latency, which is tens of milliseconds, but the disagreement
#: must not GROW -- a growing one is a slipping clock.
DRIFT_CEILING_MS = 60.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = json.loads((args.out_dir / "summary.json").read_text())
    mics = summary["mics"]
    if "side" not in mics:
        raise SystemExit("side_cut_check: this run has no side mic")

    print("=== per-band superposition residual, dB (low = the pair's sum matches its parts)")
    bands = [band["band_hz"] for band in next(iter(mics["main"].values()))["bands"]]
    print("  pose                             mic  " + "".join(f"{lo:>7.0f}" for lo, _ in bands))
    worst = (0.0, "")
    for pose in sorted(mics["main"]):
        held = {}
        for mic in ("main", "side"):
            row = mics[mic][pose]
            values = [band["superposition_residual_db"] for band in row["bands"]]
            held[mic] = np.array([np.nan if v is None else v for v in values])
            print(f"  {pose:32s} {mic:4s} " + "".join(f"{v:7.2f}" for v in held[mic]))
        inside = [i for i, (lo, hi) in enumerate(bands)
                  if lo >= CHECK_BAND_HZ[0] and hi <= CHECK_BAND_HZ[1]]
        ratio = float(np.nanmedian(held["side"][inside]) / np.nanmedian(held["main"][inside]))
        band_worst = int(np.nanargmax(held["side"][inside]))
        if ratio > worst[0]:
            worst = (ratio, pose)
        print(f"  {'':32s} median ratio side/main over "
              f"{CHECK_BAND_HZ[0]:g}-{CHECK_BAND_HZ[1]:g} Hz: {ratio:.2f}"
              f"   worst single band {bands[inside[band_worst]][0]:.0f} Hz "
              f"({held['side'][inside][band_worst]:.2f} dB)")
    print(f"\n  worst median ratio {worst[0]:.2f} at {worst[1]} (ceiling {RATIO_CEILING:g}): "
          f"{'PASS' if worst[0] <= RATIO_CEILING else 'FAIL -- suspect the cut'}")

    print("\n=== arrival gap (rear minus front at that mic), ms")
    print("  pose                             main      side      main pol   side pol")
    for pose in sorted(mics["main"]):
        rows = [mics[mic][pose] for mic in ("main", "side")]
        print(f"  {pose:32s} {rows[0]['arrival_gap']['ms']:+8.4f}  {rows[1]['arrival_gap']['ms']:+8.4f}"
              f"  {rows[0]['rear_polarity']['state']:10s} {rows[1]['rear_polarity']['state']}")
    signs = {np.sign(mics[mic][pose]["arrival_gap"]["ms"])
             for pose in mics["main"] for mic in ("main", "side")}
    print(f"  the two mics' gaps carry {'OPPOSITE' if len(signs) == 2 else 'THE SAME'} signs"
          f" -- opposite is what a front mic and a behind mic should read")

    print("\n=== where the cut landed: schedule against correlator, over the whole round")
    rows = [row for row in summary["takes"] if row["mic"] == "side"]
    if not rows:
        return 0
    rate = 48000.0
    origin = rows[0]["journal_start_epoch"]
    seconds, offsets = [], []
    for row in rows:
        predicted = (row["journal_start_epoch"] - row["side_start_epoch"]) * rate
        seconds.append(row["journal_start_epoch"] - origin)
        offsets.append(row["cut_first_sample"] + row["sweep_located_start"] - predicted)
    slope, intercept = np.polyfit(seconds, offsets, 1) if len(rows) > 1 else (0.0, offsets[0])
    residual = np.asarray(offsets) - (slope * np.asarray(seconds) + intercept)
    print("  take       t, s    found-predicted, ms   fit residual, ms   anchor  min locate  worst resid, ms")
    for row, second, offset, left in zip(rows, seconds, offsets, residual):
        print(f"  {row['take_id'][-9:]} {second:7.1f} {offset / rate * 1e3:15.1f} "
              f"{left / rate * 1e3:+18.1f}   {row['anchor_confidence']:.3f}   "
              f"{row['min_locate_confidence']:.3f}   {row['worst_residual_ms']:8.3f}")
    worst_ms = float(np.max(np.abs(residual))) / rate * 1e3
    print(f"  slope {slope / rate * 1e6:+.1f} ppm nominal over {seconds[-1]:.0f} s, "
          f"worst fit residual {worst_ms:.1f} ms (ceiling {DRIFT_CEILING_MS:g}): "
          f"{'PASS' if worst_ms <= DRIFT_CEILING_MS else 'FAIL'}")
    print("  the scatter is journal action=start latency, not the clock: read it as "
          "'no drift or dropout larger than this is present'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
