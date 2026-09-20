"""WALL1 figures: wall hole, trend RMS, and the rear woofer's per-band change.

The rig moved on 2026-09-20 ~14:00: cabinet back ~0.2 m from a wall, UMIK-2 on
the front arm at 0.81 m, Dayton iMM-6C FIXED at the listening seat ~2 m away and
~20 deg off axis. In every tool the mic named "side" is now the SEAT mic, in
front of the speaker. Nothing measured before that move is comparable.
"""
from __future__ import annotations

import json
import sys

import numpy as np

from ba_lib import POSE0, SP, curve, load, smooth

WALL_BAND = (120.0, 260.0)
TREND_BAND = (80.0, 350.0)
THIRDS = [63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630]
MICS = {"side": "seat (Dayton, 2 m, 20 deg off axis)", "main": "front (UMIK-2, 0.81 m)"}


def trend(freqs, mags, lo=60.0, hi=8000.0):
    """The trace's own broad trend: a line in dB vs log f on the 1-octave smooth."""
    sel = (freqs >= lo) & (freqs <= hi)
    octave = smooth(freqs, mags, 1.0)
    slope, intercept = np.polyfit(np.log2(freqs[sel]), octave[sel], 1)
    return slope * np.log2(freqs) + intercept


def wall_hole(freqs, mags):
    """Deepest dip inside 120-260 Hz against the trace's own trend."""
    sixth = smooth(freqs, mags, 1 / 6.0)
    dev = sixth - trend(freqs, mags)
    sel = (freqs >= WALL_BAND[0]) & (freqs <= WALL_BAND[1])
    idx = int(np.argmin(np.where(sel, dev, np.inf)))
    return round(float(dev[idx]), 2), round(float(freqs[idx]), 1)


def trend_rms(freqs, mags):
    sixth = smooth(freqs, mags, 1 / 6.0)
    dev = sixth - trend(freqs, mags)
    sel = (freqs >= TREND_BAND[0]) & (freqs <= TREND_BAND[1])
    return round(float(np.sqrt(np.mean(dev[sel] ** 2))), 2)


def third_octave(freqs, mags, centre):
    sel = (freqs >= centre / 2 ** (1 / 6)) & (freqs < centre * 2 ** (1 / 6))
    if not sel.any():
        return float(np.interp(centre, freqs, mags))
    return float(10 * np.log10(np.mean(10 ** (mags[sel] / 10))))


def normalise(freqs, mags, lo=500.0, hi=2000.0):
    """0 dB = this trace's own mean level over 500 Hz - 2 kHz."""
    sel = (freqs >= lo) & (freqs <= hi)
    return mags - float(np.mean(mags[sel]))


def main():
    result = load(sys.argv[1])
    labels = json.loads(sys.argv[2])
    out = {
        "rig": "cabinet back ~0.2 m from a wall; UMIK-2 front arm 0.81 m; "
               "Dayton at the listening seat ~2 m, ~20 deg off axis to the left",
        "mic_names": MICS,
        "normalisation": "every curve is its own level minus its mean over 500 Hz - 2 kHz",
        "wall_band_hz": list(WALL_BAND), "trend_band_hz": list(TREND_BAND),
        "wall_hole": {}, "trend_rms_db": {}, "per_band": {}, "curves": {},
    }
    for mic in MICS:
        out["wall_hole"][mic] = {}
        out["trend_rms_db"][mic] = {}
        out["curves"][mic] = {}
        for tag, fp8 in labels.items():
            freqs, mags = curve(result, mic, POSE0, fp8)
            depth, at_hz = wall_hole(freqs, mags)
            out["wall_hole"][mic][tag] = {"depth_db": depth, "freq_hz": at_hz}
            out["trend_rms_db"][mic][tag] = trend_rms(freqs, mags)
            out["curves"][mic][tag] = {
                "freqs_hz": [round(float(f), 4) for f in freqs],
                "norm_db": [round(float(v), 4) for v in normalise(freqs, mags)],
            }
        for against in ("B0", "C0"):
            if "A0" not in labels or against not in labels:
                continue
            fa, a = curve(result, mic, POSE0, labels["A0"])
            _, b = curve(result, mic, POSE0, labels[against])
            sa, sb = smooth(fa, a, 1 / 3.0), smooth(fa, b, 1 / 3.0)
            out["per_band"].setdefault(mic, {})[f"A0_minus_{against}"] = {
                str(c): round(third_octave(fa, sa, c) - third_octave(fa, sb, c), 2)
                for c in THIRDS}
    (SP / "graphs" / "wall1-numbers.json").write_text(json.dumps(out, indent=1) + "\n")
    for mic, name in MICS.items():
        print(f"== {name}")
        print("  tune      wall hole (dB @ Hz)   RMS vs trend 80-350 Hz")
        for tag in labels:
            hole = out["wall_hole"][mic][tag]
            print(f"  {tag:9s} {hole['depth_db']:+6.2f} @ {hole['freq_hz']:6.1f}      "
                  f"{out['trend_rms_db'][mic][tag]:5.2f}")
    for mic in MICS:
        for key, row in out["per_band"].get(mic, {}).items():
            print(f"{mic} {key}: " + " ".join(f"{k}:{v:+.1f}" for k, v in row.items()))
    print(SP / "graphs" / "wall1-numbers.json")


if __name__ == "__main__":
    main()
