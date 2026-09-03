# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How a feature is read off a magnitude curve — the optics every reader shares.

It sits below :mod:`.feature_classifier`, :mod:`.gate_sweep` and
:mod:`.close_reference` so all three read a feature the same way and the
import graph stays acyclic (#2662 G1). Every constant here is a frame
choice, so :func:`~.gate_sweep.frame_descriptor` publishes them beside
every result.
"""

from __future__ import annotations

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave

__all__ = [
    "CENTRE_SEARCH_OCT",
    "DETREND_FRACTION",
    "FEATURE_HALF_OCT",
    "MAGNITUDE_SMOOTH_FRACTION",
    "NEIGHBOURHOOD_OCT",
    "PHASE_GATE_LEAD_MS",
    "biquad_peaking",
    "detrend",
    "feature_q",
    "read_feature",
]

#: Magnitude smoothing. 1/12 octave keeps a feature's own shape while coarse
#: enough that grid noise does not become one.
MAGNITUDE_SMOOTH_FRACTION = 12

#: The broad tilt removed before a feature's size is read. One octave leaves a
#: ~1/6-octave feature essentially intact; a half-octave baseline would eat it.
DETREND_FRACTION = 1

#: Pre-peak lead for the PHASE window and for the ladder's rungs. Zero lead
#: splits the direct arrival's main lobe and truncates its low-frequency
#: pre-ringing, so a sub-500 Hz feature reads many dB too deep (P1).
PHASE_GATE_LEAD_MS = 1.0

#: Half-width of a feature's own band.
FEATURE_HALF_OCT = 1.0 / 12.0

#: The neighbourhood a feature is read against, and the span a centre is
#: searched over. The ladder's null-model fit searches the narrower: the
#: wider walked onto a neighbouring feature (P1).
NEIGHBOURHOOD_OCT = 1.0 / 3.0
CENTRE_SEARCH_OCT = 1.0 / 6.0

#: Width used when a feature never returns to half amplitude inside its
#: search span. Wide enough that the null model is not narrower than the
#: feature, the error direction that makes a real feature look contaminated.
_Q_FALLBACK = 4.0


def detrend(curve: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Remove the broad tilt so a narrow feature's own size is what is read."""
    return curve - smooth_fractional_octave(grid, curve, DETREND_FRACTION)


def read_feature(det_curve: np.ndarray, grid: np.ndarray, fc: float) -> float:
    """A feature's size: the mean of the detrended curve over its own band."""
    band = (grid >= fc * 2 ** -FEATURE_HALF_OCT) & (grid <= fc * 2 ** FEATURE_HALF_OCT)
    return float(np.mean(det_curve[band]))


def feature_q(det_curve: np.ndarray, grid: np.ndarray, fc: float) -> float:
    """Measured Q of a feature, from its own half-amplitude width."""
    search = (grid >= fc * 2 ** -NEIGHBOURHOOD_OCT) & (
        grid <= fc * 2 ** NEIGHBOURHOOD_OCT
    )
    freqs, values = grid[search], det_curve[search]
    if freqs.size < 3:
        return _Q_FALLBACK
    apex = int(np.argmax(np.abs(values)))
    height = values[apex]
    if height == 0:
        return _Q_FALLBACK
    below = (values / height) < 0.5
    lo, hi = freqs[0], freqs[-1]
    for i in range(apex, -1, -1):
        if below[i]:
            lo = freqs[i]
            break
    for i in range(apex, freqs.size):
        if below[i]:
            hi = freqs[i]
            break
    if hi <= lo:
        return _Q_FALLBACK
    return float(np.sqrt(lo * hi) / (hi - lo))


def biquad_peaking(
    f0: float, gain_db: float, q: float, sample_rate: int
) -> tuple[np.ndarray, np.ndarray]:
    """RBJ peaking EQ. Minimum phase by construction."""
    amp = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / sample_rate
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 + alpha * amp, -2 * np.cos(w0), 1 - alpha * amp])
    a = np.array([1 + alpha / amp, -2 * np.cos(w0), 1 - alpha / amp])
    return b / a[0], a / a[0]
