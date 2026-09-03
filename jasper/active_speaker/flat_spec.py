# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The flat-linearization spec evaluator.

Pure computation. One question: does this spatially-combined,
1/3-oct-smoothed magnitude curve meet the flat-linearization spec? The caller
supplies the curve, its axis and an optional exclusion mask; nothing here
combines, smooths, detects interference, or holds gate policy.
:func:`spec_convergence_residual`, :func:`spec_flatness_gauge` and
:func:`spec_band_tilt` are further readings of the SAME report, lifted from it
rather than recomputed, and none holds a threshold. The definition is in
docs/historical/linearization-campaign-2026-07.md, "The spec"; the reference
anchor is ADR-0194.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement import gating
from jasper.audio_measurement.room_boundary import GATED_SPEC_LOWER_EDGE_HZ
from jasper.audio_measurement.spatial_combine import merged_true_intervals

# Where grading stops, Hz. At or above this frequency the spec is best-effort:
# never evaluated against a tolerance, never counted toward overall_passed.
# NOMINAL, like GATED_SPEC_LOWER_EDGE_HZ — a `trusted_ceiling_hz` moves it in
# either direction (ADR-0194), and `FlatSpecReport.best_effort_above_hz`
# publishes where it actually landed.
BEST_EFFORT_ABOVE_HZ: float = 16000.0

# The adopted spec table. Each entry is (f_lo_hz, f_hi_hz, tolerance_db);
# band membership is f_lo <= f < f_hi (inclusive-lower, exclusive-upper).
#
# Neither OUTER EDGE is a literal: the lower one is the seam with the
# room-correction layer, owned by jasper.audio_measurement.room_boundary; the
# upper one is BEST_EFFORT_ABOVE_HZ. These edges are NOMINAL — an evaluation
# grades this table intersected with the session's trusted floor and ceiling.
SPEC_BANDS: tuple[tuple[float, float, float], ...] = (
    (GATED_SPEC_LOWER_EDGE_HZ, 2000.0, 1.5),
    (2000.0, 8000.0, 2.0),
    (8000.0, BEST_EFFORT_ABOVE_HZ, 2.5),
)

# The reference band is SPEC_BANDS[0] exactly — the LOW-MID band alone, so no
# band above 2 kHz is pooled into the zero its own deviation is stated against
# (ADR-0194). Same edge rule as SPEC_BANDS. Nominal too: a `trusted_floor_hz`
# raises this lower edge and has to, since sub-floor bins left in the frame
# while removed from every band would re-centre that zero.
# Not `gate_sweep.REFERENCE_BAND_HZ`: that one normalises, it grades nothing.
REFERENCE_BAND_HZ: tuple[float, float] = (GATED_SPEC_LOWER_EDGE_HZ, 2000.0)


@dataclass(frozen=True)
class BandResult:
    """One :data:`SPEC_BANDS` entry's evaluation outcome.

    A band left with zero non-excluded bins is ``evaluable=False`` with
    ``passed=None`` and ``None`` metrics — no evidence, which is neither a
    pass nor a failure, and :attr:`FlatSpecReport.overall_passed` treats it as
    not-passed.

    Args:
      f_lo_hz: the band's NOMINAL lower edge (inclusive), i.e. its
        :data:`SPEC_BANDS` row; read ``graded_lo_hz`` for the edge the metrics
        were actually taken from.
      f_hi_hz: the band's NOMINAL upper edge (exclusive).
      tolerance_db: the band's +/- tolerance from :data:`SPEC_BANDS`.
      max_deviation_db: the **signed** deviation at the largest-*absolute*
        non-excluded bin. ``None`` when unevaluable.
      max_deviation_hz: that bin's frequency. ``None`` when unevaluable.
      rms_deviation_db: RMS deviation over the band's non-excluded bins.
      n_bins: total bins in the graded span, excluded or not.
      n_excluded: how many of those were interference-flagged and so left out
        of every metric above.
      evaluable: whether any non-excluded bin survived to be measured.
      passed: ``abs(max_deviation_db) <= tolerance_db``, or ``None``.
      level_deviation_db: the band's OWN power-mean level minus
        :attr:`FlatSpecReport.reference_db` — where the whole band sits
        relative to the shared frame. ``None`` when unevaluable or absent.
      max_ripple_db: the **signed** worst deviation of a non-excluded bin from
        **the band's own level**, and therefore invariant to the reference
        frame entirely.
      max_ripple_hz: that bin's frequency, which equals ``max_deviation_hz``
        only when the band's level offset is zero.
      graded_lo_hz: the lower edge the metrics were **actually** taken from,
        ``max(f_lo_hz, trusted_floor_hz)``. ``>= f_hi_hz`` when the floor
        swallowed the band whole, which is the tell that ``evaluable=False``
        means "below this session's trusted floor". ``None`` on a report that
        does not carry the clamp.
      graded_hi_hz: its mirror. The TOP band's edge follows the ceiling in
        BOTH directions, because that edge and :data:`BEST_EFFORT_ABOVE_HZ`
        are one number; every lower band's edge is only ever lowered.
      max_at_graded_edge: this band was floor-truncated AND its worst bin is
        its lowest graded one, so ``max_deviation_db`` is a maximum over a
        SUBSET and a LOWER BOUND on the band's real worst deviation.
        Disclosure only; tests no SLOPE and does not feed ``passed``.
      room_entangled_below_hz: the upper edge of this band's sub-span below
        the room's floor, or ``None``. Disclosure only.
      gate_sensitivity_db: the gate sweep's null-model-corrected delta at THIS
        band's ``max_deviation_hz`` — how much of the worst bin's depth
        arrived with the analysis window rather than with the speaker.
      sigma_growth_ratio: across-pose sigma at the longest resolution-valid
        rung over the shortest one, at the same bin.
      n_valid_rungs: how many ladder rungs were resolution-valid at that bin.
        Present whenever the ladder ran.
      gate_sensitivity_note: WHY the three fields above are ``None``. The
        two-vocabulary distinction is explained beside the constants that
        spell it, in :mod:`~jasper.active_speaker.crossover_v2.round_views`.
      gate_sensitivity_detail: the ``RoundCapturesRefused`` behind a
        capture-refusal note; ``None`` for every other note.
      gate_window_verdict: the ladder's own room/speaker call at that bin —
        ``"stable"``, ``"moved"`` or ``"unresolved"``. ``None`` only when the
        band was never swept.
      gate_window_verdict_reasons: which routes produced that verdict. Empty
        on ``"stable"``; ``None`` exactly when ``gate_window_verdict`` is.

    The seven gate fields are DISCLOSURE ONLY, stamped after the fact by
    :func:`~jasper.active_speaker.crossover_v2.round_views.spec_with_gate_sensitivity`.
    Nothing in this module computes them and no grade reads them.

    For every non-excluded bin the two readings decompose exactly::

        deviation_i = ripple_i + level_deviation_db

    one term a *different* band's level can move and one it structurally
    cannot. The identity is PER BIN: ``max_deviation_db`` and
    ``max_ripple_db`` are taken at bins chosen by different criteria and do
    not add. ``abs(max_deviation_db) >= abs(level_deviation_db)`` always holds.
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
    passed: bool | None
    # Defaulted, unlike every field above: a report can be hand-built or
    # rehydrated from persistence that does not carry these fields.
    level_deviation_db: float | None = None
    max_ripple_db: float | None = None
    max_ripple_hz: float | None = None
    graded_lo_hz: float | None = None
    graded_hi_hz: float | None = None
    max_at_graded_edge: bool | None = None
    room_entangled_below_hz: float | None = None
    # Defaulted for a DIFFERENT reason: nothing in this module ever fills
    # these in, so `None` is "no sweep has read this report".
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
            "passed": self.passed,
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
        re-derivation: no band edge, floor, or tolerance is recomputed.

        ``f_lo_hz`` through ``passed`` are read with hard indexing, so a
        document missing one is CORRUPT and raises instead of rehydrating a
        plausible-looking ``None``. Only the dataclass-defaulted fields are
        read with :meth:`dict.get`.
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
            passed=raw["passed"],
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

    ``excluded_intervals`` collapses contiguous runs of the exclusion mask into
    merged ``(f_lo_hz, f_hi_hz)`` tuples. Diagnostic disclosure only — pass/fail
    reads the mask directly. ``overall_passed`` is ``True`` only when **every**
    band is both evaluable and passing.

    ``smoothing_fraction`` is **caller attestation**, not a measurement:
    nothing here smooths or validates it.

    ``trusted_floor_hz`` and ``trusted_ceiling_hz`` are the clamps this
    evaluation was intersected at, echoed so a stored report says which honesty
    limits produced its numbers; ``None`` means "not stated", never "zero".
    ``reference_band_hz`` is the span whose power mean IS ``reference_db``
    after those clamps. ``graded_band_hz`` is the whole span the table graded,
    and is NOT ``reference_band_hz``: deviations are stated FROM the low-mid
    band alone but OVER the span running to the trusted ceiling.

    ``entanglement_floor_hz``/``_source`` are the ROOM's floor and its
    provenance, echoed but clamping nothing; what they mark is
    :attr:`BandResult.room_entangled_below_hz`.

    ``gate_sweep_frame`` is the frame every band's gate fields are stated in.
    It travels WITH the numbers because one capture and one feature read a
    materially different depth under each defensible frame (#3495). ``None``
    on every report this module builds.
    """

    reference_db: float
    bands: tuple[BandResult, ...]
    overall_passed: bool
    excluded_intervals: tuple[tuple[float, float], ...]
    best_effort_above_hz: float
    smoothing_fraction: int
    # Defaulted like `BandResult`'s split fields; the defaults say "nothing
    # clamped".
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

        Every re-grade in the round's frame states these four together or
        states a frame nobody produced; splatting one dict is what makes
        "together" structural. Deliberately NOT ``smoothing_fraction``: that is
        caller attestation about ONE curve.
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
            "bands": [band.to_dict() for band in self.bands],
            "overall_passed": self.overall_passed,
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
        re-derivation.

        Same two read rules :meth:`BandResult.from_dict` documents, with
        ``excluded_intervals`` read hard: a document missing it raises rather
        than rehydrating an empty tuple that reads as "nothing was excluded".
        ``gate_sweep_frame`` rehydrates only from a mapping; anything else
        becomes ``None``. The two entanglement fields are read as a PAIR
        through :meth:`~jasper.audio_measurement.gating.EntanglementFloor.coerce`
        — the lenient door, where :func:`evaluate_flat_spec` takes the strict
        one — so a rehydrated report can never be re-graded into a refusal by
        a disagreement it inherited.
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
            overall_passed=bool(raw["overall_passed"]),
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


@dataclass(frozen=True)
class GradedSpec:
    """One :func:`evaluate_flat_spec` call: its inputs and its verdict together,
    so a consumer reading the graded CURVE cannot pair it with someone else's
    mask or a re-derived report.

    ``excluded`` is the mask as it was HANDED to the evaluator. A live
    in-process handoff, not persisted and not a wire shape, which is why it may
    hold arrays at all.
    """

    freqs_hz: np.ndarray
    curve_db: np.ndarray
    excluded: np.ndarray
    report: FlatSpecReport


def _power_mean_db(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` — the power (energy) mean, NOT a linear
    average of dB values. The single place that conversion happens here.
    """
    linear = np.power(10.0, values_db / 10.0)
    return float(10.0 * np.log10(np.mean(linear)))


def _graded_lo_hz(f_lo_hz: float, trusted_floor_hz: float | None) -> float:
    """``max(f_lo_hz, trusted_floor_hz)`` — one band edge, intersected with the
    session's trusted floor.

    The single place that intersection happens, so the bands and the reference
    band cannot drift apart on it. A ``None`` or non-finite floor clamps
    nothing; the finiteness guard is load-bearing because :func:`max` with a
    NaN is order-dependent.
    """
    if trusted_floor_hz is None or not math.isfinite(trusted_floor_hz):
        return float(f_lo_hz)
    return max(float(f_lo_hz), float(trusted_floor_hz))


def _graded_hi_hz(f_hi_hz: float, trusted_ceiling_hz: float | None) -> float:
    """One band's upper edge, intersected with the session's trusted ceiling.

    The mirror of :func:`_graded_lo_hz` with the one asymmetry that is the
    point of it: the floor only ever RAISES an edge, while the TOP band's edge
    follows the ceiling in BOTH directions — that edge and
    :data:`BEST_EFFORT_ABOVE_HZ` are one number. Every lower band's edge is
    only ever lowered. Same NaN guard as :func:`_graded_lo_hz`.
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

    The mirror of :func:`_graded_lo_hz` in spirit and its opposite in effect:
    that one MOVES an edge and changes what is graded, this one only marks. A
    floor that is not ``None`` here is finite and positive already — it came
    off an :class:`~jasper.audio_measurement.gating.EntanglementFloor`.
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

    Args:
        freqs_hz: 1-D **strictly ascending** frequency axis, Hz. Required, not
            assumed: the merged exclusion intervals use index adjacency as a
            proxy for frequency adjacency.
        spec_smoothed_db: 1-D magnitude curve, dB, same length as ``freqs_hz``,
            already combined and smoothed. Consumed verbatim.
        exclusion_mask: optional 1-D bool array. ``True`` marks an
            interference-flagged bin, excluded from the reference level AND
            from every band's deviation metrics.
        smoothing_fraction: the 1/N-octave fraction the caller attests to.
            Provenance only — nothing here validates or uses it.
        trusted_floor_hz: the session's gate-derived trusted floor, ``2.5/T``
            for the capture's reflection-free window. Every band's lower edge,
            **and the reference band's**, is raised to it before anything is
            measured. ``None`` or non-finite clamps nothing: "unknown" must not
            silently become a floor of zero. This module takes the number; it
            does not derive or second-guess it.
        trusted_ceiling_hz: the frequency above which this session's microphone
            is not trusted. The TOP band's upper edge, and with it
            :attr:`FlatSpecReport.best_effort_above_hz`, moves to this value;
            every lower band's is only lowered.
        reference_db_override: grade against a DIFFERENT capture's reference
            level without changing what the reference band is. The
            zero-non-excluded-bins check still runs first regardless.
        entanglement_floor_hz: the ROOM's floor. **Nothing is clamped and no
            grade changes**: it only sets each band's
            :attr:`BandResult.room_entangled_below_hz`.
        entanglement_floor_source: which of ``gating.ENTANGLEMENT_SOURCES`` the
            floor came from. Any other value, and any floor/source pair that
            disagrees about whether a floor is known, raises.

    Reference level: the power mean over non-excluded bins inside
    :data:`REFERENCE_BAND_HZ`, its lower edge raised to ``trusted_floor_hz``
    like every band's. Clamping is not free and its direction does not
    generalize: dropping the sub-floor region moves that zero, and the headline
    number moves with it — the same speaker graded on fewer bins, which
    :attr:`BandResult.n_bins` keeps visible.

    Each band carries the **attribution split** — ``level_deviation_db`` and
    ``max_ripple_db``/``max_ripple_hz``. Disclosure only: **no verdict reads
    them.**

    A band with zero non-excluded bins is reported ``evaluable=False`` rather
    than raised on: one band losing its evidence must not destroy the report
    for the other two, and ``graded_lo_hz >= graded_hi_hz`` distinguishes the
    clamped case. The **reference band** still raises — with no reference level
    there is nothing to compute a deviation against anywhere — and since it IS
    ``SPEC_BANDS[0]``, only the ceiling can leave a band unevaluable.

    Band membership is ``graded_lo <= f < graded_hi`` for both
    :data:`SPEC_BANDS` and :data:`REFERENCE_BAND_HZ`, which keeps the reference
    band's span exactly equal to ``SPEC_BANDS[0]`` and the best-effort boundary
    exactly adjacent to ``SPEC_BANDS[-1]``'s exclusive upper edge. Best-effort
    bins are never evaluated, never fail, and appear in no :class:`BandResult`.

    Raises:
        ValueError: for any degenerate input — empty or non-1-D arrays,
            mismatched lengths, a non-ascending ``freqs_hz``, non-finite
            values, a reference band left with zero non-excluded bins, or any
            floor/source pair ``EntanglementFloor`` refuses.
    """
    # THE validation of the pair: the rule binding a floor to its provenance
    # lives in the type, so this seam constructs one and lets its error
    # through (#3522).
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

    # Checked after finiteness, so a NaN axis is reported as non-finite rather
    # than as a spurious ordering failure (NaN comparisons are all False).
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
        _power_mean_db(spec_smoothed_db[ref_band_mask])
        if reference_db_override is None
        else float(reference_db_override)
    )

    deviation_db = spec_smoothed_db - reference_db

    band_results: list[BandResult] = []
    for nominal_lo_hz, nominal_hi_hz, tolerance_db in SPEC_BANDS:
        f_lo_hz = _graded_lo_hz(nominal_lo_hz, trusted_floor_hz)
        f_hi_hz = _graded_hi_hz(nominal_hi_hz, trusted_ceiling_hz)
        # The clamped edge defines membership, so a bin below the trusted floor
        # is not in the band at all rather than in it and excluded:
        # `n_excluded` stays the interference screen's own count.
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
                    passed=None,
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
        # Both halves are needed: an untruncated band's first bin IS the band's
        # own start. The comparison is against the LOWEST INCLUDED bin, not
        # `f_lo_hz` — exclusion can take the graded edge itself.
        max_at_graded_edge = bool(
            f_lo_hz > nominal_lo_hz and worst == int(band_indices[0])
        )
        rms_deviation_db = float(np.sqrt(np.mean(np.square(band_deviation_db))))
        # The attribution split. Same `_power_mean_db` as the reference, so
        # there is one averaging convention here. Nothing below feeds `passed`.
        band_level_db = _power_mean_db(spec_smoothed_db[band_indices])
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
                passed=bool(abs(max_deviation_db) <= tolerance_db),
                level_deviation_db=float(band_level_db - reference_db),
                max_ripple_db=float(spec_smoothed_db[worst_ripple] - band_level_db),
                max_ripple_hz=float(freqs_hz[worst_ripple]),
                graded_lo_hz=f_lo_hz,
                graded_hi_hz=f_hi_hz,
                max_at_graded_edge=max_at_graded_edge,
                room_entangled_below_hz=room_entangled_below_hz,
            )
        )

    overall_passed = all(band.evaluable and band.passed for band in band_results)
    excluded_intervals = merged_true_intervals(freqs_hz, resolved_exclusion_mask)
    # Where grading stops, and SPEC_BANDS[-1]'s own upper edge, are one number
    # by construction — so this is that edge, not a second reading of it.
    graded_top_hz = _graded_hi_hz(BEST_EFFORT_ABOVE_HZ, trusted_ceiling_hz)

    return FlatSpecReport(
        reference_db=reference_db,
        bands=tuple(band_results),
        overall_passed=overall_passed,
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
    """The S3 closed loop's residual metric for one evaluation.

    Args:
      rms_db: RMS deviation over every non-excluded bin of every
        :data:`SPEC_BANDS` band, pooled. ``None`` when no band was evaluable.
      n_bins: the pooled non-excluded spec-band bin count that RMS came from.
      n_excluded: how many spec-band bins the exclusion mask dropped, counted
        across ALL bands including any it left unevaluable.
      evaluable: ``n_bins > 0``. False means there is no residual, not a
        residual of zero.
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
    """The residual a closed correction loop converges on: RMS deviation over
    the non-excluded bins of the spec bands, pooled across all three.

    Holds no threshold and makes no verdict. **Derived from the report, not
    recomputed from the curve** — the pooled figure is reassembled as
    ``sqrt(sum_b n_b * rms_b**2 / sum_b n_b)`` with
    ``n_b = band.n_bins - band.n_excluded``, which is exactly the RMS over the
    union of those bins. Bins at or above
    :attr:`FlatSpecReport.best_effort_above_hz` never enter it, so a top octave
    the speaker cannot reach cannot stall the loop; that edge is read off the
    report so it follows the session's trusted ceiling.

    ``n_bins`` and ``n_excluded`` ride along because a residual that fell
    because the honesty mask grew is not convergence — it is the same speaker
    on fewer bins.

    With no evaluable band, ``rms_db`` is ``None`` and ``evaluable`` is
    ``False`` rather than a residual of 0.0. That state is unreachable from
    :func:`evaluate_flat_spec` but a hand-built report carries no such
    guarantee, and the alternative there is a ZeroDivisionError.
    """
    n_excluded = sum(band.n_excluded for band in report.bands)
    # One pass, so the denominator and the numerator can never be assembled
    # from different band sets: on a hand-built report a `None` rms and a zero
    # included-bin count need not coincide.
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


@dataclass(frozen=True)
class BandTilt:
    """How far the graded bands' own levels sit from EACH OTHER.

    The one figure on this report that **no reference-frame choice can move**.
    :attr:`FlatSpecReport.reference_db` is a power mean, so a band inside the
    pooled span that is uniformly off drags the shared zero toward itself and
    inflates every other band's deviation; a step between two band levels
    cannot, because the shared reference appears in both terms and cancels.
    The cancellation is exact in arithmetic and near-exact in floating point —
    measured across five candidate reference bands the worst spread in
    ``step_db`` is 8.882e-16 dB. Not a verdict and holds no threshold.

    Args:
      step_db: the largest absolute level difference between any two evaluable
        bands, as a **non-negative magnitude** — direction is carried by the
        two band fields. ``None`` when fewer than two bands carry a level.
      high_band_hz: ``(f_lo_hz, f_hi_hz)`` of the higher-sitting band of that
        pair. At ``step_db`` exactly ``0.0`` the high/low labels carry no
        information.
      low_band_hz: the lower-sitting one.
      n_bands: how many bands carried a level to compare — normally 3. A step
        chosen among two is a weaker statement than one chosen among three.
      evaluable: ``n_bands >= 2``. ``False`` means there is no step, not a step
        of zero.
    """

    step_db: float | None
    high_band_hz: tuple[float, float] | None
    low_band_hz: tuple[float, float] | None
    n_bands: int
    evaluable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_db": self.step_db,
            "high_band_hz": (
                list(self.high_band_hz) if self.high_band_hz is not None else None
            ),
            "low_band_hz": (
                list(self.low_band_hz) if self.low_band_hz is not None else None
            ),
            "n_bands": self.n_bands,
            "evaluable": self.evaluable,
        }


#: The "no step is knowable" :class:`BandTilt` — a null object, not a
#: fabricated reading. Only :class:`SpecFlatness` uses it, as the default for a
#: gauge that carries no tilt.
NO_BAND_TILT = BandTilt(
    step_db=None, high_band_hz=None, low_band_hz=None, n_bands=0, evaluable=False,
)


def spec_band_tilt(report: FlatSpecReport) -> BandTilt:
    """The largest level step between two of ``report``'s graded bands — the
    frame-free attribution reading.

    Derived from the report, not recomputed from the curve. The pair is chosen
    by largest absolute difference of :attr:`BandResult.level_deviation_db`;
    ties go to the lowest pair in :data:`SPEC_BANDS` order, matching
    :func:`spec_flatness_gauge`'s tie rule. Bands that are unevaluable, or that
    carry no ``level_deviation_db``, are skipped rather than defaulted to zero:
    inventing 0 dB would manufacture exactly the false step this exposes.
    """
    levelled = [
        (band, band.level_deviation_db)
        for band in report.bands
        if band.evaluable and band.level_deviation_db is not None
    ]
    if len(levelled) < 2:
        return BandTilt(
            step_db=None,
            high_band_hz=None,
            low_band_hz=None,
            n_bands=len(levelled),
            evaluable=False,
        )
    pairs: list[tuple[float, BandResult, BandResult]] = []
    for index, (band_a, level_a) in enumerate(levelled):
        for band_b, level_b in levelled[index + 1:]:
            high, low = (band_a, band_b) if level_a >= level_b else (band_b, band_a)
            pairs.append((abs(level_a - level_b), high, low))
    # `len(levelled) >= 2` above guarantees at least one pair.
    step_db, high_band, low_band = max(pairs, key=lambda pair: pair[0])
    return BandTilt(
        step_db=step_db,
        high_band_hz=(high_band.f_lo_hz, high_band.f_hi_hz),
        low_band_hz=(low_band.f_lo_hz, low_band.f_hi_hz),
        n_bands=len(levelled),
        evaluable=True,
    )


@dataclass(frozen=True)
class SpecFlatness:
    """The household-facing "how flat is the speaker" figures for one report.

    Every field is **lifted from the report**, never recomputed from a curve,
    so a gauge, a ledger line and the report shown for one session are the same
    numbers by construction.

    Args:
      max_db: the **signed** deviation at the worst bin of the worst evaluable
        band — the same convention as :attr:`BandResult.max_deviation_db`.
      max_hz: that bin's frequency.
      max_band_hz: ``(f_lo_hz, f_hi_hz)`` of the band that bin lives in, i.e.
        which tolerance row the number is judged against. **Not the frame the
        deviation is measured FROM** and **not "the band to fix"**: it is the
        band furthest from the shared frame, and a band that is uniformly off
        drags that frame toward itself, so this can name a flat band made to
        look proud by another band's deficit. ``tilt`` below cannot, and a
        surface rendering this pointer should render that one beside it.
      reference_band_hz: :attr:`FlatSpecReport.reference_band_hz` — the CLAMPED
        span whose power mean is the zero every ``max_db`` is stated against,
        never the module constant. Always populated, including when
        ``evaluable`` is ``False``.
      tolerance_db: that band's tolerance.
      max_band_level_deviation_db: that band's
        :attr:`BandResult.level_deviation_db` — how much of ``max_db`` is just
        where the whole band sits rather than anything happening at ``max_hz``.
      max_band_ripple_db: that band's :attr:`BandResult.max_ripple_db`. The two
        together disarm the pointer at the point of use: a band reading
        ``+3.26 dB`` of level and ``-0.10 dB`` of ripple is flat and merely
        sits high, so there is no peak at ``max_hz`` to EQ.
      rms_db: :attr:`ConvergenceResidual.rms_db`.
      n_bins: how many bins that RMS was computed from.
      n_excluded: how many spec-band bins the exclusion mask removed. A BIN
        count, deliberately not a count of
        :attr:`FlatSpecReport.excluded_intervals`, which spans the whole axis.
      evaluable: whether ANY band survived to be measured. ``False`` means
        there is no flatness number, not a flatness of zero.
      passed: :attr:`FlatSpecReport.overall_passed`, verbatim. **Read it with**
        ``evaluable``: ``passed=False, evaluable=False`` means "could not be
        measured", not "failed".
      tilt: :func:`spec_band_tilt` of the same report. Defaults to
        :data:`NO_BAND_TILT`.
    """

    max_db: float | None
    max_hz: float | None
    max_band_hz: tuple[float, float] | None
    tolerance_db: float | None
    rms_db: float | None
    n_bins: int
    n_excluded: int
    evaluable: bool
    passed: bool
    reference_band_hz: tuple[float, float] = REFERENCE_BAND_HZ
    tilt: BandTilt = NO_BAND_TILT
    max_band_level_deviation_db: float | None = None
    max_band_ripple_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_db": self.max_db,
            "max_hz": self.max_hz,
            "max_band_hz": (
                list(self.max_band_hz) if self.max_band_hz is not None else None
            ),
            "reference_band_hz": list(self.reference_band_hz),
            "tolerance_db": self.tolerance_db,
            "max_band_level_deviation_db": self.max_band_level_deviation_db,
            "max_band_ripple_db": self.max_band_ripple_db,
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "evaluable": self.evaluable,
            "passed": self.passed,
            "tilt": self.tilt.to_dict(),
        }


def spec_flatness_gauge(report: FlatSpecReport) -> SpecFlatness:
    """Reduce one :class:`FlatSpecReport` to the figures a household-facing
    flatness gauge renders.

    Derived from the report, not recomputed from the curve. The worst band is
    the one whose :attr:`BandResult.max_deviation_db` has the largest absolute
    value among evaluable bands, ties to the lowest band — deliberately NOT
    "the band that failed by the widest margin relative to its own tolerance",
    which would answer a different question than the rendered dB reading.

    Which band that picks is frame-dependent and deliberately left so: a
    uniformly-off band drags the pooled reference and this walk can name a flat
    band as the worst one. So this function carries :func:`spec_band_tilt`
    beside the pointer, and a reader is never handed the pointer alone.
    """
    residual = spec_convergence_residual(report)
    tilt = spec_band_tilt(report)
    worst: BandResult | None = None
    worst_magnitude_db = -1.0
    for band in report.bands:
        if not band.evaluable or band.max_deviation_db is None:
            continue
        magnitude_db = abs(band.max_deviation_db)
        # Strict `>` is what makes ties go to the LOWEST band: SPEC_BANDS is
        # ordered low-to-high and this walks it in order.
        if magnitude_db > worst_magnitude_db:
            worst, worst_magnitude_db = band, magnitude_db
    if worst is None:
        return SpecFlatness(
            max_db=None,
            max_hz=None,
            max_band_hz=None,
            tolerance_db=None,
            rms_db=residual.rms_db,
            n_bins=residual.n_bins,
            n_excluded=residual.n_excluded,
            evaluable=False,
            passed=report.overall_passed,
            reference_band_hz=report.reference_band_hz,
            tilt=tilt,
        )
    return SpecFlatness(
        max_db=worst.max_deviation_db,
        max_hz=worst.max_deviation_hz,
        max_band_hz=(worst.f_lo_hz, worst.f_hi_hz),
        tolerance_db=worst.tolerance_db,
        rms_db=residual.rms_db,
        n_bins=residual.n_bins,
        n_excluded=residual.n_excluded,
        evaluable=True,
        passed=report.overall_passed,
        # The frame that was USED, read off the report, not the module
        # constant: a clamped reference band re-centres every number above.
        reference_band_hz=report.reference_band_hz,
        tilt=tilt,
        max_band_level_deviation_db=worst.level_deviation_db,
        max_band_ripple_db=worst.max_ripple_db,
    )
