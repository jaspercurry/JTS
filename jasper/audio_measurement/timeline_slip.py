# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Timeline-slip detection: one discrete step in a capture's own clock (#1765).

A capture can gain or lose a whole handful of samples partway through, with
nothing anywhere reporting it. The USB isochronous case is measured: the
Stage-0 timing bank (jts3, 2026-08-19, 577 captures) found **3 captures in
577 — 0.5 %** whose two reference pilots sat a whole number of extra samples
apart, of which one was ``+7.008`` samples (146 us) and one ``+1.986``
samples (41 us), **with no ALSA error of any kind**. Both estimators there
agreed independently, and a template-free route reproduced the larger one to
0.126 us, so the audio content really moved; nothing merely mis-measured it.

**Why that matters here and not merely as trivia.** The MEASURE program
interleaves per-driver sweeps — ``w1 t1 w2 t2 w3 t3`` — and the whole
``M*C/P`` composition downstream trusts that a woofer sweep and a tweeter
sweep captured 8 s apart share one timeline. A slip landing *between* two of
those segments silently biases woofer-vs-tweeter relative timing. At 2 kHz,
41 us is ~30 degrees of relative phase against a 10-15 degree summation
budget, so a slip this size is not a rounding concern: it is the whole budget
twice over.

**The step class is not hypothetical on this rig either.** The 2026-07-27
forensic (#1765) recovered a JTS3 capture from its retained dump and
cross-correlated it (ncc 0.92-0.99) against two captures that PASSED minutes
later on the same program and rig: five of its six located sweeps agreed
within 0.8 samples while the first woofer sweep sat 64.3 samples apart — a
single **+64-sample (1.333 ms) insertion between ``sweep_w`` and
``sweep_t``**, i.e. squarely between two per-driver segments. That one step
predicts every number the capture reported (the woofer pair straddles it:
32.1 + 64/1_036_800 = 93.8 ppm against 94.118 observed; the tweeter pair sits
entirely after it, so 32.097 ppm is the TRUE drift). A +64 step was large
enough for the shipped guards to catch; the Stage-0 bank's +1.986 is the same
fault about 32x smaller, and that one they do not catch.

**What already existed, and the one thing that was wrong with it.**
:mod:`jasper.audio_measurement.program_analysis` has carried a single-step
timeline fit since #1765 and it has the right *model* — one constant per role
(absorbing each driver's own acoustic delay), one shared clock slope, and one
step at the best interior cut, admitted only when it also explains the
capture. What it did not have was a sharp enough *input*: it read the integer
``located_start``, whose measured noise on clean hardware is 2.00-3.13
samples, so its floor sat at 4.0 (``DISCONTINUITY_MIN_SAMPLES``, retired
into this module's one admission rule) and it was wired as
diagnostic-only. That is the entire gap this module closes — the model was
never the problem, the quantization was. Fed sub-sample positions (the
occurrence-vs-occurrence estimator, whose measured noise on the same corpus
is 0.038-0.299 samples), the same fit gates at 1.5 samples instead. In the
terms that matter to a capture — the smallest whole-sample slip actually
rejected — that moves the floor from 4 samples to 2, a **2x** sharpening.
A modest multiplier, deliberately stated as one: see ``SLIP_GATE_SAMPLES``
for the measured operating point, and for the one-sample bar this structure
cannot reach at all.

**Why not simply lower the residual guard's threshold instead.** The
shipped ``GLITCH_RESIDUAL_SAMPLES`` = 1.5 guard reduces a role's occurrences
to a demeaned *spread*, so a step of ``s`` shows up as a fraction of ``s``
(the pins: +4 samples reads 2.0, +2 reads 1.0) and there is no test that the
step EXPLAINS the data. Lowering that number trades slip sensitivity directly
against locate noise with nothing arbitrating, which is exactly the D7
incident — eight physically clean captures rejected, ~13 minutes of hardware
time and one round's retake budget lost. This module instead estimates the
step's size *directly* and requires it to explain the capture, so it buys
sensitivity from structure rather than from a looser threshold. The residual
guard is deliberately left exactly as D7 calibrated it.

Pure and hardware-free on purpose: everything here is plain sequences of
floats, never an ``ExcitationProgram`` or a ``SegmentLocation``. The caller
owns measuring positions out of a capture; this module owns the model, its
thresholds, and the verdict.
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
#: reuse of ``residual_desync``: that name belongs to the spread guard, whose
#: +4-rejects / +2-passes pins are load-bearing history, and a reader who sees
#: one name for two instruments cannot tell which one fired.
GLITCH_INPUT_TIMELINE_SLIP = "timeline_slip"

#: The step model must also EXPLAIN the capture, not merely absorb one extra
#: degree of freedom: its residual sum of squares must fall to this fraction
#: of the no-step fit's. This is the false-positive arbiter the spread guard
#: never had, and it is why the gate below can sit under the 4.0 floor the
#: integer-fed fit needed without inheriting the D7 false-rejection failure
#: mode.
STEP_RSS_RATIO = 0.25

#: The gating tolerance, in samples, on a SUB-SAMPLE-fed fit.
#:
#: MEASURED, not chosen. The battery in
#: ``tests/test_audio_measurement_timeline_slip.py`` sweeps clean captures
#: across locate-noise levels and records both this fit's spurious-step rate
#: and its detection rate for real steps. Calibration of the sweep against
#: hardware: D7 measured the shipped spread statistic at 0.038-0.299 samples
#: (mean 0.116) on 28 real captures, and simulating per-point locate noise at
#: sd = 0.10 reproduces that same statistic (mean 0.126, max 0.330 over 4000
#: draws) — so sd = 0.10 IS the hardware's operating point, and the sd = 0.20
#: / 0.30 rows of the battery are stress margin two to three times beyond any
#: capture ever observed.
#:
#: Every figure below is measured through THIS module's own
#: :func:`slip_rejects_capture` — gate and RSS rule together, n = 4000 clean
#: captures per row. (An earlier draft of this block quoted a script that
#: applied the size rule WITHOUT the RSS rule, so its numbers described
#: something that does not run. Stated because the premise here is "measured,
#: not chosen", and a figure from the wrong code path fails that premise
#: exactly as a guessed one would.)
#:
#: False rejection on clean captures, by locate-noise sd:
#: **0.00 %** at 0.05, **0.00 %** at 0.10 (the operating point), **0.00 %** at
#: 0.20, and **0.78 %** at 0.30. Detection at the operating point:
#: **99.83 %** of +1.986-sample slips and **100.00 %** of +7.008 — the two the
#: Stage-0 bank actually recorded.
#:
#: What this buys, stated as the quantity that changed: the shipped spread
#: guard reduces a step to roughly half its size before comparing against
#: ``GLITCH_RESIDUAL_SAMPLES`` = 1.5 (its own pins: a +4-sample insertion
#: reads 2.0, a +2 reads 1.0), so it only rejects ABOVE a step of 3.0
#: samples — in the integer-insertion pins it fires at +4 and not at +3, so
#: 4 samples (83 us) is the floor a whole-sample slip actually meets. Gating
#: the step's own size at 1.5 halves that to 2 samples (41 us), which is what
#: brings the +1.986-sample slip inside the net.
#:
#: The limit, because it is not the one originally asked for: **one-sample
#: slips are NOT reliably caught** (0.20 % at the operating point, same n).
#: Reaching a 1-sample bar would need a gate near 0.6, and that gate
#: false-rejects **15.05 %** of clean captures at sd = 0.20 and **33.33 %** at
#: sd = 0.30 — the D7 failure mode rebuilt on purpose, and worse than D7's
#: own eight-in-twenty-eight. The cost is already unacceptable one full
#: stress row BELOW the worst case, which is the part that settles it. Six
#: locate positions and a best-of-five cut search do not carry the
#: information for it; that requires adding signal to the capture (the
#: Stage-0 dual timing pilot resolves 0.002 samples because it measures a
#: known 2.2 s playback spacing at high SNR), which is a program change, not
#: an analysis one.
#:
#: Raise this and the +1.986 class starts passing again; lower it and the
#: fit's own noise starts rejecting clean captures. Both directions are
#: pinned by that test module.
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

    **Read the MAGNITUDE; do not trust the sign or the cut.** Measured on the
    interleaved ``w1 t1 w2 t2 w3 t3`` layout (the battery's attribution
    table): ``abs(step_samples)`` is recovered correctly 99-100 % of the time
    at every cut, but when the slip lands at an EVEN schedule index — where
    the affected set spans whole woofer/tweeter pairs and the role constants
    admit an equally good mirror solution — the reported sign and
    ``cut_index`` are right only about half the time. At odd cuts both are
    exact. This costs the gate nothing, because
    :func:`slip_rejects_capture` reads magnitude alone; it does mean the
    signed value and the cut are diagnostic breadcrumbs for a forensic reader,
    never inputs to a decision. Pinned by
    ``test_slip_location_and_sign_are_ambiguous_at_even_cuts``.
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
    tweeter-vs-woofer offset can never read as a step; ``eps`` is shared and
    is fitted here independently of any repeat-pair baseline precisely BECAUSE
    a step corrupts that baseline. Every interior cut is tried and the best
    residual wins, subject to the two admission rules the module constants
    own.

    Note what the model cannot see, because it bounds the claim: a step that
    falls before every occurrence of a role is absorbed entirely by that
    role's own constant. Under the interleaved ``w1 t1 w2 t2 w3 t3`` layout
    that is only ever true of a step before ``w1`` or after ``t3`` — one is
    indistinguishable from a late capture start and the other affects no
    measured segment — so every slip that can bias a woofer-vs-tweeter
    comparison is visible to at least one role.
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

    # Divide-by-zero guard only. It is genuinely unreachable on real input:
    # even a noiseless synthetic capture leaves `no_step_rss` tiny-POSITIVE
    # from float rounding rather than exactly 0.0, so the ratio is computed
    # there (measured at 2.91 on such a capture, i.e. failing the RSS rule and
    # reporting clean, which is the right answer for a capture with no step).
    # Reporting 1.0 for a true zero is the same "clean" answer by another
    # route, so neither arm can manufacture a finding.
    ratio = 1.0 if no_step_rss <= 0.0 else best_rss / no_step_rss
    return TimelineStepFit(
        step_samples=best_step,
        cut_index=best_cut,
        rss_ratio=ratio,
        resolvable=True,
    )


def slip_rejects_capture(fit: TimelineStepFit) -> bool:
    """Whether ``fit`` is a timeline slip big enough to reject the capture.

    Both admission rules apply, and the conjunction is the point: size alone
    is what the spread guard already has, and explanatory power alone would
    flag any capture whose noise happens to be step-shaped. An unresolvable
    fit never rejects — too few sweeps is missing evidence, never a fault, the
    same "``None`` means not reported" posture the capture-integrity vocabulary
    takes everywhere else.
    """
    if not fit.resolvable:
        return False
    return (
        abs(fit.step_samples) >= SLIP_GATE_SAMPLES
        and fit.rss_ratio <= STEP_RSS_RATIO
    )
