# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Honest re-readings of one already-graded flat-spec evaluation.

Three views, all derived from a report
:func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` already produced.
**Nothing here grades anything**: no view returns a pass/fail, holds a
tolerance, or re-decides band membership, the reference frame or the trusted
floor. The session's one verdict stays ``FlatSpecReport.overall_passed``
(#1868); these report residuals, which are measurements. They remove two
properties of the shipped pooling — a linear grid read by a logarithmic ear
(:func:`log_pooled_residual` re-pools per octave) and every position pooled
equally whatever question it answers (:func:`role_split_flatness`,
:func:`directivity_table`). Which WEIGHTING the spec itself should use is
still open (#1857), and moving it here would move graded verdicts.

numpy plus :mod:`jasper.active_speaker.flat_spec`; no I/O, no policy.
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

    The one weighted-RMS identity every pooling here uses — bands by octave
    span, positions by bin count, positions by octave span — and the same one
    ``spec_convergence_residual`` pools bands with. A non-positive total weight
    yields ``None``: no pooled figure exists, which is not one of zero.
    """
    total = sum(weight for weight, _rms in pairs)
    if total <= 0.0:
        return None
    return math.sqrt(sum(weight * rms ** 2 for weight, rms in pairs) / total)


def _power_mean_scalar(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` over one array — the power (energy)
    mean, NOT a linear mean of dB values.
    """
    return float(10.0 * np.log10(np.mean(np.power(10.0, values_db / 10.0))))


def _band_octaves(band: BandResult) -> float:
    """How many octaves of spectrum this band actually GRADED.

    ``log2(graded_hi_hz / graded_lo_hz)`` — the graded edges, not the nominal
    ones, because the clamps (#2551) can take most of a band away and a weight
    from nominal edges would hand a band influence over a span nothing was
    measured in. ``None`` graded edges fall back to the nominal ones, which on
    a report predating each clamp are the same thing.

    ``0.0`` for an empty, inverted or non-finite span — a weight of zero drops
    the band out of a weighted mean rather than letting a NaN swallow it.

    The upper edge is still taken as graded in full: a report carries the edges
    the clamps set but not the axis it was evaluated on, so a band whose axis
    stopped short is indistinguishable here. :attr:`BandWeight.bins_per_octave`
    is published beside the weight rather than folded into it because an
    anomalously low density is that truncation showing.
    """
    lo = band.graded_lo_hz if band.graded_lo_hz is not None else band.f_lo_hz
    hi = band.graded_hi_hz if band.graded_hi_hz is not None else band.f_hi_hz
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo <= 0.0 or hi <= lo:
        return 0.0
    return math.log2(hi / lo)


@dataclass(frozen=True)
class BandWeight:
    """One band's contribution to each pooling, side by side.

    The audit trail for :class:`LogPooledResidual`: which band the weighting
    moved, and by how much. Both shares sum to 1.0 across the evaluable bands.

    Args:
      f_lo_hz: the band's nominal lower edge — its ``SPEC_BANDS`` row.
      f_hi_hz: the band's upper edge (exclusive).
      graded_lo_hz: the edge the metrics were actually taken from, after the
        trusted-floor clamp.
      octaves: ``log2(f_hi_hz / graded_lo_hz)`` — this band's weight in the log
        pooling.
      n_bins: the band's non-excluded graded bin count — its weight in the
        shipped linear pooling.
      rms_deviation_db: the band's own RMS deviation, lifted verbatim.
      linear_share: ``n_bins / sum(n_bins)``.
      log_share: ``octaves / sum(octaves)``.
      bins_per_octave: ``n_bins / octaves``. The number the whole view is
        about: compare two bands to read the per-octave overweight the linear
        grid hands the higher one.
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
        evaluable — no residual, not a residual of zero.
      linear_rms_db: the SHIPPED pooled residual for the same report, lifted
        verbatim and never recomputed, so the two numbers can never be quoted
        from two places and disagree.
      octaves: total graded octaves the log pooling averaged over.
      n_bins: total non-excluded graded bins behind ``linear_rms_db``.
      bands: per-band weights, evaluable bands only.
      n_bands_not_evaluated: how many bands had no residual to pool. Non-zero
        means both pooled numbers speak for less of the spectrum than the band
        table suggests.
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
    """Re-pool one report's per-band residuals with **equal weight per octave**
    instead of equal weight per graded bin.

    With band ``b``'s non-excluded bin count ``n_b``, graded span ``w_b``
    octaves and own RMS ``r_b``::

        shipped (per bin)    rms = sqrt( sum_b n_b * r_b**2 / sum_b n_b )
        this view (per oct)  rms = sqrt( sum_b w_b * r_b**2 / sum_b w_b )

    Both are honest weighted RMS values over the same per-band residuals. The
    shipped form is ``spec_convergence_residual``'s and is called rather than
    recomputed, its answer carried on :attr:`LogPooledResidual.linear_rms_db`.

    **What this does NOT fix:** it re-weights *between* bands, not *within*
    one. Each ``r_b`` is still per-bin-weighted across its band, so inside a
    wide band the linear grid's tilt survives (on the 2026-08-18 corpus the
    250 Hz-2 kHz band's top octave supplies about 61 % of its bins). Removing
    that would mean re-deriving deviations from the curve, which would make
    this a second evaluator.

    When no band carries a residual, ``rms_db`` is ``None`` and ``evaluable``
    is ``False`` rather than a fabricated 0.0.
    """
    linear = spec_convergence_residual(report)
    # (band, non-excluded bins, graded octaves, its own RMS) per poolable band.
    # Walked once; the shares below need the totals this walk produces.
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

    Deliberately NOT
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture`: that
    type is the combiner's input and requires a uniform linear grid, a sample
    rate and an optional IR, none of which an already-smoothed log-spaced curve
    has or needs.

    Args:
      position_id: the position's own label.
      role: what KIND of listening position this is, in the cloud's own
        vocabulary (``onax`` / ``offax`` / ``xovr``, owned by
        ``crossover_v2.spatial.POSITION_ROLES``). Read off the position's
        record, never re-derived; this module defines no role constant.
      freqs_hz: the position's frequency axis, strictly ascending. Need not be
        log-spaced — the evaluator masks by value.
      magnitude_db: matching magnitude, dB, as analysed: gated, calibrated and
        already smoothed.
      smoothing_fraction: the 1/N-octave fraction the caller attests to.
        **Load-bearing for interpretation**: the cloud smooths per-position
        curves finer than the pooled spec curve (1/6 vs 1/3 octave on the
        2026-08-18 corpus), and finer smoothing preserves ripple, so a
        per-position residual reads HIGHER than a same-speaker pooled one for
        that reason alone.
      degrees: the microphone angle, when known. ``None`` means **not
        recorded**, never zero; every view degrades to role-only without it.
      take_id: which take survived. Provenance only; nothing here reads it.
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

    A **containment test against the published result**, not a second run of
    the screen: the screen is a mean-vs-median disagreement across the whole
    cloud and cannot even be evaluated for one position. Applying it keeps the
    per-role numbers graded on the same region of spectrum as the pooled one.

    Endpoints are inclusive on both sides: the interval spans the first and
    last excluded bin of a run. ``None`` when nothing is excluded, which is
    what the evaluator wants for "exclude nothing".
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
        carried because a residual is not comparable across two fractions.
      rms_db: this position's own pooled residual on the linear per-bin
        weighting, the same *kind* of number as the shipped headline.
      log_rms_db: the same position re-pooled per octave.
      n_bins: graded bins behind ``rms_db``.
      n_excluded: graded-band bins the report's exclusion intervals removed.
      evaluable: whether a residual exists at all.
      not_evaluated_reason: why not. Always non-empty when ``evaluable`` is
        ``False`` and always empty otherwise, mirroring the claim producer's
        first-class ``CLAIM_NOT_EVALUATED`` outcome (#1868).
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

    Pools each position's own flatness, so it answers "how flat does a
    measurement taken here read". It is NOT the flatness of the power-mean
    combination of these positions: combining fills moving nulls before the
    curve is graded, so a combined on-axis curve reads flatter than this pool.
    Producing that would mean re-running ``combine_positions`` on a subset.

    Args:
      role: the role these positions share, verbatim.
      rms_db: the role's pooled residual over its evaluated positions — the
        same bin-weighted identity ``spec_convergence_residual`` pools bands
        with, applied across positions.
      log_rms_db: the same pool over the positions' per-octave residuals,
        weighted by each position's graded octave span.
      n_bins: total graded bins behind ``rms_db``.
      n_positions: how many positions carry this role.
      n_evaluated: how many produced a residual.
      positions: every position of this role, in the caller's order, evaluated
        or not. Nothing is dropped.
      evaluable: whether ``rms_db`` exists.
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
      primary: the :class:`RoleFlatness` for ``primary_role``. ``None`` when
        the cloud carried no position of that role, which is UNSAMPLED rather
        than a zero.
      primary_role: the role that was asked for, echoed so a ``None``
        ``primary`` still says which role went missing.
      others: every other role present, each pooled within itself, in
        first-seen order. **Never merged with** ``primary`` **and never merged
        with each other** — that merge is what this view exists to undo.
      pooled_rms_db: the SHIPPED pooled residual over the whole cloud, lifted
        from the report and never recomputed.
      pooled_log_rms_db: the same report's per-octave re-pooling.
      n_not_evaluated: positions across every role that produced no residual.

    The pooled figures come from the COMBINED curve and the per-role ones from
    individual member curves at a possibly different smoothing fraction: read
    the split for the on-axis-vs-off-axis *gap*, not as a decomposition that
    adds back up to the pooled figure.
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
    position: PositionCurve, report: FlatSpecReport, *, reference_db_override: float | None = None,
) -> tuple[PositionFlatness, float]:
    """Grade-frame one position's curve against the report's own frame.

    Returns the position's residual pair plus the graded octave span its log
    residual speaks for (``0.0`` when not evaluated), because the role pooling
    needs that span as a weight.

    The evaluator is called with the report's OWN ``frame_kwargs`` and
    published exclusion intervals — read back, never re-derived — so a
    per-position number and the pooled number are stated in the same frame over
    the same region of spectrum. ``reference_db_override`` passes straight
    through. A :class:`ValueError` from the evaluator becomes a not-evaluated
    outcome carrying its message, never an exception that loses the other
    positions and never a silent zero.
    """
    try:
        position_report = evaluate_flat_spec(
            np.asarray(position.freqs_hz, dtype=float),
            np.asarray(position.magnitude_db, dtype=float),
            _exclusion_mask(
                np.asarray(position.freqs_hz, dtype=float), report.excluded_intervals,
            ),
            smoothing_fraction=position.smoothing_fraction,
            **report.frame_kwargs,
            reference_db_override=reference_db_override,
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

    ``combine_positions`` is an unweighted power mean, which is the right call
    for a combiner and the wrong number to hand a household whose seat is the
    on-axis one. This view keeps the roles apart and never averages them.

    Args:
      report: the session's evaluation. Supplies the frame every position is
        graded in and the pooled figures the split is read against.
      positions: the cloud's member curves. Order is preserved within each
        role; an empty tuple yields an empty split, not an error.
      primary_role: which role is the headline, in the cloud's own vocabulary.
        Required rather than defaulted, so this module holds no copy of a
        constant another module owns.

    A role with no evaluated position is present with ``rms_db=None``, and
    every position that produced no residual is still listed inside its role
    carrying the reason.
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
    ``combine_positions`` applies across positions. Distinct from
    ``flat_spec._power_mean_db``, which pools one curve across FREQUENCY to a
    scalar; this pools a stack of curves across POSITIONS to a curve.
    """
    return 10.0 * np.log10(np.mean(np.power(10.0, stack_db / 10.0), axis=0))


@dataclass(frozen=True)
class DirectivityBand:
    """One position's departure from on-axis across one graded band.

    Split the way ``BandResult`` splits a deviation (#1857): "this band is 2 dB
    down off-axis" and "this band is shaped differently off-axis" are different
    facts calling for different responses. For every bin ``i`` in the band,
    with ``d_i`` the position's level minus the on-axis reference's::

        d_i = level_offset_db + shape_i

    so ``level_offset_db`` alone is the band's directivity index and the
    ``shape_*`` figures are the part no level trim can remove.

    Args:
      f_lo_hz: the band's nominal lower edge.
      f_hi_hz: its upper edge (exclusive).
      graded_lo_hz: the edge actually used, read off the report's own band.
      n_bins: bins of this position's grid inside ``[graded_lo_hz, f_hi_hz)``,
        after the report's exclusion intervals.
      level_offset_db: signed, negative meaning quieter than on-axis across the
        band. ``None`` when the band held no bin.
      shape_max_db: the signed ``shape_i`` at the largest-absolute bin.
      shape_max_hz: that bin's frequency.
      shape_rms_db: RMS of ``shape_i`` across the band.
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
      degrees: its angle when recorded, else ``None`` — **not recorded**, never
        zero. A row with ``None`` here is still a complete measurement.
      take_id: provenance, verbatim.
      in_reference: whether this position is one the reference curve was built
        from. Those rows are kept — their departure from their own mean is real
        information about cloud spread — but must not be read as an off-axis
        measurement.
      level_offset_db: the position's broadband offset from the reference,
        power-pooled over the report's reference band.
      normalized_db: per-bin ``position - reference`` on the table's shared
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
      freqs_hz: the shared grid every ``normalized_db`` is sampled on. This
        module does not resample; positions that do not share the grid are
        reported as not-evaluated rows rather than silently interpolated.
      reference_role: the role the reference curve was pooled from.
      reference_position_ids: exactly which positions went into it, so the
        reference is reproducible rather than merely named.
      reference_db: the reference curve itself, the per-bin power mean of those
        positions. Every number in the table is stated against it.
      reference_band_hz: the span each row's ``level_offset_db`` is pooled
        over — the report's own reference band.
      rows: one per input position, in the caller's order, including the
        reference's own members (flagged ``in_reference``) and rows that could
        not be normalised.
      angles_recorded: whether EVERY evaluated row carries a ``degrees``.
        ``False`` means the table is role-labelled only, and a consumer must
        not assume the ``None`` angles are zero.
      evaluable: whether a reference curve exists at all.
      not_evaluated_reason: why not, when it does not. Empty otherwise.

    JSON-serialisable through :meth:`to_dict`: plain lists and floats.
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

    Kept in the table rather than dropped: a consumer counting rows must see
    the same number of positions it handed in.
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

    Each position is expressed as its departure from the on-axis reference —
    the per-bin power mean of the ``reference_role`` positions — split per
    graded band into a level offset (the band's directivity index) and the
    residual shape no level trim can remove.

    Args:
      report: the session's evaluation. Supplies the graded band edges and the
        reference band, so this table's bands are the bands actually graded.
      positions: the cloud's member curves. All must share one frequency axis;
        a position whose axis differs is reported as a not-evaluated row rather
        than resampled onto someone else's grid.
      reference_role: which role is the on-axis reference, in the cloud's own
        vocabulary. Required rather than defaulted.

    When no position carries ``reference_role`` the table is
    ``evaluable=False`` with a reason and every position still appears as a
    not-evaluated row — an absent reference is UNSAMPLED, never an implied
    flat one.
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
        # Every reference position failed against the FIRST one's own axis,
        # which that one can only do by disagreeing with itself. So the cause is
        # always a position whose magnitude and frequency arrays are different
        # lengths, never a grid disagreement between positions.
        reason = "a reference position's magnitude and frequency arrays differ in length"
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
    # Membership by IDENTITY, never by `in`: `==` on two dataclasses holding
    # numpy arrays returns an array, and two distinct positions can hold equal
    # arrays and still be two positions.
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
