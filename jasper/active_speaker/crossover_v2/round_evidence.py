# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two measurements one correction round compares, and the margin (#2291).

Reduces one summed capture (a ``ProgramAnalysis``) to a
:class:`~.verification.MeasurementComparand`, and — holding both sides — UNIONS
their exclusion masks. That union is the one judgement here: equal masks mean
equal graded bins by construction, so the residual cannot fall merely because
the honesty screen grew. Comparability (``program_id`` equality), the curve
reduction and the verdict stay with their shipped owners. The mask is this
capture's own gate-validity clamp, never the cloud's interference screen, so the
absolute spec answer stays the cloud's. No web import, no filesystem, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.program_analysis import (
    ABSOLUTE_NO_CROSSOVER_TOPOLOGY,
)

from ..flat_spec import FlatSpecReport, GradedSpec
from .contracts import (
    AdoptionDecision,
    BenefitStatus,
    CaptureValidity,
    CrossoverV2ContractError,
    EvidenceTrust,
    IterationHeadroom,
    QualityStatus,
    RealizationStatus,
    ResponseCurve,
    RoundReceipt,
    SafetyStatus,
    SpecStatus,
    VerificationResult,
    _text,
)
from .verification import FlatnessObjectives, MeasurementComparand, Verdict

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Same rule as :mod:`.verification`: ``program_analysis`` is a 5,500-line
    # scipy-backed module and should not be dragged into every package import.
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

    from .blend_correction import BlendCorrection

__all__ = [
    "BENEFIT_CURVE_MAX_BINS",
    "ENTRY_BASELINE_KIND",
    "ITERATION_PLATEAU_DB",
    "MEASURED_BENEFIT_MARGIN_DB",
    "ROUND_SERIES_CAP",
    "EntryBaseline",
    "MeasuredResponse",
    "RoundEvaluation",
    "benefit_comparands",
    "build_round_receipt",
    "evaluate_round",
    "measured_response_from_analysis",
]


# --------------------------------------------------------------------------
# the margin
# --------------------------------------------------------------------------

#: dB. How much flatter the speaker must measure before the round may say so.
#:
#: Provisionally equal to
#: :func:`~jasper.active_speaker.attempts_loop.material_improvement_db` (0.5 dB)
#: and deliberately NOT that constant: that one bounds a PREDICTION's
#: model-vs-hardware error, this one bounds two MEASUREMENTS of the same speaker
#: minutes apart through the same program, mic and analyzer — capture
#: repeatability, a different quantity. No repeat study has measured a floor for
#: this metric (the 2026-07-31 jts3 study grades ``max_db_notch_excluded``, not
#: the pooled spec residual); the nearest pooled pairs agreed to 0.040 dB and
#: 0.006 dB, so 0.5 dB is conservative rather than calibrated. THE FALLBACK: a
#: rig with a banked repeat floor takes the margin from
#: :func:`~jasper.active_speaker.repeat_floor.stopping_thresholds` instead.
MEASURED_BENEFIT_MARGIN_DB = 0.5

#: dB/round. How much the flattening series must still be moving to be worth a
#: round — the owner's #2602 ruling figure, with no arithmetic applied. Read
#: TWICE by :func:`~.verification.evaluate_iteration_headroom`: the same
#: judgement at two distances. Half :data:`MEASURED_BENEFIT_MARGIN_DB`, and that
#: is load-bearing — a plateau bar at or above the margin could never fire on
#: the passing row it exists for. THE FALLBACK, as above: a banked repeat floor
#: supplies the measured plateau through the same ``stopping_thresholds``.
ITERATION_PLATEAU_DB = 0.25

#: How many measurement+correction rounds one series may run — the owner's
#: #2602 ruling ("up to three"). A hard budget, and the FIRST stop
#: :func:`~.verification.evaluate_iteration_headroom` checks. Deliberately NOT
#: :attr:`~jasper.active_speaker.attempts_loop.AttemptBudget.target_attempts`,
#: also 3: that counts fitting ATTEMPTS inside one correction, this counts
#: measure-and-correct ROUNDS across a series.
ROUND_SERIES_CAP = 3

#: Resolution of BOTH sides of the benefit comparison — the same 512 the host
#: applies to ``verify_priors.predicted_sum``, since the entry baseline crosses
#: the durable stage bridge. Named here rather than imported because
#: :mod:`jasper.web.correction_crossover_v2` is the wrong direction for this
#: package, and because governing both sides puts them on one grid by
#: construction rather than by luck.
BENEFIT_CURVE_MAX_BINS = 512

#: ``kind`` stamped on the persisted record, matching the package's convention.
ENTRY_BASELINE_KIND = "jts_crossover_v2_entry_baseline"


# --------------------------------------------------------------------------
# one capture, reduced
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredResponse:
    """One summed at-the-mark capture, reduced to what a comparison needs.

    Deliberately not a :class:`~.verification.MeasurementComparand` yet: a
    comparand carries the mask it will be *graded* on, and that mask is not
    known until both sides are in hand (:func:`benefit_comparands`).
    """

    program_id: str
    reference_mark: str
    curve: ResponseCurve
    excluded: tuple[bool, ...]


def measured_response_from_analysis(
    analysis: "ProgramAnalysis | None", *, reference_mark: str,
) -> MeasuredResponse | None:
    """Reduce one VERIFY-program analysis to a comparison side, or ``None``.

    ``None`` for every honest "there is nothing here to compare": no analysis,
    no summed response, a degenerate curve.

    The two reduction steps and their order are
    ``crossover_v2_flow.spec_report_for_predicted_sum``'s: block-average onto
    the analysis grid first (the smoother is an O(bins x window) Python loop and
    a raw grid costs seconds on a Pi), then 1/3-octave smooth. Another order or
    fraction produces a curve ``flat_spec.evaluate_flat_spec`` grades
    differently from every other curve in this subsystem.
    """

    if analysis is None:
        return None
    summed = getattr(analysis, "summed_response", None)
    program_id = str(getattr(analysis, "program_id", "") or "")
    if summed is None or not program_id:
        return None

    from jasper.audio_measurement.analysis import smooth_fractional_octave
    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    try:
        grid, coarse_db = decimate_curve_to_analysis_grid(
            np.asarray(summed.freqs_hz, dtype=float),
            np.asarray(summed.magnitude_db, dtype=float),
            max_bins=BENEFIT_CURVE_MAX_BINS,
        )
        smoothed = smooth_fractional_octave(grid, coarse_db, fraction=3)
        curve = ResponseCurve(grid, smoothed)
    except (ValueError, TypeError, IndexError, AttributeError,
            CrossoverV2ContractError):
        # A malformed or non-finite capture is a "cannot compare this", not a
        # crash to propagate into a household decision.
        return None
    return MeasuredResponse(
        program_id=program_id,
        reference_mark=_text(reference_mark, field_name="reference_mark"),
        curve=curve,
        excluded=_validity_clamp(grid, getattr(summed, "validity_floor_hz", None)),
    )


def _validity_clamp(grid: np.ndarray, validity_floor_hz: Any) -> tuple[bool, ...]:
    """Bins below this capture's own reflection gate, as a per-bin flag.

    The same clamp ``assemble_cloud_group_result`` unions into its spec mask:
    below ``gating.f_valid_floor_hz`` the response is an artifact of a truncated
    gate window. An absent or non-finite floor screens nothing — "no evidence of
    a floor" is not "the floor is at zero".
    """

    floor = validity_floor_hz
    if floor is None or not isinstance(floor, (int, float)) or isinstance(floor, bool):
        return (False,) * int(grid.size)
    floor = float(floor)
    if not np.isfinite(floor):
        return (False,) * int(grid.size)
    return tuple(bool(value) for value in (np.asarray(grid, dtype=float) < floor))


# --------------------------------------------------------------------------
# the entry baseline, as it crosses the stage bridge
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryBaseline:
    """The pre-apply side of the round, and the graph it was measured on.

    Persisted by the host between stage 1 (which captures it) and stage 2 (which
    grades against it), so it is JSON-shaped by construction. The graph
    fingerprint travels with it because a later round binds the currently-active
    profile as its own entry graph, which a curve with no record of its graph
    cannot support.

    The flow state file's copy lives exactly as long as the round; the copy that
    outlives it is the write-once retained take
    (``spatial.entry_baseline_record``). Both are written from one
    :class:`MeasuredResponse`; neither is derived from the other.
    """

    program_id: str
    reference_mark: str
    curve: ResponseCurve
    excluded: tuple[bool, ...]
    graph_fingerprint: str
    captured_at: str
    artifact_ref: str = ""

    @classmethod
    def from_measurement(
        cls,
        measured: MeasuredResponse,
        *,
        graph_fingerprint: str,
        captured_at: str,
        artifact_ref: str = "",
    ) -> "EntryBaseline":
        return cls(
            program_id=measured.program_id,
            reference_mark=measured.reference_mark,
            curve=measured.curve,
            excluded=measured.excluded,
            graph_fingerprint=_text(
                graph_fingerprint, field_name="graph_fingerprint"
            ),
            captured_at=_text(captured_at, field_name="captured_at"),
            artifact_ref=str(artifact_ref or ""),
        )

    def as_measurement(self) -> MeasuredResponse:
        return MeasuredResponse(
            program_id=self.program_id,
            reference_mark=self.reference_mark,
            curve=self.curve,
            excluded=self.excluded,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ENTRY_BASELINE_KIND,
            "program_id": self.program_id,
            "reference_mark": self.reference_mark,
            "freqs_hz": list(self.curve.hz),
            "magnitude_db": list(self.curve.db),
            "excluded": [bool(flag) for flag in self.excluded],
            "graph_fingerprint": self.graph_fingerprint,
            "captured_at": self.captured_at,
            "artifact_ref": self.artifact_ref,
        }

    @classmethod
    def from_dict(cls, record: Any) -> "EntryBaseline | None":
        """Rehydrate, or ``None`` for anything this build did not write.

        ``None`` rather than a raise or a partially-trusted record: a pre-key
        state file, a truncated write and a hand-edited file all mean "there is
        no comparable baseline", which already has an honest verdict
        (:data:`~.verification.BENEFIT_BASELINE_UNAVAILABLE`). Curve/mask length
        agreement is checked here because
        :class:`~.verification.MeasurementComparand` would otherwise raise
        inside a verdict computation.
        """

        if not isinstance(record, Mapping):
            return None
        freqs = record.get("freqs_hz")
        levels = record.get("magnitude_db")
        excluded = record.get("excluded")
        if not isinstance(freqs, (list, tuple)) or not isinstance(
            levels, (list, tuple)
        ):
            return None
        if not isinstance(excluded, (list, tuple)) or len(excluded) != len(freqs):
            return None
        try:
            curve = ResponseCurve(freqs, levels)
            return cls(
                program_id=_text(
                    record.get("program_id"), field_name="program_id"
                ),
                reference_mark=_text(
                    record.get("reference_mark"), field_name="reference_mark"
                ),
                curve=curve,
                excluded=tuple(bool(flag) for flag in excluded),
                graph_fingerprint=_text(
                    record.get("graph_fingerprint"),
                    field_name="graph_fingerprint",
                ),
                captured_at=_text(
                    record.get("captured_at"), field_name="captured_at"
                ),
                artifact_ref=str(record.get("artifact_ref") or ""),
            )
        except CrossoverV2ContractError:
            return None


# --------------------------------------------------------------------------
# the one assembly decision: a shared mask
# --------------------------------------------------------------------------


def benefit_comparands(
    *,
    baseline: MeasuredResponse | None,
    post: MeasuredResponse | None,
) -> tuple[MeasurementComparand | None, MeasurementComparand | None]:
    """The two sides :func:`~.verification.evaluate_benefit` grades.

    The union of the two per-capture screens, applied to both sides — the
    assembly decision the evaluator explicitly leaves to its caller, taken here
    once. A bin either capture could not trust is dropped from both, which keeps
    the denominators identical.

    Comparability is NOT decided here: a program, mark or grid disagreement is
    passed through to the evaluator, since a caller that silently resampled one
    side onto the other's grid would manufacture the comparability the round is
    checking. On differing grids the two per-capture masks ride unchanged.
    """

    if baseline is None or post is None:
        return (
            None if baseline is None else _comparand(baseline, baseline.excluded),
            None if post is None else _comparand(post, post.excluded),
        )
    if baseline.curve.hz != post.curve.hz:
        return _comparand(baseline, baseline.excluded), _comparand(
            post, post.excluded
        )
    shared = tuple(
        bool(a or b) for a, b in zip(baseline.excluded, post.excluded, strict=True)
    )
    return _comparand(baseline, shared), _comparand(post, shared)


def _comparand(
    measured: MeasuredResponse, mask: Iterable[Any]
) -> MeasurementComparand:
    return MeasurementComparand(
        program_id=measured.program_id,
        reference_mark=measured.reference_mark,
        curve=measured.curve,
        exclusion_mask=mask,
    )


# --------------------------------------------------------------------------
# one round, graded
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundEvaluation:
    """Four verdicts, the contract they compose into, and what to do.

    The four :class:`~.verification.Verdict` objects are kept alongside the
    collapsed :class:`~.contracts.VerificationResult`: the contract carries the
    four statuses, the verdicts carry the numbers each was read from. The host
    logs the verdicts and puts the contract on the receipt.
    """

    capture: Verdict[CaptureValidity]
    realization: Verdict[RealizationStatus]
    benefit: Verdict[BenefitStatus]
    spec: Verdict[SpecStatus]
    result: VerificationResult
    #: The first three axes :func:`~.verification.decide_adoption` reads (#2537;
    #: #2602 added the fourth below), kept beside the verdicts they compose
    #: because the decision keeps only the row and the deciding axis's reason.
    trust: Verdict[EvidenceTrust]
    safety: Verdict[SafetyStatus]
    quality: Verdict[QualityStatus]
    #: #2602's fourth axis — whether a flatter result is still reachable, and
    #: the objectives it was read from.
    headroom: Verdict[IterationHeadroom]
    adoption: AdoptionDecision
    #: The post-apply pooled spec residual, lower-is-better, or ``None``. It IS
    #: the benefit axis's own reduction — :func:`~.verification.pooled_residual`
    #: over the post side of :func:`benefit_comparands` — so the two cannot
    #: disagree, and it is computed even when that axis cannot grade. Its
    #: readership is the round's journal line, and that is the whole list; it is
    #: NOT the same-named ``post_residual_db`` key on
    #: :func:`~.verification.evaluate_region_benefit`'s evidence, which is a
    #: different instrument's number over a different band.
    post_residual_db: float | None = None
    post_residual_bins: int | None = None
    #: Decision 10's blend-region prescription for the NEXT round. Never an
    #: adoption input: it changes who may act, not what may stop a round.
    blend: "BlendCorrection | None" = None
    #: The benefit claim restricted to the blend region — a SECOND reported
    #: claim beside the pooled one: a win confined to two octaves cannot show
    #: itself in a residual pooled over six.
    region_benefit: Verdict[BenefitStatus] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdicts": {
                "capture": self.capture.to_dict(),
                "realization": self.realization.to_dict(),
                "benefit": self.benefit.to_dict(),
                "spec": self.spec.to_dict(),
            },
            "axes": self.axes(),
            "result": self.result.to_dict(),
            "adoption": self.adoption.to_dict(),
            "post_residual_db": self.post_residual_db,
            "post_residual_bins": self.post_residual_bins,
            "blend": None if self.blend is None else self.blend.to_dict(),
            "region_benefit": (
                None if self.region_benefit is None
                else self.region_benefit.to_dict()
            ),
        }

    def axes(self) -> dict[str, Any]:
        """The four adoption axes alone, for the receipt's ``round_axes``.

        Separated from :meth:`to_dict` because the verdicts underneath them
        already ride on the receipt's ``verification`` field.
        """

        return {
            "trust": self.trust.to_dict(),
            "safety": self.safety.to_dict(),
            "quality": self.quality.to_dict(),
            "headroom": self.headroom.to_dict(),
        }


def evaluate_round(
    *,
    post_analysis: "ProgramAnalysis | None",
    entry_baseline: EntryBaseline | None,
    spec_report: "FlatSpecReport | None",
    tracking: Mapping[str, Any] | None,
    realization_tolerance_db: float,
    reference_mark: str,
    boosted: bool,
    rollback_available: bool,
    delta_probe: Any | None = None,
    restore_failed: bool = False,
    margin_db: float = MEASURED_BENEFIT_MARGIN_DB,
    previous_objectives: FlatnessObjectives | None = None,
    trusted_floor_hz: float | None = None,
    previous_trusted_floor_hz: float | None = None,
    round_ordinal: int = 1,
    round_cap: int = ROUND_SERIES_CAP,
    plateau_db: float = ITERATION_PLATEAU_DB,
    graded_spec: "GradedSpec | None" = None,
    applied_blend_correction: Sequence[Mapping[str, Any]] | None = None,
    previous_blend_residual_db: float | None = None,
) -> RoundEvaluation:
    """Grade one round: the four questions, the four axes, then the table.

    Decides nothing the evaluators do not — the table is
    :func:`~.verification.decide_adoption`'s and the four answers are the four
    functions'; this adds only the ordering between them.

    An unusable capture SHORT-CIRCUITS rather than being graded and then
    overwritten: the other three return their no-evidence verdicts carrying the
    capture's own reason, so the logged evidence cannot claim a benefit no
    usable capture supports.

    Constraints on individual inputs, not a full parameter list.
    ``entry_baseline`` ``None`` means the round has no comparable "before" and
    the benefit is :attr:`~.contracts.BenefitStatus.INDETERMINATE`.
    ``spec_report`` cannot change the adoption — spec is "any" in every row of
    #2291's table — but each failing band rides into the receipt as a next-round
    target. ``realization_tolerance_db`` is ``.contracts.VERIFY_TOLERANCE_DB``,
    passed rather than imported so this module holds no threshold another owns.
    An absent ``delta_probe`` (#2537) reports no finding rather than an unsafe
    one, and still cannot let a round call itself PASSED. ``margin_db``,
    ``round_cap`` and ``plateau_db`` default from this module's own constants,
    and no call site passes them. ``previous_objectives`` ``None`` is the first
    round, read by the headroom axis as "the plateau stop cannot fire";
    ``trusted_floor_hz`` / ``previous_trusted_floor_hz`` are the frames those
    objectives were graded in (#2609 SF5), and ``None`` on either side means the
    frame is unknown, read as "no evidence the frame moved". ``round_ordinal``
    is 1-based, and its default of 1 can only offer another round, never
    suppress a stop the evidence asked for. ``graded_spec`` must be the CLOUD's
    evaluation — its merged honesty mask is the only structural protection
    against prescribing a cut at an interference null, and #2600 item 1 records
    the null detector as uncalibrated across the blend window of any crossover
    below 4 kHz — with ``None`` meaning no blend correction is prescribed.
    ``applied_blend_correction`` is read off the APPLIED candidate: ``None``
    means it could not be established and the round refuses to prescribe rather
    than assuming zero, while ``()`` is the ordinary "it rode none".
    """

    from .blend_correction import solve_blend_correction
    from .verification import (
        decide_adoption,
        evaluate_applied_safety,
        evaluate_benefit,
        evaluate_region_benefit,
        evaluate_capture_validity,
        evaluate_evidence_trust,
        evaluate_iteration_headroom,
        evaluate_realization,
        evaluate_round_quality,
        evaluate_spec,
        flatness_objectives,
        pooled_residual,
        verification_result,
    )

    integrity = getattr(post_analysis, "capture_integrity", None)
    capture = evaluate_capture_validity(integrity)
    if capture.status is CaptureValidity.UNUSABLE:
        realization: Any = Verdict(
            RealizationStatus.UNAVAILABLE, capture.reason, capture.evidence
        )
        benefit: Any = Verdict(
            BenefitStatus.INDETERMINATE, capture.reason, capture.evidence
        )
        spec: Any = Verdict(SpecStatus.UNEVALUABLE, capture.reason, capture.evidence)
        post_residual: tuple[float, int] | None = None
        # An unusable capture prescribes nothing, on the same rule the three
        # verdicts above follow: what is logged must be what was decided.
        blend: Any = None
        region_benefit: Any = None
    else:
        realization = evaluate_realization(
            tracking=tracking, tolerance_db=realization_tolerance_db
        )
        post = measured_response_from_analysis(
            post_analysis, reference_mark=reference_mark
        )
        before, after = benefit_comparands(
            baseline=(
                None if entry_baseline is None else entry_baseline.as_measurement()
            ),
            post=post,
        )
        benefit = evaluate_benefit(
            entry_baseline=before, post=after, margin_db=margin_db
        )
        spec = evaluate_spec(spec_report)
        post_residual = None if after is None else pooled_residual(after)
        # The band is the VERIFY absolute claim's own — the output of
        # ``comparison_bands.crossover_region_band_hz``, reached through that
        # function's existing consumer rather than re-derived, so the region
        # corrected over is byte-identically the one the done screen shows. The
        # second half says whether the speaker HAS no region or this round
        # failed to establish the one it has.
        band_hz, no_crossover = _crossover_region(post_analysis)
        blend = solve_blend_correction(
            graded=graded_spec,
            band_hz=band_hz,
            incumbent=applied_blend_correction,
            previous_residual_db=previous_blend_residual_db,
            no_crossover_reason=no_crossover,
        )
        region_benefit = evaluate_region_benefit(
            entry_baseline=before, post=after, band_hz=band_hz,
            margin_db=margin_db, no_crossover_reason=no_crossover,
        )

    result = verification_result(
        capture=capture, realization=realization, benefit=benefit, spec=spec
    )
    # The axes are composed from the FOUR VERDICT OBJECTS, not from ``result``'s
    # collapsed statuses (#2537): a row's reason is the deciding axis's reason,
    # and ``result`` keeps only the statuses plus one joined string.
    trust = evaluate_evidence_trust(capture=capture, realization=realization)
    safety = evaluate_applied_safety(probe=delta_probe, integrity=integrity)
    quality = evaluate_round_quality(
        realization=realization, benefit=benefit, spec=spec,
        probe=delta_probe, spec_report=spec_report,
    )
    # #2602's axis, read from the SAME ``spec_report`` the spec verdict is — one
    # report, two questions — so the screen can never say a round passed a
    # report the headroom axis graded from different evidence.
    headroom = evaluate_iteration_headroom(
        objectives=flatness_objectives(spec_report),
        previous=previous_objectives,
        round_ordinal=round_ordinal,
        round_cap=round_cap,
        plateau_db=plateau_db,
        trusted_floor_hz=trusted_floor_hz,
        previous_trusted_floor_hz=previous_trusted_floor_hz,
    )
    adoption = decide_adoption(
        trust=trust,
        safety=safety,
        quality=quality,
        headroom=headroom,
        boosted=boosted,
        rollback_available=rollback_available,
        restore_failed=restore_failed,
    )
    return RoundEvaluation(
        capture=capture,
        realization=realization,
        benefit=benefit,
        spec=spec,
        result=result,
        trust=trust,
        safety=safety,
        quality=quality,
        headroom=headroom,
        adoption=adoption,
        post_residual_db=None if post_residual is None else post_residual[0],
        post_residual_bins=None if post_residual is None else post_residual[1],
        blend=blend,
        region_benefit=region_benefit,
    )


def _crossover_region(
    post_analysis: "ProgramAnalysis | None",
) -> tuple[tuple[float, float] | None, str | None]:
    """``(band_hz, no_crossover_reason)``, both read off the VERIFY absolute claim.

    The band's owner is
    :func:`~jasper.audio_measurement.comparison_bands.crossover_region_band_hz`,
    and this reads its output rather than calling it again. That function is
    deliberately NOT ``overlap_band_hz``, which clamps the lower edge UP to the
    tweeter's own sweep floor for single-branch consumers: on jts3 that clamp is
    1600 Hz while the series-1 dip sat at 1921-1938 Hz, so a per-branch band
    would amputate the bottom half of what this correction addresses. Reading it
    also means this module holds no copy of the corner.

    A ``None`` band for every ``not_evaluated`` arm. The reason is
    :data:`ABSOLUTE_NO_CROSSOVER_TOPOLOGY` on that claim's ONE arm saying the
    speaker HAS no region — a different next action from every other arm.
    """

    absolute = getattr(post_analysis, "verify_absolute", None)
    if not isinstance(absolute, Mapping):
        return (None, None)
    reason = (
        ABSOLUTE_NO_CROSSOVER_TOPOLOGY
        if absolute.get("not_evaluated") == ABSOLUTE_NO_CROSSOVER_TOPOLOGY
        else None
    )
    band = absolute.get("band_hz")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return (None, reason)
    try:
        lo, hi = float(band[0]), float(band[1])
    except (TypeError, ValueError):
        return (None, reason)
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo <= 0.0 or hi <= lo:
        return (None, reason)
    return ((lo, hi), reason)


def build_round_receipt(
    *,
    round_id: str,
    evaluation: RoundEvaluation,
    entry_baseline: EntryBaseline | None,
    entry_graph_fingerprint: str,
    rollback_anchor: Mapping[str, Any] | None,
    proposal_fingerprint: str,
    proposal_fingerprint_kind: str,
    applied_graph_fingerprint: str,
    post_measurement: Mapping[str, Any] | None,
    restore_result: Mapping[str, Any] | None,
    evidence_identities: Mapping[str, Any] | None,
    created_at: str,
    round_measurements: Mapping[str, Any] | None = None,
) -> RoundReceipt:
    """Assemble #2291's immutable round receipt from the round's own facts.

    **Identities, not payloads.** ``entry_baseline`` on the receipt is the
    baseline's identity — program, mark, graph fingerprint, when, and the
    retained artifact it can be re-read from — not its 512-point curve, which
    already lives in two places that outlive this record. Same rule for
    ``post_measurement``. The commanded delta and the applied candidate's
    fingerprint ride in ``evidence_identities``.

    ``proposal_fingerprint_kind`` is passed through unvalidated on purpose:
    :class:`~.contracts.RoundReceipt` owns the closed vocabulary.
    ``round_measurements`` is the round's own uncollapsed numbers, defaulted
    because a round can honestly produce neither; it is passed through rather
    than derived because the caller holds the instruments.
    """

    return RoundReceipt(
        round_id=round_id,
        entry_graph_fingerprint=entry_graph_fingerprint,
        rollback_anchor=rollback_anchor,
        entry_baseline=(
            None if entry_baseline is None else _baseline_identity(entry_baseline)
        ),
        proposal_fingerprint=proposal_fingerprint,
        proposal_fingerprint_kind=proposal_fingerprint_kind,
        applied_graph_fingerprint=applied_graph_fingerprint,
        post_measurement=post_measurement,
        verification=evaluation.result,
        adoption=evaluation.adoption,
        # The axes the row was read off (#2537, #2602), banked with the decision
        # so a receipt's targets travel to the NEXT round.
        round_axes=evaluation.axes(),
        restore_result=restore_result,
        round_measurements=round_measurements,
        evidence_identities=evidence_identities,
        created_at=created_at,
    )


def _baseline_identity(baseline: EntryBaseline) -> dict[str, Any]:
    return {
        "kind": ENTRY_BASELINE_KIND,
        "program_id": baseline.program_id,
        "reference_mark": baseline.reference_mark,
        "graph_fingerprint": baseline.graph_fingerprint,
        "captured_at": baseline.captured_at,
        "artifact_ref": baseline.artifact_ref,
        "n_bins": len(baseline.curve.hz),
        "n_excluded": sum(1 for flag in baseline.excluded if flag),
    }
