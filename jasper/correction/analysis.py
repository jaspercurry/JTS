"""Frequency-response smoothing and resampling for the magnitude
response display + filter design.

We use power-mean (RMS-of-amplitude) smoothing rather than dB-mean,
because dB-mean over-emphasizes deep nulls — Toole (Sound Reproduction
3rd ed., Ch. 4) and Welti are clear on this. REW and Acourate also
default to power-mean.

For the V1 scope (modal-range PEQ from a single-position measurement)
we don't need RT60 / Schroeder estimation — that's Phase 2+ when
multi-position averaging adds value. So this module is intentionally
small.
"""
from __future__ import annotations

import numpy as np


def smooth_fractional_octave(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    fraction: int = 48,
) -> np.ndarray:
    """1/N-octave magnitude smoothing in linear power.

    Args:
      freqs: linear-spaced frequency grid (e.g. from rfftfreq), in Hz.
      magnitude_db: matching magnitude in dB.
      fraction: 1/N-octave. 48 ≈ "psychoacoustic detail" (REW
        terminology). 3 = audiometric. Higher fractions are sharper.

    Returns:
      Smoothed magnitude in dB on the same grid.
    """
    if fraction <= 0:
        raise ValueError(f"fraction must be positive, got {fraction}")
    if len(freqs) != len(magnitude_db):
        raise ValueError(
            f"length mismatch: freqs={len(freqs)} magnitude={len(magnitude_db)}"
        )
    # dB → linear power for averaging (the right thing for
    # acoustic energy — dB-mean would over-emphasize deep nulls).
    power = 10.0 ** (magnitude_db / 10.0)
    factor = 2.0 ** (1.0 / (2.0 * fraction))

    # The straightforward implementation is O(N * window_size) which
    # at N=24k bins (rfft of 48k) and a half-octave window of ~50
    # bins is ~1.2M ops. That's fast enough (<10 ms in numpy) that
    # we don't bother with cumulative-sum tricks; clarity wins.
    smoothed = np.empty_like(power)
    n = len(freqs)
    for i in range(n):
        f = freqs[i]
        if f <= 0:
            smoothed[i] = power[i]
            continue
        lower = f / factor
        upper = f * factor
        # Linear bins in `freqs`, so use binary-search bounds.
        lo_idx = int(np.searchsorted(freqs, lower, side="left"))
        hi_idx = int(np.searchsorted(freqs, upper, side="right"))
        lo_idx = max(0, lo_idx)
        hi_idx = max(lo_idx + 1, min(n, hi_idx))
        smoothed[i] = float(np.mean(power[lo_idx:hi_idx]))

    # Clamp before log to avoid -inf for any all-zero windows.
    return 10.0 * np.log10(np.maximum(smoothed, 1e-12))


def resample_log(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    *,
    f_min: float = 20.0,
    f_max: float = 20000.0,
    n_points: int = 480,
) -> tuple[np.ndarray, np.ndarray]:
    """Resample a frequency response onto a log-spaced grid.

    Browser uPlot / canvas charts want log-frequency display, and
    480 points across 20 Hz – 20 kHz is roughly 1/48-octave —
    enough detail for the modal range and tractable for a JSON
    payload to the iPhone.
    """
    if n_points < 2:
        raise ValueError(f"n_points must be ≥ 2, got {n_points}")
    if f_max <= f_min:
        raise ValueError(f"f_max ({f_max}) must be > f_min ({f_min})")

    log_freqs = np.geomspace(f_min, f_max, n_points)
    interp = np.interp(log_freqs, freqs, magnitude_db)
    return log_freqs.astype(np.float64), interp.astype(np.float64)


def normalize_to_band(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    *,
    f_low: float = 200.0,
    f_high: float = 1000.0,
) -> np.ndarray:
    """Normalize a magnitude response so its average dB level across
    [f_low, f_high] is 0.

    Why: a measured response has arbitrary absolute level (mic gain,
    speaker SPL, distance). What matters for filter design is the
    SHAPE relative to the target. We anchor at the 200–1000 Hz
    midband — where speaker directivity is well-controlled and the
    iPhone-mic compensation is most accurate.

    Returns the magnitude shifted so band-mean = 0 dB.
    """
    band = (freqs >= f_low) & (freqs <= f_high)
    if not band.any():
        # Fall back to the full-range mean. Shouldn't hit this in
        # practice — our resample_log range covers 20–20k.
        ref = float(np.mean(magnitude_db))
    else:
        ref = float(np.mean(magnitude_db[band]))
    return (magnitude_db - ref).astype(np.float64)
