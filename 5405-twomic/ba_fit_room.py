"""Fit one room correction per base tune (A0, B0) from round BA1.

Recipe, identical for both:
  target   = straight line in dB vs log f, least squares over 60 Hz-8 kHz of the
             1-OCTAVE-smoothed mean-of-3-front-poses response
  fit on   = the 1/6-octave-smoothed mean-of-3-poses response
  band     = 40-500 Hz only
  filters  = <= 6 Peaking, 1.0 <= Q <= 5, cuts to -8 dB, boosts to +4 dB,
             sum of positive gains <= 6 dB (contract room max_total_boost_db)
"""
from __future__ import annotations

import json
import sys

import numpy as np
from scipy.optimize import least_squares

from ba_lib import POSES3, SP, curve, filter_response_db, load, peaking, smooth

TREND_LO, TREND_HI = 60.0, 8000.0
BAND_LO, BAND_HI = 40.0, 500.0
MAX_FILTERS = 6
Q_LO, Q_HI = 1.0, 5.0
CUT_DB, BOOST_DB, TOTAL_BOOST_DB = -8.0, 4.0, 6.0


def mean_front(result, fp8):
    curves = []
    for pose in POSES3:
        freqs, mags = curve(result, "main", pose, fp8)
        curves.append(mags)
    return freqs, np.mean(curves, axis=0)


def trend_line(freqs, mags):
    """Least-squares line in dB vs log2 f over the trend band."""
    sel = (freqs >= TREND_LO) & (freqs <= TREND_HI)
    octave = smooth(freqs, mags, 1.0)
    slope, intercept = np.polyfit(np.log2(freqs[sel]), octave[sel], 1)
    return slope * np.log2(freqs) + intercept, float(slope), float(intercept)


def unpack(x, n):
    out = []
    for i in range(n):
        f, g, q = x[3 * i: 3 * i + 3]
        out.append(peaking(10 ** f, g, q))
    return out


def fit(freqs, want, n):
    """`want` = target - measured, fitted with n Peaking bells inside the band."""
    sel = (freqs >= BAND_LO) & (freqs <= BAND_HI)
    centres = np.geomspace(BAND_LO * 1.15, BAND_HI / 1.15, n)
    seeds = []
    for c in centres:
        i = int(np.argmin(np.abs(freqs - c)))
        seeds.append((c, float(np.clip(want[i], CUT_DB, BOOST_DB)), 2.0))
    x0 = np.array([v for s in seeds for v in (np.log10(s[0]), s[1], s[2])])
    lo = np.array([v for _ in seeds for v in (np.log10(BAND_LO), CUT_DB, Q_LO)])
    hi = np.array([v for _ in seeds for v in (np.log10(BAND_HI), BOOST_DB, Q_HI)])

    def resid(x):
        got = filter_response_db(unpack(x, n), freqs)
        penalty = max(0.0, sum(max(0.0, x[3 * i + 1]) for i in range(n)) - TOTAL_BOOST_DB)
        return np.concatenate([(got - want)[sel], [20.0 * penalty]])

    out = least_squares(resid, x0, bounds=(lo, hi), max_nfev=40000)
    return unpack(out.x, n)


def rms(freqs, dev, lo=BAND_LO, hi=BAND_HI):
    sel = (freqs >= lo) & (freqs <= hi)
    return float(np.sqrt(np.mean(dev[sel] ** 2)))


def fit_one(result, fp8, tag):
    freqs, mean = mean_front(result, fp8)
    target, slope, intercept = trend_line(freqs, mean)
    sixth = smooth(freqs, mean, 1 / 6.0)
    # Clamp the demand to what the limits can actually deliver: a straight-line
    # target asks for ~9 dB at 40 Hz, which is the cabinet's own roll-off, and an
    # unclamped pull there spends the whole boost budget on a hole it cannot fill.
    want = np.clip(target - sixth, CUT_DB, BOOST_DB)
    want = np.where((freqs >= BAND_LO) & (freqs <= BAND_HI), want, 0.0)
    best = None
    for n in (MAX_FILTERS,):
        filters = fit(freqs, want, n)
        got = filter_response_db(filters, freqs)
        left = rms(freqs, sixth + got - target)
        if best is None or left < best[1] - 0.05:
            best = (filters, left, n)
    filters, left, n = best
    got = filter_response_db(filters, freqs)
    boosts = [f["parameters"]["gain"] for f in filters if f["parameters"]["gain"] > 0]
    row = {
        "tag": tag, "fingerprint8": fp8, "n_filters": n,
        "filters": filters,
        "largest_boost_db": round(max(boosts, default=0.0), 3),
        "sum_abs_gain_db": round(sum(abs(f["parameters"]["gain"]) for f in filters), 3),
        "sum_boost_db": round(sum(boosts), 3),
        "trend_slope_db_per_octave": round(slope, 4),
        "trend_intercept_db": round(intercept, 4),
        "rms_before_db": round(rms(freqs, sixth - target), 3),
        "rms_after_modelled_db": round(left, 3),
    }
    return row, freqs, mean, sixth, target, got


def main():
    result = load(sys.argv[1])
    labels = json.loads(sys.argv[2])  # {"1b2915e5": "A0", "1f65d837": "B0"}
    out = {}
    for fp8, tag in labels.items():
        row, freqs, mean, sixth, target, got = fit_one(result, fp8, tag)
        out[tag] = row
        out.setdefault("_curves", {})[tag] = {
            "freqs_hz": [round(float(f), 4) for f in freqs],
            "mean3_db": [round(float(v), 4) for v in mean],
            "sixth_db": [round(float(v), 4) for v in sixth],
            "target_db": [round(float(v), 4) for v in target],
            "correction_db": [round(float(v), 4) for v in got],
        }
        print(f"{tag}: {row['n_filters']} filters, largest boost {row['largest_boost_db']:+.2f} dB, "
              f"sum|gain| {row['sum_abs_gain_db']:.2f} dB, slope {row['trend_slope_db_per_octave']:+.2f} dB/oct, "
              f"rms 40-500 Hz {row['rms_before_db']:.2f} -> {row['rms_after_modelled_db']:.2f} dB")
        for f in row["filters"]:
            p = f["parameters"]
            print(f"    Peaking {p['freq']:7.2f} Hz {p['gain']:+6.2f} dB q{p['q']:.3f}")
    (SP / "search" / "BA" / "room-fits.json").write_text(json.dumps(out, indent=1) + "\n")
    print("wrote room-fits.json")


if __name__ == "__main__":
    main()
