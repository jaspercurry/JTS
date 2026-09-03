# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Timeline-slip detection: one discrete step in a capture's own clock (#1765).

A USB isochronous capture can gain or lose whole samples partway through with
no ALSA error of any kind: the Stage-0 timing bank (jts3, 577 captures) found
3 — 0.5 % — whose reference pilots sat +1.986 to +7.008 samples apart. That
matters because the MEASURE program interleaves per-driver sweeps
(``w1 t1 w2 t2 w3 t3``), so a step between two of them silently biases
woofer-vs-tweeter relative timing: at 2 kHz, 41 us is ~30 degrees of phase
against a 10-15 degree summation budget.

The shipped spread guard (``program_analysis.GLITCH_RESIDUAL_SAMPLES``) is
deliberately left as calibrated; this module estimates the step's size directly
and requires it to EXPLAIN the capture, buying sensitivity from structure
rather than from a looser threshold.

Pure and hardware-free: plain sequences of floats, never an
``ExcitationProgram`` or a ``SegmentLocation``. The caller owns measuring
positions out of a capture; this module owns the model, its thresholds and the
verdict.
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

#: The ``glitch_inputs`` key this check contributes. A NEW key rather than a
#: reuse of ``residual_desync``, which belongs to the spread guard: one name
#: for two instruments leaves a reader unable to tell which fired.
GLITCH_INPUT_TIMELINE_SLIP = "timeline_slip"

#: The step model must also EXPLAIN the capture, not merely absorb one extra
#: degree of freedom: its residual sum of squares must fall to this fraction of
#: the no-step fit's. The false-positive arbiter that lets the gate below sit
#: under the 4.0 floor an integer-fed fit needs.
STEP_RSS_RATIO = 0.25

#: The gating tolerance, in samples, on a SUB-SAMPLE-fed fit.
#:
#: MEASURED, not chosen, through this module's own :func:`slip_rejects_capture`
#: (gate and RSS rule together), n = 4000 clean captures per row. Hardware
#: operating point is a per-point locate noise of sd = 0.10, which reproduces
#: the 0.038-0.299-sample spread statistic measured on 28 real captures.
#:
#: False rejection on clean captures by locate-noise sd: 0.00 % at 0.05, 0.00 %
#: at 0.10 (the operating point), 0.00 % at 0.20, 0.78 % at 0.30. Detection at
#: the operating point: 99.83 % of +1.986-sample slips, 100.00 % of +7.008 —
#: the two the Stage-0 bank recorded.
#:
#: The limit: ONE-sample slips are not reliably caught (0.20 %). A gate near
#: 0.6 would reach them and false-rejects 15.05 % of clean captures at sd =
#: 0.20 — six locate positions and a best-of-five cut search do not carry the
#: information, which is a program change, not an analysis one.
#:
#: Raise this and the +1.986 class passes again; lower it and the fit's own
#: noise starts rejecting clean captures. Both directions are pinned by
#: ``tests/test_audio_measurement_timeline_slip.py``.
SLIP_GATE_SAMPLES = 1.5


@dataclass(frozen=True)
class TimelineStepFit:
    """One capture's best single-step timeline fit.

    ``step_samples`` is signed: positive means everything after the cut
    arrived LATE (the 2026-07-27 hardware shape). ``0.0`` with a
    ``cut_index`` of ``-1`` is the clean-capture reading, not a missing value.

    ``rss_ratio`` is ``best_rss / no_step_rss`` — how much of the capture's
    unexplained variance the step actually accounts for. ``resolvable`` is
    False when there were too few located sweeps to leave the model two
    degrees of freedom, in which case every other field is a default and must
    not be read as evidence of anything.

    **Read the MAGNITUDE; do not trust the sign or the cut.** On the
    interleaved ``w1 t1 w2 t2 w3 t3`` layout ``abs(step_samples)`` is recovered
    99-100 % of the time at every cut, but at an EVEN schedule index the role
    constants admit an equally good mirror solution and the sign and
    ``cut_index`` are right only about half the time. :func:`slip_rejects_capture`
    reads magnitude alone; the signed value and the cut are forensic
    breadcrumbs, never inputs to a decision.
    """

    step_samples: float = 0.0
    cut_index: int = -1
    rss_ratio: float = 1.0
    resolvable: bool = False


def fit_timeline_step(
    scheduled_starts: Sequence[float],
    located: Sequence[float],
    roles: Sequence[str],
) -> TimelineStepFit:
    """Fit ``located = c[role] + start*(1+eps) + step*[i >= cut]``.

    The three sequences are parallel and must already be in SCHEDULE order —
    the step is a fact about the capture's timeline, so the cut is searched
    over schedule position, never over a role's own occurrence order.

    One constant per role absorbs that driver's acoustic delay, so a real
    tweeter-vs-woofer offset can never read as a step; ``eps`` is shared and is
    fitted independently of any repeat-pair baseline, which a step corrupts.
    Every interior cut is tried and the best residual wins, subject to the two
    admission rules the module constants own.

    The bound on the claim: a step falling before every occurrence of a role is
    absorbed entirely by that role's constant. On the interleaved layout that is
    only a step before ``w1`` or after ``t3`` — a late capture start, and a step
    affecting no measured segment — so every slip that can bias a
    woofer-vs-tweeter comparison is visible to at least one role.
    """
    starts = np.asarray(scheduled_starts, dtype=np.float64)
    values = np.asarray(located, dtype=np.float64)
    role_list = list(roles)
    n = values.size
    if starts.size != n or len(role_list) != n:
        raise ValueError("scheduled_starts, located and roles must be parallel")
    distinct = sorted(set(role_list))
    # parameters = one per role + the shared slope + the step itself; two
    # degrees of freedom left over, or the fit is fitting noise.
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

    # Divide-by-zero guard only, unreachable on real input: even a noiseless
    # synthetic capture leaves `no_step_rss` tiny-POSITIVE from float rounding.
    # Reporting 1.0 for a true zero fails the RSS rule, the same "clean" answer
    # the computed ratio gives, so neither arm can manufacture a finding.
    ratio = 1.0 if no_step_rss <= 0.0 else best_rss / no_step_rss
    return TimelineStepFit(
        step_samples=best_step,
        cut_index=best_cut,
        rss_ratio=ratio,
        resolvable=True,
    )


def slip_rejects_capture(fit: TimelineStepFit) -> bool:
    """Whether ``fit`` is a timeline slip big enough to reject the capture.

    Both admission rules apply, and the conjunction is the point: size alone is
    what the spread guard already has, and explanatory power alone would flag
    any capture whose noise happens to be step-shaped. An unresolvable fit never
    rejects — too few sweeps is missing evidence, never a fault.
    """
    if not fit.resolvable:
        return False
    return (
        abs(fit.step_samples) >= SLIP_GATE_SAMPLES
        and fit.rss_ratio <= STEP_RSS_RATIO
    )
