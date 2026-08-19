# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a predicted sum is WORTH, in the units the hardware round grades in.

The scoring half of the crossover search:
:func:`~jasper.active_speaker.crossover_v2.forward_model.predict_sum` says what
a candidate will measure, and this module says whether that is better.

**Commensurate by CALLING, never by reimplementing.** Every seat's number is
:func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` over a report
from :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`, and a role
pools those seats with the weighted-RMS identity the whole grading stack pools
with. There is no second grader here. What keeps the pooling honest is not a
claim but a contract test: the same positions graded through
:func:`~jasper.active_speaker.flat_spec_views.role_split_flatness` and through
this module must produce identical numbers.

A prediction graded in any other currency reproduces the campaign's
twice-measured failure: the corner sweep this supersedes scored candidates with
:func:`~jasper.active_speaker.fc_selector.band_flatness`, whose deviation is
taken from an ARITHMETIC dB mean of an UNSMOOTHED curve with no banding, no
exclusion mask and no trusted floor — four differences from the referee, each
of which moves the number.

**Flatness is a PROFILE, not a scalar** (owner ruling, 2026-08-19). One high
chunk and one low chunk average to a neutral RMS, and the speaker that ships is
not neutral. So every view emits a :class:`FlatnessProfile`:

* per-chunk deviation over 1/3-octave chunks (:data:`CHUNK_FRACTION`),
* the WORST-CHUNK max-norm — the honest number, and the form a real spec sheet
  uses (+/- X dB over the band),
* TILT as its own number, because a broadband slope is the defect an RMS most
  readily absorbs, and a +2.5 dB-hot tweeter has shipped behind a respectable
  RMS before,
* RMS as the search-friendly summary, and only as that.

Acceptance reads the profile: :func:`dominates` requires the primary residual to
improve AND no chunk to regress past :data:`CHUNK_REGRESSION_BAR_DB`. A search
may sort on a weighted scalar — a sort needs a total order — but nothing is
ACCEPTED on one.

**Never collapsed.** The shipped pooled view power-averages the position curves
across seats BEFORE any deviation is taken
(:func:`~jasper.audio_measurement.spatial_combine.combine_positions`, the
``axis=0`` power mean), so opposite-sign structure at one frequency partially
cancels between seats;
:class:`~jasper.active_speaker.flat_spec_views.RoleFlatness` says so in its own
words — "combining fills moving nulls before the curve is graded, so a combined
on-axis curve would read flatter than this pool of individual ones". This module
therefore grades EVERY SEAT SEPARATELY and reports each seat's own profile
beside the pooled one. The pooled residual is still reported, because it is the
referee's currency; it is never the only thing reported.

**The DI-continuity term** (adopted from the owner's crossover-design research)
charges a candidate for a directivity discontinuity through the crossover.
Toole's framework makes a smoothly-changing DI one of the strongest correlates
of listener preference, and the banked armrun measured the failure directly: the
on-axis mark and the five-position pool ANTI-correlate at rho = -0.66, which is
a lobe being re-aimed — on-axis flatness bought at off-axis cost. A broadband
residual cannot see that; :func:`directivity_continuity` can.
**It is HORIZONTAL-ONLY** (:data:`DI_COVERAGE_HORIZONTAL_ONLY`), and the label
rides every result: a crossover's primary artifact is VERTICAL lobing, this rig
measures horizontal angles, and a term that did not say so would read as
coverage it does not have.

**The frozen reference is explicit, per seat, and it is not a monkeypatch.**
:class:`GradingFrame` carries the level frame a profile is stated against.
``reference_db=None`` is the shipped self-referencing behaviour; a value is the
S8.9 freeze — grade every config against the BASELINE's reference FOR THE SAME
SEAT. Per seat rather than one global constant, because seats differ in absolute
level by design and a global freeze would inject directivity into the grade. The
identity that makes it exact, from the acceptance-tested campaign tool::

    deviation_frozen = curve - base_ref
                     = (curve - own_ref) + (own_ref - base_ref)
                     = deviation_shipped + (own_ref - base_ref)

i.e. a per-seat CONSTANT on every bin. Mask, smoothing, banding, trusted floor
and chunking are untouched by the freeze.

**What the freeze does NOT reach, stated rather than implied.**
:func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` takes no reference
argument at HEAD, and shifting its input curve is a proven no-op (its reference
is a power mean, which is shift-equivariant, so both terms move and the
deviation does not). So the frozen frame reaches the PROFILE, which this module
owns, and not the banded pass/fail residual, which the shipped evaluator owns.
Giving the evaluator that parameter changes what the SPEAKER grades and belongs
in the PR that changes both readers at once — freezing one side alone would make
the two disagree by construction, which is worse than freezing neither. Until
then :attr:`ViewScore.residual_db` is self-referenced, says so through
:attr:`FlatnessProfile.reference_policy`, and the frozen profile sits beside it.

Units are in every name: ``_hz``, ``_db``, ``_deg``, ``_us``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.frame_fit import fit_frame
from jasper.audio_measurement.spatial_combine import (
    DEFAULT_SPEC_FRACTION, decimate_curve_to_analysis_grid,
)

from ..flat_spec import (
    BEST_EFFORT_ABOVE_HZ, GATED_SPEC_LOWER_EDGE_HZ, FlatSpecReport, GradedSpec,
    evaluate_flat_spec, spec_convergence_residual,
)
from .attempt_grading import PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB

__all__ = [
    "ADVISORY_WEIGHTS",
    "CHUNK_FRACTION",
    "CHUNK_REGRESSION_BAR_DB",
    "DI_COVERAGE_HORIZONTAL_ONLY",
    "ChunkDeviation",
    "DirectivityContinuity",
    "Domination",
    "FlatnessProfile",
    "GradingFrame",
    "ObjectiveScore",
    "ObjectiveWeights",
    "ViewScore",
    "directivity_continuity",
    "dominates",
    "flatness_profile",
    "score_prediction",
    "spec_graded_curve",
]

#: How wide one chunk is, as ``1/N`` octave.
#:
#: **One smoothing window wide, and that is the whole argument.** The graded
#: curve arrives already smoothed at
#: :data:`~jasper.audio_measurement.spatial_combine.DEFAULT_SPEC_FRACTION`, so a
#: narrower chunk cannot hold information the smoother has not already averaged
#: across — two 1/6-octave chunks inside one smoothing window are two correlated
#: views of one measurement, and reporting both as independent regions would
#: invent resolution. A wider chunk throws away resolution the curve has. Tying
#: the chunk to the smoothing fraction makes each chunk approximately one
#: independent look, and leaves one number to change if the spec fraction ever
#: does.
#:
#: It also lands where the ear and the industry already are: 1/3 octave is the
#: ISO band and the resolution a spec sheet's "+/- X dB" is quoted at. Over the
#: graded span (357 Hz - 16 kHz on the jts3 sessions this was written against)
#: that is 16 chunks — enough to localize a defect, few enough to read.
CHUNK_FRACTION: int = DEFAULT_SPEC_FRACTION

#: How far one chunk may regress before a candidate stops dominating, dB.
#:
#: The SAME number the repo already calls material in the improving direction
#: (:data:`~jasper.active_speaker.crossover_v2.attempt_grading.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`),
#: re-exported rather than re-chosen. A change smaller than that bar is not
#: called real when it helps, so calling it real when it hurts would be two
#: thresholds for one question; and a regression LARGER than the bar at which an
#: improvement counts is, by the repo's own calibration, a real change in the
#: opposite direction. Symmetric, one owner, and one edit to tighten.
CHUNK_REGRESSION_BAR_DB: float = PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB

#: What :func:`directivity_continuity` measured over. Rides every result.
#:
#: The horizontal fan is what this rig captures. A crossover's primary artifact
#: is VERTICAL lobing, which no horizontal pose witnesses, so a high score here
#: is evidence about the angles that were measured and about nothing else.
DI_COVERAGE_HORIZONTAL_ONLY = "horizontal_only"

# Refusal slugs for :func:`dominates`. Named rather than boolean for the reason
# ``candidate_space`` names its own: a rejected candidate has to say WHICH rule
# rejected it, because the answers differ — "it did not help" sends a reader to
# the search bounds, "it traded regions" sends them to a chunk.
_REFUSE_PRIMARY = "primary_did_not_improve"
_REFUSE_CHUNK = "chunk_regressed_past_the_bar"
_REFUSE_CHUNKS_DIFFER = "chunk_sets_are_not_comparable"
_REFUSE_UNEVALUABLE = "a_profile_was_not_evaluable"


@dataclass(frozen=True)
class ChunkDeviation:
    """One 1/3-octave chunk's deviation from the frame.

    ``level_db`` is the chunk's own mean deviation — where the whole chunk
    sits — and ``worst_db`` is its worst single bin, SIGNED. Both, because
    "this third-octave is 2 dB hot" and "this third-octave has a 2 dB notch in
    it" are different defects with different fixes, and one number hides which
    is present. The sign is kept for the same reason
    :class:`~jasper.active_speaker.flat_spec.BandResult` keeps it: too loud and
    too quiet call for opposite corrections.
    """

    f_lo_hz: float
    f_hi_hz: float
    center_hz: float
    n_bins: int
    level_db: float
    worst_db: float
    worst_hz: float
    rms_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "center_hz": self.center_hz,
            "n_bins": self.n_bins,
            "level_db": self.level_db,
            "worst_db": self.worst_db,
            "worst_hz": self.worst_hz,
            "rms_db": self.rms_db,
        }


@dataclass(frozen=True)
class FlatnessProfile:
    """How flat one curve is ALONG ITS WHOLE LENGTH, not on average.

    ``worst_chunk_db`` is the headline: the largest absolute chunk deviation
    anywhere in the graded span, reported with its sign and its frequency. It is
    the number a spec sheet quotes and the number a scalar RMS is least able to
    reproduce — a curve +3 dB in one third-octave and -3 dB in another has an
    unremarkable RMS and a 3 dB worst chunk.

    ``tilt_db_per_octave`` is fitted over the CHUNK levels rather than over the
    bins, and that is load-bearing rather than incidental: the analysis grid is
    linear in frequency, so half its bins sit above 8 kHz and a per-bin fit is a
    fit to the top octave. Chunk centres are uniform in log-frequency by
    construction, so the fit weights the spectrum the way a listener's octaves
    do. The regression itself is
    :func:`~jasper.audio_measurement.frame_fit.fit_frame` — the repo's typed
    dB-per-octave least squares, whose whole job is a tilt and whose
    ill-conditioning guards (:data:`~jasper.audio_measurement.frame_fit.MIN_FRAME_FIT_BINS`,
    a zero-span refusal, non-finite bins dropped) come with it — reused rather
    than re-derived here.

    ``tilt_span_db`` is that slope times the graded span in octaves — what the
    tilt actually costs end to end, which is the form the defect is noticed in.

    ``reference_policy`` is ``"frozen"`` or ``"self"``. Read it before comparing
    two profiles: a frozen and a self-referenced profile differ by a constant on
    every deviation, and differencing them silently would compare two frames.
    """

    view: str
    reference_db: float
    reference_policy: str
    band_hz: tuple[float, float]
    chunks: tuple[ChunkDeviation, ...]
    worst_chunk_db: float | None
    worst_chunk_hz: float | None
    tilt_db_per_octave: float | None
    tilt_span_db: float | None
    rms_db: float | None
    n_bins: int
    evaluable: bool
    not_evaluated_reason: str = ""

    @property
    def worst_chunk_abs_db(self) -> float | None:
        """The max-norm — ``+/- X dB`` as a spec sheet writes it."""
        return None if self.worst_chunk_db is None else abs(self.worst_chunk_db)

    def summary(self) -> dict[str, Any]:
        """The headline numbers without the per-chunk table.

        For surfaces that carry MANY profiles — one per seat per candidate —
        where the full chunk list is the bulk of the document and the worst
        chunk is the part that must not be collapsed. The full table stays on
        the in-process object for anything that needs to localize a defect.
        """
        return {
            "view": self.view,
            "reference_policy": self.reference_policy,
            "worst_chunk_db": self.worst_chunk_db,
            "worst_chunk_hz": self.worst_chunk_hz,
            "tilt_db_per_octave": self.tilt_db_per_octave,
            "tilt_span_db": self.tilt_span_db,
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
            "n_chunks": len(self.chunks),
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "reference_db": self.reference_db,
            "reference_policy": self.reference_policy,
            "band_hz": list(self.band_hz),
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "worst_chunk_db": self.worst_chunk_db,
            "worst_chunk_hz": self.worst_chunk_hz,
            "tilt_db_per_octave": self.tilt_db_per_octave,
            "tilt_span_db": self.tilt_span_db,
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
        }


@dataclass(frozen=True)
class GradingFrame:
    """The level frame, honesty floor and exclusion mask a profile is stated in.

    All three travel together because all three must come from the SAME place —
    the baseline the comparison is against — or two configs are graded in two
    frames and the difference between them is partly the frames. Handing them
    over as loose arguments is how one of the three gets forgotten.

    ``reference_db`` is ``None`` for the shipped self-referencing behaviour and
    a number for the S8.9 freeze. It is ONE SEAT's frame: build one of these per
    view, from that view's own baseline report.
    """

    reference_db: float | None
    trusted_floor_hz: float | None
    excluded_intervals: tuple[tuple[float, float], ...] = ()

    @property
    def policy(self) -> str:
        """``"frozen"`` when a reference was supplied, else ``"self"``."""
        return "self" if self.reference_db is None else "frozen"

    @classmethod
    def frozen_to(cls, baseline: FlatSpecReport) -> "GradingFrame":
        """The frame ``baseline`` was graded in, for grading something else.

        The baseline's OWN reference, floor and published exclusion intervals —
        so a later config is measured against the frame the baseline set rather
        than against one it computes for itself out of its own curve. That
        self-computed frame is the degree of freedom S8.9 found: every
        prescribed cut lowered its own reference (0.38-0.75 dB across two
        rounds), and the grade then measured the cut against the level the cut
        had itself produced.
        """
        return cls(
            reference_db=float(baseline.reference_db),
            trusted_floor_hz=baseline.trusted_floor_hz,
            excluded_intervals=tuple(baseline.excluded_intervals),
        )

    @classmethod
    def self_referenced(
        cls,
        *,
        trusted_floor_hz: float | None,
        excluded_intervals: Sequence[tuple[float, float]] = (),
    ) -> "GradingFrame":
        """The shipped behaviour: each curve against its own power mean."""
        return cls(
            reference_db=None,
            trusted_floor_hz=trusted_floor_hz,
            excluded_intervals=tuple(
                (float(lo), float(hi)) for lo, hi in excluded_intervals
            ),
        )


@dataclass(frozen=True)
class Domination:
    """Whether one candidate may be preferred over another, and why not.

    Both conditions, because either alone is a way to lose: an improvement that
    trades one region for another is the "ping-pong-ball flat" trade, and a
    profile with no regression that also does not help is not a reason to ask a
    household for another apply.
    """

    dominates: bool
    primary_delta_db: float | None
    worst_regression_db: float | None
    worst_regression_hz: float | None
    refusal: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dominates": self.dominates,
            "primary_delta_db": self.primary_delta_db,
            "worst_regression_db": self.worst_regression_db,
            "worst_regression_hz": self.worst_regression_hz,
            "refusal": self.refusal,
        }


@dataclass(frozen=True)
class DirectivityContinuity:
    """How abruptly the polar pattern changes through the crossover.

    Each off-axis angle is normalised to the on-axis curve, chunked, and fitted
    with a straight line in log-frequency across the crossover region. A speaker
    whose directivity changes SMOOTHLY through the handoff leaves a small
    residual about that line; a lobe being re-aimed leaves a kink, which is what
    the residual measures. ``kink_db`` pools the per-angle residuals as an RMS,
    so two angles kinking in opposite directions cannot cancel each other — the
    same never-collapse rule the flatness profile follows.

    ``coverage`` is :data:`DI_COVERAGE_HORIZONTAL_ONLY`. See the module
    docstring: a crossover's primary artifact is vertical, and this number is
    silent about it.
    """

    kink_db: float | None
    per_angle_db: tuple[tuple[float, float], ...]
    region_hz: tuple[float, float]
    coverage: str
    evaluable: bool
    not_evaluated_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kink_db": self.kink_db,
            "per_angle_db": [list(entry) for entry in self.per_angle_db],
            "region_hz": list(self.region_hz),
            "coverage": self.coverage,
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
        }


@dataclass(frozen=True)
class ObjectiveWeights:
    """What the advisory total weighs, and whether anything calibrated it.

    ``calibrated`` is ``False`` until the banked-armrun rung fits these against
    measurement, and while it is ``False`` the total is a SORT KEY and nothing
    else. Every component is reported separately on :class:`ObjectiveScore` for
    exactly that reason: a reader who wants to know why a candidate ranked where
    it did must never have to invert a weighted sum to find out.

    There is deliberately no ``sim_to_real`` term. The plan reserved one at zero
    weight to encode "the model has not earned its sign"; a zero-weight term
    changes no number and reads as coverage, so that claim is carried instead by
    the ranking-authority stamp on the shortlist, where a consumer cannot fail
    to see it.
    """

    primary: float = 1.0
    secondary: float = 0.5
    directivity: float = 0.5
    headroom: float = 0.25
    calibrated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "secondary": self.secondary,
            "directivity": self.directivity,
            "headroom": self.headroom,
            "calibrated": self.calibrated,
        }


#: The weights the total is summed with before anything calibrates them.
#:
#: The secondary view at half the primary's weight because the household's seat
#: is the on-axis one and an off-axis view is evidence about the same speaker
#: rather than about the same seat; directivity at the same weight as the
#: off-axis view because it is the same physical concern measured a different
#: way; headroom at a quarter because it is a cost rather than a defect. All
#: four are ADVISORY — see :attr:`ObjectiveWeights.calibrated`.
ADVISORY_WEIGHTS = ObjectiveWeights()


@dataclass(frozen=True)
class ViewScore:
    """One view's commensurate residual and its full flatness profile."""

    view: str
    residual_db: float | None
    n_bins: int
    profile: FlatnessProfile

    def to_dict(self, *, full_profile: bool = True) -> dict[str, Any]:
        """This view as JSON; ``full_profile=False`` drops the chunk table."""
        return {
            "view": self.view,
            "residual_db": self.residual_db,
            "n_bins": self.n_bins,
            "profile": (
                self.profile.to_dict() if full_profile else self.profile.summary()
            ),
        }


@dataclass(frozen=True)
class ObjectiveScore:
    """Every number one candidate earned, and the advisory sum of them.

    ``per_seat`` is the never-collapsed half: one profile per measured angle,
    never averaged into the views. A view's residual can improve while one seat
    inside it gets worse, and the shipped pooled curve — a power mean across
    seats taken BEFORE any deviation — cannot show that. This can.
    """

    total: float | None
    primary_view: str
    views: tuple[ViewScore, ...]
    per_seat: tuple[FlatnessProfile, ...]
    directivity: DirectivityContinuity
    headroom_charge_db: float
    weights: ObjectiveWeights
    not_evaluated: tuple[tuple[str, str], ...] = ()

    def view(self, name: str) -> ViewScore | None:
        """The named view, or ``None`` when the walk did not visit it."""
        return next((v for v in self.views if v.view == name), None)

    @property
    def primary(self) -> ViewScore | None:
        """The headline view — the seat a household actually sits in."""
        return self.view(self.primary_view)

    def to_dict(self) -> dict[str, Any]:
        # The chunk table rides the PRIMARY view only. That table is the
        # acceptance object and acceptance is decided on the primary view, so it
        # is exactly where the detail has to be; every other view and every seat
        # carries the summary, whose worst chunk is what proves nothing was
        # collapsed. Rendering every chunk of every seat of every candidate made
        # one shortlist a 220 kB document, which is a document nobody reads —
        # and the full tables are still on the in-process object for anything
        # that needs to localize a defect.
        return {
            "total": self.total,
            "primary_view": self.primary_view,
            "views": [
                v.to_dict(full_profile=v.view == self.primary_view)
                for v in self.views
            ],
            "per_seat": [p.summary() for p in self.per_seat],
            "directivity": self.directivity.to_dict(),
            "headroom_charge_db": self.headroom_charge_db,
            "weights": self.weights.to_dict(),
            "not_evaluated": [list(entry) for entry in self.not_evaluated],
        }


def spec_graded_curve(
    freqs_hz: Any,
    magnitude_db: Any,
    *,
    trusted_floor_hz: float | None,
    excluded_intervals: Sequence[tuple[float, float]] = (),
) -> GradedSpec:
    """Reduce a predicted magnitude curve and grade it, exactly as the flow does.

    ``decimate_curve_to_analysis_grid`` then
    ``smooth_fractional_octave(..., fraction=DEFAULT_SPEC_FRACTION)`` then
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` — the same three
    owners, in the same order, that
    :func:`crossover_v2_flow.spec_report_for_predicted_sum` uses on the shipped
    confirm path, and the same order the measured cloud curve goes through.
    Decimation first is not an optimisation detail: the smoother's cost scales
    with bin count and window width, and a raw prediction grid is hundreds of
    thousands of bins.

    Two differences from the flow's version, both deliberate and both
    tightenings:

    * ``trusted_floor_hz`` is REQUIRED rather than defaulted away. The flow
      grades a prediction with no floor (band 1 from 250 Hz) and the measurement
      with one (band 1 from 357.14 Hz), so the shipped predicted and measured
      residuals are not commensurate with each other — a named gap in the
      2026-08-19 armrun regrade. An objective whose whole purpose is
      commensurability cannot inherit it. ``None`` is still expressible, and
      still means "no floor was stated".
    * the session's published exclusion intervals are applied, so a prediction
      is graded over the same bins the measurement was.

    **Why this is not a second implementation of the flow's function, and what
    is owed.** ``crossover_v2/*`` may not import ``crossover_v2_flow``
    (AST-pinned), so calling it is not available; the plan's answer is to
    RELOCATE it here and leave the flow a caller. That relocation edits a file
    at its line ceiling and is deferred to the PR that may spend that budget.
    Until then this is the general form (an explicit floor and mask) and the
    flow keeps the special case (both absent), which is a disclosed second site
    with a named merge rather than a silent duplicate.
    """
    grid, curve_db = decimate_curve_to_analysis_grid(
        np.asarray(freqs_hz, dtype=np.float64),
        np.asarray(magnitude_db, dtype=np.float64),
    )
    smoothed = smooth_fractional_octave(grid, curve_db, fraction=DEFAULT_SPEC_FRACTION)
    excluded = _exclusion_mask(grid, excluded_intervals)
    report = evaluate_flat_spec(
        grid, smoothed, excluded,
        smoothing_fraction=DEFAULT_SPEC_FRACTION,
        trusted_floor_hz=trusted_floor_hz,
    )
    return GradedSpec(freqs_hz=grid, curve_db=smoothed, excluded=excluded, report=report)


def _exclusion_mask(
    freqs_hz: np.ndarray, intervals: Sequence[tuple[float, float]],
) -> np.ndarray:
    """Published exclusion intervals as a per-bin mask.

    The same construction ``flat_spec_views._evaluate_position`` applies to a
    position curve, and for the same reason: the honesty screen is a CROSS-SEAT
    mean-vs-median disagreement that a single curve has no version of, so the
    intervals the session published are re-applied rather than re-derived.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    mask = np.zeros(freqs.size, dtype=bool)
    for lo_hz, hi_hz in intervals:
        mask |= (freqs >= float(lo_hz)) & (freqs <= float(hi_hz))
    return mask


def _graded_band_hz(trusted_floor_hz: float | None) -> tuple[float, float]:
    """The span a profile speaks for: the spec's own edges, raised to the floor.

    Read from :data:`~jasper.active_speaker.flat_spec.GATED_SPEC_LOWER_EDGE_HZ`
    and :data:`~jasper.active_speaker.flat_spec.BEST_EFFORT_ABOVE_HZ` rather
    than written down, because 357.14 Hz is not a constant — it is one capture's
    ``2.5/T`` trusted floor, and a module that hardcoded it would grade the next
    capture in the wrong place.

    The floor comes from the REPORT rather than from the frame's declaration:
    it is the floor the evaluator actually graded at, so the chunks and the
    report's bands cannot end up speaking for different spectrum.
    """
    lo_hz = GATED_SPEC_LOWER_EDGE_HZ
    if trusted_floor_hz is not None and math.isfinite(trusted_floor_hz):
        lo_hz = max(lo_hz, float(trusted_floor_hz))
    return (float(lo_hz), float(BEST_EFFORT_ABOVE_HZ))


def _floors_disagree(frame_hz: float | None, graded_hz: float | None) -> bool:
    """Whether a frame's declared floor is not the one the report graded at.

    ``None`` on either side is "not stated" and cannot disagree with anything.
    Two stated floors are compared with a relative tolerance because a floor is
    a derived ``2.5/T`` and travels through JSON — the same posture, and the
    same reason, as ``verification``'s own floor-comparability check.
    """
    if frame_hz is None or graded_hz is None:
        return False
    return not math.isclose(
        float(frame_hz), float(graded_hz), rel_tol=_FLOOR_COMPARABILITY_RTOL,
    )


#: How far two stated trusted floors may differ and still be the same floor.
_FLOOR_COMPARABILITY_RTOL = 1e-3


def _chunk_edges(band_hz: tuple[float, float], fraction: int) -> list[tuple[float, float]]:
    """Geometric ``1/fraction``-octave chunk edges spanning ``band_hz``.

    Geometric because a crossover argument — and a listener — is a per-octave
    one, the same reason ``fc_sweep.fc_candidate_set`` steps its corners
    geometrically. The final chunk is truncated at the band's upper edge rather
    than allowed to run past it, so no chunk speaks for spectrum the grade does
    not cover.
    """
    lo_hz, hi_hz = band_hz
    if not (0.0 < lo_hz < hi_hz):
        return []
    step = 2.0 ** (1.0 / float(fraction))
    edges: list[tuple[float, float]] = []
    edge = float(lo_hz)
    while edge < hi_hz:
        upper = min(edge * step, float(hi_hz))
        edges.append((edge, upper))
        edge = edge * step
    return edges


def flatness_profile(
    graded: GradedSpec,
    *,
    frame: GradingFrame,
    view: str,
    fraction: int = CHUNK_FRACTION,
) -> FlatnessProfile:
    """The deviation of one graded curve ALONG ITS WHOLE LENGTH.

    ``graded`` is one evaluation — curve, mask and report together — so the
    profile cannot be paired with somebody else's mask. The deviation is taken
    against ``frame.reference_db`` when the frame is frozen and against the
    report's own ``reference_db`` when it is not; nothing else about the
    evaluation changes between the two, which is the S8.9 identity in the module
    docstring.

    Excluded bins take no part, exactly as they take no part in the grade: a bin
    the honesty screen removed is not evidence, and a chunk that holds only such
    bins is ABSENT from ``chunks`` rather than reported as flat. A curve with no
    surviving chunk is ``evaluable=False`` with a reason — never a zero, which
    would read as perfect.

    **A frame whose floor is not the report's is refused, not reconciled.**
    Grading at one floor and profiling at another silently states two numbers
    over two different spans, which is the exact confusion
    :class:`GradingFrame` exists to make impossible; a refusal that names it is
    the only honest outcome, since neither span is wrong and only the caller
    knows which was meant.
    """
    reference_db = (
        float(graded.report.reference_db) if frame.reference_db is None
        else float(frame.reference_db)
    )
    band_hz = _graded_band_hz(graded.report.trusted_floor_hz)
    if _floors_disagree(frame.trusted_floor_hz, graded.report.trusted_floor_hz):
        return FlatnessProfile(
            view=view,
            reference_db=reference_db,
            reference_policy=frame.policy,
            band_hz=band_hz,
            chunks=(),
            worst_chunk_db=None,
            worst_chunk_hz=None,
            tilt_db_per_octave=None,
            tilt_span_db=None,
            rms_db=None,
            n_bins=0,
            evaluable=False,
            not_evaluated_reason=(
                f"frame states a trusted floor of {frame.trusted_floor_hz} Hz "
                f"but this curve was graded at {graded.report.trusted_floor_hz} Hz"
            ),
        )
    freqs = np.asarray(graded.freqs_hz, dtype=np.float64)
    deviation = np.asarray(graded.curve_db, dtype=np.float64) - reference_db
    included = np.isfinite(deviation) & (freqs >= band_hz[0]) & (freqs < band_hz[1])
    if graded.excluded is not None:
        included &= ~np.asarray(graded.excluded, dtype=bool)

    chunks: list[ChunkDeviation] = []
    for lo_hz, hi_hz in _chunk_edges(band_hz, fraction):
        inside = included & (freqs >= lo_hz) & (freqs < hi_hz)
        n_bins = int(inside.sum())
        if n_bins == 0:
            continue
        values = deviation[inside]
        worst = int(np.argmax(np.abs(values)))
        chunks.append(ChunkDeviation(
            f_lo_hz=float(lo_hz),
            f_hi_hz=float(hi_hz),
            center_hz=float(math.sqrt(lo_hz * hi_hz)),
            n_bins=n_bins,
            level_db=float(np.mean(values)),
            worst_db=float(values[worst]),
            worst_hz=float(freqs[inside][worst]),
            rms_db=float(np.sqrt(np.mean(values ** 2))),
        ))

    if not chunks:
        return FlatnessProfile(
            view=view,
            reference_db=reference_db,
            reference_policy=frame.policy,
            band_hz=band_hz,
            chunks=(),
            worst_chunk_db=None,
            worst_chunk_hz=None,
            tilt_db_per_octave=None,
            tilt_span_db=None,
            rms_db=None,
            n_bins=0,
            evaluable=False,
            not_evaluated_reason=(
                "no bin survived the graded band, the trusted floor and the mask"
            ),
        )

    worst_chunk = max(chunks, key=lambda chunk: abs(chunk.worst_db))
    n_bins = sum(chunk.n_bins for chunk in chunks)
    graded_values = deviation[included]
    # Fitted over CHUNK LEVELS, not bins: chunk centres are uniform in log f by
    # construction, while the analysis grid is linear in f and would weight the
    # top octave by an order of magnitude. ``fit_frame`` against a zero
    # reference makes the chunk levels themselves the difference it fits.
    centers = np.array([chunk.center_hz for chunk in chunks], dtype=np.float64)
    levels = np.array([chunk.level_db for chunk in chunks], dtype=np.float64)
    fit = fit_frame(centers, levels, np.zeros_like(levels))
    octaves = math.log2(band_hz[1] / band_hz[0])
    return FlatnessProfile(
        view=view,
        reference_db=reference_db,
        reference_policy=frame.policy,
        band_hz=band_hz,
        chunks=tuple(chunks),
        worst_chunk_db=worst_chunk.worst_db,
        worst_chunk_hz=worst_chunk.worst_hz,
        tilt_db_per_octave=fit.tilt_db_per_octave,
        tilt_span_db=(
            None if fit.tilt_db_per_octave is None
            else float(fit.tilt_db_per_octave * octaves)
        ),
        rms_db=float(np.sqrt(np.mean(graded_values ** 2))),
        n_bins=n_bins,
        evaluable=True,
    )


def dominates(
    candidate: FlatnessProfile,
    incumbent: FlatnessProfile,
    *,
    candidate_residual_db: float | None,
    incumbent_residual_db: float | None,
    improvement_bar_db: float = PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    regression_bar_db: float = CHUNK_REGRESSION_BAR_DB,
) -> Domination:
    """Whether ``candidate`` beats ``incumbent`` on BOTH counts.

    The primary residual must improve by at least ``improvement_bar_db`` — the
    burden of proof is on the change, which asks a household for another apply —
    AND no chunk may regress by more than ``regression_bar_db``. The second
    condition is the one that cannot be bought: a candidate may not trade a
    region away to buy a better average, however much the average improves.

    Chunks are matched by their own ``(f_lo_hz, f_hi_hz)`` edges, which two
    profiles share exactly when they were chunked over the same band at the same
    fraction. When they were not, the comparison REFUSES rather than matching by
    position or interpolating — a chunk-by-chunk verdict between two different
    chunkings is not a verdict about the speaker.

    Regression is measured on the chunk's worst-bin magnitude, not on its level
    or its RMS: a chunk that develops a notch has regressed even if its mean and
    its RMS barely move, and the notch is what a listener finds.
    """
    if not (candidate.evaluable and incumbent.evaluable):
        return Domination(
            dominates=False,
            primary_delta_db=None,
            worst_regression_db=None,
            worst_regression_hz=None,
            refusal=_REFUSE_UNEVALUABLE,
        )
    if candidate_residual_db is None or incumbent_residual_db is None:
        return Domination(
            dominates=False,
            primary_delta_db=None,
            worst_regression_db=None,
            worst_regression_hz=None,
            refusal=_REFUSE_UNEVALUABLE,
        )

    by_edges = {(chunk.f_lo_hz, chunk.f_hi_hz): chunk for chunk in incumbent.chunks}
    worst_regression_db: float | None = None
    worst_regression_hz: float | None = None
    for chunk in candidate.chunks:
        before = by_edges.get((chunk.f_lo_hz, chunk.f_hi_hz))
        if before is None:
            return Domination(
                dominates=False,
                primary_delta_db=None,
                worst_regression_db=None,
                worst_regression_hz=None,
                refusal=_REFUSE_CHUNKS_DIFFER,
            )
        regression_db = abs(chunk.worst_db) - abs(before.worst_db)
        if worst_regression_db is None or regression_db > worst_regression_db:
            worst_regression_db = float(regression_db)
            worst_regression_hz = float(chunk.worst_hz)

    delta_db = float(candidate_residual_db) - float(incumbent_residual_db)
    if delta_db > -float(improvement_bar_db):
        refusal = _REFUSE_PRIMARY
    elif worst_regression_db is not None and worst_regression_db > float(regression_bar_db):
        refusal = _REFUSE_CHUNK
    else:
        refusal = ""
    return Domination(
        dominates=not refusal,
        primary_delta_db=delta_db,
        worst_regression_db=worst_regression_db,
        worst_regression_hz=worst_regression_hz,
        refusal=refusal,
    )


def directivity_continuity(
    curves_db_by_angle: Mapping[float, np.ndarray],
    freqs_hz: Any,
    *,
    on_axis_deg: float,
    region_hz: tuple[float, float],
    fraction: int = CHUNK_FRACTION,
) -> DirectivityContinuity:
    """How smoothly directivity changes through the crossover region.

    Each off-axis curve is normalised to the on-axis one, chunked across
    ``region_hz``, and fitted with a straight line in log-frequency. A speaker
    whose beamwidth narrows SMOOTHLY through the handoff produces a normalised
    curve that is close to a straight line in log f; a lobe that re-aims at the
    crossover produces a kink, and this returns the RMS of what the straight
    line could not explain.

    The tilt is DISCARDED on purpose. A steady off-axis rolloff is what a
    directional loudspeaker does, and charging a candidate for it would charge
    it for its drivers. Only the DEPARTURE from steady is the crossover's, which
    is the same reason ``fc_selector.score_candidate`` charged lateral behaviour
    as an EXCESS over the design axis rather than as a level.

    Angles pool as an RMS across angles, so two angles kinking oppositely cannot
    cancel. ``on_axis_deg`` must be present in ``curves_db_by_angle``; without a
    reference there is nothing to normalise against and the result is
    unevaluable rather than assumed flat.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    region = (float(region_hz[0]), float(region_hz[1]))
    reference = curves_db_by_angle.get(float(on_axis_deg))
    if reference is None:
        return DirectivityContinuity(
            kink_db=None,
            per_angle_db=(),
            region_hz=region,
            coverage=DI_COVERAGE_HORIZONTAL_ONLY,
            evaluable=False,
            not_evaluated_reason=f"no curve at the on-axis angle {on_axis_deg:g} deg",
        )
    on_axis_db = np.asarray(reference, dtype=np.float64)
    edges = _chunk_edges(region, fraction)

    per_angle: list[tuple[float, float]] = []
    for angle_deg in sorted(curves_db_by_angle):
        if float(angle_deg) == float(on_axis_deg):
            continue
        angle_db = np.asarray(curves_db_by_angle[angle_deg], dtype=np.float64)
        if angle_db.shape != on_axis_db.shape or angle_db.shape != freqs.shape:
            # An angle on a different grid is not comparable to the reference,
            # and normalising it would broadcast-error or silently align two
            # different spectra. Skipped: the remaining angles still answer.
            continue
        with np.errstate(invalid="ignore"):
            # An angle and the reference can both be -inf where the model says
            # the speaker is silent, and their difference is then NaN. Expected
            # rather than exceptional, and dropped by the finite filter below.
            normalized = angle_db - on_axis_db
        centers: list[float] = []
        levels: list[float] = []
        for lo_hz, hi_hz in edges:
            inside = np.isfinite(normalized) & (freqs >= lo_hz) & (freqs < hi_hz)
            if not bool(inside.any()):
                continue
            centers.append(float(math.sqrt(lo_hz * hi_hz)))
            levels.append(float(np.mean(normalized[inside])))
        if not centers:
            continue
        level_array = np.array(levels, dtype=np.float64)
        center_array = np.array(centers, dtype=np.float64)
        fit = fit_frame(center_array, level_array, np.zeros_like(level_array))
        if not fit.fitted:
            continue
        residual = level_array - fit.frame_db(center_array)
        per_angle.append((float(angle_deg), float(np.sqrt(np.mean(residual ** 2)))))

    if not per_angle:
        return DirectivityContinuity(
            kink_db=None,
            per_angle_db=(),
            region_hz=region,
            coverage=DI_COVERAGE_HORIZONTAL_ONLY,
            evaluable=False,
            not_evaluated_reason=(
                "no off-axis angle carried enough of the region to fit a trend"
            ),
        )
    kinks = np.array([kink for _angle, kink in per_angle], dtype=np.float64)
    return DirectivityContinuity(
        kink_db=float(np.sqrt(np.mean(kinks ** 2))),
        per_angle_db=tuple(per_angle),
        region_hz=region,
        coverage=DI_COVERAGE_HORIZONTAL_ONLY,
        evaluable=True,
    )


def _pool_rms(pairs: Sequence[tuple[float, float]]) -> float | None:
    """``sqrt(sum w*r**2 / sum w)`` over ``(weight, rms)`` pairs, or ``None``.

    The one weighted-RMS identity the grading stack pools everything with —
    :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` pools
    bands by bin count with it, ``flat_spec_views._pool`` pools positions by bin
    count and by octave span with it. Kept local for the reason that module
    keeps its own power mean local: reaching across a boundary for a private
    name couples this module to an implementation detail it does not own. What
    keeps the two from drifting is not the copy but the test — a contract test
    grades the same positions through ``role_split_flatness`` and through this
    module and requires the two pooled numbers to be identical.

    A non-positive total weight yields ``None``: no pooled figure exists, which
    is not the same as one of zero.
    """
    total = sum(weight for weight, _rms in pairs)
    if total <= 0.0:
        return None
    return math.sqrt(sum(weight * rms ** 2 for weight, rms in pairs) / total)


def score_prediction(
    curves_db_by_angle: Mapping[float, np.ndarray],
    freqs_hz: Any,
    *,
    role_by_angle: Mapping[float, str],
    primary_role: str,
    on_axis_deg: float,
    frame_by_angle: Mapping[float, GradingFrame],
    crossover_region_hz: tuple[float, float],
    headroom_charge_db: float = 0.0,
    weights: ObjectiveWeights = ADVISORY_WEIGHTS,
) -> ObjectiveScore:
    """Score one candidate's predicted sum, per seat and per role.

    ``curves_db_by_angle`` are RAW magnitude curves — ``20*log10|S|`` from
    :func:`~jasper.active_speaker.crossover_v2.forward_model.predict_sum` — all
    on the one shared ``freqs_hz`` grid. Each is reduced and graded here by
    :func:`spec_graded_curve`, once per seat: the reduction depends on that
    seat's own frame (its trusted floor and its exclusion intervals), so a
    caller that reduced first would have to know the frame anyway and would then
    be holding two halves of one evaluation apart. An angle whose curve does not
    fit the shared grid is refused by name rather than graded against somebody
    else's frequencies.

    ``role_by_angle`` maps each measured angle to the cloud's own position role.
    Required rather than defaulted for the reason ``flat_spec_views`` requires
    its own: the role vocabulary is owned by the flow, which this package may
    not import, so this module holds no copy of it.

    ``frame_by_angle`` is one :class:`GradingFrame` per SEAT — the S8.9 rule
    that the freeze is per seat and never one global constant.

    Every seat is graded on its own and reported on its own in ``per_seat``.
    Roles pool the per-seat residuals with :func:`_pool_rms`, weighted by graded
    bin count, which is what ``role_split_flatness`` does with the same weights;
    nothing here averages one role into another, and nothing averages the CURVES
    at all.

    ``total`` is the advisory weighted sum, ``None`` when the primary view could
    not be graded. It is a sort key and not a verdict: acceptance is
    :func:`dominates` on the profile.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    not_evaluated: list[tuple[str, str]] = []
    per_seat: list[FlatnessProfile] = []
    by_role: dict[str, list[tuple[float, float]]] = {}
    seat_profiles_by_role: dict[str, list[FlatnessProfile]] = {}

    for angle_deg in sorted(curves_db_by_angle):
        role = role_by_angle.get(angle_deg)
        view = f"{role or 'unroled'}@{angle_deg:g}"
        frame = frame_by_angle.get(angle_deg)
        if role is None or frame is None:
            not_evaluated.append((view, "no role or no grading frame for this angle"))
            continue
        if np.asarray(curves_db_by_angle[angle_deg]).shape != freqs.shape:
            # One grid for every angle is the contract, and a curve that does
            # not fit it would otherwise be graded against somebody else's
            # frequencies without saying so. Refused per angle rather than
            # raised: the other seats are still answerable, and an angle that
            # could not be graded is exactly what ``not_evaluated`` is for.
            not_evaluated.append((view, "curve does not fit the shared frequency grid"))
            continue
        graded = spec_graded_curve(
            freqs, curves_db_by_angle[angle_deg],
            trusted_floor_hz=frame.trusted_floor_hz,
            excluded_intervals=frame.excluded_intervals,
        )
        profile = flatness_profile(graded, frame=frame, view=view)
        per_seat.append(profile)
        seat_profiles_by_role.setdefault(role, []).append(profile)
        residual = spec_convergence_residual(graded.report)
        if not residual.evaluable or residual.rms_db is None:
            not_evaluated.append((view, "no spec band survived the floor and the mask"))
            continue
        by_role.setdefault(role, []).append(
            (float(residual.n_bins), float(residual.rms_db)),
        )

    views: list[ViewScore] = []
    for role in sorted(seat_profiles_by_role):
        pairs = by_role.get(role, [])
        views.append(ViewScore(
            view=role,
            residual_db=_pool_rms(pairs),
            n_bins=int(sum(int(n_bins) for n_bins, _rms in pairs)),
            profile=_merge_profiles(seat_profiles_by_role[role], view=role),
        ))

    directivity = directivity_continuity(
        curves_db_by_angle, freqs,
        on_axis_deg=on_axis_deg,
        region_hz=crossover_region_hz,
    )
    if not directivity.evaluable:
        not_evaluated.append(("directivity", directivity.not_evaluated_reason))

    scored = ObjectiveScore(
        total=None,
        primary_view=primary_role,
        views=tuple(views),
        per_seat=tuple(per_seat),
        directivity=directivity,
        headroom_charge_db=float(headroom_charge_db),
        weights=weights,
        not_evaluated=tuple(not_evaluated),
    )
    return _with_total(scored)


def _merge_profiles(
    profiles: Sequence[FlatnessProfile], *, view: str,
) -> FlatnessProfile:
    """One role's seats as a single profile, WITHOUT averaging their curves.

    Each chunk takes the WORST seat's worst bin, so a defect present at one seat
    survives into the role's profile instead of being diluted by the seats that
    do not have it. That is the whole point: the shipped pooled curve is a power
    mean across seats taken before any deviation, and a role profile built the
    same way would reproduce exactly the cancellation this module exists to
    avoid. The chunk's ``rms_db`` and ``level_db`` pool by the same weighted-RMS
    and bin-weighted-mean the grade uses, so the summary numbers stay
    comparable; only the WORST is a max, because a worst is not an average.

    Tilt is re-fitted over the merged chunk levels rather than averaged across
    seats, so it remains the slope of the thing the profile actually reports.

    ``reference_db`` on a merged profile is the MEAN of the seats' own frames
    and is disclosure only — there is no single frame here, because each chunk
    is stated in the frame of whichever seat contributed it. Read
    ``reference_policy`` for the question a comparison actually needs answered
    (whether these numbers are frozen or self-referenced), and read the seat
    profiles for a frame a number is really stated in.
    """
    evaluable = [profile for profile in profiles if profile.evaluable]
    if not evaluable:
        first = profiles[0] if profiles else None
        return FlatnessProfile(
            view=view,
            reference_db=float("nan") if first is None else first.reference_db,
            reference_policy="self" if first is None else first.reference_policy,
            band_hz=(0.0, 0.0) if first is None else first.band_hz,
            chunks=(),
            worst_chunk_db=None,
            worst_chunk_hz=None,
            tilt_db_per_octave=None,
            tilt_span_db=None,
            rms_db=None,
            n_bins=0,
            evaluable=False,
            not_evaluated_reason="no seat of this role was evaluable",
        )

    by_edges: dict[tuple[float, float], list[ChunkDeviation]] = {}
    for profile in evaluable:
        for chunk in profile.chunks:
            by_edges.setdefault((chunk.f_lo_hz, chunk.f_hi_hz), []).append(chunk)

    chunks: list[ChunkDeviation] = []
    for (lo_hz, hi_hz), seats in sorted(by_edges.items()):
        worst = max(seats, key=lambda chunk: abs(chunk.worst_db))
        n_bins = sum(chunk.n_bins for chunk in seats)
        pooled_rms = _pool_rms([(float(c.n_bins), c.rms_db) for c in seats])
        chunks.append(ChunkDeviation(
            f_lo_hz=lo_hz,
            f_hi_hz=hi_hz,
            center_hz=float(math.sqrt(lo_hz * hi_hz)),
            n_bins=n_bins,
            level_db=float(
                sum(c.level_db * c.n_bins for c in seats) / n_bins
            ),
            worst_db=worst.worst_db,
            worst_hz=worst.worst_hz,
            rms_db=0.0 if pooled_rms is None else pooled_rms,
        ))

    worst_chunk = max(chunks, key=lambda chunk: abs(chunk.worst_db))
    centers = np.array([chunk.center_hz for chunk in chunks], dtype=np.float64)
    levels = np.array([chunk.level_db for chunk in chunks], dtype=np.float64)
    fit = fit_frame(centers, levels, np.zeros_like(levels))
    band_hz = evaluable[0].band_hz
    octaves = math.log2(band_hz[1] / band_hz[0])
    return FlatnessProfile(
        view=view,
        reference_db=float(np.mean([profile.reference_db for profile in evaluable])),
        reference_policy=evaluable[0].reference_policy,
        band_hz=band_hz,
        chunks=tuple(chunks),
        worst_chunk_db=worst_chunk.worst_db,
        worst_chunk_hz=worst_chunk.worst_hz,
        tilt_db_per_octave=fit.tilt_db_per_octave,
        tilt_span_db=(
            None if fit.tilt_db_per_octave is None
            else float(fit.tilt_db_per_octave * octaves)
        ),
        rms_db=_pool_rms([
            (float(profile.n_bins), profile.rms_db)
            for profile in evaluable if profile.rms_db is not None
        ]),
        n_bins=sum(profile.n_bins for profile in evaluable),
        evaluable=True,
    )


def _with_total(scored: ObjectiveScore) -> ObjectiveScore:
    """``scored`` with its advisory weighted total filled in.

    ``None`` when the primary view produced no residual: a total that silently
    dropped the headline term would rank candidates by their off-axis behaviour
    and say nothing about it.

    **The one deliberate collapse in this module**, and it is confined to a SORT
    KEY. The secondary term is the plain mean of the non-primary views'
    residuals, because a sort needs one number and there is no principled
    exchange rate between two roles. Nothing is accepted on it — acceptance is
    :func:`dominates` on the primary view's profile — and every component that
    went into it is on :class:`ObjectiveScore` separately, so a reader never has
    to invert this sum to find out why a candidate ranked where it did.
    """
    primary = scored.primary
    if primary is None or primary.residual_db is None:
        return scored
    weights = scored.weights
    total = weights.primary * primary.residual_db
    secondary = [
        view.residual_db for view in scored.views
        if view.view != scored.primary_view and view.residual_db is not None
    ]
    if secondary:
        total += weights.secondary * (sum(secondary) / len(secondary))
    if scored.directivity.evaluable and scored.directivity.kink_db is not None:
        total += weights.directivity * scored.directivity.kink_db
    total += weights.headroom * scored.headroom_charge_db
    return ObjectiveScore(
        total=float(total),
        primary_view=scored.primary_view,
        views=scored.views,
        per_seat=scored.per_seat,
        directivity=scored.directivity,
        headroom_charge_db=scored.headroom_charge_db,
        weights=weights,
        not_evaluated=scored.not_evaluated,
    )
