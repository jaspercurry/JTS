# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How a feature is read off a magnitude curve — the optics every reader shares.

The smoothing fractions, the broad-tilt removal, the two spans a feature's own
size and width are read over, the pre-peak window lead, and the minimum-phase
section that SYNTHESIZES a feature of a measured shape. Nothing here windows,
deconvolves or decides; it is what a curve is put through before anything is
concluded from it.

It lives below its readers rather than inside one of them.
:mod:`.feature_classifier` (the classification instrument),
:mod:`.gate_sweep` (the window ladder) and :mod:`.close_reference` all read
features off curves, and the ladder is the classifier's own window verdict —
so the classifier consuming the ladder while the ladder consumed the
classifier's optics was a cycle, which
``tests/test_crossover_v2_journey.py::test_the_package_import_graph_stays_acyclic``
forbids (#2662's G1). One shared bottom breaks it, and it is the honest shape
anyway: two instruments reading one feature must read it the same way or their
numbers are about different things.

**The frame is not free.** Every constant here is a frame choice, and P1
measured one capture's one feature reading materially different depths under
each defensible frame (``captures/recommission-day2-2026-09-01/
p1-position-window/P1-REPORT.md`` sec 6). A number read through these is only
comparable with another read through the same ones, which is why
:func:`~.gate_sweep.frame_descriptor` publishes them beside every result.
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

#: Magnitude smoothing. 1/12 octave is fine enough to keep a feature's own
#: shape and coarse enough that grid noise does not become one.
MAGNITUDE_SMOOTH_FRACTION = 12

#: The broad tilt removed before a feature's size is read. One octave passes
#: the tilt and leaves a ~1/6-octave feature essentially intact; a half-octave
#: baseline would eat it.
DETREND_FRACTION = 1

#: Pre-peak lead for the PHASE window and for the ladder's rungs. A zero-lead
#: window starts at ``argmax(|ir|)`` and so splits the direct arrival's own
#: main lobe — harmless for magnitude, not harmless for phase, and at a
#: crossover the other driver's arrival can lead the peak. It is also
#: load-bearing and measured on the ladder: a zero-lead window truncates the
#: direct arrival's own low-frequency pre-ringing and reads a sub-500 Hz
#: feature many dB too deep (P1). The classifier measures the zero-lead
#: reading alongside and reports it as ``lead_sensitivity_us`` so the choice
#: is evidenced.
PHASE_GATE_LEAD_MS = 1.0

#: Half-width of a feature's own band.
FEATURE_HALF_OCT = 1.0 / 12.0

#: The neighbourhood a feature is read against, and the span a centre is
#: searched over. The two are not interchangeable: the ladder's null-model fit
#: searches the NARROWER of them, because the wider span walked off onto a
#: neighbouring feature on half the poses and fitted the model to the wrong
#: thing (P1).
NEIGHBOURHOOD_OCT = 1.0 / 3.0
CENTRE_SEARCH_OCT = 1.0 / 6.0

#: Width used when a feature never returns to half amplitude inside its search
#: span. Wide enough that the null model built from it is not accidentally
#: narrower than the feature, which is the error direction that makes a real
#: feature look reflection-contaminated.
_Q_FALLBACK = 4.0


def detrend(curve: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Remove the broad tilt so a narrow feature's own size is what is read."""
    return curve - smooth_fractional_octave(grid, curve, DETREND_FRACTION)


def read_feature(det_curve: np.ndarray, grid: np.ndarray, fc: float) -> float:
    """A feature's size: the mean of the detrended curve over its own band."""
    band = (grid >= fc * 2 ** -FEATURE_HALF_OCT) & (grid <= fc * 2 ** FEATURE_HALF_OCT)
    return float(np.mean(det_curve[band]))


def feature_q(det_curve: np.ndarray, grid: np.ndarray, fc: float) -> float:
    """Measured Q of a feature, from its own half-amplitude width.

    The gate null model is only as good as the width it assumes: too low a Q
    makes a speaker-own feature look reflection-contaminated by comparison. So
    the width is measured off the feature rather than guessed, and falls back
    to a wide-but-plausible value when the curve never returns to half
    amplitude inside the search span.
    """
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
