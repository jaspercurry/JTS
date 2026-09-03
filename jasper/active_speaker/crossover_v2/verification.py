# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Four verification verdicts, four adoption axes, and the adoption table.

Capture validity, realization, benefit and spec are graded independently — spec
is an outcome, never a proxy for benefit — and compose into four adoption axes
from which :func:`decide_adoption` selects one of seven rows.

This module invents no DSP and owns no threshold: every number it reports is
lifted from a shipped primitive, and every threshold is a required parameter
because it is loop policy. Absent evidence is reported as unavailable, never as
a failure. See docs/measurement-loop-doctrine.md for the series policy.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Iterable, Mapping, Sequence, TypeVar

import numpy as np

from jasper.log_event import log_event

from ..delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED,
    VERDICT_SAFETY_ONLY,
    seam_rollback_deferral,
)
from ..flat_spec import (
    FlatSpecReport,
    SPEC_BANDS,
    evaluate_flat_spec,
    spec_band_tilt,
    spec_convergence_residual,
    spec_flatness_gauge,
)
from .contracts import (
    CLAIM_FAIL,
    CLAIM_NOT_EVALUATED,
    CLAIM_PASS,
    ADOPTION_ROW_KEEP,
    ADOPTION_ROW_KEEP_FOR_ITERATION,
    ADOPTION_ROW_KEEP_ITERATING,
    ADOPTION_ROW_KEEP_MISSED_EXHAUSTED,
    ADOPTION_ROW_RESTORE_FAILED,
    ADOPTION_ROW_RESTORE_REGRESSION,
    ADOPTION_ROW_RESTORE_UNSAFE,
    ADOPTION_ROW_RESTORE_UNTRUSTED,
    AdoptionDecision,
    AdoptionOutcome,
    BenefitStatus,
    CaptureValidity,
    CrossoverV2ContractError,
    EvidenceTrust,
    IterationHeadroom,
    QualityStatus,
    RealizationStatus,
    ResponseCurve,
    SafetyStatus,
    SpecStatus,
    VERIFY_TOLERANCE_DB,
    VerificationResult,
    detached_json,
)

# Package-private vocabulary, owned once by the sibling module: the validators
# beside the types they validate, and the record-field coercer both this
# evaluator and :mod:`.diagnostics` read.
from .contracts import _positive as _positive_db
from .contracts import _rounded
from .contracts import _text

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only: importing :mod:`jasper.audio_measurement.program_analysis` at
    # runtime would drag scipy into every import of this evaluator. Only
    # ``.failed`` / ``.not_evaluated`` are read, duck-typed.
    from jasper.audio_measurement.program_analysis import CaptureIntegrity

logger = logging.getLogger(__name__)

__all__ = [
    "ADOPTION_MEASURED_REGRESSION",
    "ADOPTION_NO_ROLLBACK_ANCHOR",
    "ADOPTION_REALIZATION_FAILED",
    "ADOPTION_REALIZATION_UNAVAILABLE",
    "ADOPTION_REALIZED_AND_IMPROVED",
    "ADOPTION_RESTORE_FAILED",
    "ADOPTION_UNPROVEN",
    "ADOPTION_UNPROVEN_BOOST",
    "BENEFIT_BASELINE_UNAVAILABLE",
    "BENEFIT_GRID_MISMATCH",
    "BENEFIT_IMPROVED",
    "BENEFIT_MARK_MISMATCH",
    "BENEFIT_MASK_MISMATCH",
    "BENEFIT_POST_UNAVAILABLE",
    "BENEFIT_PROGRAM_MISMATCH",
    "BENEFIT_REGRESSED",
    "BENEFIT_RESIDUAL_UNEVALUABLE",
    "BENEFIT_WITHIN_MARGIN",
    "CAPTURE_INTEGRITY_CLEAN",
    "CAPTURE_INTEGRITY_FAILED",
    "CAPTURE_INTEGRITY_UNAVAILABLE",
    "CLAIM_NO_PER_BRANCH_CAPTURE",
    "CLIPPED_RUN_CHECK",
    "ECHO_BAND_HF_REGIME_FLOOR_HZ",
    "HEADROOM_CAP_REACHED",
    "HEADROOM_NO_OBJECTIVES",
    "HEADROOM_PLATEAUED",
    "HEADROOM_REACHABLE",
    "HEADROOM_WITHIN_PLATEAU",
    "FlatnessObjectives",
    "MeasurementComparand",
    "REALIZATION_COMPARAND",
    "REALIZATION_NO_COMPARATOR",
    "REALIZATION_NO_TRACKING",
    "REALIZATION_OUT_OF_TOLERANCE",
    "REALIZATION_WITHIN_TOLERANCE",
    "RESULT_INCONCLUSIVE",
    "RESULT_KEEP_PREVIOUS",
    "RESULT_VERIFIED_BEST_EVALUATED",
    "RESULT_VERIFIED_TARGET",
    "SAFETY_BOOST_OVER_DECLARED_BOUND",
    "SAFETY_CLIPPED_CAPTURE",
    "SAFETY_NO_FINDING",
    "SAFETY_NO_FINDING_UNMEASURED",
    "SAFETY_UNCOMMANDED_LEVEL_LOUDER",
    "SPEC_BAND_OUT_OF_TOLERANCE",
    "SPEC_IN_TOLERANCE",
    "SPEC_NO_EVALUABLE_BAND",
    "SPEC_NO_REPORT",
    "SPEC_PARTIAL_COVERAGE",
    "TRACKING_COMPARATOR_KEY",
    "TRUST_MEASURED",
    "Verdict",
    "committed_crossover_region_hz",
    "decide_adoption",
    "evaluate_applied_safety",
    "evaluate_benefit",
    "evaluate_capture_validity",
    "evaluate_evidence_trust",
    "evaluate_iteration_headroom",
    "evaluate_realization",
    "evaluate_round_quality",
    "evaluate_spec",
    "flatness_objectives",
    "identity_mismatch",
    "pooled_residual",
    "spec_band_rows",
    "verification_result",
    "verify_absolute_tolerance_db",
]


# --------------------------------------------------------------------------
# the shared verdict envelope
# --------------------------------------------------------------------------


StatusT = TypeVar("StatusT", bound=Enum)


@dataclass(frozen=True)
class Verdict(Generic[StatusT]):
    """One verdict, its reason, and the evidence it was read from.

    ``reason`` is drawn from this module's named constants, so a consumer
    branches on a symbol rather than matching prose. ``evidence`` is a
    JSON-shaped mapping of the numbers the verdict was read from, deep-copied
    at construction by :func:`~.contracts.detached_json` so that a frozen
    record can never alias a caller's live mapping.
    """

    status: StatusT
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", detached_json(dict(self.evidence)))

    def to_dict(self) -> dict[str, Any]:
        # Detached on the way OUT as well as in: a shallow ``dict()`` would
        # hand a consumer the verdict's own nested containers to edit.
        return {
            "status": self.status.value,
            "reason": self.reason,
            "evidence": detached_json(dict(self.evidence)),
        }


# --------------------------------------------------------------------------
# 1. capture validity
# --------------------------------------------------------------------------

#: The integrity record was absent — the same reason the attempts ledger
#: stamps (``crossover_v2_flow.attempt_record_from_verify``).
CAPTURE_INTEGRITY_UNAVAILABLE = "capture_integrity_unavailable"
CAPTURE_INTEGRITY_FAILED = "capture_integrity_failed"
CAPTURE_INTEGRITY_CLEAN = "capture_integrity_clean"


def evaluate_capture_validity(
    integrity: "CaptureIntegrity | None",
) -> Verdict[CaptureValidity]:
    """Was this capture usable? The shipped comparability rule, verbatim.

    * a **missing** record is unusable — ``None`` means "no evidence", never
      "clean", so absence fails closed;
    * a record with **failed** checks is unusable, named by those checks;
    * a record carrying only **not_evaluated** checks is *usable*
      (``comparable = not integrity.failed``); those names still ride in
      ``evidence`` so a reader sees what went ungraded.
    """

    if integrity is None:
        return Verdict(CaptureValidity.UNUSABLE, CAPTURE_INTEGRITY_UNAVAILABLE, {})
    failed = tuple(integrity.failed)
    not_evaluated = tuple(integrity.not_evaluated)
    evidence: dict[str, Any] = {
        "failed": list(failed),
        "not_evaluated": list(not_evaluated),
    }
    if failed:
        return Verdict(CaptureValidity.UNUSABLE, CAPTURE_INTEGRITY_FAILED, evidence)
    return Verdict(CaptureValidity.USABLE, CAPTURE_INTEGRITY_CLEAN, evidence)


# --------------------------------------------------------------------------
# 2. realization
# --------------------------------------------------------------------------

#: The comparator the deployed VERIFY gate reads out of
#: ``ProgramAnalysis.verify_tracking``.
TRACKING_COMPARATOR_KEY = "max_db_notch_excluded"

#: WHAT the comparator above compares the measured VERIFY sum AGAINST:
#: ``MeasurementPriors.predicted_sum``, the applied candidate's own predicted
#: summed magnitude at its committed trim and delay — an ABSOLUTE
#: curve-against-curve claim.
#:
#: On the record because the receipt carries a SECOND realization number
#: measured against something else: the delta probe's per-band
#: realized/commanded ratios, whose comparand is the COMMANDED DELTA
#: (ADR-0209). The two disagree routinely and neither impeaches the other.
REALIZATION_COMPARAND = "applied_candidate_predicted_sum"

REALIZATION_NO_TRACKING = "tracking_unavailable"
REALIZATION_NO_COMPARATOR = "tracking_comparator_absent"
REALIZATION_WITHIN_TOLERANCE = "tracking_within_tolerance"
REALIZATION_OUT_OF_TOLERANCE = "tracking_out_of_tolerance"


def evaluate_realization(
    *,
    tracking: Mapping[str, Any] | None,
    tolerance_db: float,
) -> Verdict[RealizationStatus]:
    """Did the applied graph produce the change we commanded?

    Grades ``ProgramAnalysis.verify_tracking``'s
    :data:`TRACKING_COMPARATOR_KEY` against ``tolerance_db`` — the same number,
    from the same key, as the deployed VERIFY gate. The verdict names its
    COMPARAND (:data:`REALIZATION_COMPARAND`), which is not the comparator.

    Absent evidence is not a failure: a missing mapping, or a
    missing/non-numeric/non-finite comparator, is
    :attr:`~.contracts.RealizationStatus.UNAVAILABLE` rather than
    :attr:`~.contracts.RealizationStatus.FAILED`, because ``failed`` restores.
    ``bool`` is rejected as a comparator because ``True`` is an ``int`` and
    would silently grade as 1.0 dB.

    ``tolerance_db`` is required and must be positive: this module holds no
    threshold policy.
    """

    tolerance = _positive_db(tolerance_db, field_name="tolerance_db")
    if tracking is None:
        return Verdict(RealizationStatus.UNAVAILABLE, REALIZATION_NO_TRACKING, {})
    measured = tracking.get(TRACKING_COMPARATOR_KEY)
    if isinstance(measured, bool) or not isinstance(measured, (int, float)):
        return Verdict(
            RealizationStatus.UNAVAILABLE,
            REALIZATION_NO_COMPARATOR,
            {
                "comparator": TRACKING_COMPARATOR_KEY,
                "comparand": REALIZATION_COMPARAND,
                "tolerance_db": tolerance,
            },
        )
    deviation_db = float(measured)
    evidence = {
        "comparator": TRACKING_COMPARATOR_KEY,
        # WHAT the number beside it was measured against (#3483). Named on
        # every graded verdict, because "matched" is one of the four adoption
        # inputs and the receipt carries a second realization number graded
        # against a different comparand — see :data:`REALIZATION_COMPARAND`.
        "comparand": REALIZATION_COMPARAND,
        "deviation_db": deviation_db,
        "tolerance_db": tolerance,
    }
    if not np.isfinite(deviation_db):
        return Verdict(
            RealizationStatus.UNAVAILABLE, REALIZATION_NO_COMPARATOR, evidence
        )
    if deviation_db > tolerance:
        return Verdict(
            RealizationStatus.FAILED, REALIZATION_OUT_OF_TOLERANCE, evidence
        )
    return Verdict(
        RealizationStatus.MATCHED, REALIZATION_WITHIN_TOLERANCE, evidence
    )


# --------------------------------------------------------------------------
# 3. measured benefit
# --------------------------------------------------------------------------

BENEFIT_BASELINE_UNAVAILABLE = "entry_baseline_unavailable"
BENEFIT_POST_UNAVAILABLE = "post_measurement_unavailable"
BENEFIT_PROGRAM_MISMATCH = "incomparable_program"
BENEFIT_MARK_MISMATCH = "incomparable_reference_mark"
BENEFIT_GRID_MISMATCH = "incomparable_frequency_grid"
BENEFIT_MASK_MISMATCH = "incomparable_exclusion_mask"
BENEFIT_RESIDUAL_UNEVALUABLE = "residual_unevaluable"
BENEFIT_IMPROVED = "residual_improved"
BENEFIT_REGRESSED = "residual_regressed"
BENEFIT_WITHIN_MARGIN = "residual_within_margin"


@dataclass(frozen=True, init=False)
class MeasurementComparand:
    """One side of the before→after benefit comparison.

    ``program_id`` is a SHA-256 over the whole excitation schedule — phase,
    sample rate, channels, and every segment including its ``gain_db`` and
    ``effective_peak_dbfs`` — so equal ids guarantee the same program AND the
    same level. ``reference_mark`` is the position identity the program id does
    not cover.

    Calibration and analyzer version are NOT checkable here: neither is carried
    on ``ProgramAnalysis``, so a caller must not re-calibrate between a round's
    two captures.

    ``curve`` is the spatially-combined, 1/3-octave-smoothed magnitude
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` consumes;
    ``exclusion_mask`` is that evaluator's per-bin interference screen, one
    flag per curve point.
    """

    program_id: str
    reference_mark: str
    curve: ResponseCurve
    exclusion_mask: tuple[bool, ...]

    def __init__(
        self,
        *,
        program_id: str,
        reference_mark: str,
        curve: ResponseCurve,
        exclusion_mask: Iterable[Any] | None = None,
    ) -> None:
        if not isinstance(curve, ResponseCurve):
            raise CrossoverV2ContractError("curve must be a ResponseCurve")
        mask = (
            (False,) * len(curve.hz)
            if exclusion_mask is None
            else tuple(bool(value) for value in exclusion_mask)
        )
        if len(mask) != len(curve.hz):
            raise CrossoverV2ContractError(
                "exclusion_mask needs one flag per curve point"
            )
        object.__setattr__(
            self, "program_id", _text(program_id, field_name="program_id")
        )
        object.__setattr__(
            self, "reference_mark", _text(reference_mark, field_name="reference_mark")
        )
        object.__setattr__(self, "curve", curve)
        object.__setattr__(self, "exclusion_mask", mask)


def evaluate_benefit(
    *,
    entry_baseline: MeasurementComparand | None,
    post: MeasurementComparand | None,
    margin_db: float,
) -> Verdict[BenefitStatus]:
    """Did the measured speaker get better? The core empirical claim.

    The metric is the pooled spec residual —
    :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` over
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` — read on each
    side and differenced: ``improvement_db = baseline.rms_db - post.rms_db``
    (lower is flatter, so a positive difference is an improvement).

    Comparability is checked, never assumed: a missing side, or any mismatch in
    program, reference mark, frequency grid or exclusion mask, is
    :attr:`~.contracts.BenefitStatus.INDETERMINATE` with the reason naming
    which broke. The two masks must be IDENTICAL, because a residual that fell
    only because the mask grew is the same speaker graded on fewer bins; a
    caller wanting the union of two screens computes it and passes it on both
    sides.

    ``margin_db`` is required and positive, and the band is symmetric, so a
    difference this instrument cannot resolve never becomes a claim.
    """

    margin = _positive_db(margin_db, field_name="margin_db")
    if entry_baseline is None:
        return Verdict(
            BenefitStatus.INDETERMINATE, BENEFIT_BASELINE_UNAVAILABLE, {}
        )
    if post is None:
        return Verdict(BenefitStatus.INDETERMINATE, BENEFIT_POST_UNAVAILABLE, {})

    mismatch = _comparability_mismatch(entry_baseline, post)
    if mismatch is not None:
        return Verdict(
            BenefitStatus.INDETERMINATE,
            mismatch,
            {
                "baseline_program_id": entry_baseline.program_id,
                "post_program_id": post.program_id,
                "baseline_reference_mark": entry_baseline.reference_mark,
                "post_reference_mark": post.reference_mark,
            },
        )

    before = pooled_residual(entry_baseline)
    after = pooled_residual(post)
    if before is None or after is None:
        return Verdict(
            BenefitStatus.INDETERMINATE,
            BENEFIT_RESIDUAL_UNEVALUABLE,
            {
                "baseline_residual_db": None if before is None else before[0],
                "post_residual_db": None if after is None else after[0],
            },
        )
    baseline_rms_db, baseline_bins = before
    post_rms_db, post_bins = after
    improvement_db = baseline_rms_db - post_rms_db
    evidence = {
        "baseline_residual_db": baseline_rms_db,
        "post_residual_db": post_rms_db,
        "improvement_db": improvement_db,
        "margin_db": margin,
        "n_bins": post_bins,
        "baseline_n_bins": baseline_bins,
    }
    if improvement_db >= margin:
        return Verdict(BenefitStatus.IMPROVED, BENEFIT_IMPROVED, evidence)
    if improvement_db <= -margin:
        return Verdict(BenefitStatus.REGRESSED, BENEFIT_REGRESSED, evidence)
    return Verdict(BenefitStatus.INDETERMINATE, BENEFIT_WITHIN_MARGIN, evidence)


def identity_mismatch(
    *,
    program_id: str,
    reference_mark: str,
    other_program_id: str,
    other_reference_mark: str,
) -> str | None:
    """Are two captures the same PROGRAM at the same MARK?

    The identity half of :func:`_comparability_mismatch`, shared with the delta
    probe's pre-apply anchor. Ordered most-identifying first, so the reason
    names the root difference; ``None`` when both match.

    The GRID and the MASK are deliberately not here: they are the arithmetic's
    to answer, and each caller does that its own way — the benefit axis
    refuses, the probe interpolates onto its own grid.
    """

    if program_id != other_program_id:
        return BENEFIT_PROGRAM_MISMATCH
    if reference_mark != other_reference_mark:
        return BENEFIT_MARK_MISMATCH
    return None


def _comparability_mismatch(
    baseline: MeasurementComparand, post: MeasurementComparand
) -> str | None:
    """The first way these two cannot be compared, or ``None``.

    Ordered most-identifying first, so the reason names the root difference:
    a different program makes the grids differing uninteresting.
    """

    identity = identity_mismatch(
        program_id=baseline.program_id,
        reference_mark=baseline.reference_mark,
        other_program_id=post.program_id,
        other_reference_mark=post.reference_mark,
    )
    if identity is not None:
        return identity
    if baseline.curve.hz != post.curve.hz:
        return BENEFIT_GRID_MISMATCH
    if baseline.exclusion_mask != post.exclusion_mask:
        return BENEFIT_MASK_MISMATCH
    return None


def pooled_residual(
    comparand: MeasurementComparand,
) -> tuple[float, int] | None:
    """``(rms_db, n_bins)`` from the shipped evaluator, or ``None``.

    ``evaluate_flat_spec`` raises on a degenerate curve — a non-ascending axis,
    a reference band with no surviving bins — which become ``None`` here and
    :data:`BENEFIT_RESIDUAL_UNEVALUABLE` above.

    Not :func:`~jasper.active_speaker.flat_spec_views.log_pooled_residual`,
    which re-pools a FINISHED report by octave; this one grades a curve.
    """

    try:
        report = evaluate_flat_spec(
            np.asarray(comparand.curve.hz, dtype=np.float64),
            np.asarray(comparand.curve.db, dtype=np.float64),
            np.asarray(comparand.exclusion_mask, dtype=bool),
        )
    except ValueError:
        return None
    residual = spec_convergence_residual(report)
    if not residual.evaluable or residual.rms_db is None:
        return None
    return float(residual.rms_db), int(residual.n_bins)


#: The region benefit claim had no crossover region to grade over. Distinct
#: from :data:`BENEFIT_RESIDUAL_UNEVALUABLE`, which means there WAS a band and
#: the curve could not be graded over it.
BENEFIT_NO_REGION_BAND = "no_region_band"


def evaluate_region_benefit(
    *,
    entry_baseline: MeasurementComparand | None,
    post: MeasurementComparand | None,
    band_hz: tuple[float, float] | None,
    margin_db: float,
    no_crossover_reason: str | None = None,
) -> Verdict[BenefitStatus]:
    """The benefit claim again, restricted to the crossover blend region.

    :func:`evaluate_benefit` pools across all three ``SPEC_BANDS`` — 250 Hz to
    the graded ceiling — so a win confined to a two-octave blend region is
    diluted across that span and lands inside the margin.

    A second REPORTED claim, never a second gate: the pooled verdict stays the
    adoption input. Everything except the band is unchanged, the margin
    included — narrowing the band does not sharpen the instrument.

    Narrowing the mask re-routes ``evaluate_flat_spec``'s own centering: a
    region-masked evaluation is referenced to the REGION's surviving bins
    rather than to the low-mid band, so the verdict is blind to a level change.
    The right frame for grading a SHAPE and the wrong one for prescribing a
    cut, which is why :func:`~.blend_correction.solve_blend_correction` does
    not use it. Applied to BOTH sides identically, so
    :func:`_comparability_mismatch`'s identical-mask guarantee holds.

    ``no_crossover_reason`` (a 1-way main) is reported ahead of
    :data:`BENEFIT_NO_REGION_BAND`, which says a round could not establish the
    region its speaker does have — two facts with two remedies.
    """

    if no_crossover_reason is not None:
        return Verdict(BenefitStatus.INDETERMINATE, no_crossover_reason, {})
    if band_hz is None:
        return Verdict(BenefitStatus.INDETERMINATE, BENEFIT_NO_REGION_BAND, {})
    lo, hi = float(band_hz[0]), float(band_hz[1])
    return evaluate_benefit(
        entry_baseline=_region_masked(entry_baseline, lo, hi),
        post=_region_masked(post, lo, hi),
        margin_db=margin_db,
    )


def _region_masked(
    comparand: MeasurementComparand | None, lo_hz: float, hi_hz: float,
) -> MeasurementComparand | None:
    """``comparand`` with every bin outside ``[lo_hz, hi_hz]`` also excluded."""

    if comparand is None:
        return None
    return MeasurementComparand(
        program_id=comparand.program_id,
        reference_mark=comparand.reference_mark,
        curve=comparand.curve,
        exclusion_mask=[
            bool(excluded or hz < lo_hz or hz > hi_hz)
            for excluded, hz in zip(
                comparand.exclusion_mask, comparand.curve.hz, strict=True,
            )
        ],
    )


# --------------------------------------------------------------------------
# 4. spec
# --------------------------------------------------------------------------

SPEC_NO_REPORT = "no_spec_report"
SPEC_NO_EVALUABLE_BAND = "no_evaluable_band"
SPEC_BAND_OUT_OF_TOLERANCE = "band_out_of_tolerance"
SPEC_PARTIAL_COVERAGE = "partial_band_coverage"
SPEC_IN_TOLERANCE = "all_bands_in_tolerance"


def evaluate_spec(report: FlatSpecReport | None) -> Verdict[SpecStatus]:
    """Is the resulting speaker inside the target envelope?

    Classifies; it does not re-grade. Nothing here recomputes a deviation, a
    band membership, or a tolerance.

    ``overall_passed`` is ``False`` both for a band that measured out of
    tolerance and for a band nothing could be measured in, so the split is
    three-way:

    * some evaluable band failed on its merits → :attr:`~.contracts.SpecStatus.FAILED`;
    * nothing was evaluable, or coverage was partial →
      :attr:`~.contracts.SpecStatus.UNEVALUABLE`;
    * every band evaluable and passing → :attr:`~.contracts.SpecStatus.PASSED`.

    A ``FAILED`` band outranks partial coverage: a measured exceedance is a
    fact about the speaker, and losing another band's evidence does not unmake
    it. A ``None`` report is UNEVALUABLE.
    """

    if report is None:
        return Verdict(SpecStatus.UNEVALUABLE, SPEC_NO_REPORT, {})
    evidence = spec_flatness_gauge(report).to_dict()
    # The gauge names ONE band; these are every band and the graded span they
    # were read over. Same rows the quality axis's misses come from.
    evidence["bands"] = spec_band_rows(report)
    evidence["graded_band_hz"] = list(report.graded_band_hz)
    evidence["trusted_ceiling_hz"] = report.trusted_ceiling_hz
    if report.overall_passed:
        return Verdict(SpecStatus.PASSED, SPEC_IN_TOLERANCE, evidence)
    if any(band.evaluable and band.passed is False for band in report.bands):
        return Verdict(SpecStatus.FAILED, SPEC_BAND_OUT_OF_TOLERANCE, evidence)
    if any(band.evaluable for band in report.bands):
        return Verdict(SpecStatus.UNEVALUABLE, SPEC_PARTIAL_COVERAGE, evidence)
    return Verdict(SpecStatus.UNEVALUABLE, SPEC_NO_EVALUABLE_BAND, evidence)


# --------------------------------------------------------------------------
# composing the four into the contract
# --------------------------------------------------------------------------


def verification_result(
    *,
    capture: Verdict[CaptureValidity],
    realization: Verdict[RealizationStatus],
    benefit: Verdict[BenefitStatus],
    spec: Verdict[SpecStatus],
) -> VerificationResult:
    """The four verdicts as one :class:`~.contracts.VerificationResult`.

    An unusable capture collapses the other three to their no-evidence values
    and carries the capture's reason. Otherwise the reason joins all four into
    one string; the per-verdict evidence stays on the :class:`Verdict` objects.
    """

    if capture.status is CaptureValidity.UNUSABLE:
        return VerificationResult(
            capture_validity=CaptureValidity.UNUSABLE,
            realization=RealizationStatus.UNAVAILABLE,
            benefit=BenefitStatus.INDETERMINATE,
            spec=SpecStatus.UNEVALUABLE,
            reason=capture.reason,
        )
    return VerificationResult(
        capture_validity=capture.status,
        realization=realization.status,
        benefit=benefit.status,
        spec=spec.status,
        reason=(
            f"capture={capture.reason};realization={realization.reason};"
            f"benefit={benefit.reason};spec={spec.reason}"
        ),
    )


# --------------------------------------------------------------------------
# 5. evidence trust — was there anything to grade?
# --------------------------------------------------------------------------

#: The round measured the state it applied.
TRUST_MEASURED = "applied_state_measured"


def evaluate_evidence_trust(
    *,
    capture: Verdict[CaptureValidity],
    realization: Verdict[RealizationStatus],
) -> Verdict[EvidenceTrust]:
    """Could this round measure the state it applied?

    A composition rather than a fifth measurement: the first of the two
    evidence verdicts that says no, carrying that verdict's own reason.

    * an unusable capture — failed integrity checks, or no record at all —
      means there is no post-apply measurement;
    * an unavailable realization means the VERIFY comparator produced no
      number.

    An indeterminate BENEFIT is deliberately not here: the post-apply capture
    itself is fine, and an unprovable improvement is a QUALITY unknown.
    """

    if capture.status is CaptureValidity.UNUSABLE:
        return Verdict(
            EvidenceTrust.UNTRUSTED, capture.reason, dict(capture.evidence)
        )
    if realization.status is RealizationStatus.UNAVAILABLE:
        return Verdict(
            EvidenceTrust.UNTRUSTED, realization.reason, dict(realization.evidence)
        )
    return Verdict(
        EvidenceTrust.TRUSTED,
        TRUST_MEASURED,
        {
            "capture": capture.reason,
            "realization": realization.reason,
        },
    )


# --------------------------------------------------------------------------
# 6. safety — the only axis that pulls a measured graph off
# --------------------------------------------------------------------------

#: A boost measured MORE lift across the apply than the graph declared
#: (:func:`~jasper.active_speaker.delta_probe.boost_overshoot`). The bound this
#: names is the probe's own per-bin measurement tolerance
#: (:data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_TOLERANCE_LOW_DB`),
#: not a declared boost limit.
SAFETY_BOOST_OVER_DECLARED_BOUND = "boost_realized_above_probe_tolerance"
#: The speaker measured LOUDER than declared where nothing was commanded.
SAFETY_UNCOMMANDED_LEVEL_LOUDER = "uncommanded_level_shift_louder"
#: A stimulus segment carried a full-scale run.
SAFETY_CLIPPED_CAPTURE = "clipped_capture"
#: Nothing in the evidence available says this state is unsafe, **and the
#: realized-energy check was one of the instruments that looked.**
SAFETY_NO_FINDING = "no_unsafe_finding"
#: Nothing says this state is unsafe, and the realized-energy check did NOT
#: run — no pre-apply capture to difference this one against. A first-ever
#: round reaches this by construction, as does one whose crossover corner
#: moved out from under the applied profile, and every capture whose quiet bins
#: are too few to anchor. The STATUS stays
#: :attr:`~.contracts.SafetyStatus.SAFE` and the adoption row is unchanged;
#: what changes is what the receipt and the journal SAY.
SAFETY_NO_FINDING_UNMEASURED = "no_unsafe_finding_realized_energy_unmeasured"

#: The integrity check name a clipped stimulus segment fails. A copy of
#: :data:`jasper.audio_measurement.program_analysis.INTEGRITY_CHECK_CLIPPED_RUN`,
#: repeated rather than imported to keep this module free of that heavy import.
CLIPPED_RUN_CHECK = "clipped_run"


def evaluate_applied_safety(
    *,
    probe: Any | None,
    integrity: "CaptureIntegrity | None",
) -> Verdict[SafetyStatus]:
    """Is the applied state safe to leave on a household's speaker?

    The adoption table's hard stop, and the only axis that pulls a MEASURED
    graph off for something other than the absence of evidence. Three findings,
    each read from a shipped instrument and none re-derived here:

    * a boost realized above the probe's tolerance
      (:attr:`~jasper.active_speaker.delta_probe.DeltaProbeMap.boost_over_declared_bound`)
      — energy in a driver the graph did not declare;
    * an uncommanded level shift measured LOUDER than declared
      (:data:`~jasper.active_speaker.delta_probe.VERDICT_LEVEL_MISMATCH` with a
      positive residual past its own tolerance);
    * a clipped stimulus segment in the post-apply capture.

    The first is the anchored excess — measured post minus measured pre, with
    the apply's commanded change and declared level move removed — so a
    standing model error cancels and delivered energy does not. It cannot be
    measured without a pre-apply capture, and ``safety_anchored`` in
    ``evidence`` says whether that half ran. With no anchor the LEVEL rule
    still holds (``residual_offset_db`` is gated on quiet bins, not an anchor)
    and the CLIPPED check needs no probe; only the ``safety_only`` path drops
    the level rule, where ``residual_offset_db`` is ``None`` by construction.

    DIRECTION is the discriminator: quieter-than-declared costs output and
    reaches :class:`~.contracts.QualityStatus.MISSED`, louder-than-declared is
    energy nobody asked for and reaches
    :class:`~.contracts.SafetyStatus.UNSAFE`. A band-scoped level claim narrows
    only WHERE the level was measured, so a positive shift in a sliver is still
    unsafe.

    :data:`SAFETY_NO_FINDING` means no instrument that RAN reported a hazard,
    not that none exists; ``probe_graded`` lets a reader tell "safe because
    nothing was found" from "safe because nothing looked". ``seam_deferred``
    carries the probe's own deferral reason and changes no status.

    Duck-typed on both inputs, so a host that never ran a probe passes ``None``.
    """

    clipped = (
        () if integrity is None
        else tuple(name for name in integrity.failed if name == CLIPPED_RUN_CHECK)
    )
    verdict = str(getattr(probe, "verdict", "") or "") if probe is not None else ""
    residual = getattr(probe, "residual_offset_db", None) if probe is not None else None
    residual_tolerance = (
        getattr(probe, "residual_offset_tolerance_db", None)
        if probe is not None else None
    )
    boost_over_bound = bool(
        getattr(probe, "boost_over_declared_bound", False)
    ) if probe is not None else False
    evidence: dict[str, Any] = {
        # Which instruments actually looked, so "safe" can be read honestly.
        "probe_graded": bool(verdict),
        # ...and HOW MUCH of the probe looked: a ``safety_only`` map grades
        # the MODEL's departure and no more, so the shape half never ran.
        "probe_shape_graded": bool(verdict) and verdict != VERDICT_SAFETY_ONLY,
        "probe_verdict": verdict,
        "probe_reason": (
            str(getattr(probe, "reason", "") or "") if probe is not None else ""
        ),
        "integrity_graded": integrity is not None,
        "clipped_checks": list(clipped),
        "residual_offset_db": (
            float(residual) if isinstance(residual, (int, float))
            and not isinstance(residual, bool) else None
        ),
        "residual_offset_tolerance_db": (
            float(residual_tolerance)
            if isinstance(residual_tolerance, (int, float))
            and not isinstance(residual_tolerance, bool) else None
        ),
        # Did the hearing half run at all? ``False`` means the two directional
        # findings below are absences rather than passes. Read off the probe,
        # never inferred from those findings being False — they are also False
        # on a round that measured and found nothing.
        "safety_anchored": (
            bool(getattr(probe, "safety_anchored", False))
            if probe is not None else False
        ),
        "boost_over_declared_bound": boost_over_bound,
        "boost_overshoot_db": (
            getattr(probe, "boost_overshoot_db", None) if probe is not None else None
        ),
        # WHICH WAY the graded bins missed, and whether the probe's seam
        # therefore handed its rollback to this table. On the SAFETY axis
        # because the basis is a safety-direction fact. ``seam_deferred`` of
        # ``""`` means no deferral, which must be distinguishable from a
        # restore.
        "realized_louder_than_commanded": (
            bool(getattr(probe, "realized_louder_than_commanded", False))
            if probe is not None else False
        ),
        # The MODEL's own upward departure — a different instrument on a
        # different reference, belonging to the next round rather than this
        # decision. Here because this is the block the receipt renders.
        "model_departure_over_tolerance": (
            bool(getattr(probe, "model_departure_over_tolerance", False))
            if probe is not None else False
        ),
        "max_signed_error_db": (
            getattr(probe, "max_signed_error_db", None) if probe is not None else None
        ),
        "seam_deferred": seam_rollback_deferral(probe),
    }

    if boost_over_bound:
        return Verdict(
            SafetyStatus.UNSAFE, SAFETY_BOOST_OVER_DECLARED_BOUND, evidence
        )
    louder = (
        verdict == VERDICT_LEVEL_MISMATCH
        and evidence["residual_offset_db"] is not None
        and evidence["residual_offset_tolerance_db"] is not None
        and evidence["residual_offset_db"]
        > evidence["residual_offset_tolerance_db"]
    )
    if louder:
        return Verdict(
            SafetyStatus.UNSAFE, SAFETY_UNCOMMANDED_LEVEL_LOUDER, evidence
        )
    if clipped:
        return Verdict(SafetyStatus.UNSAFE, SAFETY_CLIPPED_CAPTURE, evidence)
    # Nothing found — and the reason says whether the instrument that finds it
    # was able to look. See :data:`SAFETY_NO_FINDING_UNMEASURED`.
    return Verdict(
        SafetyStatus.SAFE,
        SAFETY_NO_FINDING if evidence["safety_anchored"]
        else SAFETY_NO_FINDING_UNMEASURED,
        evidence,
    )


# --------------------------------------------------------------------------
# 7. quality — what the next round is for
# --------------------------------------------------------------------------

ADOPTION_MEASURED_REGRESSION = "measured_regression"
#: The delta probe measured the emitted filters not doing what the fit's model
#: of them says, in one of the classes the project reverts — realized-vs-
#: commanded, where :data:`ADOPTION_MEASURED_REGRESSION` is before/after.
#:
#: The cause carries the CLASS as ``<prefix>:<verdict>``, because the three
#: rollback classes have three different household sentences
#: (:data:`~.refusal_copy.DELTA_PROBE_REASON_BY_VERDICT`) that
#: :func:`~.refusal_copy.round_restore_reason` reads back off it.
ADOPTION_PROBE_ROLLBACK_CLASS = "delta_probe_rollback_class"
ADOPTION_REALIZED_AND_IMPROVED = "realized_and_improved"
ADOPTION_REALIZATION_FAILED = "realization_failed"
ADOPTION_UNPROVEN = "benefit_unproven"
#: Distinct from :data:`ADOPTION_UNPROVEN` because the benefit in that one
#: cell was *improved* — what is missing is the evidence that the graph we
#: applied is why. A receipt saying "benefit unproven" there would be false.
ADOPTION_REALIZATION_UNAVAILABLE = "realization_unavailable"

#: The nine ``(realization, benefit)`` cells and the cause each reports. All
#: nine exist so a combination cannot fall through to a default.
#: ``any | regressed`` is the only quality answer that restores, and all three
#: of those cells carry the regression as the cause rather than the realization
#: failure. ``unavailable | improved`` is MISSED rather than PASSED: the
#: speaker measured better, but with no realization evidence the round cannot
#: say the graph it applied is why.
_QUALITY_TABLE: Mapping[
    tuple[RealizationStatus, BenefitStatus], tuple[QualityStatus, str]
] = {
    (RealizationStatus.MATCHED, BenefitStatus.IMPROVED): (
        QualityStatus.PASSED, ADOPTION_REALIZED_AND_IMPROVED,
    ),
    (RealizationStatus.MATCHED, BenefitStatus.REGRESSED): (
        QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION,
    ),
    (RealizationStatus.MATCHED, BenefitStatus.INDETERMINATE): (
        QualityStatus.MISSED, ADOPTION_UNPROVEN,
    ),
    (RealizationStatus.UNAVAILABLE, BenefitStatus.IMPROVED): (
        QualityStatus.MISSED, ADOPTION_REALIZATION_UNAVAILABLE,
    ),
    (RealizationStatus.UNAVAILABLE, BenefitStatus.REGRESSED): (
        QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION,
    ),
    (RealizationStatus.UNAVAILABLE, BenefitStatus.INDETERMINATE): (
        QualityStatus.MISSED, ADOPTION_UNPROVEN,
    ),
    (RealizationStatus.FAILED, BenefitStatus.IMPROVED): (
        QualityStatus.MISSED, ADOPTION_REALIZATION_FAILED,
    ),
    (RealizationStatus.FAILED, BenefitStatus.REGRESSED): (
        QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION,
    ),
    (RealizationStatus.FAILED, BenefitStatus.INDETERMINATE): (
        QualityStatus.MISSED, ADOPTION_REALIZATION_FAILED,
    ),
}


def evaluate_round_quality(
    *,
    realization: Verdict[RealizationStatus],
    benefit: Verdict[BenefitStatus],
    spec: Verdict[SpecStatus],
    probe: Any | None,
    spec_report: FlatSpecReport | None,
) -> Verdict[QualityStatus]:
    """How good is the measured result, and what should the next round fix?

    The STATUS is :data:`_QUALITY_TABLE`, keyed on ``(realization, benefit)``.
    A probe verdict in
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`
    that the probe's own seam did not defer overrides it to
    :attr:`~.contracts.QualityStatus.REGRESSED`.

    The TARGETS are disclosure and move no status. Spec is an outcome, not a
    proxy for benefit — every row reads "any" for spec, and
    :func:`decide_adoption` never reads a :class:`~.contracts.SpecStatus` — so
    a round can be PASSED with targets outstanding.
    """

    quality, reason = _QUALITY_TABLE[(realization.status, benefit.status)]
    targets: list[str] = []
    if realization.status is not RealizationStatus.MATCHED:
        targets.append(f"realization:{realization.reason}")
    if benefit.status is not BenefitStatus.IMPROVED:
        targets.append(f"benefit:{benefit.reason}")
    if spec.status is not SpecStatus.PASSED:
        targets.append(f"spec:{spec.reason}")
    probe_verdict = str(getattr(probe, "verdict", "") or "") if probe is not None else ""
    if probe_verdict and probe_verdict != VERDICT_MATCHED:
        targets.append(
            f"delta_probe:{str(getattr(probe, 'reason', '') or probe_verdict)}"
        )
    targets.extend(_model_departure_target(probe))
    probe_rollback = _probe_rollback_class(probe, probe_verdict)
    if probe_rollback:
        quality = QualityStatus.REGRESSED
        reason = f"{ADOPTION_PROBE_ROLLBACK_CLASS}:{probe_rollback}"

    return Verdict(quality, reason, {
        "targets": targets,
        "spec_bands": _failing_spec_bands(spec_report),
        # WHICH probe class escalated, or ``""``. Named rather than
        # re-derived from ``targets``: the row's reason is a constant.
        "probe_rollback_class": probe_rollback,
    })


#: The next-round target the room's upward departure from the two-branch model
#: lands on. Shape: ``<prefix>:<amount>dB@<hz>``, so the target reads as an
#: instruction rather than a label.
QUALITY_MODEL_DEPARTURE = "model_departure"


def _model_departure_target(probe: Any | None) -> list[str]:
    """The model's upward departure as a next-round target, or nothing.

    The amount is the unanchored ``max_signed_error_db``; whether it cleared
    tolerance is the probe's own answer against its own per-bin tolerance
    curve, read rather than re-derived, because this module owns no tolerance.

    The frequency is ``max_signed_error_hz``, never ``worst_hz``: they are two
    reductions over two bin sets — worst ABSOLUTE error over the graded bins,
    worst POSITIVE departure over the safety bins — so quoting one bin's dB at
    the other's frequency sends the next round after the wrong feature.

    Nothing for an absent probe, an unmeasured departure, or one inside
    tolerance.
    """

    if probe is None:
        return []
    if not bool(getattr(probe, "model_departure_over_tolerance", False)):
        return []
    amount = _finite_or_none(getattr(probe, "max_signed_error_db", None))
    if amount is None:
        return []
    where = _finite_or_none(getattr(probe, "max_signed_error_hz", None))
    at = "" if where is None else f"@{where:.0f}Hz"
    return [f"{QUALITY_MODEL_DEPARTURE}:{amount:.2f}dB{at}"]


def _probe_rollback_class(probe: Any | None, verdict: str) -> str:
    """The probe verdict that takes this graph off, or ``""``.

    Two owners consulted, neither re-derived here:
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`
    for which classes restore, and
    :func:`~jasper.active_speaker.delta_probe.seam_rollback_deferral` for the
    ones that are spared.
    """

    if not verdict or verdict not in DELTA_PROBE_ROLLBACK_VERDICTS:
        return ""
    return "" if seam_rollback_deferral(probe) else verdict


def spec_band_rows(report: FlatSpecReport | None) -> list[dict[str, Any]]:
    """Every evaluable band's own verdict and its worst bin.

    Lifted from the report's own
    :class:`~jasper.active_speaker.flat_spec.BandResult` fields — nothing here
    re-grades a band or recomputes a deviation.

    Carries the GRADED edges beside the nominal ones because the two differ at
    the top: a session whose microphone is trusted to 20 kHz grades its top band
    to 20 kHz, while a row printing only ``f_hi_hz`` would say 16 kHz. A band
    that could not be graded is absent rather than present with ``None``
    metrics — an unevaluable band is not a target.
    """

    if report is None:
        return []
    bands: list[dict[str, Any]] = []
    for band in report.bands:
        if not band.evaluable:
            continue
        bands.append({
            "f_lo_hz": float(band.f_lo_hz),
            "f_hi_hz": float(band.f_hi_hz),
            "graded_lo_hz": (
                None if band.graded_lo_hz is None else float(band.graded_lo_hz)
            ),
            "graded_hi_hz": (
                None if band.graded_hi_hz is None else float(band.graded_hi_hz)
            ),
            "tolerance_db": float(band.tolerance_db),
            "passed": band.passed,
            "max_deviation_db": (
                None if band.max_deviation_db is None
                else float(band.max_deviation_db)
            ),
            "max_deviation_hz": (
                None if band.max_deviation_hz is None
                else float(band.max_deviation_hz)
            ),
        })
    return bands


def _failing_spec_bands(report: FlatSpecReport | None) -> list[dict[str, Any]]:
    """:func:`spec_band_rows` filtered to the bands that measured out of
    tolerance — the quality axis's evidence, which names only the misses."""

    return [row for row in spec_band_rows(report) if row["passed"] is False]


# --------------------------------------------------------------------------
# 7b. headroom — is a flatter result still reachable?
# --------------------------------------------------------------------------

HEADROOM_CAP_REACHED = "round_cap_reached"
HEADROOM_NO_OBJECTIVES = "objectives_unevaluable"
HEADROOM_WITHIN_PLATEAU = "objectives_within_plateau"
HEADROOM_PLATEAUED = "improvement_plateaued"
HEADROOM_REACHABLE = "flatter_result_reachable"


@dataclass(frozen=True)
class FlatnessObjectives:
    """The two graded flatness objectives, as one round measured them.

    Both are FRAME-INVARIANT, which is what lets them be differenced across
    rounds: ``ripple_db`` is each band's deviation from its OWN level and
    ``tilt_db`` a difference of two levels, so the spec's across-band reference
    cancels out of both. ``None`` means "this round could not grade it", never
    zero.

    Neither is invariant to which BINS were graded, and the session's trusted
    floor sets each band's ``graded_lo_hz``: on an UNCHANGED curve a 7↔10 ms
    gate change alone produces ±0.518 dB of spurious movement here, 2.1×
    :data:`~.round_evidence.ITERATION_PLATEAU_DB`. So the floor is banked
    beside these numbers and a round whose floor differs from the previous
    round's refuses the movement comparison.
    """

    #: Largest level step between two graded bands.
    tilt_db: float | None
    #: Worst within-band deviation from that band's own level.
    ripple_db: float | None

    @property
    def worst_db(self) -> float | None:
        """The larger of the two, or ``None`` when neither graded.

        A MAX rather than a sum or an RMS: the series is done only when BOTH
        are small, and pooling would let a large tilt hide behind flat bands.
        """

        graded = [
            abs(value)
            for value in (self.tilt_db, self.ripple_db)
            if value is not None
        ]
        return max(graded) if graded else None

    def to_dict(self) -> dict[str, Any]:
        return {"tilt_db": self.tilt_db, "ripple_db": self.ripple_db}


def flatness_objectives(report: FlatSpecReport | None) -> FlatnessObjectives:
    """Reduce a post-apply spec report to the two flatness objectives.

    **Derived from the report, never recomputed from the curve**: band
    membership, the exclusion mask's effect, and each band's own level are
    answered exactly once, by ``evaluate_flat_spec``. The tilt half IS
    :func:`~jasper.active_speaker.flat_spec.spec_band_tilt`, reused rather than
    re-derived; the ripple half is a ``max`` over the same report's bands.
    """

    if report is None:
        return FlatnessObjectives(tilt_db=None, ripple_db=None)
    tilt = spec_band_tilt(report)
    ripples = [
        abs(band.max_ripple_db)
        for band in report.bands
        if band.evaluable and band.max_ripple_db is not None
    ]
    return FlatnessObjectives(
        tilt_db=tilt.step_db if tilt.evaluable else None,
        ripple_db=max(ripples) if ripples else None,
    )


#: How closely two rounds' trusted floors must agree to be one graded frame.
#:
#: Dimensionless relative tolerance, wide on purpose: it exists to admit the
#: same float after a JSON round trip, not to tolerate a real change. The
#: mechanism it screens for moves the floor by tens of percent, so a genuine
#: gate change lands orders of magnitude outside it and refuses.
FLOOR_COMPARABILITY_RTOL = 1e-3


def _floors_comparable(
    this_floor_hz: float | None, previous_floor_hz: float | None
) -> bool:
    """May two rounds' objectives be differenced?

    ``True`` unless there is POSITIVE evidence of a floor change: an unknown or
    non-finite floor is not evidence that the frame moved, and refusing on it
    would disable the plateau stop until every path threads a floor. Two KNOWN
    floors disagreeing by more than :data:`FLOOR_COMPARABILITY_RTOL` is the one
    case that refuses.
    """

    if this_floor_hz is None or previous_floor_hz is None:
        return True
    if not (math.isfinite(this_floor_hz) and math.isfinite(previous_floor_hz)):
        return True
    return math.isclose(
        float(this_floor_hz),
        float(previous_floor_hz),
        rel_tol=FLOOR_COMPARABILITY_RTOL,
        abs_tol=0.0,
    )


def evaluate_iteration_headroom(
    *,
    objectives: FlatnessObjectives,
    previous: FlatnessObjectives | None,
    round_ordinal: int,
    round_cap: int,
    plateau_db: float,
    trusted_floor_hz: float | None = None,
    previous_trusted_floor_hz: float | None = None,
) -> Verdict[IterationHeadroom]:
    """Should the series run another round?

    Quality grades what this round DID; this grades what a next one could still
    get. Three ways a series is over, checked most-binding first so the reason
    names the fact that actually ended it:

    1. The round cap. At ``round_ordinal >= round_cap`` there is no next round
       to have headroom for; naming a plateau here would imply more rounds
       would not have helped, which the measurement did not say.
    2. Already flat enough — both objectives inside ``plateau_db``.
    3. Plateaued: the objectives moved less than ``plateau_db`` since the
       previous round. Measured on the OBJECTIVES rather than the pooled
       residual, because a round only reaches
       :attr:`~.contracts.QualityStatus.PASSED` by improving past
       :data:`~.round_evidence.MEASURED_BENEFIT_MARGIN_DB`, a wider bar.

    Ungradable objectives are NOT a fourth stop: they resolve to
    :attr:`~.contracts.IterationHeadroom.REACHABLE` under
    :data:`HEADROOM_NO_OBJECTIVES`. ``previous is None`` is the first round,
    where the plateau stop cannot fire and the answer rests on distance alone —
    which is why the cap is checked independently rather than inferred from
    absent history.

    Args:
      objectives: this round's :func:`flatness_objectives`.
      previous: the previous round's, off the durable receipt, or ``None``.
      round_ordinal: 1-based position of this round in the series.
      round_cap / plateau_db: the series policy, passed rather than imported.
        ``plateau_db`` must be positive; both are defined in
        :mod:`.round_evidence` beside the benefit margin.
      trusted_floor_hz: the floor ``objectives`` were graded against, and
        ``previous_trusted_floor_hz`` the previous round's. Banked in the
        evidence whether or not they decide anything here, because the NEXT
        round reads them back. See :func:`_floors_comparable`.
    """

    cap = int(round_cap)
    ordinal = int(round_ordinal)
    plateau = _positive_db(plateau_db, field_name="plateau_db")
    worst = objectives.worst_db
    previous_worst = None if previous is None else previous.worst_db
    movement_db = (
        None if worst is None or previous_worst is None
        else previous_worst - worst
    )
    movement_comparable = _floors_comparable(
        trusted_floor_hz, previous_trusted_floor_hz
    )
    evidence: dict[str, Any] = {
        "round_ordinal": ordinal,
        "round_cap": cap,
        "plateau_db": plateau,
        "objectives": objectives.to_dict(),
        "previous_objectives": None if previous is None else previous.to_dict(),
        "worst_db": worst,
        "previous_worst_db": previous_worst,
        # Signed, and positive means "got flatter" — the same lower-is-better
        # convention ``improvement_db`` uses on the benefit verdict.
        "movement_db": movement_db,
        # The frame the two objectives above were graded in, banked BESIDE them
        # so the next round can check the frame rather than assume it.
        "trusted_floor_hz": _finite_or_none(trusted_floor_hz),
        "previous_trusted_floor_hz": _finite_or_none(previous_trusted_floor_hz),
        "movement_comparable": movement_comparable,
    }

    if ordinal >= cap:
        return Verdict(IterationHeadroom.EXHAUSTED, HEADROOM_CAP_REACHED, evidence)
    if worst is None:
        # REACHABLE, not EXHAUSTED: missing evidence is not a plateau, and the
        # reason still names which ending this was.
        return Verdict(
            IterationHeadroom.REACHABLE, HEADROOM_NO_OBJECTIVES, evidence
        )
    if worst <= plateau:
        return Verdict(
            IterationHeadroom.EXHAUSTED, HEADROOM_WITHIN_PLATEAU, evidence
        )
    if movement_comparable and movement_db is not None and movement_db < plateau:
        return Verdict(IterationHeadroom.EXHAUSTED, HEADROOM_PLATEAUED, evidence)
    return Verdict(IterationHeadroom.REACHABLE, HEADROOM_REACHABLE, evidence)


def _finite_or_none(value: float | None) -> float | None:
    """A finite number, or ``None`` — the same unknown-vs-zero rule as elsewhere.

    Banking a NaN would give the next round a number that compares false
    against everything: a silent permanent refusal rather than an absence.
    """

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------
# 8. adoption — the four axes, as a table
# --------------------------------------------------------------------------

#: Read only on :data:`~.contracts.ADOPTION_ROW_RESTORE_UNTRUSTED`. With trusted
#: evidence a boost is judged realized-vs-declared on the safety axis; with
#: untrusted evidence there is nothing to judge it by, and a boost whose benefit
#: cannot be shown is energy put into a driver that cannot be justified, so it
#: comes off.
ADOPTION_UNPROVEN_BOOST = "unproven_boost_failed_closed"
ADOPTION_RESTORE_FAILED = "restore_failed"
ADOPTION_NO_ROLLBACK_ANCHOR = "restore_required_without_rollback_anchor"

#: Which row a TRUSTED, SAFE round lands on, by its quality answer.
#:
#: A MAPPING rather than a guard ladder: a lookup with no default means a fourth
#: :class:`~.contracts.QualityStatus` member raises ``KeyError`` at its first
#: call instead of falling through to whichever branch happened to come last.
_QUALITY_ROWS: Mapping[QualityStatus, tuple[AdoptionOutcome, str]] = {
    QualityStatus.PASSED: (AdoptionOutcome.KEEP, ADOPTION_ROW_KEEP),
    QualityStatus.MISSED: (
        AdoptionOutcome.KEEP_FOR_ITERATION, ADOPTION_ROW_KEEP_FOR_ITERATION,
    ),
    QualityStatus.REGRESSED: (
        AdoptionOutcome.RESTORE, ADOPTION_ROW_RESTORE_REGRESSION,
    ),
}

#: Where the table's one PASSED cell splits, by the fourth axis: whether that
#: keep is TERMINAL.
#:
#: Only the passing cell consults the headroom STATUS: a MISSED round keeps
#: iterating however flat the axis says the result is, and a REGRESSED one
#: restores before this table is reached. The one fact that crosses to the
#: missing cell is the spent BUDGET, as the axis's reason rather than its
#: status — see :func:`decide_adoption`.
_PASSED_ROWS: Mapping[IterationHeadroom, tuple[AdoptionOutcome, str]] = {
    IterationHeadroom.EXHAUSTED: (AdoptionOutcome.KEEP, ADOPTION_ROW_KEEP),
    IterationHeadroom.REACHABLE: (
        AdoptionOutcome.KEEP_FOR_ITERATION, ADOPTION_ROW_KEEP_ITERATING,
    ),
}


def decide_adoption(
    *,
    trust: Verdict[EvidenceTrust],
    safety: Verdict[SafetyStatus],
    quality: Verdict[QualityStatus],
    headroom: Verdict[IterationHeadroom],
    boosted: bool,
    rollback_available: bool,
    restore_failed: bool = False,
) -> AdoptionDecision:
    """Keep, keep-and-iterate, restore, or escalate — the adoption table.

    Headroom can NEVER keep a graph the other axes said to take off: it is read
    only on the branch trust, safety and quality all passed, where it splits
    the passing cell (:data:`_PASSED_ROWS`). One further fact crosses to the
    MISSED cell — the spent round budget — keyed on the axis's REASON, not its
    status, so the plateau stops do not cross and a MISSED round still iterates
    below the cap. :data:`HEADROOM_CAP_REACHED` is minted in exactly one place.

    The seven rows, by their :data:`~.contracts.ADOPTION_ROWS` identifiers:

    ========================================== ============================
    row                                        outcome
    ========================================== ============================
    ``row1_trusted_safe_passed``               ``KEEP``
    ``row2_trusted_safe_missed``               ``KEEP_FOR_ITERATION``
    ``row3_unsafe``                            ``RESTORE``
    ``row4_untrusted_evidence``                ``RESTORE``
    ``row5_trusted_safe_regressed``            ``RESTORE``
    ``row6_trusted_safe_passed_reachable``     ``KEEP_FOR_ITERATION``
    ``row7_trusted_safe_missed_exhausted``     ``KEEP``
    ========================================== ============================

    Args:
      trust / safety / quality / headroom: the four axis verdicts. VERDICTS,
        not bare statuses: the reason a row fires under IS the deciding axis's
        own reason. This function decides nothing an evaluator did not; it
        selects which axis speaks, and on both passing rows that axis is
        headroom. A verdict of the wrong type raises
        :class:`~.contracts.CrossoverV2ContractError`.
      boosted: does the applied intervention contain a boost? Computed by the
        host with ``camilla_yaml.linearization_has_boost``. Read ONLY on the
        untrusted row — see :data:`ADOPTION_UNPROVEN_BOOST`.
      rollback_available: can the host actually restore the entry graph? A host
        that binds no rollback must get a different answer, not a restore
        instruction nothing can carry out.
      restore_failed: a restore was attempted and did not complete.

    Ordering, stated because this decides whether a graph stays on a speaker:

    * A failed restore outranks everything, checked first: the speaker is then
      in neither the entry graph nor the intended one.
    * Safety is checked BEFORE trust. Both rows restore, so the order only
      decides which name the receipt carries, and naming the hazard beats
      naming the absence when both are true (a clipped capture is both).
    * A measured regression still restores — going back is going back to a
      measured tune — and an unmeasured applied state never stays either.
    * A spec band out of tolerance is not a restore trigger; see
      :func:`evaluate_round_quality`.
    * A restore we cannot perform is not a restore: with no rollback anchor the
      answer is ``recovery_required``, never a ``restore`` the host cannot
      execute and never a ``keep``.
    * The fourth axis chooses the sentence, never the graph: ``KEEP`` and
      ``KEEP_FOR_ITERATION`` leave the speaker in the same state.
    """

    for name, value, kind in (
        ("trust", trust, EvidenceTrust),
        ("safety", safety, SafetyStatus),
        ("quality", quality, QualityStatus),
        ("headroom", headroom, IterationHeadroom),
    ):
        if not isinstance(value, Verdict) or not isinstance(value.status, kind):
            raise CrossoverV2ContractError(
                f"{name} must be a Verdict carrying a {kind.__name__}"
            )

    if restore_failed:
        return AdoptionDecision(
            outcome=AdoptionOutcome.RECOVERY_REQUIRED,
            reason=ADOPTION_RESTORE_FAILED,
            row=ADOPTION_ROW_RESTORE_FAILED,
        )
    if safety.status is SafetyStatus.UNSAFE:
        return _restore_or_recover(
            safety.reason,
            row=ADOPTION_ROW_RESTORE_UNSAFE,
            rollback_available=rollback_available,
        )
    if trust.status is EvidenceTrust.UNTRUSTED:
        return _restore_or_recover(
            ADOPTION_UNPROVEN_BOOST if boosted else trust.reason,
            row=ADOPTION_ROW_RESTORE_UNTRUSTED,
            rollback_available=rollback_available,
        )
    outcome, row = _QUALITY_ROWS[quality.status]
    if outcome is AdoptionOutcome.RESTORE:
        return _restore_or_recover(
            quality.reason, row=row, rollback_available=rollback_available,
        )
    if outcome is AdoptionOutcome.KEEP:
        # Keyed off the OUTCOME the table above resolved to rather than off
        # ``quality.status`` directly, so the passing cell has exactly one
        # definition and this branch cannot drift from it.
        outcome, row = _PASSED_ROWS[headroom.status]
        return AdoptionDecision(outcome=outcome, reason=headroom.reason, row=row)
    if headroom.reason == HEADROOM_CAP_REACHED:
        # The budget ends a MISSED series too. Reached only from the iterating
        # cell; the passing one returned above through
        # ``_PASSED_ROWS[EXHAUSTED]``, which is the same ending by the same
        # reason.
        return AdoptionDecision(
            outcome=AdoptionOutcome.KEEP,
            reason=headroom.reason,
            row=ADOPTION_ROW_KEEP_MISSED_EXHAUSTED,
        )
    return AdoptionDecision(outcome=outcome, reason=quality.reason, row=row)


def _restore_or_recover(
    reason: str, *, row: str, rollback_available: bool
) -> AdoptionDecision:
    """A restore the host can run, or the escalation when it cannot.

    The branch is taken before the attempt: a decision to restore with no
    anchor to restore TO is not one the host can carry out. The ROW is the same
    either way — the rule fired, and only its execution was impossible.
    """

    if rollback_available:
        return AdoptionDecision(
            outcome=AdoptionOutcome.RESTORE, reason=reason, row=row
        )
    return AdoptionDecision(
        outcome=AdoptionOutcome.RECOVERY_REQUIRED,
        reason=f"{ADOPTION_NO_ROLLBACK_ANCHOR}:{reason}",
        row=row,
    )


# --------------------------------------------------------------------------
# 9. the round's household-facing outcome
# --------------------------------------------------------------------------

#: What the household is told a graded round came to. The domain owns these
#: four even though the web host picks one: the renderer
#: (:mod:`jasper.active_speaker.crossover_envelope_v2`) may not import
#: :mod:`jasper.web`, so both sides import the symbol from here.
#:
#: NOT :class:`~.contracts.AdoptionOutcome`, which decides what happens to the
#: GRAPH for one round; these name what a whole commission's post-apply grade
#: came to for a PERSON, and a session can grade ``RESULT_KEEP_PREVIOUS`` off
#: inputs no single round's adoption decision saw.
RESULT_VERIFIED_TARGET = "verified_target"
RESULT_VERIFIED_BEST_EVALUATED = "verified_best_evaluated"
RESULT_KEEP_PREVIOUS = "keep_previous"
#: Shares its value with the host's ``GRADE_INCONCLUSIVE``, which answers the
#: neighbouring question ("did the check finish?") about the same round. A bare
#: ``"inconclusive"`` in the renderer therefore cannot be attributed to one of
#: the two by its value alone.
RESULT_INCONCLUSIVE = "inconclusive"


# --------------------------------------------------------------------------- #
# region, null-registry and flatness diagnostics
# --------------------------------------------------------------------------- #

def _band_edge(band: Any, index: int) -> float | None:
    """One edge of a persisted ``[lo, hi]`` band pair, or ``None``.

    For log lines that carry a band as two scalars (the shape
    ``_log_verify_diag``'s ``tracking_band_lo_hz``/``_hi_hz`` established)
    rather than one bracketed value logfmt would have to quote.
    """
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    edge = band[index]
    return float(edge) if isinstance(edge, (int, float)) else None


def _flatness_tilt_log_field(flatness: Any) -> str:
    """The band-to-band level step as one logfmt token — a frame-free reading.

    ``flatness_max_db`` and ``flatness_bands`` are distances from a reference
    pooled ACROSS bands, so a uniformly-off band drags that zero. A step
    BETWEEN two band levels cannot be moved by the frame.

    Shape: ``<step>dB:<lo>-<hi>Hz><lo>-<hi>Hz``, higher-sitting band first, no
    space or bracket for logfmt to quote. ``""`` when the gauge carried no
    tilt. Copied from
    :func:`~jasper.active_speaker.flat_spec.spec_band_tilt`; nothing is
    recomputed and no verdict moves.
    """
    if not isinstance(flatness, Mapping):
        return ""
    tilt = flatness.get("tilt")
    if not isinstance(tilt, Mapping) or tilt.get("evaluable") is not True:
        return ""
    step_db = tilt.get("step_db")
    high, low = tilt.get("high_band_hz"), tilt.get("low_band_hz")
    if (
        not isinstance(step_db, (int, float)) or isinstance(step_db, bool)
        or not isinstance(high, (list, tuple)) or len(high) != 2
        or not isinstance(low, (list, tuple)) or len(low) != 2
    ):
        return ""
    edges = [_band_edge(high, 0), _band_edge(high, 1), _band_edge(low, 0), _band_edge(low, 1)]
    if any(edge is None for edge in edges):
        return ""
    high_lo, high_hi, low_lo, low_hi = edges
    return (
        f"{step_db:.2f}dB:{high_lo:.0f}-{high_hi:.0f}Hz>{low_lo:.0f}-{low_hi:.0f}Hz"
    )


def _per_band_flatness_log_field(bands: Any) -> str:
    """One compact token per graded spec band, each band's own worst deviation
    from the SAME reference ``flatness_max_db`` is stated against.

    A uniformly-off band drags the shared reference toward itself and can make
    an unrelated band's ordinary ripple read as the LARGER deviation, so a log
    reader must not be limited to the single band the gauge flagged as worst.

    Shape: ``lo-hiHz:+dev.ddB:pass|fail``, semicolon-joined, no bracket or
    space for logfmt to quote. ``""`` when ``bands`` is absent or no band
    survives. Disclosure only: every figure is copied from the same
    :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`, nothing is
    recomputed, and no verdict moves. Same skip rule as
    ``crossover_envelope_v2._per_band_flatness_lines``.
    """
    if not isinstance(bands, list):
        return ""
    parts: list[str] = []
    for band in bands:
        if not isinstance(band, Mapping) or not band.get("evaluable"):
            continue
        lo, hi = band.get("f_lo_hz"), band.get("f_hi_hz")
        deviation_db, passed = band.get("max_deviation_db"), band.get("passed")
        if (
            not isinstance(lo, (int, float)) or not isinstance(hi, (int, float))
            or not isinstance(deviation_db, (int, float))
            or isinstance(deviation_db, bool) or not isinstance(passed, bool)
        ):
            continue
        parts.append(
            f"{lo:.0f}-{hi:.0f}Hz:{deviation_db:+.2f}dB:{'pass' if passed else 'fail'}"
        )
    return ";".join(parts)


# --------------------------------------------------------------------------- #
# the VERIFY record: thresholds, evidence, claims and frame
# --------------------------------------------------------------------------- #

def verify_absolute_tolerance_db(band_hz: Sequence[float]) -> float | None:
    """How far the realized sum may sit from the candidate's own crossover
    target across ``band_hz``, in dB — or ``None``, no tolerance to apply.

    DERIVED, never chosen: returns the LOOSEST ``flat_spec.SPEC_BANDS`` entry
    the region overlaps, so a crossover-region result is never held to a
    tighter bar than the speaker's own spec applies somewhere inside it. For
    the shipped 2 kHz two-way, ``max(1.5 [250–2k], 2.0 [2k–8k]) = 2.0 dB``.

    NOT :data:`VERIFY_TOLERANCE_DB`, which bounds measured-vs-MODEL; this
    bounds measured-vs-DESIGN. Same units, different question.

    Known contributor, not corrected for: a frame tilt between VERIFY's in-room
    curve and its on-axis model lands partly in this residual. DISCLOSED beside
    the number (``_verify_frame_lines``) rather than removed — a measured tilt
    is evidence, not permission to re-grade.

    ``None`` when the region overlaps no specced band, so the caller records
    the claim not-evaluated rather than inventing a bar.

    Reads the NOMINAL table, so this gate does not move with a session's
    microphone-trust ceiling: on a session trusted past 16 kHz the spec grades
    a region this claim still declines.
    """
    if len(band_hz) != 2:
        return None
    lo, hi = float(band_hz[0]), float(band_hz[1])
    overlapping = [tol for f_lo, f_hi, tol in SPEC_BANDS if f_lo < hi and lo < f_hi]
    return max(overlapping) if overlapping else None


def _verify_evidence_from_tracking(
    tracking: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The verify_fail expert-disclosure numbers: the notch-excluded max the
    tolerance gates on, the RMS, and the tolerance itself.

    ``None`` when the gated max is not a real number. The graded band is NOT
    here — it belongs to :func:`_verify_graded_band_from_tracking`, because
    this block is persisted only for a NON-pass outcome and the band is a
    property of the comparison rather than of its failure.
    """
    max_db = tracking.get("max_db_notch_excluded")
    if not isinstance(max_db, (int, float)):
        return None
    rms_db = tracking.get("rms_db")
    return {
        "max_db": float(max_db),
        "rms_db": float(rms_db) if isinstance(rms_db, (int, float)) else None,
        "tolerance_db": float(VERIFY_TOLERANCE_DB),
    }


def _verify_graded_band_from_tracking(
    tracking: Mapping[str, Any],
) -> list[float] | None:
    """The frequency span VERIFY's tracking comparison actually graded.

    ``[lo, hi]``, or ``None`` when this capture never reached a tracking
    comparison — absent means "nothing was graded", never "graded everywhere".

    Disclosed on a PASS too, because the band is not the nominal Fc±1 octave:
    ``overlap_band_hz`` clamps its lower edge UP to the tweeter's actual
    MEASURE sweep floor, and ``_analyze_verify`` clamps it up again to the
    capture's own gate-derived validity floor. A "Verified." badge over an
    unstated band reads as "verified everywhere".
    """
    band = tracking.get("tracking_band_hz")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    lo, hi = band
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return None
    return [float(lo), float(hi)]


#: Why the two per-branch claims are never graded today: a VERIFY program
#: plays ONE mono summed sweep (``build_verify_program``'s
#: ``KIND_SUMMED_SWEEP``), so the capture holds no woofer-alone or HF-alone
#: response to compare with its candidate branch.
CLAIM_NO_PER_BRANCH_CAPTURE = "no_per_branch_verify_capture"
#: A crossover-region band exists but ``flat_spec.SPEC_BANDS`` sets no
#: tolerance across it — see :func:`verify_absolute_tolerance_db`.
ABSOLUTE_NO_SPEC_TOLERANCE = "no_spec_tolerance_for_region"


def _verify_claims(
    tracking: Mapping[str, Any], absolute: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The plan §7 claim record for one VERIFY capture — ONE producer.

    Four entries for three claims: the third is two questions in one sentence,
    so ``integration`` is the tracking half and ``absolute`` the other. Every
    number is LIFTED from the record its owner already computed, so a screen
    and a gate cannot quote different figures. The kernel supplies the absolute
    band and scalars; the tolerance and both verdicts are this module's.
    """
    tracking_max = tracking.get("max_db_notch_excluded")
    band = (absolute or {}).get("band_hz")
    absolute_max = (absolute or {}).get("max_db")
    tolerance_db = (
        verify_absolute_tolerance_db(band) if isinstance(band, (list, tuple)) else None
    )
    absolute_claim: dict[str, Any]
    if not isinstance(absolute_max, (int, float)):
        # The kernel's own reason survives, never re-labelled: "no trusted
        # crossover region" and "no candidate target" stay distinguishable.
        absolute_claim = {
            "status": CLAIM_NOT_EVALUATED,
            "reason": str((absolute or {}).get("not_evaluated") or CLAIM_NOT_EVALUATED),
        }
    elif tolerance_db is None or not isinstance(band, (list, tuple)):
        absolute_claim = {
            "status": CLAIM_NOT_EVALUATED, "reason": ABSOLUTE_NO_SPEC_TOLERANCE,
        }
    else:
        absolute_claim = {
            "status": CLAIM_PASS if absolute_max <= tolerance_db else CLAIM_FAIL,
            "tolerance_db": float(tolerance_db),
            "band_hz": [float(band[0]), float(band[1])],
            **{k: _rounded((absolute or {}).get(k), 4)
               for k in ("max_db", "rms_db", "worst_db", "worst_hz")},
        }
    branch = {"status": CLAIM_NOT_EVALUATED, "reason": CLAIM_NO_PER_BRANCH_CAPTURE}
    return {
        "woofer_branch": dict(branch),
        "hf_branch": dict(branch),
        "integration": {
            "status": (
                CLAIM_NOT_EVALUATED if not isinstance(tracking_max, (int, float))
                else CLAIM_PASS if tracking_max <= VERIFY_TOLERANCE_DB
                else CLAIM_FAIL
            ),
            "max_db": _rounded(tracking_max, 4),
            "tolerance_db": float(VERIFY_TOLERANCE_DB),
            "band_hz": _verify_graded_band_from_tracking(tracking),
        },
        "absolute": absolute_claim,
    }


def _verify_frame_from_tracking(
    tracking: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The FRAME VERIFY's comparison spanned, and the residual both ways.

    VERIFY differences an ON-AXIS two-branch model against an IN-ROOM gated
    measurement — two instruments, and on the 2026-07-29 corpus a single
    −0.79 dB/octave tilt between them was 84 % of the reported prediction
    error. ``program_analysis._analyze_verify`` fits that frame; nothing is
    recomputed here.

    Rendered on EVERY outcome: a PASS is exactly the case where an unstated
    tilt lets a reader take instrument agreement for model agreement.

    ``None`` when no tracking comparison ran, or when the frame could not be
    fitted. The tilt-removed keys are omitted individually rather than
    defaulted to their raw twins, because a beside-number equal to its twin
    would read as "removing the frame changed nothing".

    ``max_db_tilt_removed`` is the twin of the NOTCH-EXCLUDED max, matching
    what :func:`_verify_evidence_from_tracking` calls ``max_db``: a second
    spelling would invite comparing two numbers over different bin sets.

    ``pivot_hz``/``n_bins``/``band_hz`` travel too, because a two-parameter fit
    over few bins or a narrow span is ill-conditioned and
    :mod:`jasper.audio_measurement.frame_fit` reports that span rather than
    inventing a confidence policy. They also disclose WHICH bins the frame was
    estimated from — the notch-excluded, validity-floor-clamped set.
    """
    frame = tracking.get("frame")
    if not isinstance(frame, Mapping):
        return None
    offset_db = frame.get("offset_db")
    tilt = frame.get("tilt_db_per_octave")
    if not isinstance(offset_db, (int, float)) or not isinstance(tilt, (int, float)):
        return None
    out: dict[str, Any] = {
        "offset_db": float(offset_db),
        "tilt_db_per_octave": float(tilt),
    }
    pivot_hz = frame.get("pivot_hz")
    if isinstance(pivot_hz, (int, float)):
        out["pivot_hz"] = float(pivot_hz)
    n_bins = frame.get("n_bins")
    if isinstance(n_bins, int):
        out["n_bins"] = n_bins
    band_hz = frame.get("band_hz")
    if (
        isinstance(band_hz, (list, tuple))
        and len(band_hz) == 2
        and all(isinstance(edge, (int, float)) for edge in band_hz)
    ):
        out["band_hz"] = [float(band_hz[0]), float(band_hz[1])]
    tilt_removed = frame.get("tilt_removed")
    if isinstance(tilt_removed, Mapping):
        for key, source in (
            ("rms_db_tilt_removed", "rms_db"),
            ("max_db_tilt_removed", "max_db"),
        ):
            value = tilt_removed.get(source)
            if isinstance(value, (int, float)):
                out[key] = float(value)
    # The RAW pair the tilt-removed numbers sit beside. Carried here because
    # the durable ``verify.evidence`` block is persisted only on a NON-pass
    # outcome, so a passing screen would otherwise render the frame-removed
    # half of a comparison with nothing to compare it to.
    raw = frame.get("raw")
    if isinstance(raw, Mapping):
        for key, source in (("rms_db_raw", "rms_db"), ("max_db_raw", "max_db")):
            value = raw.get(source)
            if isinstance(value, (int, float)):
                out[key] = float(value)
    return out


# The contract-derived echo/null analysis band's LOWER edge must not drift
# below this floor. Measured on the JTS3 cdhorn corpus as how far the
# detector's signal-presence screen clears the band-below-passband condition:
# (5000, 19000) clears by 40.43-41.98 dB, (4000, 20000) by 35.46-35.58 dB,
# (3000, 19000) by only 1.53 dB, and (2000, 19000) not at all — a false
# negative at 18.21-18.23 dB, because
# that speaker's 2 kHz crossover puts the woofer's own passband inside the
# analysed band. 4000 Hz is the lowest edge with comfortable headroom.
#
# A declared contract whose derived echo band dips below this floor is CLAMPED
# up to it, and the clamp is disclosed (event + payload). Kept apart here: the
# driver's declared measurement WINDOW (owned by measurement_band_hz) and the
# echo/null ANALYSIS band (a detector-calibration concern, owned by this
# floor).
#
# Clamping costs no cross-session comparability: the detector's quefrency step
# is 1e6 / BANDWIDTH, so the clamped JTS3 band (4000, 18000) resolves at
# 71.4 us — identical to the module default (5000, 19000), also 14 kHz wide.
#
# See _derive_cloud_echo_band_hz.
ECHO_BAND_HF_REGIME_FLOOR_HZ = 4000.0


def _null_registry_to_dict(report: Any) -> dict[str, Any]:
    """``InterferenceNullReport`` -> a plain JSON dict, mirroring
    ``FlatSpecReport.to_dict``'s shape so the two persisted reports read
    consistently."""
    return {
        "nulls": [
            {
                "f_lo_hz": n.f_lo_hz, "f_hi_hz": n.f_hi_hz,
                "f_center_hz": n.f_center_hz, "n": n.n, "tau_us": n.tau_us,
                "r_time": n.r_time, "r_freq": n.r_freq,
                "agreement": n.agreement, "depth_db": n.depth_db,
                "classification": n.classification,
                "evidence": dict(n.evidence),
            }
            for n in report.nulls
        ],
        "excluded_bands_hz": [list(b) for b in report.excluded_bands_hz],
        "excluded_fraction": float(report.excluded_fraction),
        "refusals": [
            {
                "f_center_hz": r.f_center_hz, "depth_db": r.depth_db,
                "reason": r.reason, "evidence": dict(r.evidence),
            }
            for r in report.refusals
        ],
        "reason": report.reason,
        "classification": report.classification,
        "band_hz": list(report.band_hz),
        "tau_ladder_us": float(report.tau_ladder_us),
        "arrival_tau_us": float(report.arrival_tau_us),
        "arrival_r_time": float(report.arrival_r_time),
        "arrival_r_max": float(report.arrival_r_max),
        "n_corroborating": int(report.n_corroborating),
        "r_freq": float(report.r_freq),
        "agreement": float(report.agreement),
        "ladder_arrival_gap": float(report.ladder_arrival_gap),
        "capped": bool(report.capped),
        "min_depth_db": float(report.min_depth_db),
        "n_candidates": int(report.n_candidates),
    }
def _crossover_region_null_registry(
    combined: Any,
    *,
    echo_band_hz: tuple[float, float],
    crossover_region_hz: tuple[float, float] | None,
    identify: Any,
) -> dict[str, Any] | None:
    """Ask the null registry about the CROSSOVER REGION — and never let the
    answer gate anything.

    The gating band's lower edge is floored at
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ` (4 kHz), so on a 2 kHz crossover the
    one region that dominates the residual is structurally unreachable by the
    one instrument built to explain it. This extends the band down to the
    region and publishes what it finds.

    **The floor itself does not move.** ``echo_band_hz`` is unchanged, the
    gating registry still runs on it, and this function's output is unioned into
    NOTHING — not ``merged_mask``, not ``spec_mask``, not the trusted floor, not
    a verdict. Classification that can never reach a decision cannot be
    corrupted by a mis-calibrated screen; the worst case is a finding a reader
    discounts. ``gating.SEARCH_T_MIN_MS`` makes the same trade for the same
    reason: a false detection that gates is catastrophic, one that only
    classifies is noise.

    The finding is published WITH the band that produced it, so a reader gets
    "a null inside the committed handoff" rather than an unattributed anomaly.

    Returns ``None`` — never an empty dict — when there is no committed
    crossover to name a region with, when the gating band already reaches the
    region (nothing was hidden, so there is nothing to disclose), or when the
    extension would be degenerate.
    """
    if crossover_region_hz is None:
        return None
    region_lo_hz = float(crossover_region_hz[0])
    gating_lo_hz, gating_hi_hz = float(echo_band_hz[0]), float(echo_band_hz[1])
    if region_lo_hz >= gating_lo_hz:
        return None
    if region_lo_hz <= 0.0 or region_lo_hz >= gating_hi_hz:
        return None

    # The SAME upper edge as the gating band, lowered to reach the region:
    # extending rather than carving a narrow window keeps the detector's
    # quefrency step (1e6 / bandwidth) comparable.
    band_hz = (region_lo_hz, gating_hi_hz)
    try:
        report = identify(combined, band_hz=band_hz)
    except Exception:  # noqa: BLE001 - a classify-only surface may never
        # break a session; an extension that cannot be computed is absent.
        log_event(
            logger, "correction.crossover_v2_crossover_region_registry_failed",
            level=logging.WARNING, band_hz=list(band_hz),
        )
        return None

    block = _null_registry_to_dict(report)
    block.update({
        "band_hz": list(band_hz),
        # The two load-bearing flags, spelled out rather than implied.
        "gating": False,
        "regime": "uncalibrated_below_hf_floor",
        "hf_regime_floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
        "crossover_region_hz": [
            float(crossover_region_hz[0]), float(crossover_region_hz[1]),
        ],
        "why": (
            "Classification only. Below "
            f"{ECHO_BAND_HF_REGIME_FLOOR_HZ:.0f} Hz the detector's "
            "signal-presence screen is uncalibrated for a band that spans the "
            "committed crossover, so a finding here is evidence to read, "
            "never a reason to exclude a band or move a verdict."
        ),
    })
    log_event(
        logger, "correction.crossover_v2_crossover_region_registry",
        band_hz=list(band_hz),
        crossover_region_hz=list(crossover_region_hz),
        classification=str(block.get("classification", "")),
        n_candidates=int(block.get("n_candidates", 0) or 0),
        gating=False,
    )
    return block


def committed_crossover_region_hz(
    regions: Iterable[Any], *, octaves: float = 1.0,
) -> tuple[float, float] | None:
    """The band the COMMITTED crossover hands off in — ``Fc ± octaves`` across
    every committed region, or ``None`` when nothing is committed.

    Derived from the preset's own ``crossover_regions``, never from the
    session's working Fc, because this band's whole purpose is to say where the
    SHIPPED graph divides the spectrum. A speaker with no committed region has
    no handoff and gets ``None``.

    One octave is the span the crossover report uses for correction-authority
    tapering, so it is a default here rather than three literals.
    """
    fcs = [
        float(getattr(r, "fc_hz", 0.0)) for r in regions
        if float(getattr(r, "fc_hz", 0.0)) > 0.0
    ]
    if not fcs:
        return None
    span = 2.0 ** octaves
    return (min(fcs) / span, max(fcs) * span)
