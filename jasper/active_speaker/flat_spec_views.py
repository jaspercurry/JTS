# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Honest re-readings of one already-graded flat-spec evaluation.

Three views, all derived from a :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`
that :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` already
produced. **Nothing here grades anything.** No view returns a pass/fail, no
view holds a tolerance, no view re-decides band membership, the reference
frame, or the trusted floor: every one of those is answered exactly once, by
the evaluator, and each view reads the answer off the report it is handed.
That is the same "derived from the report, not recomputed from the curve"
rule :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` and
:func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge` follow, and this
module follows it for the same reason — a second owner of any of those
decisions is a second thing to drift.

The session's ONE verdict stays
:attr:`~jasper.active_speaker.flat_spec.FlatSpecReport.overall_passed`,
produced by the one grader and verified by the one claim producer
(``crossover_v2_flow._verify_claims``, issue #1868). These views deliberately
do not carry it forward and deliberately do not manufacture one of their own:
they report **residuals**, which are measurements, not judgements.

Why they exist
--------------
The pooled residual a household reads is honest and still answers a question
nobody asked. Two properties of the shipped pooling make it so, and each
view removes exactly one of them:

1. **The grid is linear, the ear is logarithmic.** The cloud pipeline hands
   the evaluator the linear ``rfft`` frequency axis, so the number of graded
   bins in a band is proportional to its width in *hertz*, not in *octaves*.
   On the 2026-08-18 jts3 arm-run corpus that is 5,462 bins across the one
   octave 8-16 kHz against 1,121 bins across the 2.485 graded octaves of the
   250 Hz-2 kHz band — 5,462 vs 451 bins per octave, a **12.1:1**
   per-octave overweight of the top octave over the midrange.
   :func:`log_pooled_residual` re-pools the report's own per-band figures
   with equal weight per octave instead.
2. **Every position is pooled equally, whatever question it answers.**
   :func:`~jasper.audio_measurement.spatial_combine.combine_positions` is an
   unweighted power mean over the whole cloud — an on-axis position and a
   position out at the coverage edge enter the one headline number with the
   same weight, so an off-axis result a household will never sit in makes
   the on-axis result it does sit in look worse than it measures.
   :func:`role_split_flatness` reports each role separately and **never
   averages them together**; :func:`directivity_table` reports how far each
   position departs from on-axis, which is the question the off-axis
   captures were actually taken to answer.

Both are *views*, not fixes. Neither changes a graded verdict, and neither
claims to be a better grader — which anchor and which weighting the spec
*should* use is an open owner decision (issue #1857's Q-E,
``docs/attribution-stage-plan.md`` section 9), and moving it here would move
graded verdicts, which this module must never do.

Purity
------
numpy plus :mod:`jasper.active_speaker.flat_spec`. No I/O, no logging, no
JSON parsing, no product policy. Callers hand over typed, in-memory values;
reading them back out of a persisted receipt is the caller's job, not this
module's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from jasper.active_speaker.flat_spec import (
    BandResult,
    ConvergenceResidual,
    FlatSpecReport,
    evaluate_flat_spec,
    spec_convergence_residual,
)


def _pool(pairs: list[tuple[float, float]]) -> float | None:
    """``sqrt(sum w*r**2 / sum w)`` over ``(weight, rms)`` pairs, or ``None``.

    The one weighted-RMS identity every pooling in this module uses — bands
    by octave span, positions by bin count, positions by octave span — so
    three poolings cannot drift into three different means. It is the same
    identity
    :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` pools
    bands with; that function is still the one that pools the SHIPPED
    figure, and this never recomputes it.

    A non-positive total weight yields ``None``: no pooled figure exists,
    which is not the same as one of zero.
    """
    total = sum(weight for weight, _rms in pairs)
    if total <= 0.0:
        return None
    return math.sqrt(sum(weight * rms ** 2 for weight, rms in pairs) / total)


def _power_mean_scalar(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` over one array — the power (energy)
    mean, NOT a linear mean of dB values.

    Deliberately a local definition rather than an import: the equivalent
    private helper in :mod:`jasper.active_speaker.flat_spec` is that
    module's own, and reaching across a module boundary for a private name
    would couple this view to an implementation detail it does not own.
    Kept to one place *here* for the same reason that one exists there — the
    linear-mean-vs-power-mean confusion has shipped wrong in this repo
    before, so no caller in this module inlines it.
    """
    return float(10.0 * np.log10(np.mean(np.power(10.0, values_db / 10.0))))


def _band_octaves(band: BandResult) -> float:
    """How many octaves of spectrum this band actually GRADED.

    ``log2(f_hi_hz / graded_lo_hz)`` — the graded edge, not the nominal one,
    because a trusted-floor clamp (issue #2551) can take most of a band away
    and a weight computed from the nominal edge would then hand that band
    influence over a span nothing was measured in.
    :attr:`~jasper.active_speaker.flat_spec.BandResult.graded_lo_hz` is
    ``None`` on a report predating the clamp; there the nominal edge IS the
    graded edge, so it is the honest fallback rather than a guess.

    Returns ``0.0`` for any band whose span is empty, inverted, or
    non-finite — a weight of zero, which drops the band out of a weighted
    mean rather than letting a NaN swallow the whole pooled figure.
    """
    lo = band.graded_lo_hz if band.graded_lo_hz is not None else band.f_lo_hz
    hi = band.f_hi_hz
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo <= 0.0 or hi <= lo:
        return 0.0
    return math.log2(hi / lo)


@dataclass(frozen=True)
class BandWeight:
    """One band's contribution to each pooling, side by side.

    The audit trail for :class:`LogPooledResidual`: it is not enough to say
    the two pooled numbers differ, a reader has to be able to see *which*
    band the weighting moved and by how much. Both shares sum to 1.0 across
    the evaluable bands, so they compare directly.

    Args:
      f_lo_hz: the band's nominal lower edge — its
        :data:`~jasper.active_speaker.flat_spec.SPEC_BANDS` row.
      f_hi_hz: the band's upper edge (exclusive).
      graded_lo_hz: the edge the metrics were actually taken from, after the
        session's trusted-floor clamp. Equal to ``f_lo_hz`` when nothing
        clamped.
      octaves: ``log2(f_hi_hz / graded_lo_hz)`` — the span this band's
        residual speaks for, and its weight in the log pooling.
      n_bins: the band's non-excluded graded bin count — its weight in the
        shipped linear pooling.
      rms_deviation_db: the band's own RMS deviation, lifted verbatim from
        :attr:`~jasper.active_speaker.flat_spec.BandResult.rms_deviation_db`.
      linear_share: ``n_bins / sum(n_bins)`` — the fraction of the shipped
        pooled mean-square this band supplies.
      log_share: ``octaves / sum(octaves)`` — the same fraction under equal
        per-octave weight.
      bins_per_octave: ``n_bins / octaves``. The number the whole view is
        about: compare two bands' values to read the per-octave overweight
        the linear grid hands the higher one.
    """

    f_lo_hz: float
    f_hi_hz: float
    graded_lo_hz: float
    octaves: float
    n_bins: int
    rms_deviation_db: float
    linear_share: float
    log_share: float
    bins_per_octave: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "graded_lo_hz": self.graded_lo_hz,
            "octaves": self.octaves,
            "n_bins": self.n_bins,
            "rms_deviation_db": self.rms_deviation_db,
            "linear_share": self.linear_share,
            "log_share": self.log_share,
            "bins_per_octave": self.bins_per_octave,
        }


@dataclass(frozen=True)
class LogPooledResidual:
    """The pooled residual re-weighted to equal influence per octave.

    Args:
      rms_db: the log-pooled RMS deviation, dB. ``None`` when no band was
        evaluable — there is no residual, not a residual of zero, the same
        rule :class:`~jasper.active_speaker.flat_spec.ConvergenceResidual`
        follows.
      linear_rms_db: the SHIPPED pooled residual for the same report —
        :attr:`~jasper.active_speaker.flat_spec.ConvergenceResidual.rms_db`,
        lifted verbatim, never recomputed. Carried so the two numbers can
        never be quoted from two places and disagree.
      octaves: total graded octaves the log pooling averaged over.
      n_bins: total non-excluded graded bins the linear pooling averaged
        over — the denominator behind ``linear_rms_db``.
      bands: per-band weights, see :class:`BandWeight`. Evaluable bands
        only; an unevaluable band contributes to neither pooling and is
        counted in ``n_bands_not_evaluated`` instead of being silently
        dropped.
      n_bands_not_evaluated: how many of the report's bands had no residual
        to pool. A non-zero value means both pooled numbers speak for less
        of the spectrum than the band table suggests.
      evaluable: whether any band was poolable.
    """

    rms_db: float | None
    linear_rms_db: float | None
    octaves: float
    n_bins: int
    bands: tuple[BandWeight, ...]
    n_bands_not_evaluated: int
    evaluable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rms_db": self.rms_db,
            "linear_rms_db": self.linear_rms_db,
            "octaves": self.octaves,
            "n_bins": self.n_bins,
            "bands": [band.to_dict() for band in self.bands],
            "n_bands_not_evaluated": self.n_bands_not_evaluated,
            "evaluable": self.evaluable,
        }


def log_pooled_residual(report: FlatSpecReport) -> LogPooledResidual:
    """Re-pool one report's per-band residuals with **equal weight per
    octave** instead of equal weight per graded bin.

    The arithmetic, beside the shipped pooling it replaces. Let band ``b``
    have non-excluded bin count ``n_b = n_bins - n_excluded``, graded span
    ``w_b = log2(f_hi_b / graded_lo_b)`` octaves, and its own RMS deviation
    ``r_b``::

        shipped (per bin)    rms = sqrt( sum_b n_b * r_b**2 / sum_b n_b )
        this view (per oct)  rms = sqrt( sum_b w_b * r_b**2 / sum_b w_b )

    Both are honest weighted RMS values over the same per-band residuals;
    they differ only in what a band's influence is proportional to. The
    shipped form is exactly
    :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual`'s, and
    is not recomputed here — it is called, and its answer carried on
    :attr:`LogPooledResidual.linear_rms_db`, so the two numbers this view
    prints side by side can never come from two implementations.

    Why the shipped weighting reads the way it does: the cloud pipeline
    grades on the linear ``rfft`` axis, where ``n_b`` is proportional to the
    band's width in hertz. A band one octave wide at the top of the spectrum
    therefore holds an order of magnitude more bins than a band two-and-a-
    half octaves wide across the midrange, and the pooled number is
    dominated by the octave a listener is least sensitive to.
    :attr:`BandWeight.bins_per_octave` reports that density per band so the
    ratio is read off the data rather than asserted.

    **What this view does NOT fix, stated plainly.** It re-weights *between*
    bands, not *within* one. Each ``r_b`` is still the report's own
    per-bin-weighted RMS across its band, so inside a wide band the linear
    grid's tilt survives: on the 2026-08-18 corpus the 250 Hz-2 kHz band's
    graded range holds 1,121 bins of which the top octave (1-2 kHz) supplies
    683, about 61%. Removing that would mean re-deriving deviations from the
    curve, which would make this a second evaluator; it is deliberately not
    one. What the view removes is the dominant term — the 12:1 cross-band
    per-octave skew — and it names the residual one rather than implying it
    is gone.

    Args:
      report: the evaluation to re-read. Only its bands are consulted.

    Returns:
      A :class:`LogPooledResidual`. When no band carries a residual,
      ``rms_db`` is ``None`` and ``evaluable`` is ``False`` rather than a
      fabricated 0.0 — and ``linear_rms_db`` still reports whatever
      :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual`
      says for the same report, including its own ``None``.
    """
    linear = spec_convergence_residual(report)
    # (band, non-excluded bins, graded octaves, its own RMS) for every poolable
    # band. Walked once; the shares below need totals this walk produces. The
    # RMS rides along already narrowed to a float, so nothing downstream has to
    # re-test the `None` this loop has already excluded.
    poolable: list[tuple[BandResult, int, float, float]] = []
    n_not_evaluated = 0
    for band in report.bands:
        n_bins = band.n_bins - band.n_excluded
        octaves = _band_octaves(band)
        if band.rms_deviation_db is None or n_bins <= 0 or octaves <= 0.0:
            n_not_evaluated += 1
            continue
        poolable.append((band, n_bins, octaves, float(band.rms_deviation_db)))
    if not poolable:
        return LogPooledResidual(
            rms_db=None,
            linear_rms_db=linear.rms_db,
            octaves=0.0,
            n_bins=0,
            bands=(),
            n_bands_not_evaluated=n_not_evaluated,
            evaluable=False,
        )
    total_octaves = sum(octaves for _band, _n, octaves, _rms in poolable)
    total_bins = sum(n_bins for _band, n_bins, _o, _rms in poolable)
    return LogPooledResidual(
        rms_db=_pool([(octaves, rms) for _band, _n, octaves, rms in poolable]),
        linear_rms_db=linear.rms_db,
        octaves=total_octaves,
        n_bins=total_bins,
        bands=tuple(
            BandWeight(
                f_lo_hz=band.f_lo_hz,
                f_hi_hz=band.f_hi_hz,
                graded_lo_hz=(
                    band.graded_lo_hz if band.graded_lo_hz is not None else band.f_lo_hz
                ),
                octaves=octaves,
                n_bins=n_bins,
                rms_deviation_db=rms,
                linear_share=n_bins / total_bins,
                log_share=octaves / total_octaves,
                bins_per_octave=n_bins / octaves,
            )
            for band, n_bins, octaves, rms in poolable
        ),
        n_bands_not_evaluated=n_not_evaluated,
        evaluable=True,
    )


@dataclass(frozen=True)
class PositionCurve:
    """One cloud position's own analysed magnitude curve, ready to re-read.

    The member curve behind the aggregate — what the cloud banks per
    position beside the combined one. Deliberately NOT
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture`: that
    type is the combiner's input and requires a **uniform linear** grid
    (its smoothing kernel assumes one) plus a sample rate and an optional
    IR, none of which a already-smoothed log-spaced curve has or needs.

    Args:
      position_id: the position's own label, carried so a number can be
        named back to the spot it came from.
      role: what KIND of listening position this is, in the cloud's own
        vocabulary (``onax`` / ``offax`` / ``xovr``, owned by
        ``crossover_v2_flow.POSITION_ROLES``). Read off the position's
        record, never re-derived from a prompt string. This module never
        interprets the value beyond grouping equal ones together and
        matching the caller's chosen ``primary_role``; it defines no role
        constant of its own, so there is nothing here to drift from the
        owner.
      freqs_hz: the position's frequency axis, strictly ascending. The
        cloud's is log-spaced, but nothing here requires that — the views
        pass it to the evaluator, which masks by value.
      magnitude_db: matching magnitude, dB, as analysed: gated, calibrated
        and already smoothed.
      smoothing_fraction: the 1/N-octave fraction the caller attests
        ``magnitude_db`` was smoothed at. **Provenance, and load-bearing
        for interpretation**: the cloud smooths its per-position curves
        finer than the pooled spec curve (1/6 vs 1/3 octave on the
        2026-08-18 corpus), and a finer smoothing preserves ripple a
        coarser one averages away, so a per-position residual reads HIGHER
        than a same-speaker pooled one for that reason alone. Carried onto
        every result so the comparison is never made silently.
      degrees: the microphone angle this position was captured at, when
        known. ``None`` means **not recorded**, never zero. A cloud position
        record carries a role, not an angle, so a caller reading one back
        supplies this from wherever its session did record the angle — the
        capture run's own walk log, for a banked corpus. Every view degrades
        to role-only when it is absent, so nothing here requires it.
      take_id: which take survived for this position, when known. Carried
        for provenance only; nothing here reads it.
    """

    position_id: str
    role: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    smoothing_fraction: int
    degrees: float | None = None
    take_id: str = ""


def _exclusion_mask(
    freqs_hz: np.ndarray, intervals: tuple[tuple[float, float], ...],
) -> np.ndarray | None:
    """The report's own published exclusion intervals, applied to another axis.

    :attr:`~jasper.active_speaker.flat_spec.FlatSpecReport.excluded_intervals`
    is the merged ``(f_lo, f_hi)`` span of every bin the honesty screen
    removed from the combined curve. Re-expressing it as a mask on a
    position's grid is a **containment test against the published result**,
    not a second run of the screen: the screen is a mean-vs-median
    disagreement across the whole cloud and cannot even be evaluated for one
    position. Applying it keeps the per-role numbers graded on the same
    region of spectrum as the pooled one, which is the only way the two are
    comparable at all.

    Endpoints are inclusive on both sides, because the interval spans the
    first and last excluded bin of a run and a bin landing exactly on one of
    those frequencies is inside the flagged region, not beside it.

    Returns ``None`` when nothing is excluded, which is what
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` wants for
    "exclude nothing" — an all-``False`` array would work too, and the
    ``None`` just keeps the no-op case unmistakable.
    """
    if not intervals:
        return None
    mask = np.zeros(len(freqs_hz), dtype=bool)
    for f_lo, f_hi in intervals:
        mask |= (freqs_hz >= f_lo) & (freqs_hz <= f_hi)
    if not bool(mask.any()):
        return None
    return mask


@dataclass(frozen=True)
class PositionFlatness:
    """One position's residual pair, or the reason it has none.

    Args:
      position_id: the position's label.
      role: its role, verbatim.
      degrees: its angle when recorded, else ``None`` (**not recorded**, not
        zero).
      take_id: provenance, verbatim.
      smoothing_fraction: what the curve was attested to be smoothed at —
        carried onto the result because a residual is not comparable across
        two smoothing fractions. See :class:`PositionCurve`.
      rms_db: this position's own pooled residual on the linear per-bin
        weighting, so it is the same *kind* of number as the shipped
        headline. ``None`` when not evaluated.
      log_rms_db: the same position re-pooled per octave —
        :func:`log_pooled_residual` of its own evaluation. ``None`` when not
        evaluated.
      n_bins: graded bins behind ``rms_db``.
      n_excluded: graded-band bins the report's published exclusion
        intervals removed from this position.
      evaluable: whether a residual exists at all.
      not_evaluated_reason: why not, when ``evaluable`` is ``False``. Always
        a non-empty string in that case and always empty otherwise, so a
        consumer can never read a missing residual as a zero one and can
        never be left without a cause. This mirrors, at view level, the
        first-class ``CLAIM_NOT_EVALUATED`` outcome the claim producer
        keeps distinct from pass and fail (issue #1868).
    """

    position_id: str
    role: str
    degrees: float | None
    take_id: str
    smoothing_fraction: int
    rms_db: float | None
    log_rms_db: float | None
    n_bins: int
    n_excluded: int
    evaluable: bool
    not_evaluated_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "role": self.role,
            "degrees": self.degrees,
            "take_id": self.take_id,
            "smoothing_fraction": self.smoothing_fraction,
            "rms_db": self.rms_db,
            "log_rms_db": self.log_rms_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
        }


@dataclass(frozen=True)
class RoleFlatness:
    """Every position sharing one role, pooled — and never beyond it.

    Args:
      role: the role these positions share, verbatim.
      rms_db: the role's pooled residual, ``sqrt(sum_i n_i * r_i**2 / sum_i
        n_i)`` over its evaluated positions — the same bin-weighted identity
        :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual`
        pools bands with, applied across positions. ``None`` when no
        position in the role was evaluated.
      log_rms_db: the same pool over the positions' per-octave residuals,
        weighted by each position's graded octave span.
      n_bins: total graded bins behind ``rms_db``.
      n_positions: how many positions carry this role.
      n_evaluated: how many of them produced a residual. ``n_positions -
        n_evaluated`` were not evaluated and are still present in
        ``positions`` with their reason attached.
      positions: every position of this role, in the caller's order,
        evaluated or not. Nothing is dropped.
      evaluable: whether ``rms_db`` exists.

    **What this number is, and is not.** It pools each position's own
    flatness, so it answers "how flat does a measurement taken here read".
    It is NOT the flatness of the power-mean combination of these positions:
    combining fills moving nulls before the curve is graded, so a combined
    on-axis curve would read flatter than this pool of individual ones.
    Producing that instead would mean re-running
    :func:`~jasper.audio_measurement.spatial_combine.combine_positions` on a
    subset of the cloud, which is a second combination of the same evidence
    — a thing this module refuses to be.
    """

    role: str
    rms_db: float | None
    log_rms_db: float | None
    n_bins: int
    n_positions: int
    n_evaluated: int
    positions: tuple[PositionFlatness, ...]
    evaluable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "rms_db": self.rms_db,
            "log_rms_db": self.log_rms_db,
            "n_bins": self.n_bins,
            "n_positions": self.n_positions,
            "n_evaluated": self.n_evaluated,
            "positions": [p.to_dict() for p in self.positions],
            "evaluable": self.evaluable,
        }


@dataclass(frozen=True)
class RoleSplitFlatness:
    """The on-axis-primary reading: one headline, and the rest kept apart.

    Args:
      primary: the :class:`RoleFlatness` for ``primary_role`` — the headline
        number, the one a household's listening seat actually corresponds
        to. ``None`` when the cloud carried no position of that role, which
        is UNSAMPLED rather than a zero (a shorter walk need not visit every
        role).
      primary_role: the role that was asked for, echoed so a ``None``
        ``primary`` still says which role went missing.
      others: every other role present, each pooled within itself, in first-
        seen order. **Never merged with** ``primary`` **and never merged
        with each other** — that merge is the thing this view exists to
        undo.
      pooled_rms_db: the SHIPPED pooled residual over the whole cloud,
        lifted from the report via :func:`log_pooled_residual` and never
        recomputed. Carried so the split can be read against the number it
        is explaining, in one record, without a second lookup.
      pooled_log_rms_db: the same report's per-octave re-pooling, likewise
        carried rather than recomputed.
      n_not_evaluated: positions across every role that produced no
        residual. Non-zero means the split speaks for fewer positions than
        the cloud holds; each one names its own reason inside its role.

    The pooled figures come from the COMBINED curve and the per-role ones
    from individual member curves at a possibly different smoothing
    fraction, so they are two kinds of number: read the split for the
    on-axis-vs-off-axis *gap*, not as a decomposition that adds back up to
    the pooled figure. :attr:`PositionFlatness.smoothing_fraction` and
    :attr:`FlatSpecReport.smoothing_fraction` state both attestations.
    """

    primary: RoleFlatness | None
    primary_role: str
    others: tuple[RoleFlatness, ...]
    pooled_rms_db: float | None
    pooled_log_rms_db: float | None
    n_not_evaluated: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_role": self.primary_role,
            "primary": self.primary.to_dict() if self.primary is not None else None,
            "others": [r.to_dict() for r in self.others],
            "pooled_rms_db": self.pooled_rms_db,
            "pooled_log_rms_db": self.pooled_log_rms_db,
            "n_not_evaluated": self.n_not_evaluated,
        }


def _evaluate_position(
    position: PositionCurve, report: FlatSpecReport,
) -> tuple[PositionFlatness, float]:
    """Grade-frame one position's curve against the report's own frame.

    Returns the position's residual pair plus the graded octave span its log
    residual speaks for (``0.0`` when it was not evaluated), because the
    role pooling needs that span as a weight and re-deriving it at the call
    site would be a second owner of the same walk.

    The evaluator is called with the report's OWN ``trusted_floor_hz`` and
    the report's OWN published exclusion intervals, so a per-position number
    and the pooled number are stated in the same frame over the same region
    of spectrum. A :class:`ValueError` from the evaluator — an empty
    reference band, a non-ascending axis, a length mismatch — becomes a
    not-evaluated outcome carrying the evaluator's own message, never an
    exception that loses the other positions and never a silent zero.
    """
    try:
        position_report = evaluate_flat_spec(
            np.asarray(position.freqs_hz, dtype=float),
            np.asarray(position.magnitude_db, dtype=float),
            _exclusion_mask(
                np.asarray(position.freqs_hz, dtype=float), report.excluded_intervals,
            ),
            smoothing_fraction=position.smoothing_fraction,
            trusted_floor_hz=report.trusted_floor_hz,
        )
    except ValueError as exc:
        return (
            PositionFlatness(
                position_id=position.position_id,
                role=position.role,
                degrees=position.degrees,
                take_id=position.take_id,
                smoothing_fraction=position.smoothing_fraction,
                rms_db=None,
                log_rms_db=None,
                n_bins=0,
                n_excluded=0,
                evaluable=False,
                not_evaluated_reason=f"evaluator declined: {exc}",
            ),
            0.0,
        )
    linear: ConvergenceResidual = spec_convergence_residual(position_report)
    logged = log_pooled_residual(position_report)
    evaluable = linear.evaluable and logged.evaluable
    return (
        PositionFlatness(
            position_id=position.position_id,
            role=position.role,
            degrees=position.degrees,
            take_id=position.take_id,
            smoothing_fraction=position.smoothing_fraction,
            rms_db=linear.rms_db if evaluable else None,
            log_rms_db=logged.rms_db if evaluable else None,
            n_bins=linear.n_bins,
            n_excluded=linear.n_excluded,
            evaluable=evaluable,
            not_evaluated_reason=(
                "" if evaluable else "no graded bin survived the floor and the mask"
            ),
        ),
        logged.octaves if evaluable else 0.0,
    )


def role_split_flatness(
    report: FlatSpecReport,
    positions: tuple[PositionCurve, ...],
    *,
    primary_role: str,
) -> RoleSplitFlatness:
    """Report each position role's flatness **separately**, with
    ``primary_role`` as the headline.

    The shipped pooled number averages an on-axis position and a position
    out at the coverage edge with equal weight, because
    :func:`~jasper.audio_measurement.spatial_combine.combine_positions` is
    an unweighted power mean with no ``weights=`` argument anywhere in it.
    That is the right call for a *combiner* — the roles are carried, never
    read, precisely so no reduction quietly prefers one — and it is the
    wrong number to hand a household, whose seat is the on-axis one. This
    view keeps them apart instead: on-axis is its own figure, off-axis is
    its own figure, and nothing here ever averages the two.

    Args:
      report: the session's evaluation. Supplies the frame every position is
        graded in — the trusted floor and the published exclusion
        intervals — and the pooled figures the split is read against.
      positions: the cloud's member curves. Order is preserved within each
        role. An empty tuple yields an empty split, not an error.
      primary_role: which role is the headline, in the cloud's own
        vocabulary (``crossover_v2_flow.POSITION_ROLE_ONAX`` for on-axis).
        Required rather than defaulted, so this module holds no copy of a
        constant another module owns.

    Returns:
      A :class:`RoleSplitFlatness`. A role with no evaluated position is
      present with ``rms_db=None`` and ``evaluable=False``, and every
      position that produced no residual is still listed inside its role
      carrying the reason — nothing collapses into a pass, a zero, or a
      gap.
    """
    graded: dict[str, list[tuple[PositionFlatness, float]]] = {}
    for position in positions:
        graded.setdefault(position.role, []).append(_evaluate_position(position, report))
    roles: dict[str, RoleFlatness] = {}
    n_not_evaluated = 0
    for role, entries in graded.items():
        evaluated = [
            (flatness, octaves)
            for flatness, octaves in entries
            if flatness.evaluable
            and flatness.rms_db is not None
            and flatness.log_rms_db is not None
        ]
        n_not_evaluated += len(entries) - len(evaluated)
        n_bins = sum(flatness.n_bins for flatness, _octaves in evaluated)
        roles[role] = RoleFlatness(
            role=role,
            rms_db=_pool(
                [
                    (float(flatness.n_bins), float(flatness.rms_db))
                    for flatness, _octaves in evaluated
                    if flatness.rms_db is not None
                ],
            ),
            log_rms_db=_pool(
                [
                    (octaves, float(flatness.log_rms_db))
                    for flatness, octaves in evaluated
                    if flatness.log_rms_db is not None
                ],
            ),
            n_bins=n_bins,
            n_positions=len(entries),
            n_evaluated=len(evaluated),
            positions=tuple(flatness for flatness, _octaves in entries),
            evaluable=bool(evaluated) and n_bins > 0,
        )
    pooled = log_pooled_residual(report)
    return RoleSplitFlatness(
        primary=roles.get(primary_role),
        primary_role=primary_role,
        others=tuple(role for name, role in roles.items() if name != primary_role),
        pooled_rms_db=pooled.linear_rms_db,
        pooled_log_rms_db=pooled.rms_db,
        n_not_evaluated=n_not_evaluated,
    )


def _power_mean_across(stack_db: np.ndarray) -> np.ndarray:
    """Per-column power (energy) mean across rows of a dB matrix.

    ``10*log10(mean(10**(dB/10), axis=0))`` — the same reduction
    :func:`~jasper.audio_measurement.spatial_combine.combine_positions`
    applies across positions to build its primary estimator, kept here
    because this module must not import a combiner to average two curves.
    Distinct from
    :func:`jasper.active_speaker.flat_spec._power_mean_db`, which pools one
    curve across FREQUENCY to a scalar; this pools a stack of curves across
    POSITIONS to a curve. NOT a mean of dB values, which is the
    linear-mean-vs-power-mean trap this repo has shipped wrong before.
    """
    return 10.0 * np.log10(np.mean(np.power(10.0, stack_db / 10.0), axis=0))


@dataclass(frozen=True)
class DirectivityBand:
    """One position's departure from on-axis across one graded band.

    Split the same way :class:`~jasper.active_speaker.flat_spec.BandResult`
    splits a deviation (issue #1857), and for the same reason: "this band is
    2 dB down off-axis" and "this band is shaped differently off-axis" are
    different facts calling for different responses, and a single number
    hides which one is present.

    For every bin ``i`` in the band, with ``d_i`` the position's level minus
    the on-axis reference's::

        d_i = level_offset_db + shape_i

    where ``level_offset_db`` is the power mean of the position over the
    band minus the power mean of the reference over the same band, and
    ``shape_i`` is what is left. So ``level_offset_db`` alone is the
    directivity index for this band, and the ``shape_*`` figures are the
    part no level trim can remove.

    Args:
      f_lo_hz: the band's nominal lower edge.
      f_hi_hz: its upper edge (exclusive).
      graded_lo_hz: the edge actually used, after the report's trusted-floor
        clamp — read off the report's own band, not re-derived.
      n_bins: bins of this position's grid inside ``[graded_lo_hz,
        f_hi_hz)``, after the report's exclusion intervals.
      level_offset_db: signed, negative meaning this position is quieter
        than on-axis across the band. ``None`` when the band held no bin.
      shape_max_db: the signed ``shape_i`` at the largest-absolute bin.
        ``None`` when the band held no bin.
      shape_max_hz: that bin's frequency. ``None`` likewise.
      shape_rms_db: RMS of ``shape_i`` across the band. ``None`` likewise.
      evaluable: whether the band held any bin at all.
      not_evaluated_reason: why not, when it did not. Empty otherwise.
    """

    f_lo_hz: float
    f_hi_hz: float
    graded_lo_hz: float
    n_bins: int
    level_offset_db: float | None
    shape_max_db: float | None
    shape_max_hz: float | None
    shape_rms_db: float | None
    evaluable: bool
    not_evaluated_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "graded_lo_hz": self.graded_lo_hz,
            "n_bins": self.n_bins,
            "level_offset_db": self.level_offset_db,
            "shape_max_db": self.shape_max_db,
            "shape_max_hz": self.shape_max_hz,
            "shape_rms_db": self.shape_rms_db,
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
        }


@dataclass(frozen=True)
class DirectivityRow:
    """One position, normalised to the on-axis reference.

    Args:
      position_id: the position's label.
      role: its role, verbatim.
      degrees: its angle when recorded, else ``None`` — **not recorded**,
        never zero. A row with ``None`` here is still a complete
        measurement; only its label is coarser.
      take_id: provenance, verbatim.
      in_reference: whether this position is one of the ones the reference
        curve was built from. Those rows are kept (their residual departure
        from their own mean is real information about cloud spread) but a
        consumer must not read one as an off-axis measurement.
      level_offset_db: the position's broadband offset from the reference,
        power-pooled over the report's reference band. ``None`` when that
        band held no bin of this grid.
      normalized_db: per-bin ``position - reference``, on the table's shared
        grid. The curve a prescriber consumes; every reduction beside it is
        derived from this.
      bands: the per-band split, see :class:`DirectivityBand`.
      evaluable: whether the row carries a normalised curve at all.
      not_evaluated_reason: why not, when it does not. Empty otherwise.
    """

    position_id: str
    role: str
    degrees: float | None
    take_id: str
    in_reference: bool
    level_offset_db: float | None
    normalized_db: tuple[float, ...]
    bands: tuple[DirectivityBand, ...]
    evaluable: bool
    not_evaluated_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "role": self.role,
            "degrees": self.degrees,
            "take_id": self.take_id,
            "in_reference": self.in_reference,
            "level_offset_db": self.level_offset_db,
            "normalized_db": list(self.normalized_db),
            "bands": [band.to_dict() for band in self.bands],
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
        }


@dataclass(frozen=True)
class DirectivityTable:
    """Every position's departure from on-axis, as one consumable table.

    Args:
      freqs_hz: the shared grid every ``normalized_db`` is sampled on — the
        grid the caller's positions already share. This module does not
        resample; positions that do not share the grid are reported as
        not-evaluated rows rather than silently interpolated onto it.
      reference_role: the role the reference curve was pooled from.
      reference_position_ids: exactly which positions went into it, so the
        reference is reproducible rather than merely named.
      reference_db: the reference curve itself, the per-bin power mean of
        those positions. Carried because every number in the table is
        stated against it and a table that named its frame without
        publishing it would be half a measurement.
      reference_band_hz: the span each row's broadband ``level_offset_db``
        is pooled over — the report's own reference band, so it matches the
        frame the graded numbers use.
      rows: one per input position, in the caller's order, including the
        reference's own members (flagged ``in_reference``) and including
        rows that could not be normalised.
      angles_recorded: whether EVERY evaluated row carries a ``degrees``.
        ``False`` means the table is role-labelled only — still complete,
        just coarser — and a consumer wanting per-angle directivity must
        say so rather than assume the ``None`` angles are zero.
      evaluable: whether a reference curve exists at all.
      not_evaluated_reason: why not, when it does not. Empty otherwise.

    JSON-serialisable through :meth:`to_dict`, which is the shape a
    prescriber consumes: plain lists and floats, no numpy, no dataclasses.
    """

    freqs_hz: tuple[float, ...]
    reference_role: str
    reference_position_ids: tuple[str, ...]
    reference_db: tuple[float, ...]
    reference_band_hz: tuple[float, float]
    rows: tuple[DirectivityRow, ...]
    angles_recorded: bool
    evaluable: bool
    not_evaluated_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "freqs_hz": list(self.freqs_hz),
            "reference_role": self.reference_role,
            "reference_position_ids": list(self.reference_position_ids),
            "reference_db": list(self.reference_db),
            "reference_band_hz": list(self.reference_band_hz),
            "rows": [row.to_dict() for row in self.rows],
            "angles_recorded": self.angles_recorded,
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
        }


def _unevaluated_row(position: PositionCurve, reason: str) -> DirectivityRow:
    """A row that carries its reason instead of a number.

    Kept in the table rather than dropped: a position that could not be
    normalised is evidence about the cloud, and a consumer counting rows
    must see the same number of positions it handed in.
    """
    return DirectivityRow(
        position_id=position.position_id,
        role=position.role,
        degrees=position.degrees,
        take_id=position.take_id,
        in_reference=False,
        level_offset_db=None,
        normalized_db=(),
        bands=(),
        evaluable=False,
        not_evaluated_reason=reason,
    )


def _directivity_band(
    band: BandResult,
    freqs_hz: np.ndarray,
    position_db: np.ndarray,
    reference_db: np.ndarray,
    included: np.ndarray,
) -> DirectivityBand:
    """One band of one row's split. See :class:`DirectivityBand` for the math."""
    graded_lo = band.graded_lo_hz if band.graded_lo_hz is not None else band.f_lo_hz
    inside = included & (freqs_hz >= graded_lo) & (freqs_hz < band.f_hi_hz)
    n_bins = int(inside.sum())
    if n_bins == 0:
        return DirectivityBand(
            f_lo_hz=band.f_lo_hz,
            f_hi_hz=band.f_hi_hz,
            graded_lo_hz=graded_lo,
            n_bins=0,
            level_offset_db=None,
            shape_max_db=None,
            shape_max_hz=None,
            shape_rms_db=None,
            evaluable=False,
            not_evaluated_reason=(
                "no bin of this grid survives the graded edge and the exclusion mask"
            ),
        )
    level_offset_db = _power_mean_scalar(position_db[inside]) - _power_mean_scalar(
        reference_db[inside],
    )
    shape = (position_db[inside] - reference_db[inside]) - level_offset_db
    worst = int(np.argmax(np.abs(shape)))
    return DirectivityBand(
        f_lo_hz=band.f_lo_hz,
        f_hi_hz=band.f_hi_hz,
        graded_lo_hz=graded_lo,
        n_bins=n_bins,
        level_offset_db=level_offset_db,
        shape_max_db=float(shape[worst]),
        shape_max_hz=float(freqs_hz[inside][worst]),
        shape_rms_db=float(np.sqrt(np.mean(shape ** 2))),
        evaluable=True,
        not_evaluated_reason="",
    )


def directivity_table(
    report: FlatSpecReport,
    positions: tuple[PositionCurve, ...],
    *,
    reference_role: str,
) -> DirectivityTable:
    """Every position's curve normalised to the on-axis reference, as a table.

    The off-axis captures exist to answer "how does the speaker behave away
    from the design axis". Pooling them into the flatness headline throws
    that answer away and damages the headline in the same motion; this view
    recovers it. Each position is expressed as its departure from the
    on-axis reference — the per-bin power mean of the ``reference_role``
    positions — split per graded band into a level offset (the band's
    directivity index) and the residual shape no level trim can remove.

    Args:
      report: the session's evaluation. Supplies the graded band edges and
        the reference band, so this table's bands are the bands that were
        actually graded rather than a second set.
      positions: the cloud's member curves. All must share one frequency
        axis; a position whose axis differs in length or values is reported
        as a not-evaluated row rather than resampled onto someone else's
        grid.
      reference_role: which role is the on-axis reference, in the cloud's
        own vocabulary. Required rather than defaulted — see
        :func:`role_split_flatness`.

    Returns:
      A :class:`DirectivityTable`, JSON-serialisable via
      :meth:`DirectivityTable.to_dict`. When no position carries
      ``reference_role`` there is nothing to normalise against, so the table
      is ``evaluable=False`` with a reason and every position still appears
      as a not-evaluated row — an absent reference is UNSAMPLED, never an
      implied flat one.
    """
    reference = [p for p in positions if p.role == reference_role]
    if not reference:
        reason = f"no position carries the reference role {reference_role!r}"
        return DirectivityTable(
            freqs_hz=(),
            reference_role=reference_role,
            reference_position_ids=(),
            reference_db=(),
            reference_band_hz=report.reference_band_hz,
            rows=tuple(_unevaluated_row(p, reason) for p in positions),
            angles_recorded=False,
            evaluable=False,
            not_evaluated_reason=reason,
        )
    grid = np.asarray(reference[0].freqs_hz, dtype=float)

    def _on_grid(position: PositionCurve) -> bool:
        freqs = np.asarray(position.freqs_hz, dtype=float)
        magnitude = np.asarray(position.magnitude_db, dtype=float)
        return (
            freqs.shape == grid.shape
            and magnitude.shape == grid.shape
            and bool(np.allclose(freqs, grid, rtol=1e-9, atol=0.0))
        )

    usable_reference = [p for p in reference if _on_grid(p)]
    if not usable_reference:
        reason = "reference positions do not share one frequency grid"
        return DirectivityTable(
            freqs_hz=tuple(float(f) for f in grid),
            reference_role=reference_role,
            reference_position_ids=(),
            reference_db=(),
            reference_band_hz=report.reference_band_hz,
            rows=tuple(_unevaluated_row(p, reason) for p in positions),
            angles_recorded=False,
            evaluable=False,
            not_evaluated_reason=reason,
        )
    reference_db = _power_mean_across(
        np.vstack([np.asarray(p.magnitude_db, dtype=float) for p in usable_reference]),
    )
    excluded = _exclusion_mask(grid, report.excluded_intervals)
    included = np.ones(len(grid), dtype=bool) if excluded is None else ~excluded
    reference_ids = tuple(p.position_id for p in usable_reference)
    # Membership by IDENTITY, never by `in`: `PositionCurve` is an ordinary
    # dataclass holding numpy arrays, so `==` on two of them returns an array
    # and `in` would raise on its ambiguous truth value. Two distinct
    # positions can also hold equal arrays, and they are still two positions.
    reference_identities = {id(p) for p in usable_reference}
    ref_lo, ref_hi = report.reference_band_hz

    rows: list[DirectivityRow] = []
    for position in positions:
        if not _on_grid(position):
            rows.append(
                _unevaluated_row(
                    position, "frequency grid differs from the reference positions'",
                ),
            )
            continue
        position_db = np.asarray(position.magnitude_db, dtype=float)
        in_band = included & (grid >= ref_lo) & (grid < ref_hi)
        level_offset_db: float | None = None
        if bool(in_band.any()):
            level_offset_db = _power_mean_scalar(
                position_db[in_band],
            ) - _power_mean_scalar(reference_db[in_band])
        rows.append(
            DirectivityRow(
                position_id=position.position_id,
                role=position.role,
                degrees=position.degrees,
                take_id=position.take_id,
                in_reference=id(position) in reference_identities,
                level_offset_db=level_offset_db,
                normalized_db=tuple(float(v) for v in (position_db - reference_db)),
                bands=tuple(
                    _directivity_band(band, grid, position_db, reference_db, included)
                    for band in report.bands
                ),
                evaluable=True,
                not_evaluated_reason="",
            ),
        )
    evaluated = [row for row in rows if row.evaluable]
    return DirectivityTable(
        freqs_hz=tuple(float(f) for f in grid),
        reference_role=reference_role,
        reference_position_ids=reference_ids,
        reference_db=tuple(float(v) for v in reference_db),
        reference_band_hz=report.reference_band_hz,
        rows=tuple(rows),
        angles_recorded=bool(evaluated) and all(row.degrees is not None for row in evaluated),
        evaluable=True,
        not_evaluated_reason="",
    )
