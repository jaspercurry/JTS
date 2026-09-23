# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The flat-linearization spec evaluator.

Pure computation. One question: does this spatially-combined,
1/3-oct-smoothed magnitude curve meet the flat-linearization spec? The
caller supplies the curve, its axis and an optional exclusion mask;
nothing here combines, smooths, detects interference, or holds gate
policy. :func:`spec_convergence_residual` is a further reading of the SAME
report, lifted from it rather than recomputed. See ADR-0194.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement import gating
from jasper.audio_measurement.band_ladders import (
    BEST_EFFORT_ABOVE_HZ as BEST_EFFORT_ABOVE_HZ, SPEC_BANDS as SPEC_BANDS, SPEC_BAND_EDGES_HZ,
)
from jasper.audio_measurement.room_boundary import GATED_SPEC_LOWER_EDGE_HZ
from jasper.audio_measurement.series_stats import power_mean_db
from jasper.audio_measurement.spatial_combine import merged_true_intervals

# SPEC_BANDS[0] exactly — the LOW-MID band alone, so no band above 2 kHz is
# pooled into the zero its own deviation is stated against (ADR-0194). Not
# `gate_sweep.REFERENCE_BAND_HZ`, which normalises rather than grades.
REFERENCE_BAND_HZ: tuple[float, float] = SPEC_BAND_EDGES_HZ[0]


@dataclass(frozen=True)
class BandResult:
    """One :data:`SPEC_BANDS` entry's evaluation outcome.

    A band with zero non-excluded bins is ``evaluable=False``,
    ``within_target=None`` — no evidence, treated as not-within-target by
    :attr:`FlatSpecReport.overall_within_target`. Per bin,
    ``deviation_i = ripple_i + level_deviation_db``: level is what a
    different band's own level can move, ripple structurally cannot.
    ``max_deviation_db`` and ``max_ripple_db`` are taken at different bins
    and do not add; ``abs(max_deviation_db) >= abs(level_deviation_db)``
    always holds.

    ``f_lo_hz``/``f_hi_hz`` are NOMINAL edges from :data:`SPEC_BANDS`;
    ``graded_lo_hz``/``graded_hi_hz`` are the edges actually used after the
    session's trusted floor/ceiling clamp (``None`` on a report without the
    clamp; ``graded_lo_hz >= f_hi_hz`` means the floor swallowed the band).
    ``max_deviation_db``/``_hz`` are signed, at the largest-magnitude
    non-excluded bin. ``level_deviation_db`` is this band's own power-mean
    level minus :attr:`FlatSpecReport.reference_db`. ``max_ripple_db``/
    ``_hz`` are the worst deviation from THIS band's own level — invariant
    to the reference frame. ``max_at_graded_edge``: floor-truncated with
    its worst bin at that edge, so ``max_deviation_db`` is a lower bound
    (disclosure only). ``room_entangled_below_hz``: upper edge of this
    band's room-entangled sub-span, disclosure only.

    The seven ``gate_*`` fields are DISCLOSURE ONLY, carried by a report
    that was stamped with a gate sweep — nothing here computes or reads
    them. ``gate_sensitivity_db``: the
    gate sweep's null-model-corrected delta at this band's worst bin.
    ``sigma_growth_ratio``: across-pose sigma at the longest
    resolution-valid rung over the shortest, same bin. ``n_valid_rungs``:
    how many ladder rungs were resolution-valid there. ``gate_sensitivity_note``:
    why the three above are ``None``. ``gate_sensitivity_detail``: the
    ``RoundCapturesRefused`` behind a capture-refusal note. ``gate_window_verdict``:
    ``"stable"``/``"moved"``/``"unresolved"``, ``None`` only if never swept.
    ``gate_window_verdict_reasons``: which routes produced it.
    """

    f_lo_hz: float
    f_hi_hz: float
    tolerance_db: float
    max_deviation_db: float | None
    max_deviation_hz: float | None
    rms_deviation_db: float | None
    n_bins: int
    n_excluded: int
    evaluable: bool
    within_target: bool | None
    # Defaulted: a report can be hand-built or rehydrated without these.
    level_deviation_db: float | None = None
    max_ripple_db: float | None = None
    max_ripple_hz: float | None = None
    graded_lo_hz: float | None = None
    graded_hi_hz: float | None = None
    max_at_graded_edge: bool | None = None
    room_entangled_below_hz: float | None = None
    # Defaulted for a DIFFERENT reason: nothing here fills these in, so
    # `None` means "no sweep has read this report".
    gate_sensitivity_db: float | None = None
    sigma_growth_ratio: float | None = None
    n_valid_rungs: int | None = None
    gate_sensitivity_note: str | None = None
    gate_sensitivity_detail: dict | None = None
    gate_window_verdict: str | None = None
    gate_window_verdict_reasons: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "tolerance_db": self.tolerance_db,
            "max_deviation_db": self.max_deviation_db,
            "max_deviation_hz": self.max_deviation_hz,
            "rms_deviation_db": self.rms_deviation_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "evaluable": self.evaluable,
            "within_target": self.within_target,
            "level_deviation_db": self.level_deviation_db,
            "max_ripple_db": self.max_ripple_db,
            "max_ripple_hz": self.max_ripple_hz,
            "graded_lo_hz": self.graded_lo_hz,
            "graded_hi_hz": self.graded_hi_hz,
            "max_at_graded_edge": self.max_at_graded_edge,
            "room_entangled_below_hz": self.room_entangled_below_hz,
            "gate_sensitivity_db": self.gate_sensitivity_db,
            "sigma_growth_ratio": self.sigma_growth_ratio,
            "n_valid_rungs": self.n_valid_rungs,
            "gate_sensitivity_note": self.gate_sensitivity_note,
            "gate_sensitivity_detail": self.gate_sensitivity_detail,
            "gate_window_verdict": self.gate_window_verdict,
            "gate_window_verdict_reasons": (
                None if self.gate_window_verdict_reasons is None
                else list(self.gate_window_verdict_reasons)
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BandResult":
        """The exact inverse of :meth:`to_dict` — a rehydration, never a
        re-derivation. ``f_lo_hz`` through ``within_target`` are read with hard
        indexing (a document missing one is CORRUPT and raises); only the
        dataclass-defaulted fields use :meth:`dict.get`.
        """
        reasons = raw.get("gate_window_verdict_reasons")
        return cls(
            f_lo_hz=float(raw["f_lo_hz"]),
            f_hi_hz=float(raw["f_hi_hz"]),
            tolerance_db=float(raw["tolerance_db"]),
            max_deviation_db=raw["max_deviation_db"],
            max_deviation_hz=raw["max_deviation_hz"],
            rms_deviation_db=raw["rms_deviation_db"],
            n_bins=int(raw["n_bins"]),
            n_excluded=int(raw["n_excluded"]),
            evaluable=bool(raw["evaluable"]),
            within_target=raw["within_target"],
            level_deviation_db=raw.get("level_deviation_db"),
            max_ripple_db=raw.get("max_ripple_db"),
            max_ripple_hz=raw.get("max_ripple_hz"),
            graded_lo_hz=raw.get("graded_lo_hz"),
            graded_hi_hz=raw.get("graded_hi_hz"),
            max_at_graded_edge=raw.get("max_at_graded_edge"),
            room_entangled_below_hz=raw.get("room_entangled_below_hz"),
            gate_sensitivity_db=raw.get("gate_sensitivity_db"),
            sigma_growth_ratio=raw.get("sigma_growth_ratio"),
            n_valid_rungs=raw.get("n_valid_rungs"),
            gate_sensitivity_note=raw.get("gate_sensitivity_note"),
            gate_sensitivity_detail=raw.get("gate_sensitivity_detail"),
            gate_window_verdict=raw.get("gate_window_verdict"),
            gate_window_verdict_reasons=None if reasons is None else tuple(reasons),
        )


@dataclass(frozen=True)
class FlatSpecReport:
    """The full flat-spec evaluation for one combined+smoothed curve.

    ``excluded_intervals`` collapses contiguous exclusion-mask runs into
    merged ``(f_lo_hz, f_hi_hz)`` tuples — disclosure only, pass/fail reads
    the mask directly. ``overall_within_target`` is True only when every band is
    both evaluable and within target. ``smoothing_fraction`` is caller
    attestation, not a measurement. ``trusted_floor_hz``/
    ``trusted_ceiling_hz`` are the clamps this evaluation was intersected
    at (``None`` means "not stated", never "zero"). ``reference_band_hz``
    is the span whose power mean IS ``reference_db``; ``graded_band_hz`` is
    the whole span graded (deviations are stated FROM the low-mid band but
    OVER the span to the trusted ceiling). ``entanglement_floor_hz``/
    ``_source`` are the ROOM's floor and provenance, clamping nothing —
    they mark :attr:`BandResult.room_entangled_below_hz`. ``gate_sweep_frame``
    is the frame every band's gate fields are stated in (#3495); ``None``
    on every report this module builds.
    """

    reference_db: float
    bands: tuple[BandResult, ...]
    overall_within_target: bool
    excluded_intervals: tuple[tuple[float, float], ...]
    best_effort_above_hz: float
    smoothing_fraction: int
    # Defaults say "nothing clamped".
    trusted_floor_hz: float | None = None
    reference_band_hz: tuple[float, float] = REFERENCE_BAND_HZ
    trusted_ceiling_hz: float | None = None
    graded_band_hz: tuple[float, float] = (
        GATED_SPEC_LOWER_EDGE_HZ, BEST_EFFORT_ABOVE_HZ,
    )
    entanglement_floor_hz: float | None = None
    entanglement_floor_source: str = gating.ENTANGLEMENT_SOURCE_UNKNOWN
    gate_sweep_frame: dict[str, Any] | None = None

    @property
    def frame_kwargs(self) -> dict[str, Any]:
        """This report's FRAME, as :func:`evaluate_flat_spec`'s own keywords.
        Splatting one dict makes stating all four together structural.
        Deliberately NOT ``smoothing_fraction`` — caller attestation about
        ONE curve.
        """
        return {
            "trusted_floor_hz": self.trusted_floor_hz,
            "trusted_ceiling_hz": self.trusted_ceiling_hz,
            "entanglement_floor_hz": self.entanglement_floor_hz,
            "entanglement_floor_source": self.entanglement_floor_source,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_db": self.reference_db,
            "ladder": "speaker_spec",
            "bands": [band.to_dict() for band in self.bands],
            "overall_within_target": self.overall_within_target,
            "excluded_intervals": [list(interval) for interval in self.excluded_intervals],
            "best_effort_above_hz": self.best_effort_above_hz,
            "smoothing_fraction": self.smoothing_fraction,
            "trusted_floor_hz": self.trusted_floor_hz,
            "reference_band_hz": list(self.reference_band_hz),
            "trusted_ceiling_hz": self.trusted_ceiling_hz,
            "graded_band_hz": list(self.graded_band_hz),
            "entanglement_floor_hz": self.entanglement_floor_hz,
            "entanglement_floor_source": self.entanglement_floor_source,
            "gate_sweep_frame": self.gate_sweep_frame,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FlatSpecReport":
        """The exact inverse of :meth:`to_dict` — a rehydration, never a
        re-derivation. Same read rules as :meth:`BandResult.from_dict`;
        ``excluded_intervals`` is read hard (missing it raises rather than
        rehydrating an empty "nothing excluded" tuple). The entanglement
        pair is read through
        :meth:`~jasper.audio_measurement.gating.EntanglementFloor.coerce`
        — the lenient door, where :func:`evaluate_flat_spec` takes the
        strict one — so rehydration never re-grades into a refusal.
        """
        kwargs: dict[str, Any] = {}
        reference_band = raw.get("reference_band_hz")
        if reference_band is not None:
            kwargs["reference_band_hz"] = (float(reference_band[0]), float(reference_band[1]))
        graded_band = raw.get("graded_band_hz")
        if graded_band is not None:
            kwargs["graded_band_hz"] = (float(graded_band[0]), float(graded_band[1]))
        raw_frame = raw.get("gate_sweep_frame")
        gate_sweep_frame = dict(raw_frame) if isinstance(raw_frame, Mapping) else None
        entanglement = gating.EntanglementFloor.coerce(
            raw.get("entanglement_floor_hz"), raw.get("entanglement_floor_source")
        )
        return cls(
            reference_db=float(raw["reference_db"]),
            bands=tuple(BandResult.from_dict(b) for b in raw["bands"]),
            overall_within_target=bool(raw["overall_within_target"]),
            excluded_intervals=tuple(
                (float(lo), float(hi)) for lo, hi in raw["excluded_intervals"]
            ),
            best_effort_above_hz=float(raw["best_effort_above_hz"]),
            smoothing_fraction=int(raw["smoothing_fraction"]),
            trusted_floor_hz=raw.get("trusted_floor_hz"),
            trusted_ceiling_hz=raw.get("trusted_ceiling_hz"),
            gate_sweep_frame=gate_sweep_frame,
            entanglement_floor_hz=entanglement.hz,
            entanglement_floor_source=entanglement.source,
            **kwargs,
        )


def _graded_lo_hz(f_lo_hz: float, trusted_floor_hz: float | None) -> float:
    """``max(f_lo_hz, trusted_floor_hz)``. The single place this
    intersection happens. ``None``/non-finite clamps nothing — the
    finiteness guard is load-bearing since :func:`max` with NaN is
    order-dependent.
    """
    if trusted_floor_hz is None or not math.isfinite(trusted_floor_hz):
        return float(f_lo_hz)
    return max(float(f_lo_hz), float(trusted_floor_hz))


def _graded_hi_hz(f_hi_hz: float, trusted_ceiling_hz: float | None) -> float:
    """Mirror of :func:`_graded_lo_hz`, with the asymmetry that is the
    point: the floor only ever RAISES an edge, while the TOP band's edge
    follows the ceiling in BOTH directions (it and
    :data:`BEST_EFFORT_ABOVE_HZ` are one number); every lower edge is only
    ever lowered.
    """
    if trusted_ceiling_hz is None or not math.isfinite(trusted_ceiling_hz):
        return float(f_hi_hz)
    if f_hi_hz >= BEST_EFFORT_ABOVE_HZ:
        return float(trusted_ceiling_hz)
    return min(float(f_hi_hz), float(trusted_ceiling_hz))


def _room_entangled_below_hz(
    graded_lo_hz: float,
    graded_hi_hz: float,
    entanglement_floor_hz: float | None,
) -> float | None:
    """The upper edge of one band's room-entangled sub-span, or ``None``.
    Opposite in effect to :func:`_graded_lo_hz`: that one MOVES an edge,
    this one only marks.
    """
    if entanglement_floor_hz is None:
        return None
    if entanglement_floor_hz <= graded_lo_hz:
        return None
    return min(float(entanglement_floor_hz), float(graded_hi_hz))


def evaluate_flat_spec(
    freqs_hz: np.ndarray,
    spec_smoothed_db: np.ndarray,
    exclusion_mask: np.ndarray | None = None,
    *,
    smoothing_fraction: int = 3,
    trusted_floor_hz: float | None = None,
    trusted_ceiling_hz: float | None = None,
    reference_db_override: float | None = None,
    entanglement_floor_hz: float | None = None,
    entanglement_floor_source: str = gating.ENTANGLEMENT_SOURCE_UNKNOWN,
) -> FlatSpecReport:
    """Evaluate the flat-linearization spec against one combined,
    1/3-oct-smoothed magnitude curve.

    ``freqs_hz`` must be 1-D strictly ascending — the merged exclusion
    intervals use index adjacency as a proxy for frequency adjacency.
    ``exclusion_mask`` (``True`` = interference-flagged) excludes a bin
    from the reference level AND every band's deviation metrics.
    ``smoothing_fraction`` is provenance only, unused here.
    ``trusted_floor_hz``/``trusted_ceiling_hz`` raise/lower every band's
    edge (and the reference band's) before anything is measured; ``None``
    or non-finite clamps nothing. ``reference_db_override`` grades against
    a different capture's level without changing what the reference band
    is. ``entanglement_floor_hz``/``_source`` clamp nothing and change no
    grade — they only set :attr:`BandResult.room_entangled_below_hz`; an
    unrecognized source, or a floor/source pair that disagrees about
    whether a floor is known, raises.

    Reference level: power mean over non-excluded bins inside
    :data:`REFERENCE_BAND_HZ`, floor-raised like every band. Clamping
    moves the same speaker onto fewer bins (:attr:`BandResult.n_bins`
    stays visible). Each band also carries the attribution split
    (``level_deviation_db``, ``max_ripple_db``/``_hz``) — disclosure only,
    no verdict reads them.

    A band with zero non-excluded bins is ``evaluable=False`` rather than
    raised on, so one band's lost evidence doesn't destroy the other two;
    the REFERENCE band still raises (nothing to grade against). Band
    membership is ``graded_lo <= f < graded_hi``; best-effort bins are
    never evaluated and appear in no :class:`BandResult`.

    Raises ``ValueError`` for any degenerate input: empty/non-1-D arrays,
    mismatched lengths, non-ascending ``freqs_hz``, non-finite values, a
    reference band with zero non-excluded bins, or a refused
    floor/source pair.
    """
    # The rule binding a floor to its provenance lives in the type (#3522).
    entanglement = gating.EntanglementFloor(
        entanglement_floor_hz, entanglement_floor_source
    )

    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    spec_smoothed_db = np.asarray(spec_smoothed_db, dtype=np.float64)

    if freqs_hz.ndim != 1 or spec_smoothed_db.ndim != 1:
        raise ValueError(
            "freqs_hz and spec_smoothed_db must be 1-D arrays "
            f"(got ndim={freqs_hz.ndim} and ndim={spec_smoothed_db.ndim})"
        )
    if freqs_hz.size == 0 or spec_smoothed_db.size == 0:
        raise ValueError("freqs_hz and spec_smoothed_db must not be empty")
    if freqs_hz.shape != spec_smoothed_db.shape:
        raise ValueError(
            f"freqs_hz shape {freqs_hz.shape} does not match "
            f"spec_smoothed_db shape {spec_smoothed_db.shape}"
        )

    if exclusion_mask is None:
        resolved_exclusion_mask = np.zeros_like(freqs_hz, dtype=bool)
    else:
        resolved_exclusion_mask = np.asarray(exclusion_mask, dtype=bool)
        if resolved_exclusion_mask.shape != freqs_hz.shape:
            raise ValueError(
                f"exclusion_mask shape {resolved_exclusion_mask.shape} does not "
                f"match freqs_hz shape {freqs_hz.shape}"
            )

    if not np.all(np.isfinite(freqs_hz)) or not np.all(np.isfinite(spec_smoothed_db)):
        raise ValueError(
            "freqs_hz and spec_smoothed_db must contain only finite values "
            "(found NaN or Inf)"
        )

    # Checked after finiteness, so a NaN axis reports as non-finite rather
    # than a spurious ordering failure.
    if np.any(np.diff(freqs_hz) <= 0.0):
        raise ValueError(
            "freqs_hz must be strictly increasing (the merged exclusion "
            "intervals treat index adjacency as frequency adjacency)"
        )

    included_mask = ~resolved_exclusion_mask

    # The clamp is applied to the reference band first, because every band's
    # deviation is stated against it.
    nominal_ref_lo_hz, nominal_ref_hi_hz = REFERENCE_BAND_HZ
    ref_lo_hz = _graded_lo_hz(nominal_ref_lo_hz, trusted_floor_hz)
    ref_hi_hz = _graded_hi_hz(nominal_ref_hi_hz, trusted_ceiling_hz)
    ref_band_mask = (freqs_hz >= ref_lo_hz) & (freqs_hz < ref_hi_hz) & included_mask
    if not ref_band_mask.any():
        raise ValueError(
            f"reference band {ref_lo_hz}-{ref_hi_hz} Hz has zero non-excluded "
            "bins; cannot compute reference level"
        )
    reference_db = (
        power_mean_db(spec_smoothed_db[ref_band_mask])
        if reference_db_override is None
        else float(reference_db_override)
    )

    deviation_db = spec_smoothed_db - reference_db

    band_results: list[BandResult] = []
    for nominal_lo_hz, nominal_hi_hz, tolerance_db in SPEC_BANDS:
        f_lo_hz = _graded_lo_hz(nominal_lo_hz, trusted_floor_hz)
        f_hi_hz = _graded_hi_hz(nominal_hi_hz, trusted_ceiling_hz)
        # The clamped edge defines membership, so a sub-floor bin is not in
        # the band at all: `n_excluded` stays the interference screen's own.
        band_mask = (freqs_hz >= f_lo_hz) & (freqs_hz < f_hi_hz)
        included_band_mask = band_mask & included_mask
        n_bins = int(band_mask.sum())
        n_excluded = int((band_mask & resolved_exclusion_mask).sum())
        room_entangled_below_hz = _room_entangled_below_hz(
            f_lo_hz, f_hi_hz, entanglement.hz
        )
        if not included_band_mask.any():
            band_results.append(
                BandResult(
                    f_lo_hz=float(nominal_lo_hz),
                    f_hi_hz=float(nominal_hi_hz),
                    tolerance_db=float(tolerance_db),
                    max_deviation_db=None,
                    max_deviation_hz=None,
                    rms_deviation_db=None,
                    n_bins=n_bins,
                    n_excluded=n_excluded,
                    evaluable=False,
                    within_target=None,
                    graded_lo_hz=f_lo_hz,
                    graded_hi_hz=f_hi_hz,
                    room_entangled_below_hz=room_entangled_below_hz,
                )
            )
            continue
        band_indices = np.flatnonzero(included_band_mask)
        band_deviation_db = deviation_db[band_indices]
        worst = int(band_indices[np.argmax(np.abs(band_deviation_db))])
        max_deviation_db = float(deviation_db[worst])
        # Compared against the LOWEST INCLUDED bin, not `f_lo_hz` —
        # exclusion can take the graded edge itself.
        max_at_graded_edge = bool(
            f_lo_hz > nominal_lo_hz and worst == int(band_indices[0])
        )
        rms_deviation_db = float(np.sqrt(np.mean(np.square(band_deviation_db))))
        # The attribution split. Nothing below feeds `within_target`.
        band_level_db = power_mean_db(spec_smoothed_db[band_indices])
        band_ripple_db = spec_smoothed_db[band_indices] - band_level_db
        worst_ripple = int(band_indices[np.argmax(np.abs(band_ripple_db))])
        band_results.append(
            BandResult(
                f_lo_hz=float(nominal_lo_hz),
                f_hi_hz=float(nominal_hi_hz),
                tolerance_db=float(tolerance_db),
                max_deviation_db=max_deviation_db,
                max_deviation_hz=float(freqs_hz[worst]),
                rms_deviation_db=rms_deviation_db,
                n_bins=n_bins,
                n_excluded=n_excluded,
                evaluable=True,
                within_target=bool(abs(max_deviation_db) <= tolerance_db),
                level_deviation_db=float(band_level_db - reference_db),
                max_ripple_db=float(spec_smoothed_db[worst_ripple] - band_level_db),
                max_ripple_hz=float(freqs_hz[worst_ripple]),
                graded_lo_hz=f_lo_hz,
                graded_hi_hz=f_hi_hz,
                max_at_graded_edge=max_at_graded_edge,
                room_entangled_below_hz=room_entangled_below_hz,
            )
        )

    overall_within_target = all(
        band.evaluable and band.within_target for band in band_results
    )
    excluded_intervals = merged_true_intervals(freqs_hz, resolved_exclusion_mask)
    # Where grading stops, and SPEC_BANDS[-1]'s own upper edge, are one number
    # by construction — so this is that edge, not a second reading of it.
    graded_top_hz = _graded_hi_hz(BEST_EFFORT_ABOVE_HZ, trusted_ceiling_hz)

    return FlatSpecReport(
        reference_db=reference_db,
        bands=tuple(band_results),
        overall_within_target=overall_within_target,
        excluded_intervals=excluded_intervals,
        best_effort_above_hz=graded_top_hz,
        smoothing_fraction=int(smoothing_fraction),
        trusted_floor_hz=(
            float(trusted_floor_hz)
            if trusted_floor_hz is not None and math.isfinite(trusted_floor_hz)
            else None
        ),
        reference_band_hz=(ref_lo_hz, ref_hi_hz),
        trusted_ceiling_hz=(
            float(trusted_ceiling_hz)
            if trusted_ceiling_hz is not None and math.isfinite(trusted_ceiling_hz)
            else None
        ),
        graded_band_hz=(
            _graded_lo_hz(SPEC_BANDS[0][0], trusted_floor_hz), graded_top_hz,
        ),
        entanglement_floor_hz=entanglement.hz,
        entanglement_floor_source=entanglement.source,
    )


@dataclass(frozen=True)
class ConvergenceResidual:
    """The S3 closed loop's residual metric for one evaluation. ``rms_db``
    is RMS deviation over every non-excluded bin of every :data:`SPEC_BANDS`
    band, pooled — ``None`` when no band was evaluable. ``n_bins`` is the
    pooled bin count; ``n_excluded`` counts across ALL bands including any
    left unevaluable. ``evaluable`` is ``n_bins > 0`` — False means no
    residual, not a residual of zero.
    """

    rms_db: float | None
    n_bins: int
    n_excluded: int
    evaluable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "evaluable": self.evaluable,
        }


def spec_convergence_residual(report: FlatSpecReport) -> ConvergenceResidual:
    """The residual a closed correction loop converges on: RMS deviation
    over the non-excluded bins of the spec bands, pooled across all three.
    Holds no threshold, makes no verdict. Derived from the report, not
    recomputed from the curve — reassembled as
    ``sqrt(sum_b n_b * rms_b**2 / sum_b n_b)`` with
    ``n_b = band.n_bins - band.n_excluded``, exactly the RMS over the union
    of those bins. Bins at or above
    :attr:`FlatSpecReport.best_effort_above_hz` never enter it. ``n_bins``/
    ``n_excluded`` ride along so a residual that fell because the honesty
    mask grew is not mistaken for convergence. With no evaluable band,
    ``rms_db`` is ``None`` and ``evaluable`` is ``False`` rather than 0.0.
    """
    n_excluded = sum(band.n_excluded for band in report.bands)
    # One pass, so numerator and denominator can't be assembled from
    # different band sets.
    measured = [
        (band.n_bins - band.n_excluded, band.rms_deviation_db)
        for band in report.bands
        if band.rms_deviation_db is not None
    ]
    n_bins = sum(count for count, _rms_db in measured)
    if n_bins <= 0:
        return ConvergenceResidual(
            rms_db=None, n_bins=0, n_excluded=n_excluded, evaluable=False,
        )
    sum_squares = sum(count * rms_db ** 2 for count, rms_db in measured)
    return ConvergenceResidual(
        rms_db=float(np.sqrt(sum_squares / n_bins)),
        n_bins=n_bins,
        n_excluded=n_excluded,
        evaluable=True,
    )
