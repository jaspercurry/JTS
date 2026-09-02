# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The flat-linearization spec evaluator.

Pure computation -- numpy plus one shared interval-merge helper. No I/O, no
logging, no product policy, no CamillaDSP/emission imports. It answers one
question: does this spatially-combined, 1/3-oct-smoothed magnitude curve meet
the flat-linearization spec? The caller supplies that curve, its frequency
axis and an optional interference-exclusion mask as plain arrays; nothing
here combines captures, smooths, or detects interference.

:func:`spec_convergence_residual`, :func:`spec_flatness_gauge` and
:func:`spec_band_tilt` are further readings of the SAME report, each lifted
from it rather than recomputed from the curve, and none holds a threshold.

See docs/historical/linearization-campaign-2026-07.md, section "The spec -- what 'flat' means
here," for the definition this module implements: deviation = curve -
reference, evaluated per band at the tolerances in :data:`SPEC_BANDS`, with
interference-flagged bins excluded from both the reference and every band's
deviation metric. The reference is a power mean over
:data:`REFERENCE_BAND_HZ`, the LOW-MID band alone -- see that constant for
the anchor decision behind it (See ADR-0194).

:data:`SPEC_BANDS` and :data:`REFERENCE_BAND_HZ` are room-agnostic nominal
constants. What an evaluation actually grades is that table intersected with
the session's trusted floor and ceiling -- see :func:`evaluate_flat_spec`.
This module holds no gate policy; it takes both as numbers from a caller
that measured them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement import gating
from jasper.audio_measurement.room_boundary import GATED_SPEC_LOWER_EDGE_HZ
from jasper.audio_measurement.spatial_combine import merged_true_intervals

# Where grading stops, Hz. At or above this frequency the spec is
# best-effort: never evaluated against a tolerance, never counted toward
# overall_passed. A bin at exactly this frequency is best-effort, not the top
# of SPEC_BANDS[-1] (the same value, by reference below) -- the two partitions
# meet with no gap or overlap.
#
# NOMINAL, the same way GATED_SPEC_LOWER_EDGE_HZ is: a `trusted_ceiling_hz`
# moves it in either direction (See ADR-0194).
# `FlatSpecReport.best_effort_above_hz` publishes where it actually landed.
BEST_EFFORT_ABOVE_HZ: float = 16000.0

# The adopted spec table -- docs/historical/linearization-campaign-2026-07.md, "The spec --
# what 'flat' means here." Each entry is (f_lo_hz, f_hi_hz, tolerance_db);
# band membership is f_lo <= f < f_hi (inclusive-lower, exclusive-upper).
#
# Neither OUTER EDGE is a literal here: the lower one is the seam with the
# room-correction layer, owned by jasper.audio_measurement.room_boundary; the
# upper one is BEST_EFFORT_ABOVE_HZ above. The tolerances and the inner edges
# are this module's own.
#
# These edges are NOMINAL. What a given evaluation actually grades is this
# table intersected with that session's trusted floor and ceiling -- see
# `evaluate_flat_spec`'s `trusted_floor_hz`/`trusted_ceiling_hz` and
# `BandResult.graded_lo_hz`/`graded_hi_hz`.
SPEC_BANDS: tuple[tuple[float, float, float], ...] = (
    (GATED_SPEC_LOWER_EDGE_HZ, 2000.0, 1.5),
    (2000.0, 8000.0, 2.0),
    (8000.0, BEST_EFFORT_ABOVE_HZ, 2.5),
)

# The reference band is SPEC_BANDS[0] exactly -- the LOW-MID band alone, so no
# band above 2 kHz is pooled into the zero its own deviation is stated against
# (See ADR-0194). Same inclusive-lower/exclusive-upper edge rule as
# SPEC_BANDS, so the two spans coincide with no gap or overlap at the 2 kHz
# seam, and the lower edge reads the same owner.
#
# Nominal, like SPEC_BANDS: a `trusted_floor_hz` raises this lower edge too,
# and has to. The reference is a power mean, so sub-floor bins left in the
# frame while removed from every band would re-centre the zero each surviving
# deviation is stated against.
# `FlatSpecReport.reference_band_hz` publishes the span actually pooled.
REFERENCE_BAND_HZ: tuple[float, float] = (GATED_SPEC_LOWER_EDGE_HZ, 2000.0)


@dataclass(frozen=True)
class BandResult:
    """One :data:`SPEC_BANDS` entry's evaluation outcome.

    A band left with zero non-excluded bins -- the axis never reached it, the
    trusted floor or ceiling left nothing of it, or the interference screen
    flagged every bin -- is ``evaluable=False`` with ``passed=None`` and
    ``None`` metrics. That is no evidence, which is neither a pass nor a
    failure; :attr:`FlatSpecReport.overall_passed` treats it as not-passed, so
    it can never be mistaken for a clean band.

    Args:
      f_lo_hz: the band's NOMINAL lower edge (inclusive), i.e. its
        :data:`SPEC_BANDS` row -- so a reader can tell which tolerance row
        this result answers for even when nothing in it was graded. Read
        ``graded_lo_hz`` for the edge the metrics were actually taken from.
      f_hi_hz: the band's NOMINAL upper edge (exclusive), read like
        ``f_lo_hz`` against ``graded_hi_hz``.
      tolerance_db: the band's +/- tolerance from :data:`SPEC_BANDS`.
      max_deviation_db: the **signed** deviation at the largest-*absolute*
        non-excluded bin -- signed, because "2.4 dB too loud" and "2.4 dB too
        quiet" call for opposite corrections. ``None`` when unevaluable.
      max_deviation_hz: that bin's frequency. ``None`` when unevaluable.
      rms_deviation_db: RMS deviation over the band's non-excluded bins.
        ``None`` when unevaluable.
      n_bins: total bins in the graded span, excluded or not.
      n_excluded: how many of those were interference-flagged, and so left
        out of every metric above. ``n_bins - n_excluded`` is what they were
        computed from.
      evaluable: whether any non-excluded bin survived to be measured.
      passed: ``abs(max_deviation_db) <= tolerance_db``, or ``None`` when
        unevaluable.
      level_deviation_db: the band's OWN power-mean level (same non-excluded
        bins) minus :attr:`FlatSpecReport.reference_db` -- where the whole
        band sits relative to the shared frame, saying nothing about what
        happens inside it. ``None`` when unevaluable, or on a report that
        does not carry the split.
      max_ripple_db: the **signed** worst deviation of a non-excluded bin
        from **the band's own level** rather than from
        :attr:`FlatSpecReport.reference_db`, and therefore invariant to the
        reference frame entirely: change :data:`REFERENCE_BAND_HZ` to
        anything and this number does not move. ``None`` when unevaluable /
        absent.
      max_ripple_hz: that bin's frequency. Deliberately NOT assumed equal to
        ``max_deviation_hz``: the two coincide only when the band's level
        offset is zero. ``None`` when unevaluable / absent.
      graded_lo_hz: the lower edge the metrics above were **actually** taken
        from -- ``max(f_lo_hz, trusted_floor_hz)``. Equal to ``f_lo_hz`` when
        no floor was supplied or it sits below the band, and **>=**
        ``f_hi_hz`` when the floor swallowed the band whole, which is the
        tell that ``evaluable=False`` here means "below this session's
        trusted floor" rather than "the axis never reached it" or "the screen
        took every bin". ``None`` on a report that does not carry the clamp;
        read ``f_lo_hz`` then.
      graded_hi_hz: the upper edge the metrics were **actually** taken to,
        the mirror of ``graded_lo_hz``. Equal to ``f_hi_hz`` when no ceiling
        was supplied. The TOP band's edge follows the ceiling in BOTH
        directions -- 8-20 kHz on a ``reference`` microphone, 8-12 kHz on a
        ``consumer`` one -- because that edge and
        :data:`BEST_EFFORT_ABOVE_HZ` are one number; every lower band's edge
        is only ever lowered. ``<= graded_lo_hz`` when the ceiling swallowed
        the band whole. ``None`` on a report that does not carry the clamp.
      max_at_graded_edge: this band was floor-truncated
        (``graded_lo_hz > f_lo_hz``) **and** ``max_deviation_hz`` is its
        LOWEST graded bin -- the band continues below the floor, ungraded.
        What follows is that ``max_deviation_db`` is a maximum over a SUBSET
        of the band and so a LOWER BOUND on its real worst deviation; the
        flag tests no SLOPE and does not claim the curve keeps rising below
        the floor. Disclosure only: ``passed`` is not computed from it, and
        an edge extremum is still a real graded bin. ``False`` on a band
        whose worst bin sits inside the graded span and on an untruncated
        band; ``None`` when unevaluable, or on a report without the field.
      room_entangled_below_hz: the upper edge of the sub-span of this band
        below the room's floor, where no window separates speaker from room
        (:func:`jasper.audio_measurement.gating.f_entanglement_floor_hz`;
        unknown marks nothing) -- ``min(entanglement_floor_hz,
        graded_hi_hz)``, or ``None``. Disclosure only: the grade above is
        computed exactly as it was without it.
      gate_sensitivity_db: the gate sweep's null-model-corrected delta at
        THIS band's ``max_deviation_hz`` -- how much of the worst bin's depth
        arrived with the analysis window rather than with the speaker
        (:mod:`~jasper.active_speaker.crossover_v2.gate_sweep`'s
        ``corrected_delta_db``). ``None`` whenever
        ``gate_sensitivity_note`` says why there is no number.
      sigma_growth_ratio: across-pose sigma at the longest resolution-valid
        rung over the shortest one, at the same bin. The room/speaker
        discriminator: it is sigma that GROWS with window length that says
        room, never sigma that is merely large (#3495).
      n_valid_rungs: how many ladder rungs were resolution-valid at that bin
        -- the denominator behind the two numbers above. Present whenever the
        ladder ran, including when it then declined to publish a ratio.
      gate_sensitivity_note: WHY the three fields above are ``None``, or
        ``None`` when they carry numbers. The two-vocabulary distinction
        (the ladder's own refusal versus a band it never ran on) is
        explained once, beside the constants that spell it, in
        :mod:`~jasper.active_speaker.crossover_v2.round_views` -- not
        restated here.
      gate_sensitivity_detail: the ``RoundCapturesRefused`` behind a
        capture-refusal note -- ``{"reason": ..., **exc.detail}`` -- so the
        specific missing input survives a bucket slug that only names the
        general shape. ``None`` for every other note, swept or not.

    The five gate fields are DISCLOSURE ONLY and are stamped after the fact,
    by a reader that holds the round's captures as well as its verdict
    (:func:`~jasper.active_speaker.crossover_v2.round_views.spec_with_gate_sensitivity`).
    Nothing in this module computes them, no grade reads them, and a report
    that never met a sweep carries them as ``None`` -- which is why they
    default rather than being required.

    For every non-excluded bin ``i`` in the band the two readings decompose
    exactly::

        deviation_i = curve_i - reference_db
                    = (curve_i - band_level) + (band_level - reference_db)
                    = ripple_i               + level_deviation_db

    one term a *different* band's level can move (the frame is pooled across
    bands) and one it structurally cannot. The identity is PER BIN:
    ``max_deviation_db`` and ``max_ripple_db`` are taken at bins chosen by
    different criteria and do not add. What always holds is
    ``abs(max_deviation_db) >= abs(level_deviation_db)`` -- a power mean lies
    between its inputs' min and max, so some bin always has ripple of each
    sign.
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
    # rehydrated from persistence that does not carry these fields. `None`
    # there is honest, and `spec_band_tilt` treats it as such rather than
    # fabricating a level.
    level_deviation_db: float | None = None
    max_ripple_db: float | None = None
    max_ripple_hz: float | None = None
    graded_lo_hz: float | None = None
    graded_hi_hz: float | None = None
    max_at_graded_edge: bool | None = None
    room_entangled_below_hz: float | None = None
    # Defaulted for a DIFFERENT reason than the fields above: nothing in this
    # module ever fills these in. They are stamped afterwards by a reader
    # holding the round's captures as well as its verdict, so `None` here is
    # "no sweep has read this report", not "this document is old".
    gate_sensitivity_db: float | None = None
    sigma_growth_ratio: float | None = None
    n_valid_rungs: int | None = None
    gate_sensitivity_note: str | None = None
    gate_sensitivity_detail: dict | None = None

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
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BandResult":
        """The exact inverse of :meth:`to_dict` -- a rehydration, never a
        re-derivation: no band edge, floor, or tolerance is recomputed.

        Two read rules, and conflating them is a silent-wrong trap.
        ``f_lo_hz`` through ``passed`` are read with hard indexing, so a
        document missing one is CORRUPT and raises ``KeyError`` instead of
        rehydrating a plausible-looking ``None``. (Several of them are
        legitimately ``None`` on an unevaluable band, which ``raw["..."]``
        preserves; the hardening is against the KEY being absent.) Only the
        dataclass-defaulted fields, ``level_deviation_db`` through
        ``gate_sensitivity_detail``, are read with :meth:`dict.get`, so a document
        without them rehydrates with the same ``None`` a hand-built report
        would carry.
        """
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
        )


@dataclass(frozen=True)
class FlatSpecReport:
    """The full flat-spec evaluation for one combined+smoothed curve.

    ``excluded_intervals`` collapses contiguous (by array index, on the
    strictly-ascending ``freqs_hz`` :func:`evaluate_flat_spec` requires) runs
    of the exclusion mask into merged ``(f_lo_hz, f_hi_hz)`` tuples spanning
    each run's first and last excluded bin. Diagnostic disclosure only --
    pass/fail reads the exclusion mask directly, not this derived field.
    ``best_effort_above_hz`` echoes where grading stopped, so a consumer
    needs no second import to know where the disclosed-only region begins.

    ``overall_passed`` is ``True`` only when **every** band is both evaluable
    and passing: the evaluator will not report a clean bill of health for a
    spectrum it could not fully measure.

    ``smoothing_fraction`` is **caller attestation**, not a measurement. A
    bare magnitude array carries no evidence of how it was smoothed and this
    module does not smooth; the field records what the caller says it handed
    over, and nothing here validates it.

    ``trusted_floor_hz`` and ``trusted_ceiling_hz`` are the clamps this
    evaluation was intersected at, echoed so a stored report says on its face
    which honesty limits produced its numbers. ``None`` means not supplied --
    **"not stated", never "zero"**. ``reference_band_hz`` is the span whose
    power mean IS ``reference_db`` after those clamps, and is what
    :func:`spec_flatness_gauge` publishes rather than the module constant, so
    a surface naming the frame names the frame that was used.

    ``graded_band_hz`` is the whole span the table graded --
    ``(lowest band's graded_lo_hz, best_effort_above_hz)``. It is NOT
    ``reference_band_hz``: the frame deviations are stated FROM is the
    low-mid band alone, while the span they are stated OVER runs to the
    trusted ceiling. A consumer asking "which bins did this evaluation
    grade" must read this one.

    ``entanglement_floor_hz`` and ``entanglement_floor_source`` are the
    ROOM's floor and its provenance
    (:func:`jasper.audio_measurement.gating.f_entanglement_floor_hz`; unknown
    marks nothing), echoed the way the clamps above are but clamping nothing:
    what they mark is :attr:`BandResult.room_entangled_below_hz`.

    ``gate_sweep_frame`` is the frame every band's four gate fields are
    stated in
    (:func:`~jasper.active_speaker.crossover_v2.gate_sweep.frame_descriptor`
    -- window shape, ladder, smoothing, grid, resolution bars). It travels
    WITH the numbers because one capture and one feature read a materially
    different depth under each defensible frame (#3495), so a sensitivity
    without its frame is the frame's number rather than the room's. ``None``
    on every report no sweep ever stamped, which is every report this module
    builds: nothing here fills it in.
    """

    reference_db: float
    bands: tuple[BandResult, ...]
    overall_passed: bool
    excluded_intervals: tuple[tuple[float, float], ...]
    best_effort_above_hz: float
    smoothing_fraction: int
    # Defaulted for the same reason `BandResult`'s split fields are: a report
    # can be hand-built or rehydrated from persistence that does not carry the
    # clamp. The defaults say "nothing clamped".
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

        Every re-grade in the round's frame -- an entry baseline, a single
        position's own reference, a per-position re-evaluation -- states these
        four together or states a frame nobody produced. Splatting one dict is
        what makes "together" structural instead of remembered: a frame field
        added here reaches all three sites at once.

        Deliberately NOT ``smoothing_fraction``: that is caller attestation
        about ONE curve, and a re-grade of a different curve states its own.
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
        """The exact inverse of :meth:`to_dict` -- a rehydration, never a
        re-derivation: no band edge, floor, or reference is recomputed.

        Same two read rules :meth:`BandResult.from_dict` documents.
        ``excluded_intervals`` is read with hard indexing, so a document
        missing it raises rather than rehydrating an empty tuple that reads
        as "nothing was excluded" when the truth is "the field was lost". The
        four clamp fields, the two entanglement fields and ``gate_sweep_frame``
        are read with :meth:`dict.get` and fall back to the dataclass defaults.

        ``gate_sweep_frame`` rehydrates only from a mapping; anything else in
        that key -- a scalar, a list, a stray ``true`` -- becomes ``None``
        rather than a "frame" no reader can index, since a frame that cannot
        be read is the same evidence as no frame at all.

        The two entanglement fields are read as a PAIR, through
        :meth:`~jasper.audio_measurement.gating.EntanglementFloor.coerce` --
        the lenient door, where :func:`evaluate_flat_spec` takes the strict
        one. Anything a document can carry that the evaluator would refuse
        rehydrates as ``(None, unknown)``, so a rehydrated report can never be
        re-graded into a refusal by a disagreement it inherited. The rule
        itself is the type's; this seam only chooses which door.
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
    """One :func:`evaluate_flat_spec` call: its inputs and its verdict,
    together, so a consumer reading the graded CURVE cannot pair it with
    someone else's mask or a re-derived report.

    ``excluded`` is the mask as it was HANDED to the evaluator -- for a
    spatial cloud, the merged honesty mask (the combiner's power-vs-median
    screen unioned with the identified-null registry).

    A live in-process handoff, not persisted and not a wire shape, which is
    why it may hold arrays at all; :meth:`FlatSpecReport.to_dict` remains the
    durable copy of the half that is durable.
    """

    freqs_hz: np.ndarray
    curve_db: np.ndarray
    excluded: np.ndarray
    report: FlatSpecReport


def _power_mean_db(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` -- the power (energy) mean, NOT a
    linear average of dB values. The single place that conversion happens in
    this module; :func:`evaluate_flat_spec` never inlines it.
    """
    linear = np.power(10.0, values_db / 10.0)
    return float(10.0 * np.log10(np.mean(linear)))


def _graded_lo_hz(f_lo_hz: float, trusted_floor_hz: float | None) -> float:
    """``max(f_lo_hz, trusted_floor_hz)`` -- one band edge, intersected with
    the session's trusted floor.

    The single place that intersection happens, so the bands and the
    reference band cannot drift apart on it. A ``None`` or non-finite floor
    clamps nothing and returns ``f_lo_hz`` unchanged; the finiteness guard is
    load-bearing rather than defensive, because :func:`max` with a NaN is
    order-dependent and would silently return whichever argument came first.
    """
    if trusted_floor_hz is None or not math.isfinite(trusted_floor_hz):
        return float(f_lo_hz)
    return max(float(f_lo_hz), float(trusted_floor_hz))


def _graded_hi_hz(f_hi_hz: float, trusted_ceiling_hz: float | None) -> float:
    """One band's upper edge, intersected with the session's trusted ceiling.

    The mirror of :func:`_graded_lo_hz`, with the one asymmetry that is the
    whole point of it: the floor only ever RAISES an edge, while the TOP
    band's edge follows the ceiling in BOTH directions -- that edge and
    :data:`BEST_EFFORT_ABOVE_HZ` are one number, so a microphone trusted to
    20 kHz scores to 20 kHz and one trusted to 12 kHz only to 12 kHz. Every
    lower band's edge is only ever lowered; nothing here widens 250-2000 Hz.

    The finiteness guard is load-bearing for the same reason
    :func:`_graded_lo_hz`'s is: :func:`min` with a NaN is order-dependent.
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

    Below the room's floor no window separates speaker from room
    (:func:`jasper.audio_measurement.gating.f_entanglement_floor_hz`; unknown
    marks nothing). The mirror of :func:`_graded_lo_hz` in spirit and its
    opposite in effect: that one MOVES an edge and changes what is graded,
    this one only marks.

    A floor that is not ``None`` here is finite and positive already: it came
    off a :class:`~jasper.audio_measurement.gating.EntanglementFloor`.
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
    """Evaluate the flat-linearization spec against one combined, 1/3-oct-
    smoothed magnitude curve (docs/historical/linearization-campaign-2026-07.md, "The spec --
    what 'flat' means here").

    Args:
        freqs_hz: 1-D **strictly ascending** frequency axis, Hz. Required,
            not assumed: band membership is masked by value, but the merged
            exclusion intervals use index adjacency as a proxy for frequency
            adjacency, which holds only on a sorted axis.
        spec_smoothed_db: 1-D magnitude curve, dB, same length as
            ``freqs_hz``, already spatially combined and 1/3-oct smoothed.
            This module neither smooths nor combines; it consumes the curve
            verbatim.
        exclusion_mask: optional 1-D bool array, same length as
            ``freqs_hz``. ``True`` marks an interference-flagged bin,
            excluded from the reference-level computation AND from every
            band's deviation metrics. ``None`` (the default) excludes
            nothing.
        smoothing_fraction: the 1/N-octave fraction the caller attests
            ``spec_smoothed_db`` was smoothed at, recorded verbatim on the
            report. Provenance only -- nothing here validates or uses it.
        trusted_floor_hz: the session's gate-derived trusted floor in Hz,
            ``2.5/T`` for the capture's own reflection-free window
            (:func:`jasper.audio_measurement.gating.f_trusted_floor_hz`).
            Every band's lower edge, **and the reference band's**, is raised
            to ``max(f_lo, trusted_floor_hz)`` before anything is measured,
            so no graded number rests on a bin the gate cannot support.
            ``None`` (the default) or a non-finite value clamps nothing:
            "unknown" must not silently become a floor of zero, nor withhold
            the evidence above an unverified edge. This module takes the
            number; it does not derive, validate, or second-guess it.
        trusted_ceiling_hz: the frequency above which this session's
            microphone is not trusted -- the taper zero of
            :func:`jasper.active_speaker.linearization_envelope.mic_trust_limit`
            for the tier that measured. The TOP band's upper edge, and with
            it :attr:`FlatSpecReport.best_effort_above_hz`, moves to this
            value; every lower band's is lowered to it if it sits above.
            ``None`` or non-finite clamps nothing and grades the nominal
            table. Same take-the-number rule as ``trusted_floor_hz``.
        reference_db_override: use this value as ``reference_db`` instead of
            computing it, when not ``None``. Lets a caller grade a curve
            against a DIFFERENT capture's reference level -- e.g. a target
            round compared against a baseline round's own on-axis reference
            -- without touching what the reference band is or how band
            levels are computed: the zero-non-excluded-bins check below
            still runs first regardless, so a position with nothing to grade
            in the reference band still raises even when an override is
            supplied. ``None`` (the default) computes the reference the
            ordinary way.
        entanglement_floor_hz: the ROOM's floor in Hz, below which no window
            separates speaker from room
            (:func:`jasper.audio_measurement.gating.f_entanglement_floor_hz`;
            unknown marks nothing). **Nothing is clamped and no grade
            changes**: it only sets each band's
            :attr:`BandResult.room_entangled_below_hz`.
        entanglement_floor_source: which of
            :data:`jasper.audio_measurement.gating.ENTANGLEMENT_SOURCES` the
            floor came from, echoed onto the report verbatim. Any other value
            -- and any floor/source pair that disagrees about whether a floor
            is known -- raises ``ValueError`` from
            :class:`~jasper.audio_measurement.gating.EntanglementFloor`: this
            is the caller's own vocabulary, not data read off a document.

    Reference level: the power mean (:func:`_power_mean_db`) over
    non-excluded bins inside :data:`REFERENCE_BAND_HZ`, its lower edge raised
    to ``trusted_floor_hz`` like every band's, so an untrustworthy low end
    cannot re-centre it. :attr:`FlatSpecReport.reference_band_hz` reports the
    span actually pooled. Clamping is not free and its direction does not
    generalize: dropping the sub-floor region moves that zero, and the
    headline number moves with it whenever the worst bin survives — the same
    speaker graded on fewer bins, which is what
    :attr:`BandResult.n_bins`/:attr:`ConvergenceResidual.n_bins` keep
    visible.

    Deviation: ``spec_smoothed_db - reference_db``, evaluated per
    :data:`SPEC_BANDS` entry over that band's non-excluded bins:
    ``max_deviation_db`` is the signed deviation at the largest-absolute
    bin (with ``max_deviation_hz`` naming that bin), ``rms_deviation_db`` is
    the RMS deviation, and ``passed`` is
    ``abs(max_deviation_db) <= tolerance_db``.

    Each band additionally carries the **attribution split** --
    ``level_deviation_db`` (where the whole band sits relative to the shared
    frame) and ``max_ripple_db``/``max_ripple_hz`` (what the curve does
    relative to *that band's own* level, which no reference choice can
    move). Disclosure only: **no verdict reads them.** See
    :class:`BandResult` for the per-bin identity and :func:`spec_band_tilt`
    for the frame-free reading built on them.

    A band with **zero non-excluded bins** -- no coverage on the axis, every
    bin interference-flagged, or nothing left inside the trusted range -- is
    reported as ``evaluable=False`` with ``passed=None`` and ``None``
    metrics, not raised on: one band losing its evidence must not destroy the
    report for the other two. ``graded_lo_hz >= graded_hi_hz`` distinguishes
    the clamped case from the other two. The **reference band** is different
    and still raises: with no reference level there is nothing to compute a
    deviation against anywhere. Since that band IS ``SPEC_BANDS[0]``, a floor
    high enough to empty it empties the frame with it and raises rather than
    reporting an unevaluable band; the ceiling is the clamp that can leave a
    band unevaluable, at the top band.

    Band membership is ``graded_lo <= f < graded_hi`` -- inclusive-lower,
    exclusive-upper -- for both :data:`SPEC_BANDS` entries and
    :data:`REFERENCE_BAND_HZ`. Applied uniformly, that keeps the reference
    band's span exactly equal to ``SPEC_BANDS[0]`` and the best-effort
    boundary exactly adjacent to ``SPEC_BANDS[-1]``'s exclusive upper edge,
    at whatever value the ceiling put them. So a bin at exactly 2000 Hz lands
    in the 2-8 kHz band, one at exactly 8000 Hz in the top band, and one at
    exactly :attr:`FlatSpecReport.best_effort_above_hz` is best-effort rather
    than the top of it. Best-effort bins are never evaluated and never fail;
    they do not appear in any :class:`BandResult` at all.

    Raises:
        ValueError: for any degenerate input -- empty or non-1-D arrays,
            mismatched array lengths, a ``freqs_hz`` that is not strictly
            ascending, :data:`REFERENCE_BAND_HZ` left with zero
            non-excluded bins (no reference level is computable), or any
            non-finite (NaN/Inf) value in ``freqs_hz`` or
            ``spec_smoothed_db``; and for any floor/source pair
            :class:`~jasper.audio_measurement.gating.EntanglementFloor`
            refuses.
    """
    # THE validation of the pair, and deliberately not a local one: the rule
    # binding a floor to its provenance lives in the type, so this seam gets
    # it by constructing one and letting its ValueError through (#3522).
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

    # Checked after finiteness, so a NaN axis is reported as non-finite
    # rather than as a spurious ordering failure (NaN comparisons are all
    # False, so np.diff would not catch it).
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
        # The clamped edge is what defines membership, so a bin below the
        # trusted floor is not in the band at all rather than in it and
        # excluded: `n_excluded` stays the interference screen's own count,
        # and `graded_lo_hz` below carries the clamp instead.
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
        # Both halves are needed: an untruncated band's first bin IS the
        # band's own start, with nothing ungraded below it to warn about, so
        # `worst == band_indices[0]` alone would fire on every band whose
        # worst bin happens to be its lowest. The comparison is against the
        # LOWEST INCLUDED bin, not `f_lo_hz`: exclusion can take the graded
        # edge itself, and what matters is where the evidence starts.
        max_at_graded_edge = bool(
            f_lo_hz > nominal_lo_hz and worst == int(band_indices[0])
        )
        rms_deviation_db = float(np.sqrt(np.mean(np.square(band_deviation_db))))
        # The attribution split -- the band's own level, and what the curve
        # does relative to THAT rather than to the pooled frame. Same
        # `_power_mean_db` as the reference, so there is one averaging
        # convention in this module, not two. Nothing below feeds `passed`.
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

    One number plus the two counts that make it interpretable. See
    :func:`spec_convergence_residual` for the definition and for why the
    counts ride along.

    Args:
      rms_db: RMS deviation over every non-excluded bin of every
        :data:`SPEC_BANDS` band, as one pooled figure. ``None`` when no
        band was evaluable.
      n_bins: how many bins that RMS was computed from — the pooled
        non-excluded spec-band bin count.
      n_excluded: how many spec-band bins were dropped by the exclusion
        mask. Counted across ALL bands, including any band the exclusion
        left unevaluable.
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
    """The residual a closed correction loop converges on: **RMS deviation
    over the non-excluded bins of the spec bands**, pooled across all three.

    Holds no threshold and makes no verdict; loop policy (how much
    improvement counts, how many iterations, when to stop) is the caller's.

    **Derived from the report, not recomputed from the curve** -- band
    membership, what the exclusion mask removed, and which reference level
    the deviation is measured against are each answered once, by
    :func:`evaluate_flat_spec`. The pooled figure is reassembled from each
    band's own :attr:`BandResult.rms_deviation_db` and its included-bin
    count:

        rms = sqrt( sum_b n_b * rms_b**2 / sum_b n_b ),
        n_b = band.n_bins - band.n_excluded

    which is exactly the RMS over the union of those bins, because
    ``n_b * rms_b**2`` is that band's sum of squared deviations.

    Bins at or above :attr:`FlatSpecReport.best_effort_above_hz` never enter
    it, so a top octave the speaker cannot reach cannot stall the loop. That
    edge is read off the report and so follows the session's trusted ceiling;
    reading the module constant here would name a boundary the numbers were
    not taken at.

    ``n_bins`` and ``n_excluded`` ride along because a residual that fell
    because the honesty mask grew is not convergence -- it is the same
    speaker on fewer bins, and that must be visible in the same record as the
    number.

    Returns:
      A :class:`ConvergenceResidual`. When no band is evaluable, ``rms_db``
      is ``None`` and ``evaluable`` is ``False`` rather than a residual of
      0.0 for a spectrum nothing was measured in. That state is unreachable
      from :func:`evaluate_flat_spec` -- :data:`REFERENCE_BAND_HZ` is exactly
      ``SPEC_BANDS[0]``, so an evaluation that did not raise on an empty
      reference band left at least one non-excluded spec-band bin behind --
      but a hand-built or rehydrated report carries no such guarantee, and
      the alternative there is a ZeroDivisionError.
    """
    n_excluded = sum(band.n_excluded for band in report.bands)
    # One pass, so the denominator and the numerator can never be assembled
    # from different band sets. On a hand-built report the
    # `rms_deviation_db is None` filter and a zero included-bin count need not
    # coincide, and `n_bins` must keep meaning "bins this RMS came from".
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

    The one figure on this report that **no reference-frame choice can
    move**, and that is the whole point of it.
    :attr:`FlatSpecReport.reference_db` is a power mean, so a band inside the
    pooled span that is uniformly off drags the shared zero toward itself and
    inflates every other band's deviation. A step between two band levels
    cannot: each level is stated as :attr:`BandResult.level_deviation_db`, so
    the shared reference appears in both terms and cancels. Re-anchor the
    spec anywhere and ``step_db`` does not move. This class does not pick an
    anchor; it states the relationship every anchor agrees on.

    The cancellation is exact in arithmetic and near-exact in floating point
    -- ``(L_a - ref) - (L_b - ref)`` rounds differently from ``L_a - L_b``.
    Measured across five candidate reference bands the worst spread in
    ``step_db`` is 8.882e-16 dB, two ULPs at a 5 dB step;
    :attr:`BandResult.max_ripple_db` is bit-identical across all five,
    because it never touches the reference at all.

    Not a verdict and holds no threshold: nothing here is compared against a
    tolerance or feeds :attr:`BandResult.passed` /
    :attr:`FlatSpecReport.overall_passed`.

    Args:
      step_db: the largest absolute level difference between any two
        evaluable bands, as a **non-negative magnitude** -- the direction is
        carried by the two band fields rather than by a sign. ``None`` when
        fewer than two bands carry a level.
      high_band_hz: ``(f_lo_hz, f_hi_hz)`` of the higher-sitting band of
        that pair. ``None`` when unevaluable.
      low_band_hz: ``(f_lo_hz, f_hi_hz)`` of the lower-sitting one. ``None``
        when unevaluable. At ``step_db`` exactly ``0.0`` the two bands are
        level and the high/low labels carry no information.
      n_bands: how many bands carried a level to compare -- normally 3; an
        unevaluable band, or a report without the split, lowers it. A step
        chosen among two bands is a weaker statement than one chosen among
        three, and that must be visible in the same record as the number.
      evaluable: ``n_bands >= 2``. ``False`` means there is no step, not a
        step of zero -- one band cannot tilt against itself.
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


#: The "no step is knowable" :class:`BandTilt` -- a null object, not a
#: fabricated reading. Only :class:`SpecFlatness` uses it, as the default for
#: a gauge that carries no tilt; :func:`spec_band_tilt` builds its own
#: unevaluable result with the real ``n_bands`` it counted.
NO_BAND_TILT = BandTilt(
    step_db=None, high_band_hz=None, low_band_hz=None, n_bands=0, evaluable=False,
)


def spec_band_tilt(report: FlatSpecReport) -> BandTilt:
    """The largest level step between two of ``report``'s graded bands -- the
    frame-free attribution reading.

    **Derived from the report, not recomputed from the curve**, the same rule
    as :func:`spec_convergence_residual` and :func:`spec_flatness_gauge`.

    The pair is chosen by largest absolute difference of
    :attr:`BandResult.level_deviation_db`; ties go to the lowest pair in
    :data:`SPEC_BANDS` order, matching :func:`spec_flatness_gauge`'s tie rule
    so the two reductions never disagree about which band came first.
    (:func:`max` returns the FIRST maximal element and the pairs are built in
    band order, so that rule is the builtin's own.)

    Bands that are unevaluable, or that carry no ``level_deviation_db``, are
    skipped rather than defaulted to zero: inventing 0 dB for a band with no
    measured level would manufacture exactly the false step this function
    exists to expose.
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
    # `len(levelled) >= 2` above guarantees at least one pair, so this cannot
    # be an empty max().
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
    """The household-facing "how flat is the speaker" figures for one report:
    the worst deviation and where it sits, that band's own tolerance, the
    pooled average error, and how much of the spectrum it was computed from.

    Every field is **lifted from the report**, never recomputed from a curve:
    ``max_*``/``tolerance_db`` are one :class:`BandResult`'s own values
    verbatim, and ``rms_db``/``n_bins``/``n_excluded`` come from
    :func:`spec_convergence_residual`. So a gauge, a ledger line and the
    report shown for one session are the same numbers by construction, not by
    two code paths agreeing.

    Args:
      max_db: the **signed** deviation at the worst bin of the worst
        evaluable band -- the same number and convention as
        :attr:`BandResult.max_deviation_db`. ``None`` when no band was
        evaluable.
      max_hz: that bin's frequency. ``None`` when unevaluable.
      max_band_hz: ``(f_lo_hz, f_hi_hz)`` of the band that worst bin lives
        in -- which tolerance row the number is judged against. ``None`` when
        unevaluable. **Not the frame the deviation is measured FROM** (that
        is ``reference_band_hz``), and **not "the band to fix"**: it is the
        band furthest from the shared frame, and a band that is uniformly off
        drags that frame toward itself, so the band named here can be a flat
        one made to look proud by another band's deficit. ``tilt`` below
        cannot do that, and a surface rendering this pointer should render
        that one beside it.
      reference_band_hz: :attr:`FlatSpecReport.reference_band_hz` -- the span
        whose power mean over non-excluded bins IS the zero every ``max_db``
        here is stated against. That is :data:`REFERENCE_BAND_HZ` clamped to
        the session's trusted floor, so this reads the CLAMPED span and never
        the module constant: the frame is not a detail of the number, it is
        half of it, and a surface printing a worst band without naming its
        frame states half a measurement. Always populated, including when
        ``evaluable`` is ``False`` -- which frame WOULD have been used is
        knowable even when no band could be graded.
      tolerance_db: that band's tolerance. ``None`` when unevaluable.
      max_band_level_deviation_db: that same band's
        :attr:`BandResult.level_deviation_db` -- how much of ``max_db`` is
        just where the whole band sits relative to the pooled frame rather
        than anything happening at ``max_hz``. ``None`` when unevaluable, or
        when the report does not carry the split.
      max_band_ripple_db: that same band's :attr:`BandResult.max_ripple_db`
        -- the worst the curve gets *inside* the band, measured from the
        band's own level, which no reference choice can move. The two
        together disarm the pointer at the point of use: a band reading
        ``+3.26 dB`` of level and ``-0.10 dB`` of ripple is flat and merely
        sits high, so there is no peak at ``max_hz`` to EQ. ``None`` when
        unevaluable / absent.
      rms_db: :attr:`ConvergenceResidual.rms_db` -- RMS deviation pooled over
        every non-excluded spec-band bin. ``None`` when unevaluable.
      n_bins: how many bins the RMS was computed from.
      n_excluded: how many spec-band bins the exclusion mask removed. With
        ``n_bins``, the "graded on how much of the spectrum" pair. A BIN
        count, deliberately not a count of
        :attr:`FlatSpecReport.excluded_intervals`: that field spans the whole
        axis including regions no spec band covers.
      evaluable: whether ANY band survived to be measured. ``False`` means
        there is no flatness number, not a flatness of zero.
      passed: :attr:`FlatSpecReport.overall_passed`, verbatim. **Read it
        with** ``evaluable``: that field is ``False`` for an unmeasurable
        spectrum too, so ``passed=False, evaluable=False`` means "could not
        be measured", not "failed".
      tilt: :func:`spec_band_tilt` of the same report -- the largest level
        step between two graded bands and which sits higher. The one figure
        here no reference-frame choice can move, and the answer to what
        ``max_db``/``max_band_hz`` cannot answer: *which* bands disagree. A
        surface rendering the pointer should render this beside it. Defaults
        to :data:`NO_BAND_TILT` (unevaluable -- no step known, not a step of
        zero) for a gauge that carries no tilt.
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

    **Derived from the report, not recomputed from the curve** -- the same
    rule as :func:`spec_convergence_residual`.

    The worst band is the one whose :attr:`BandResult.max_deviation_db` has
    the largest **absolute** value among evaluable bands; ties go to the
    lowest band, so the choice is deterministic. Deliberately NOT "the band
    that failed by the widest margin relative to its own tolerance": the
    rendered claim is a dB reading of how far from flat the speaker measured,
    and re-ranking by tolerance headroom would answer a different question.

    Which band that picks is frame-dependent, and deliberately left so: the
    reference is a power mean, so a uniformly-off band inside the pooled span
    drags it and this walk can name a flat band as the worst one. Re-ranking
    here would only move the anchor question somewhere less visible; instead
    this function carries :func:`spec_band_tilt` beside the pointer, so the
    frame-free reading travels with the frame-dependent one and a reader is
    never handed the pointer alone.
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
