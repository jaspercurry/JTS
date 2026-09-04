# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Low-level signal helpers shared by every analysis phase."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from jasper.audio_measurement import calibration as calibration_mod, gating
from jasper.audio_measurement.alignment import (
    cross_correlation_alignment,
    parabolic_peak,
)
from .model import CLIP_ABS_THRESHOLD, CLIP_RUN_SAMPLES, DBFS_FLOOR

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve


def _peak_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return DBFS_FLOOR
    peak = float(np.max(np.abs(x)))
    if peak <= 0 or not math.isfinite(peak):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(peak))


def _has_clipped_run(
    x: np.ndarray, *, threshold: float = CLIP_ABS_THRESHOLD, run: int = CLIP_RUN_SAMPLES
) -> bool:
    """True if ``x`` has a run of ``run`` consecutive samples at ≥ full scale."""
    if x.size < run:
        return False
    at_fs = np.abs(x) >= threshold
    if not bool(np.any(at_fs)):
        return False
    # Longest run of True via reset-on-False cumulative counting.
    count = 0
    for flag in at_fs:
        if flag:
            count += 1
            if count >= run:
                return True
        else:
            count = 0
    return False


def _locate(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    sample_rate: int,
    max_capture_s: float,
):
    """Matched-filter ``stimulus`` in ``capture``; return the alignment result."""
    return cross_correlation_alignment(
        capture,
        stimulus,
        sample_rate=sample_rate,
        max_capture_s=max_capture_s,
    )


def _analytic_envelope(x: np.ndarray) -> np.ndarray:
    """Delegates to :func:`gating.analytic_envelope` — one implementation."""
    return gating.analytic_envelope(x)


def _subsample_separation(
    capture: np.ndarray,
    arrival_a: int,
    arrival_b: int,
    length: int,
) -> float:
    """Sub-sample separation ``arrival_b − arrival_a`` of two identical stimuli.

    Cross-correlates the two captured windows (same stimulus + same room IR, so
    the peak is sharp) and refines it on the upsampled analytic envelope —
    Gamper's repeat-ratio idea. Returns the refined ``(arrival_b − arrival_a)``.
    """
    from scipy.signal import correlate

    a = np.asarray(capture[arrival_a:arrival_a + length], dtype=np.float64)
    b = np.asarray(capture[arrival_b:arrival_b + length], dtype=np.float64)
    n = min(a.size, b.size)
    if n < 8:
        return float(arrival_b - arrival_a)
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    corr = correlate(b, a, mode="full", method="fft")
    env = _analytic_envelope(corr)
    peak = int(np.argmax(env))
    refined = parabolic_peak(env, peak)
    lag = refined - (n - 1)  # b ≈ a shifted right by lag
    return float((arrival_b - arrival_a) + lag)


def _complex_tf(
    ir: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int,
    calibration: "CalibrationCurve | None",
):
    """Complex TF of an IR on a fixed grid, with the mic cal folded in (real)."""
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    H = np.fft.rfft(ir, n=n_fft)
    if calibration is not None:
        correction_db = calibration_mod.apply_calibration_curve(
            freqs, np.zeros_like(freqs), calibration
        )
        H = H * np.power(10.0, correction_db / 20.0)
    return freqs.astype(np.float64), H


def _band_average_db(freqs: np.ndarray, magnitude_db: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        raise ValueError("overlap band has no frequency bins")
    power = np.power(10.0, magnitude_db[mask] / 10.0)
    return 10.0 * math.log10(max(float(np.mean(power)), 1e-12))
