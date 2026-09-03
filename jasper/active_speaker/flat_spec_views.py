# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Honest re-readings of one already-graded flat-spec evaluation.

Three views, all derived from a report
:func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` already
produced. Nothing here grades anything — no pass/fail, no tolerance, no
re-deciding band membership, the reference frame or the trusted floor
(that stays ``FlatSpecReport.overall_passed``, #1868). They remove two
properties of the shipped pooling: a linear grid read by a logarithmic ear
(:func:`log_pooled_residual`) and every position pooled equally regardless
of role (:func:`role_split_flatness`, :func:`directivity_table`). Which
WEIGHTING the spec itself should use is still open (#1857).
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
    """``sqrt(sum w*r**2 / sum w)`` over ``(weight, rms)`` pairs, or
    ``None``. The one weighted-RMS identity every pooling here uses. A
    non-positive total weight yields ``None`` — no pooled figure, not zero.
    """
    total = sum(weight for weight, _rms in pairs)
    if total <= 0.0:
        return None
    return math.sqrt(sum(weight * rms ** 2 for weight, rms in pairs) / total)


def _power_mean_scalar(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` — power (energy) mean, NOT a linear
    mean of dB values."""
    return float(10.0 * np.log10(np.mean(np.power(10.0, values_db / 10.0))))


def _band_octaves(band: BandResult) -> float:
    """How many octaves of spectrum this band actually GRADED:
    ``log2(graded_hi_hz / graded_lo_hz)`` (graded edges, not nominal ones,
    since clamps (#2551) can take most of a band away). ``None`` graded
    edges fall back to nominal. ``0.0`` for an empty, inverted or
    non-finite span — a zero weight drops the band rather than letting a
    NaN swallow it.
    """
    lo = band.graded_lo_hz if band.graded_lo_hz is not None else band.f_lo_hz
    hi = band.graded_hi_hz if band.graded_hi_hz is not None else band.f_hi_hz
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo <= 0.0 or hi <= lo:
        return 0.0
    return math.log2(hi / lo)


@dataclass(frozen=True)
class BandWeight:
    """One band's contribution to each pooling, side by side — the audit
    trail for :class:`LogPooledResidual`. Both shares sum to 1.0 across
    evaluable bands. ``graded_lo_hz`` is the edge after the trusted-floor
    clamp; ``octaves`` and ``n_bins`` are this band's weight in the log vs.
    shipped linear pooling; ``linear_share``/``log_share`` are each
    normalized to 1.0. ``bins_per_octave`` is the number the whole view is
    about — compare two bands to read the linear grid's per-octave
    overweight.
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
    ``rms_db`` is ``None`` when no band was evaluable (no residual, not
    zero). ``linear_rms_db`` is the SHIPPED pooled residual, lifted
    verbatim and never recomputed. ``bands`` holds evaluable bands only;
    ``n_bands_not_evaluated`` non-zero means both pooled numbers speak for
    less of the spectrum than the band table suggests.
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
    """Re-pool one report's per-band residuals with EQUAL WEIGHT PER OCTAVE
    instead of per graded bin: ``rms = sqrt(sum_b w_b * r_b**2 / sum_b
    w_b)`` with octave span ``w_b``, vs. the shipped
    ``spec_convergence_residual`` weighted by bin count ``n_b`` (called,
    not recomputed, carried on :attr:`LogPooledResidual.linear_rms_db`).
    Re-weights BETWEEN bands only, not within one — a wide band's own
    linear-grid tilt survives (250 Hz-2 kHz's top octave supplied ~61% of
    its bins on the 2026-08-18 corpus). ``rms_db=None``, ``evaluable=False``
    when no band carries a residual.
    """
    linear = spec_convergence_residual(report)
    # (band, non-excluded bins, graded octaves, own RMS) per poolable band.
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
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture` —
    that requires a uniform linear grid, sample rate and optional IR, none
    of which an already-smoothed log-spaced curve needs.

    ``role`` is the cloud's own vocabulary (``onax``/``offax``/``xovr``),
    read off the record, never re-derived. ``smoothing_fraction`` is
    LOAD-BEARING for interpretation: the cloud smooths per-position curves
    finer than the pooled spec curve (1/6 vs 1/3 octave on the 2026-08-18
    corpus), so a per-position residual reads HIGHER for that reason
    alone. ``degrees=None`` means NOT RECORDED, never zero.
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
    """The report's own published exclusion intervals, applied to another
    axis — a containment test against the published result, not a second
    run of the screen (which cannot even be evaluated for one position).
    Endpoints inclusive on both sides. ``None`` when nothing is excluded.
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
    ``degrees=None`` means not recorded, not zero. ``smoothing_fraction``
    is carried because a residual isn't comparable across two fractions.
    ``rms_db``/``log_rms_db`` are this position's own linear- and
    log-pooled residuals. ``not_evaluated_reason`` is non-empty exactly
    when ``evaluable`` is False (mirrors ``CLAIM_NOT_EVALUATED``, #1868).
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
    Answers "how flat does a measurement taken here read"; NOT the
    flatness of the power-mean combination of these positions (combining
    fills moving nulls first, so a combined curve reads flatter). ``rms_db``
    pools by bin count, ``log_rms_db`` by graded octave span. ``positions``
    lists every position of this role, evaluated or not — nothing dropped.
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
    ``primary`` is ``None`` when the cloud carried no position of
    ``primary_role`` — UNSAMPLED, not zero. ``others`` are never merged
    with ``primary`` or each other — that merge is what this view undoes.
    ``pooled_rms_db``/``pooled_log_rms_db`` are the SHIPPED report figures,
    lifted verbatim. Read the split for the on-axis-vs-off-axis GAP, not as
    a decomposition that adds back up to the pooled figure (the pooled
    figures come from the COMBINED curve, the per-role ones from individual
    member curves at a possibly different smoothing fraction).
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
    Returns the residual pair plus the graded octave span its log residual
    speaks for (``0.0`` when not evaluated), for the role pooling's weight.
    Called with the report's OWN ``frame_kwargs`` and exclusion intervals
    (read back, never re-derived). A :class:`ValueError` becomes a
    not-evaluated outcome carrying its message, never a lost exception or
    a silent zero.
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
    """Report each position role's flatness SEPARATELY, with
    ``primary_role`` as the headline. ``combine_positions`` is an
    unweighted power mean — right for a combiner, wrong for a household
    whose seat is on-axis — so this view keeps roles apart and never
    averages them. An empty ``positions`` yields an empty split, not an
    error. A role with no evaluated position is present with
    ``rms_db=None``; every position that produced no residual is still
    listed inside its role, carrying the reason.
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
    """Per-column power (energy) mean across rows of a dB matrix — the
    same reduction ``combine_positions`` applies across positions. Distinct
    from ``flat_spec._power_mean_db``, which pools one curve across
    FREQUENCY to a scalar; this pools curves across POSITIONS to a curve.
    """
    return 10.0 * np.log10(np.mean(np.power(10.0, stack_db / 10.0), axis=0))


@dataclass(frozen=True)
class DirectivityBand:
    """One position's departure from on-axis across one graded band. Split
    the way ``BandResult`` splits a deviation (#1857): "2 dB down
    off-axis" and "shaped differently off-axis" are different facts. Per
    bin ``d_i = level_offset_db + shape_i``, so ``level_offset_db`` alone
    is the band's directivity index and ``shape_*`` is the part no level
    trim removes. ``level_offset_db`` is signed, negative = quieter than
    on-axis; ``None`` when the band held no bin.
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
    """One position, normalised to the on-axis reference. ``degrees=None``
    means not recorded, never zero — the row is still a complete
    measurement. ``in_reference`` rows are kept (their departure from
    their own mean is real cloud-spread information) but must not be read
    as an off-axis measurement. ``normalized_db`` is per-bin
    ``position - reference`` on the shared grid — the curve a prescriber
    consumes; every other reduction derives from it.
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
    ``freqs_hz`` is the shared grid; this module does not resample —
    positions off it are not-evaluated rows. ``reference_position_ids``
    make the reference reproducible, not merely named.
    ``angles_recorded=False`` means the table is role-labelled only; a
    consumer must not assume ``None`` angles are zero. JSON-serialisable
    through :meth:`to_dict`.
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
    """A row that carries its reason instead of a number, kept rather than
    dropped so row count always matches input count."""
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
    """Every position's curve normalised to the on-axis reference, as a
    table: departure from the per-bin power mean of the ``reference_role``
    positions, split per graded band into a level offset (directivity
    index) and the residual shape no level trim removes. All positions
    must share one frequency axis; a mismatched one is a not-evaluated
    row, never resampled. No ``reference_role`` position leaves the table
    ``evaluable=False`` with every position still listed (UNSAMPLED, never
    an implied flat reference).

    Consumed by ``jasper-round-views directivity`` (#3865).
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
        # Failing against the FIRST reference's own axis is only possible
        # by disagreeing with itself: mismatched magnitude/frequency lengths.
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
    # Membership by IDENTITY, never `in`: `==` on dataclasses holding numpy
    # arrays returns an array, not a bool.
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
