# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Timeline-slip detection: one discrete step in a capture's own clock (#1765). A USB
isochronous capture can gain/lose whole samples with no ALSA error (Stage-0 bank, jts3: 3/577
captures at +1.986 to +7.008 samples), biasing woofer-vs-tweeter timing in the interleaved
``w1 t1 w2 t2 w3 t3`` schedule (at 2 kHz, 41 us is ~30 degrees against a 10-15 degree budget).
The shipped spread guard (``program_analysis.GLITCH_RESIDUAL_SAMPLES``) stays calibrated
as-is; this module estimates the step directly and requires it to explain the capture. Pure and
hardware-free: plain float sequences, never an ``ExcitationProgram`` or ``SegmentLocation``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

__all__ = [
    "GLITCH_INPUT_TIMELINE_SLIP",
    "SLIP_GATE_SAMPLES",
    "STEP_RSS_RATIO",
    "TimelineStepFit",
    "fit_timeline_step",
    "slip_rejects_capture",
]

#: A NEW ``glitch_inputs`` key, distinct from the spread guard's ``residual_desync`` — one name
#: for two instruments would leave a reader unable to tell which fired.
GLITCH_INPUT_TIMELINE_SLIP = "timeline_slip"

#: The step must also EXPLAIN the capture: residual sum of squares must fall to this fraction
#: of the no-step fit's, the false-positive arbiter under the gate's 4.0 floor.
STEP_RSS_RATIO = 0.25

#: Gating tolerance in samples, MEASURED (not chosen) at n=4000 clean captures/row, locate-noise
#: sd=0.10 operating point (reproduces the 0.038-0.299-sample spread measured on 28 real
#: captures): 0.00/0.00/0.00/0.78 % false-reject at sd=0.05/0.10/0.20/0.30; 99.83 %/100.00 %
#: detection of the Stage-0 bank's +1.986/+7.008-sample slips. One-sample slips are not
#: reliably caught (0.20 %); both directions pinned by
#: ``tests/test_audio_measurement_timeline_slip.py``.
SLIP_GATE_SAMPLES = 1.5


@dataclass(frozen=True)
class TimelineStepFit:
    """``step_samples`` positive means everything after the cut arrived LATE; ``0.0``/``-1`` is
    the clean reading, not missing. ``resolvable=False`` means too few sweeps for two degrees of
    freedom — every other field is then a default. Read the MAGNITUDE only: at an EVEN schedule
    index a mirror solution makes sign/``cut_index`` right only about half the time."""

    step_samples: float = 0.0
    cut_index: int = -1
    rss_ratio: float = 1.0
    resolvable: bool = False


def fit_timeline_step(
    scheduled_starts: Sequence[float],
    located: Sequence[float],
    roles: Sequence[str],
) -> TimelineStepFit:
    """Fit ``located = c[role] + start*(1+eps) + step*[i >= cut]``. The three sequences must
    already be in SCHEDULE order — the cut is searched over schedule position, not occurrence
    order. Bound: a step before every occurrence of a role is absorbed entirely by that role's
    constant, which on this interleaved layout means only a step before ``w1``/after ``t3``
    escapes detection."""
    starts = np.asarray(scheduled_starts, dtype=np.float64)
    values = np.asarray(located, dtype=np.float64)
    role_list = list(roles)
    n = values.size
    if starts.size != n or len(role_list) != n:
        raise ValueError("scheduled_starts, located and roles must be parallel")
    distinct = sorted(set(role_list))
    # One param per role + shared slope + step; two degrees of freedom must remain.
    if n < len(distinct) + 4:
        return TimelineStepFit()

    column_of = {role: index for index, role in enumerate(distinct)}
    base = np.zeros((n, len(distinct) + 1))
    for row, role in enumerate(role_list):
        base[row, column_of[role]] = 1.0
    base[:, -1] = starts

    def _fit(design: np.ndarray) -> tuple[np.ndarray, float]:
        coef, *_ = np.linalg.lstsq(design, values, rcond=None)
        resid = values - design @ coef
        return coef, float(resid @ resid)

    _, no_step_rss = _fit(base)
    best_rss = math.inf
    best_step = 0.0
    best_cut = -1
    for cut in range(1, n):
        step_column = np.zeros((n, 1))
        step_column[cut:, 0] = 1.0
        coef, rss = _fit(np.hstack([base, step_column]))
        if rss < best_rss:
            best_rss, best_step, best_cut = rss, float(coef[-1]), cut

    # Divide-by-zero guard only; unreachable on real input. 1.0 fails the RSS rule either way.
    ratio = 1.0 if no_step_rss <= 0.0 else best_rss / no_step_rss
    return TimelineStepFit(
        step_samples=best_step,
        cut_index=best_cut,
        rss_ratio=ratio,
        resolvable=True,
    )


def slip_rejects_capture(fit: TimelineStepFit) -> bool:
    """Both admission rules apply: size alone is what the spread guard already has, and
    explanatory power alone would flag noise that happens to be step-shaped. An unresolvable
    fit never rejects — too few sweeps is missing evidence, not a fault."""
    if not fit.resolvable:
        return False
    return (
        abs(fit.step_samples) >= SLIP_GATE_SAMPLES
        and fit.rss_ratio <= STEP_RSS_RATIO
    )
