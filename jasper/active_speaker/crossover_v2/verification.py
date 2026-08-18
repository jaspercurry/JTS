# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Four verification verdicts, four adoption axes, and the table (#2291, #2537, #2602).

**The defect this module exists to make impossible.** On 2026-08-10 a jts3
round reported VERIFY tracking the model to within 1.291 dB (tolerance
1.5 dB) while the post-apply cloud failed all three spec bands, worst
+7.727 dB at 428.44 Hz — and the run read as *passed*. One overloaded
pass/fail let a realization answer stand in for an acoustic one. So the four
questions are computed by four functions here, each answering only its own.

The four, in the issue's words:

* **Capture validity** — was the post-apply capture usable at all?
* **Realization** — did the applied graph produce the change we commanded?
* **Benefit** — did the measured speaker get better than it was?
* **Spec** — is the result inside the target envelope?

Spec is an *outcome*, never a proxy for benefit: "realized, improved, and
still out of spec" keeps the graph on the speaker, and "realized while the
speaker regressed" restores it. Both fit in the data model because the
statuses are independent.

**The second defect, and the one the adoption table was rebuilt for (#2537).**
Four independent verdicts still left one question — *what do we do with the
graph* — keyed on whether the round could PROVE it helped. On 2026-08-15 a
jts3 round measured its pooled residual from 3.304 to 0.915 dB on a usable
capture, could not compare it to a before, carried a boost, and was therefore
reverted to a state nobody had measured at all. The owner's ruling that day:
*we're looking for the least bad MEASURED tune. reverting to an unknown
measured state seems dumb… the first application is not the end point, it is
just the start.* So the four verdicts now compose into **four adoption axes**
— :func:`evaluate_evidence_trust`, :func:`evaluate_applied_safety`,
:func:`evaluate_round_quality`, and (since #2602)
:func:`evaluate_iteration_headroom` — and :func:`decide_adoption` selects one of
seven rows from those. Keeping an imperfect measured result and handing its
misses forward is a first-class outcome
(:attr:`~.contracts.AdoptionOutcome.KEEP_FOR_ITERATION`); the hard stops are
reserved for the safety class and for evidence that does not exist.

#2602's axis is the newest and the narrowest: it decides whether a round that
PASSED ends the series or runs another, and — since #2656 — whether a round
that MISSED has a round left to run at all. That second question is the ONE
fact that crosses to the missing cell, and it crosses as the axis's banked
:data:`HEADROOM_CAP_REACHED` reason rather than as its status, so the plateau
stops still cannot reach that cell. Either way the axis can keep a graph on
and it can never take one off — *in-tolerance is not done*.

**This module invents no DSP and owns no threshold.** Every number it
reports is lifted from a shipped primitive:

* the benefit residual is
  :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` over
  :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` — the same
  pooled spec residual the pre-apply prediction gate already grades
  model-vs-model (:data:`.attempt_grading.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`,
  0.5 dB); this is that comparison's *measured* twin, before-vs-after on the
  real speaker rather than model-vs-model;
* the spec verdict reads :attr:`~jasper.active_speaker.flat_spec.FlatSpecReport.overall_passed`
  and each band's own ``evaluable``/``passed`` — it classifies, it never
  re-grades;
* capture validity applies the shipped comparability rule verbatim
  (``crossover_v2_flow.attempt_record_from_verify``: unusable when
  ``CaptureIntegrity.failed`` is non-empty, and when the record is absent
  entirely, because ``None`` means "no evidence", never "clean");
* realization applies the shipped VERIFY comparator, ``max_db_notch_excluded``
  against its tolerance (``crossover_v2_flow`` reads exactly that key at its
  VERIFY gate), with one honest change named below.

The thresholds themselves — the benefit margin, the realization tolerance —
are **required parameters, not module constants**. That is deliberate and it
follows :mod:`~jasper.active_speaker.flat_spec`'s own line: that module owns
the spec table (what "flat" means) but explicitly not the loop policy ("how
much improvement counts... is not here and must not be"). A benefit margin is
loop policy. Phase 3c passes
:func:`~jasper.active_speaker.attempts_loop.material_improvement_db` — the
repo's single owner of "an improvement worth applying" — rather than this
module keeping a second copy of that number. One constant, one owner.

**The one honest change from the shipped comparator.** The deployed VERIFY
gate collapses "no tracking number" into its failure branch (``if not
isinstance(max_db, (int, float)) or max_db > VERIFY_TOLERANCE_DB``). Here
those are different answers: absent evidence is
:attr:`~.contracts.RealizationStatus.UNAVAILABLE`, not
:attr:`~.contracts.RealizationStatus.FAILED`. They lead to different
adoption rows, and conflating them is how "we do not know" gets reported as
a verdict.

**Everything here is live.** #2291 Phase 3c wired these functions into the
journey/host through :mod:`.round_evidence` and :mod:`.coordinator`, which
compute the verdicts, log their ``to_dict()`` payloads, and apply the
decision. This module still emits no logs, reads no clock, touches no file,
and imports neither :mod:`jasper.web` nor
:mod:`jasper.active_speaker.crossover_v2_flow` — same inputs, same outputs,
every time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Iterable, Mapping, TypeVar

import numpy as np

from ..delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MATCHED,
    VERDICT_SAFETY_ONLY,
    seam_rollback_deferral,
)
from ..flat_spec import (
    FlatSpecReport,
    evaluate_flat_spec,
    spec_band_tilt,
    spec_convergence_residual,
    spec_flatness_gauge,
)
from .contracts import (
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
    VerificationResult,
    detached_json,
)

# Underscored, and imported anyway: these are the package's validator
# vocabulary, owned once by the sibling module that defines the types they
# validate. A private-by-convention name shared inside one package is a
# smaller thing than two copies of "what counts as a positive dB" drifting
# apart — which is the failure this whole migration is about.
from .contracts import _positive as _positive_db
from .contracts import _text

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only: :mod:`jasper.audio_measurement.program_analysis` is a
    # 5,500-line module, and a pure evaluator should not drag it (or its
    # scipy weight) into every import of these contracts. Only
    # ``.failed`` / ``.not_evaluated`` are read at runtime.
    from jasper.audio_measurement.program_analysis import CaptureIntegrity

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
    "CLIPPED_RUN_CHECK",
    "HEADROOM_CAP_REACHED",
    "HEADROOM_NO_OBJECTIVES",
    "HEADROOM_PLATEAUED",
    "HEADROOM_REACHABLE",
    "HEADROOM_WITHIN_PLATEAU",
    "FlatnessObjectives",
    "MeasurementComparand",
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
    "SAFETY_UNCOMMANDED_LEVEL_LOUDER",
    "SPEC_BAND_OUT_OF_TOLERANCE",
    "SPEC_IN_TOLERANCE",
    "SPEC_NO_EVALUABLE_BAND",
    "SPEC_NO_REPORT",
    "SPEC_PARTIAL_COVERAGE",
    "TRACKING_COMPARATOR_KEY",
    "TRUST_MEASURED",
    "Verdict",
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
    "verification_result",
]


# --------------------------------------------------------------------------
# the shared verdict envelope
# --------------------------------------------------------------------------


StatusT = TypeVar("StatusT", bound=Enum)


@dataclass(frozen=True)
class Verdict(Generic[StatusT]):
    """One verdict, its reason, and the evidence it was read from.

    One envelope for all four rather than four near-identical records: they
    differ only in which status enum they carry, and that difference is
    already expressed by the type parameter.

    ``reason`` is drawn from this module's named constants, so a consumer
    branches on a symbol rather than matching prose. ``evidence`` is a
    JSON-shaped mapping of the numbers the verdict was actually read from —
    it exists so the Phase 3c host can log *why*, not just *what*, without
    re-deriving anything. Nothing here logs; a pure evaluator that emitted
    journal lines would be untestable as a function.

    ``evidence`` is **deep-copied at construction** via
    :func:`~.contracts.detached_json`, the package's own rule (#2307 note
    N1): a frozen record holding a caller's live mapping is immutable in
    name only, and a verdict whose evidence can change after the fact is
    exactly the kind of thing this module exists to stop. The copy is deep
    because a shallow one leaves the nested containers shared.
    """

    status: StatusT
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", detached_json(dict(self.evidence)))

    def to_dict(self) -> dict[str, Any]:
        # Detached on the way OUT as well as in: a shallow ``dict()`` here
        # hands a consumer the verdict's own nested containers, so a caller
        # editing the payload it is about to log would rewrite the verdict
        # behind itself. Same rule, other direction.
        return {
            "status": self.status.value,
            "reason": self.reason,
            "evidence": detached_json(dict(self.evidence)),
        }


# --------------------------------------------------------------------------
# 1. capture validity
# --------------------------------------------------------------------------

#: The integrity record was absent. Reuses the literal
#: ``crossover_v2_flow.ATTEMPT_INTEGRITY_UNAVAILABLE`` so one absence reads
#: the same word in the attempts ledger and here.
CAPTURE_INTEGRITY_UNAVAILABLE = "capture_integrity_unavailable"
CAPTURE_INTEGRITY_FAILED = "capture_integrity_failed"
CAPTURE_INTEGRITY_CLEAN = "capture_integrity_clean"


def evaluate_capture_validity(
    integrity: "CaptureIntegrity | None",
) -> Verdict[CaptureValidity]:
    """Was this capture usable? The shipped comparability rule, verbatim.

    ``crossover_v2_flow.attempt_record_from_verify`` already answers exactly
    this question for the attempts ledger, and this reproduces its rule
    rather than inventing a second one:

    * a **missing** record is unusable. The repeated convention across
      :mod:`jasper.audio_measurement.program_analysis` is that ``None`` means
      "no evidence", never "clean" — so absence fails closed.
    * a record with **failed** checks is unusable, named by those checks.
    * a record carrying only **not_evaluated** checks is *usable*. That is
      the shipped line (``comparable = not integrity.failed``, and
      :attr:`CaptureIntegrity.glitched` is likewise ``bool(self.failed)``),
      and tightening it here would silently discard rounds the ledger
      already counts. The not-evaluated names still ride in ``evidence`` so
      a reader sees what went ungraded.

    Reading the properties duck-typed keeps this module free of a heavy
    import; see the module's ``TYPE_CHECKING`` note.
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
#: ``ProgramAnalysis.verify_tracking``. Named rather than inlined so the key
#: this module grades and the key the flow grades are visibly the same one.
TRACKING_COMPARATOR_KEY = "max_db_notch_excluded"

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
    :data:`TRACKING_COMPARATOR_KEY` against ``tolerance_db`` — the same
    number, from the same key, as the deployed VERIFY gate. The 2026-08-10
    round measured 1.291 dB against a 1.5 dB tolerance and matched; the
    defect was never this comparison, it was that this comparison was
    allowed to speak for the other three questions.

    **Absent evidence is not a failure.** The deployed gate's
    ``not isinstance(max_db, (int, float)) or max_db > tolerance``
    collapses "no number" into the fail branch; here a missing mapping or a
    missing/non-numeric comparator is
    :attr:`~.contracts.RealizationStatus.UNAVAILABLE`. The two lead to
    different adoption rows — ``failed`` restores, ``unavailable`` does not
    claim success — so they must not be the same answer. ``bool`` is
    rejected as a comparator for the same reason the contracts module
    rejects it: ``True`` is an ``int`` and would silently grade as 1.0 dB.

    ``tolerance_db`` is a required argument: this module holds no threshold
    policy (see the module docstring). Phase 3c passes the flow's shipped
    ``VERIFY_TOLERANCE_DB``.

    The commanded-delta probe
    (:func:`~jasper.active_speaker.delta_probe.classify_delta_probe`) is the
    sibling instrument for this same question, and it is still **not** joined
    in *here*: this verdict answers the VERIFY comparator's question and only
    that. #2537 located the precedence policy the original cut was waiting for,
    and it is a separation rather than a merge — the probe's directional
    findings feed :func:`evaluate_applied_safety`, and everything else it says
    becomes a target in :func:`evaluate_round_quality`. Two instruments, two
    axes, no arbitration between them.
    """

    tolerance = _positive_db(tolerance_db, field_name="tolerance_db")
    if tracking is None:
        return Verdict(RealizationStatus.UNAVAILABLE, REALIZATION_NO_TRACKING, {})
    measured = tracking.get(TRACKING_COMPARATOR_KEY)
    if isinstance(measured, bool) or not isinstance(measured, (int, float)):
        return Verdict(
            RealizationStatus.UNAVAILABLE,
            REALIZATION_NO_COMPARATOR,
            {"comparator": TRACKING_COMPARATOR_KEY, "tolerance_db": tolerance},
        )
    deviation_db = float(measured)
    evidence = {
        "comparator": TRACKING_COMPARATOR_KEY,
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

    #2291 requires the entry baseline and the post-apply VERIFY to share
    "the same program, level, analyzer, calibration, reference mark, and
    comparable frequency mask". Four of those collapse into two fields:

    * ``program_id`` is a SHA-256 over the whole excitation schedule —
      phase, sample rate, channels, and every segment including its
      ``gain_db`` and ``effective_peak_dbfs``
      (:func:`jasper.audio_measurement.program._program_id`, re-derived and
      asserted in ``ExcitationProgram.__post_init__``). Equal ids are
      therefore a cryptographic guarantee of the same program **and** the
      same level, not a convention two captures might both be violating.
    * ``reference_mark`` is the position/mark identity, which the program id
      does not cover — a capture taken a metre away is the same program.

    **Calibration and analyzer version are not checkable here**, and saying
    so is better than a check that looks like one: neither is carried on
    ``ProgramAnalysis`` (the analyzer version is the module-level constant
    ``ANALYSIS_KIND``; the calibration curve is an input that does not
    survive onto the result). Phase 3c must not re-calibrate between the two
    captures of one round; nothing in this type can catch it if it does.

    The curve is the spatially-combined, 1/3-octave-smoothed magnitude
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` consumes —
    this module does not smooth, combine, or screen for interference.
    ``exclusion_mask`` is that evaluator's per-bin interference screen.
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
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` — read on
    each side and differenced:
    ``improvement_db = baseline.rms_db - post.rms_db`` (lower is flatter, so
    a positive difference is an improvement). That is the same arithmetic
    the pre-apply prediction gate already runs on two *predictions*; this is
    it on two *measurements*, which is the whole point of #2291's entry
    baseline. No new DSP: this module hands two curves to the shipped
    evaluator and subtracts two of its numbers.

    **Comparability is checked, never assumed** — the 2026-08-10 run had no
    comparable baseline at all, and reported success anyway. Missing either
    side, or any mismatch in program, reference mark, frequency grid, or
    exclusion mask, yields
    :attr:`~.contracts.BenefitStatus.INDETERMINATE` with the reason naming
    which comparability broke. An unusable capture reaches this the same
    way: Phase 3c passes ``None`` for a side it cannot compare.

    Requiring the two masks to be *identical* is what keeps the comparison
    honest about its own denominator. ``spec_convergence_residual``'s
    docstring names the trap directly — "a residual that fell because the
    honesty mask grew is not convergence, it is the same speaker graded on
    fewer bins" — and equal masks mean equal graded bins by construction.
    A caller that wants the union of two per-capture screens computes that
    union and passes it on both sides; choosing between "union" and
    "intersection" is an assembly decision, not a verdict.

    ``margin_db`` is what makes a regression *clearly* one, in the issue's
    word, and a required argument for the reason the module docstring gives:
    how much improvement counts is loop policy, and this module owns none.
    The band is symmetric — an improvement smaller than the margin is
    :attr:`~.contracts.BenefitStatus.INDETERMINATE`, not a small win — so a
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

    before = _pooled_residual(entry_baseline)
    after = _pooled_residual(post)
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


def _comparability_mismatch(
    baseline: MeasurementComparand, post: MeasurementComparand
) -> str | None:
    """The first way these two cannot be compared, or ``None``.

    Ordered most-identifying first, so the reason names the root difference:
    a different program makes the grids differing uninteresting.
    """

    if baseline.program_id != post.program_id:
        return BENEFIT_PROGRAM_MISMATCH
    if baseline.reference_mark != post.reference_mark:
        return BENEFIT_MARK_MISMATCH
    if baseline.curve.hz != post.curve.hz:
        return BENEFIT_GRID_MISMATCH
    if baseline.exclusion_mask != post.exclusion_mask:
        return BENEFIT_MASK_MISMATCH
    return None


def _pooled_residual(
    comparand: MeasurementComparand,
) -> tuple[float, int] | None:
    """``(rms_db, n_bins)`` from the shipped evaluator, or ``None``.

    ``evaluate_flat_spec`` raises on a degenerate curve — a non-ascending
    axis, a reference band with no surviving bins. Those are honest
    "cannot grade this" answers to a verdict function, not crashes to
    propagate into a household decision, so they become ``None`` here and
    :data:`BENEFIT_RESIDUAL_UNEVALUABLE` above.
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


#: The region benefit claim had no crossover region to grade over — the VERIFY
#: absolute claim was not evaluated, so there is no band. Distinct from
#: :data:`BENEFIT_RESIDUAL_UNEVALUABLE`, which means there WAS a band and the
#: curve could not be graded over it.
BENEFIT_NO_REGION_BAND = "no_region_band"


def evaluate_region_benefit(
    *,
    entry_baseline: MeasurementComparand | None,
    post: MeasurementComparand | None,
    band_hz: tuple[float, float] | None,
    margin_db: float,
) -> Verdict[BenefitStatus]:
    """The benefit claim again, restricted to the crossover blend region.

    **Why this exists.** :func:`evaluate_benefit` pools its residual across all
    three ``SPEC_BANDS`` — 250 Hz to 16 kHz. A win confined to a two-octave
    blend region is diluted across that whole span and lands inside the margin,
    so a correction that worked reads as ``residual_within_margin``. That is
    not a broken axis; it is a correct verdict about the wrong question. This
    asks the narrow one.

    **A second reported claim, never a second gate.** The pooled verdict stays
    the adoption input. Decision 10 changes the owner and the allowed tool in
    this region; it does not add a safety class or a new hard stop, and making
    a localized claim gate a round would be exactly that. Disclose and
    recommend.

    **Everything except the band is unchanged**, deliberately: the same
    estimator, the same curves, the same comparability rules, and the SAME
    0.5 dB margin. The margin is this model's own measured tracking error on
    jts3 — claiming an improvement smaller than the gap between what is
    modelled and what the hardware realizes is claiming something the
    instrument cannot resolve, and narrowing the band does not sharpen the
    instrument.

    **One consequence, stated rather than discovered.** Narrowing the mask
    re-routes ``evaluate_flat_spec``'s own centering: its reference level comes
    from ``REFERENCE_BAND_HZ`` intersected with what survives, so a
    region-masked evaluation is referenced to the REGION's surviving bins
    rather than to 250 Hz–8 kHz. For a SHAPE claim about a region that is the
    right frame — it matches the VERIFY absolute claim's own mean-removal, and
    it makes the verdict blind to a level change, which is the trim's fact and
    not this one's. It is also why the blend SOLVER does not use this framing:
    a filter derived against a region-local mean would cut the shoulders of a
    region that is merely quiet, trading a narrow dip for a wide one. Grading
    a shape and prescribing a cut are different questions with different right
    references; see
    :func:`~.blend_correction.solve_blend_correction` for the other half.

    The exclusion narrowing is applied to BOTH sides identically, so
    :func:`_comparability_mismatch`'s "identical exclusion masks" guarantee is
    preserved rather than forfeited.
    """

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

    Classifies; it does not re-grade. The pass answer is
    :attr:`~jasper.active_speaker.flat_spec.FlatSpecReport.overall_passed`
    verbatim, and the split *below* a pass reads each band's own
    ``evaluable``/``passed``. Nothing here recomputes a deviation, a band
    membership, or a tolerance — :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`
    owns all three, and a second owner is how two answers start.

    The three-way split is what ``overall_passed`` alone cannot say. That
    flag is ``False`` both for a band that measured out of tolerance and for
    a band nothing could be measured in — and
    :class:`~jasper.active_speaker.flat_spec.SpecFlatness` warns in those
    words that ``passed=False, evaluable=False`` means "could not be
    measured", not "failed". So:

    * some evaluable band failed on its merits → :attr:`~.contracts.SpecStatus.FAILED`;
    * nothing was evaluable, or coverage was partial →
      :attr:`~.contracts.SpecStatus.UNEVALUABLE` — a clean bill of health is
      not issued for a spectrum that was not fully measured, and a partial
      one is not called a failure either;
    * every band evaluable and passing → :attr:`~.contracts.SpecStatus.PASSED`.

    A ``FAILED`` band outranks partial coverage: a measured exceedance is a
    fact about the speaker, and losing another band's evidence does not
    unmake it.
    """

    if report is None:
        return Verdict(SpecStatus.UNEVALUABLE, SPEC_NO_REPORT, {})
    evidence = spec_flatness_gauge(report).to_dict()
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

    An unusable capture collapses the other three to their
    no-evidence values and carries the capture's reason. That is the
    contract's own construction invariant, applied here rather than left for
    a caller to trip over: a capture that could not be graded cannot have
    produced a graded answer, whatever a stale mapping might still say.

    Otherwise the reason joins all four so one string carries the whole
    story into a log line or a receipt. The richer per-verdict evidence stays
    on the :class:`Verdict` objects, which the host logs separately.
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
    """Could this round measure the state it applied? (#2537)

    The first axis of the adoption table, and a *composition* rather than a
    fifth measurement: it reads the two verdicts that already answer "is there
    evidence here", and reports the first one that says no, carrying that
    verdict's own reason so the row names the actual absence.

    * an **unusable capture** — integrity checks failed, or no integrity record
      at all — means there is no post-apply measurement. Nothing downstream can
      be read from a capture that could not be graded.
    * an **unavailable realization** means the VERIFY comparator produced no
      number, so the round cannot say the applied graph did what it commanded.

    **A benefit that came out indeterminate is deliberately NOT here, and that
    is the whole correction #2537 makes.** An indeterminate benefit means the
    round could not compare the applied state to a BEFORE — the post-apply
    capture itself is fine. Treating that as untrusted evidence is what threw
    away the 2026-08-15 JTS3 cycle-4 candidate: a measured state, graded on a
    usable capture, restored to a previous state whose own measurement was
    exactly the thing that was missing. In the owner's words, *reverting to an
    unknown measured state seems dumb*. An unprovable improvement is a QUALITY
    unknown — it becomes a next-round target — not a reason to discard the
    measurement.
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

#: A commanded boost realized MORE lift than it declared
#: (:func:`~jasper.active_speaker.delta_probe.boost_overshoot`).
SAFETY_BOOST_OVER_DECLARED_BOUND = "boost_realized_above_declared_bound"
#: The speaker measured LOUDER than declared where nothing was commanded.
SAFETY_UNCOMMANDED_LEVEL_LOUDER = "uncommanded_level_shift_louder"
#: A stimulus segment carried a full-scale run.
SAFETY_CLIPPED_CAPTURE = "clipped_capture"
#: Nothing in the evidence available says this state is unsafe. **Not the same
#: claim as "this state is safe"** — see :func:`evaluate_applied_safety`, whose
#: evidence mapping reports which instruments actually looked.
SAFETY_NO_FINDING = "no_unsafe_finding"

#: The integrity check name a clipped stimulus segment fails. The literal is
#: :data:`jasper.audio_measurement.program_analysis.INTEGRITY_CHECK_CLIPPED_RUN`,
#: repeated rather than imported for the reason the module's ``TYPE_CHECKING``
#: block gives — that module is 5,500 lines of scipy — and pinned against the
#: original by a contract test so the copy cannot drift.
CLIPPED_RUN_CHECK = "clipped_run"


def evaluate_applied_safety(
    *,
    probe: Any | None,
    integrity: "CaptureIntegrity | None",
) -> Verdict[SafetyStatus]:
    """Is the applied state safe to leave on a household's speaker? (#2537)

    The adoption table's hard stop, and the only axis that can pull a *measured*
    graph off for something other than the absence of evidence. Three findings,
    each read from a shipped instrument and none of them re-derived here:

    * a commanded **boost realized above its declared bound**
      (:attr:`~jasper.active_speaker.delta_probe.DeltaProbeMap.boost_over_declared_bound`)
      — energy in a driver the graph did not declare;
    * an uncommanded level shift measured **LOUDER** than declared
      (:data:`~jasper.active_speaker.delta_probe.VERDICT_LEVEL_MISMATCH` with a
      positive residual past its own tolerance);
    * a **clipped** stimulus segment in the post-apply capture.

    **Direction is the discriminator, and it is load-bearing.** The same
    magnitude of uncommanded level shift is a hard stop in one direction and a
    learning signal in the other: quieter-than-declared costs a household some
    output and tells the next round something, while louder-than-declared is
    energy nobody asked for. So a −2.3 dB residual reaches
    :class:`~.contracts.QualityStatus.MISSED` and stays live; a +2.3 dB one
    reaches :class:`~.contracts.SafetyStatus.UNSAFE` and comes off. The
    2026-08-15 JTS3 cycle-4 residual was negative.

    **A band-scoped level claim does not soften the direction.** #2533 narrows
    an uncommanded-shift *reason* when the quiet bins that measured it do not
    span the graded band, and that narrowing is a statement about WHERE the
    level was measured, never about whether it happened. A positive shift
    measured in a sliver is still a positive shift, so it is still unsafe, and
    the reason it was narrowed under travels in the evidence.

    **What "safe" does and does not claim.** :data:`SAFETY_NO_FINDING` means no
    instrument that ran reported a hazard — it is not a warrant that none
    exists. An absent or ungraded probe reports no finding here rather than a
    hazard, which is the shipped rule this module must not contradict:
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`
    deliberately excludes ``unavailable`` because "an absent measurement is not
    evidence of a bad correction, and rolling back on it would revert every
    session whose household closed the phone before the post-apply sweep". So
    the evidence mapping reports ``probe_graded`` — a reader can tell "safe
    because nothing was found" from "safe because nothing looked".

    **It also discloses a restore that did NOT happen (#2559).** The probe's own
    seam-bound rollback preempts this table, so a round the seam let through on
    the quieter-only ``model_error`` class would otherwise reach a household and
    a receipt with no record that an immediate restore was declined. ``evidence``
    carries the direction facts and ``seam_deferred``, the deferral's stable
    reason from its single owner
    (:func:`~jasper.active_speaker.delta_probe.seam_rollback_deferral`) — read
    here, never re-derived. It changes no status: a deferral is a statement
    about the seam, and the axes still answer for themselves.

    Duck-typed on both inputs, exactly like
    :func:`evaluate_capture_validity`: the two attributes read off ``integrity``
    keep this module free of a heavy import, and reading the probe by attribute
    lets a host that never ran one pass ``None``.
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
        # ...and HOW MUCH of the probe looked (#2614). A ``safety_only`` map is
        # a real grade of the two directional findings and no grade at all of
        # the correction's shape — the alternative-Fc case, where the previous
        # graph is unnameable and the change axis with it. ``probe_graded``
        # alone cannot say that, and a reader who took it for a full grade
        # would read "safe" as "the shape check passed" on a round where it
        # never ran.
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
        "boost_over_declared_bound": boost_over_bound,
        "boost_overshoot_db": (
            getattr(probe, "boost_overshoot_db", None) if probe is not None else None
        ),
        # WHICH WAY the graded bins missed, and whether the probe's own seam
        # therefore handed its rollback to this table instead of restoring on it
        # (#2559). It rides the SAFETY axis rather than the quality one because
        # its basis is a safety-direction fact — nothing realized louder than
        # commanded — and because ``round_axes`` is what puts it on the receipt.
        # ``""`` means no deferral, which is every round the seam did not
        # preempt; a reader must be able to tell that from a restore.
        "realized_louder_than_commanded": (
            bool(getattr(probe, "realized_louder_than_commanded", False))
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
    return Verdict(SafetyStatus.SAFE, SAFETY_NO_FINDING, evidence)


# --------------------------------------------------------------------------
# 7. quality — what the next round is for
# --------------------------------------------------------------------------

ADOPTION_MEASURED_REGRESSION = "measured_regression"
#: The delta probe measured the emitted filters not doing what the fit's model
#: of them says, in one of the classes the project reverts. Distinct from
#: :data:`ADOPTION_MEASURED_REGRESSION` because they are different instruments
#: answering different questions — that one is the before/after summed
#: comparison, this one is realized-vs-commanded — and a receipt that named the
#: pooled comparison for a probe finding would send the next round looking in
#: the wrong place.
#:
#: **The cause carries the CLASS, as ``<prefix>:<verdict>``**, reusing the
#: composite-cause shape :func:`decide_adoption` already mints for its
#: no-anchor row. The three rollback classes have three different household
#: sentences (:data:`~.vocabulary.DELTA_PROBE_REASON_BY_VERDICT`), and they
#: were reached through the probe's own seam before the routing move. A bare
#: prefix would have collapsed all three into one generic restore sentence — a
#: copy regression smuggled in as plumbing — so
#: :func:`~.vocabulary.round_restore_reason` reads the class back off the cause
#: and renders exactly what it always did.
ADOPTION_PROBE_ROLLBACK_CLASS = "delta_probe_rollback_class"
ADOPTION_REALIZED_AND_IMPROVED = "realized_and_improved"
ADOPTION_REALIZATION_FAILED = "realization_failed"
ADOPTION_UNPROVEN = "benefit_unproven"
#: Distinct from :data:`ADOPTION_UNPROVEN` because the benefit in that one
#: cell was *improved* — what is missing is the evidence that the graph we
#: applied is why. A receipt saying "benefit unproven" there would be false.
ADOPTION_REALIZATION_UNAVAILABLE = "realization_unavailable"

#: #2291's adoption table, transcribed — **the same nine cells, keyed on the
#: same two statuses, carrying the same nine causes.** What #2537 changed is
#: what the non-keep cells RESOLVE TO, not what they are keyed on.
#:
#: All nine pairs exist here so a combination cannot fall through to a default,
#: and a new enum member fails the exhaustiveness test rather than silently
#: landing on one. Read against the issue's rows:
#:
#: * ``matched | improved`` — the one PASSED row.
#: * ``any | clearly regressed`` — REGRESSED, the only quality answer that
#:   restores. All three carry the regression as their cause rather than the
#:   realization failure the pre-#2537 table preferred for ``failed |
#:   regressed``: a realization failure is no longer a restore trigger, so the
#:   cause that actually takes the graph off is the measured regression.
#: * everything else — MISSED, keeping its original cause verbatim, because the
#:   cause was always right and only the consequence was wrong.
#:
#: ``unavailable | improved`` is MISSED rather than PASSED because the pass row
#: names ``matched`` and this table does not widen it: the speaker measured
#: better, but with no realization evidence the round cannot say the graph it
#: applied is why.
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

    The adoption table's third axis (#2537), and the one the owner's ruling is
    actually about: *the first application is not the end point, it is just the
    start. The plan is to iterate and learn.*

    **The STATUS is #2291's own table, unchanged in what it reads.**
    :data:`_QUALITY_TABLE` is keyed on ``(realization, benefit)`` — the same two
    statuses, the same nine cells, the same nine causes. #2537 changes what a
    non-keep cell resolves to (``keep_for_iteration`` rather than ``restore`` or
    a ``user_decision`` nobody rendered), not what decides it.

    **The TARGETS are disclosure, and they are a strictly separate question.**
    Spec verdicts, each failing spec band, and the delta probe's own reason ride
    in ``evidence`` for the next round to chase. **None of them moves the
    status**, and for two independent reasons that must both be stated:

    * *Spec is an outcome, not a proxy for benefit.* Every row of #2291's table
      reads "any" for spec, because "improved and still out of spec" is an
      honest first pass. ``test_the_spec_verdict_never_moves_the_quality_STATUS``
      pins that by permutation, and that pin is load-bearing.
    * *The spec VERDICT still decides nothing, on any axis.*
      :func:`decide_adoption` never reads a :class:`~.contracts.SpecStatus`.

    **What #2602 changed here, and what it did not.** The fourth axis
    (:func:`evaluate_iteration_headroom`) reads the same post-apply report this
    function is handed — but it reads *measured dB* off it (each band's own
    ripple, and the level step between bands), never the pass/fail against a
    tolerance row, and it can only choose whether another round runs. So the
    sentence above holds unchanged: the spec verdict moves nothing.

    That axis also required a precondition this docstring used to name as
    outstanding — spec numbers computed with **no intersection against the
    session's own trusted floor**, which would have made any decision keyed on
    them inherit a gate-length term no round controls. **#2551 landed that
    intersection**, and the post-apply cloud report is built with the floor
    supplied (``crossover_v2_flow``'s ``cloud_trusted_floor_hz``), so the
    numbers the fourth axis reads are already floor-intersected. An axis
    reading them would not have been admissible before that; it is now.

    So a round can be PASSED with targets outstanding, and that is not a
    contradiction: the status answers "did this round realize and improve the
    speaker", the targets answer "what should the next one chase". They are
    different questions, and #2291's whole design is about not letting one
    answer stand in for another.

    **What the probe's ROLLBACK CLASSES now do here, and why they moved.** Until
    the fifth principle landed, three probe verdicts
    (:data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`)
    restored the graph from a seam in the flow that ran *before* this table and
    ended the session on its own code. That seam was a SECOND owner of "restore
    the previous graph", and it PREEMPTED this table — which is why
    ``keep_for_iteration`` could never fire on those classes, and why the
    2026-08-16 shortfall round left its realization in journal events with no
    receipt behind it. The seam is gone; the class reaches this axis and lands
    on :attr:`~.contracts.QualityStatus.REGRESSED`, the one quality answer that
    restores, so :func:`decide_adoption` is the single decider and
    :func:`~jasper.active_speaker.crossover_v2.coordinator.run_round` banks the
    round either way.

    **The restore SET is unchanged by that move, deliberately.** The same three
    verdicts restore, the #2559 deferral still spares the quieter-only
    ``model_error``, and no class that used to restore now keeps. Narrowing the
    set — gating ``level_dependent_shortfall`` on a band-resolved realization
    inside the trusted band, and ``model_error`` on measured-worse — is the
    design brief's §2.2 re-audit and is deliberately NOT done here: it is a
    change to WHICH graphs come off a household's speaker, and it belongs in a
    change that is about that rather than riding a plumbing move.
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
    probe_rollback = _probe_rollback_class(probe, probe_verdict)
    if probe_rollback:
        quality = QualityStatus.REGRESSED
        reason = f"{ADOPTION_PROBE_ROLLBACK_CLASS}:{probe_rollback}"

    return Verdict(quality, reason, {
        "targets": targets,
        "spec_bands": _failing_spec_bands(spec_report),
        # WHICH probe class escalated, or ``""``. Named rather than left to be
        # re-derived from ``targets``, because the row's reason is a constant
        # and a reader needs the class behind it.
        "probe_rollback_class": probe_rollback,
    })


def _probe_rollback_class(probe: Any | None, verdict: str) -> str:
    """The probe verdict that takes this graph off, or ``""``.

    Two owners consulted, neither re-derived here:
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`
    for which classes restore, and
    :func:`~jasper.active_speaker.delta_probe.seam_rollback_deferral` for the
    one that is spared (#2559 — a ``model_error`` pointing entirely quieter
    than commanded is a quality miss the series keeps and learns from).

    That deferral function keeps its name even though the seam it was named
    for is gone: ``seam_rollback_deferral`` is also a KEY on
    :meth:`~jasper.active_speaker.delta_probe.DeltaProbeMap.to_dict`, and so on
    every banked receipt. Renaming the symbol without the wire key would be two
    names for one fact; renaming both would rewrite persisted vocabulary to fix
    a comment. The reader moved, the name did not.
    """

    if not verdict or verdict not in DELTA_PROBE_ROLLBACK_VERDICTS:
        return ""
    return "" if seam_rollback_deferral(probe) else verdict


def _failing_spec_bands(report: FlatSpecReport | None) -> list[dict[str, Any]]:
    """Each evaluable band that measured out of tolerance, as a next-round target.

    Per-band rather than "spec failed", because the receipt is what the NEXT
    round reads and "250-2000 Hz missed by 0.8 dB at 331.8 Hz" is an
    instruction where "spec failed" is only a mood. Lifted from the report's own
    :class:`~jasper.active_speaker.flat_spec.BandResult` fields — nothing here
    re-grades a band or recomputes a deviation.
    """

    if report is None:
        return []
    bands: list[dict[str, Any]] = []
    for band in report.bands:
        if not (band.evaluable and band.passed is False):
            continue
        bands.append({
            "f_lo_hz": float(band.f_lo_hz),
            "f_hi_hz": float(band.f_hi_hz),
            "tolerance_db": float(band.tolerance_db),
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
    """#2602's two graded objectives, as one round measured them.

    Both are **frame-invariant**, which is the property that lets them be
    differenced across rounds at all: the spec's reference level is a mean
    pooled across bands, so it moves when the speaker does, and two rounds'
    frame-*dependent* numbers are not comparable even when the same speaker
    got flatter. ``ripple_db`` is each band's deviation from its OWN level and
    ``tilt_db`` is a difference of two levels — the shared reference cancels
    out of both.

    ``None`` on either field means "this round could not grade it", never
    "zero": a report with fewer than two levelled bands has no tilt, and
    inventing 0 dB would read as perfect alignment. The same unknown-vs-zero
    rule :func:`~jasper.active_speaker.flat_spec.spec_band_tilt` already
    follows for the band levels it skips.

    **Frame-invariance is necessary for cross-round differencing but not
    sufficient, and the missing half is the graded BAND.** Both numbers are
    invariant to the reference *level*, and neither is invariant to which BINS
    were graded. The session's trusted floor sets each band's ``graded_lo_hz``,
    so a round whose gate came out shorter re-scopes the lowest band and moves
    both objectives with no acoustic change at all — the same mechanism
    :func:`~jasper.active_speaker.crossover_v2_flow.cloud_trusted_floor_hz`
    documents for the 1-4 kHz band across 3/5/7/10 ms gates. Measured on an
    UNCHANGED curve, a 7↔10 ms gate change alone produces ±0.518 dB of spurious
    movement here, which is 2.1× :data:`~.round_evidence.ITERATION_PLATEAU_DB`
    — enough to mask a plateau or invent one.

    **#2609 SF5 closes it**: the floor these numbers were graded against is
    banked beside them (``trusted_floor_hz`` in
    :func:`evaluate_iteration_headroom`'s evidence, carried onto the round
    receipt), and a round whose floor differs from the previous round's
    **refuses the movement comparison** rather than reading a gate-length
    artefact as progress. Only positive evidence of a floor change refuses; two
    unknown floors compare exactly as they did before.
    """

    #: Largest level step between two graded bands — the owner's 2.37 dB.
    tilt_db: float | None
    #: Worst within-band deviation from that band's own level.
    ripple_db: float | None

    @property
    def worst_db(self) -> float | None:
        """The larger of the two, or ``None`` when neither graded.

        A MAX rather than a sum or an RMS: the two objectives are different
        misses in the same speaker, and the series is done only when BOTH are
        small. Pooling them would let a large tilt hide behind flat bands.
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
    """Reduce a post-apply spec report to #2602's two objectives.

    **Derived from the report, never recomputed from the curve** — the same
    rule, and the same reason, as
    :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` and
    :func:`~jasper.active_speaker.flat_spec.spec_band_tilt`: band membership,
    the exclusion mask's effect, and each band's own level are answered exactly
    once, by ``evaluate_flat_spec``.

    The tilt half IS ``spec_band_tilt`` — the shipped #1857 reduction, reused
    rather than re-derived, so the frame-free reading the attribution surfaces
    already render and the one the series iterates on cannot disagree. The
    ripple half is a one-line ``max`` over the same report's bands and is kept
    private here rather than promoted beside its sibling: it has exactly one
    consumer. Promote it into ``flat_spec`` the moment a second surface needs
    it, and not before.
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
#: A relative tolerance, and a wide margin on purpose: it exists to admit the
#: same float after a JSON round trip, not to tolerate a real change. The
#: mechanism it screens for moves the floor by tens of percent — the 1-4 kHz
#: band's ``graded_lo_hz`` across a 7↔10 ms gate — so anything a genuine gate
#: change produces is orders of magnitude outside this and refuses, while a
#: floor that merely travelled through ``json.dumps`` and back compares equal.
FLOOR_COMPARABILITY_RTOL = 1e-3


def _floors_comparable(
    this_floor_hz: float | None, previous_floor_hz: float | None
) -> bool:
    """May two rounds' objectives be differenced? (#2609 SF5)

    ``True`` unless there is POSITIVE evidence of a floor change, and the
    direction is the whole point. An unknown floor on either side — a tier that
    banked none, a receipt from before SF5 — is not evidence that the frame
    moved, and refusing on it would disable the plateau stop the ruling names
    on every round until every path threads a floor. Two KNOWN floors that
    disagree by more than :data:`FLOOR_COMPARABILITY_RTOL` is the one case that
    refuses, because then the movement between them contains a gate-length term
    no round controls.
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
    """Should the series run another round? The #2602 axis.

    The owner's ruling, 2026-08-16: *in-tolerance is not done*. A round that
    passes spec ends the series only when nothing better is plausibly within
    reach — so this asks the question :class:`~.contracts.QualityStatus` never
    could, because quality grades **what this round did** and this grades
    **what a next one could still get**.

    Three ways a series is over, checked most-binding first so the reason names
    the fact that actually ended it:

    1. **The round cap.** Checked first because it is the one stop no evidence
       can argue with: at ``round_ordinal >= round_cap`` there is no next round
       to have headroom for, however much is left on the table. Naming a
       plateau here would imply more rounds would not have helped, which the
       measurement did not say.
    2. **Already flat enough.** Both objectives inside ``plateau_db``. There is
       nothing left worth a round, which is a genuinely different answer from
       "we stopped improving" and gets its own reason so the screen can say the
       good news rather than the resigned one.
    3. **Plateaued.** The objectives moved less than ``plateau_db`` since the
       previous round. This is the stop the ruling names by its number, and it
       is measured on the OBJECTIVES rather than on the pooled residual for a
       structural reason: a round only reaches
       :attr:`~.contracts.QualityStatus.PASSED` by improving past
       :data:`~.round_evidence.MEASURED_BENEFIT_MARGIN_DB` (0.5 dB), which is
       already twice this bar, so a plateau read off the benefit verdict could
       never fire on the row that needs it. Movement toward *flat* and movement
       of the *pooled residual* are not the same quantity, and it is the first
       one the series is iterating on.

    **Ungradable objectives are NOT a fourth stop, and that reversal is the
    ethos's** (``docs/audio-commissioning-roadmap.md``, "Least-bad measured,
    honed in bites", owner-ratified 2026-08-16): *only the round budget, the
    plateau, and the safety class end a series.* Until this change a tier that
    walked no post-apply cloud, or a report whose bands all fell below the
    session's trusted floor, resolved to :attr:`~.contracts.IterationHeadroom.
    EXHAUSTED` under :data:`HEADROOM_NO_OBJECTIVES` — "we cannot tell" ended the
    series. That reads missing evidence as a plateau, which is exactly the
    conflation the ruling forbids: a round that could not grade how flat the
    result is has not shown that a flatter one is out of reach. So the reason
    survives (a household is still owed the specific ending, and a receipt from
    before this change still carries it) and the STATUS is now
    :attr:`~.contracts.IterationHeadroom.REACHABLE`. The blast radius is bounded
    by the cap above and by the household: another round is only ever *offered*,
    the graph is untouched either way, and the review screen's decline closes
    the series on request.

    ``previous is None`` is the first round of a series: there is no movement
    to judge yet, so the plateau stop cannot fire and the answer rests on
    distance alone. That is the honest reading — a first round has not had a
    chance to stall — and it is also why the cap is checked independently
    rather than inferred from the absence of history.

    Args:
      objectives: this round's :func:`flatness_objectives`.
      previous: the previous round's, carried forward on the durable receipt,
        or ``None`` for the first round of a series.
      round_ordinal: 1-based position of this round in the series.
      round_cap / plateau_db: the series policy, passed rather than imported
        for the same reason :func:`evaluate_benefit` takes ``margin_db`` — how
        many rounds are worth running, and how much movement counts, are loop
        policy and this module owns none. Their single definitions live in
        :mod:`.round_evidence` beside the benefit margin.
      trusted_floor_hz: the floor ``objectives`` were graded against, and
        ``previous_trusted_floor_hz`` the one the previous round's were
        (#2609 SF5). Banked in the evidence beside the objectives whether or
        not they decide anything here, because the NEXT round is what reads
        them back. See :func:`_floors_comparable` for the one case that
        refuses the movement comparison and why an unknown floor does not.
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
        # convention ``improvement_db`` uses on the benefit verdict, so the two
        # numbers on one journal line never read in opposite directions.
        "movement_db": movement_db,
        # #2609 SF5. The frame the two objectives above were graded in, banked
        # BESIDE them so the next round can check the frame rather than assume
        # it. ``movement_comparable`` is the answer this round reached about
        # the pair, recorded rather than left to be re-derived from them.
        "trusted_floor_hz": _optional_floor(trusted_floor_hz),
        "previous_trusted_floor_hz": _optional_floor(previous_trusted_floor_hz),
        "movement_comparable": movement_comparable,
    }

    if ordinal >= cap:
        return Verdict(IterationHeadroom.EXHAUSTED, HEADROOM_CAP_REACHED, evidence)
    if worst is None:
        # REACHABLE, not EXHAUSTED — see the docstring. Missing evidence is not
        # a plateau, and the reason still names which ending this was.
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


def _optional_floor(value: float | None) -> float | None:
    """A finite floor, or ``None`` — the same unknown-vs-zero rule as elsewhere.

    A non-finite floor is an unreadable one: banking a NaN would give the next
    round a number that compares false against everything, which is a silent
    permanent refusal rather than an honest absence.
    """

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------
# 8. adoption — the four axes, as a table
# --------------------------------------------------------------------------

#: The one cause that survives the #2537 rewrite unchanged, and only on
#: :data:`~.contracts.ADOPTION_ROW_RESTORE_UNTRUSTED`. Before #2537 a boosted
#: intervention failed closed whenever the BENEFIT was indeterminate, which is
#: how a measured, safe, improving candidate was reverted on 2026-08-15. With
#: trusted evidence a boost is now judged realized-vs-declared on the safety
#: axis; with untrusted evidence there is nothing to judge it by, and the
#: pre-#2537 argument still stands verbatim — *a boost whose benefit we cannot
#: show is energy we put into a driver and cannot justify, so it comes off.*
ADOPTION_UNPROVEN_BOOST = "unproven_boost_failed_closed"
ADOPTION_RESTORE_FAILED = "restore_failed"
ADOPTION_NO_ROLLBACK_ANCHOR = "restore_required_without_rollback_anchor"

#: Which row a TRUSTED, SAFE round lands on, by its quality answer — the last
#: three of the table's five, and the only ones with more than one possible
#: outcome between them.
#:
#: A MAPPING rather than a guard ladder, and that is the one structural thing
#: this table kept from #2291's: a lookup with no default means a fourth
#: :class:`~.contracts.QualityStatus` member raises ``KeyError`` at its first
#: call instead of falling through to whichever branch happened to come last.
#: A ladder can only get that property from a trailing ``raise`` that the
#: signature's own type guard makes unreachable — a branch no test can enter,
#: which is a guard in name only.
_QUALITY_ROWS: Mapping[QualityStatus, tuple[AdoptionOutcome, str]] = {
    QualityStatus.PASSED: (AdoptionOutcome.KEEP, ADOPTION_ROW_KEEP),
    QualityStatus.MISSED: (
        AdoptionOutcome.KEEP_FOR_ITERATION, ADOPTION_ROW_KEEP_FOR_ITERATION,
    ),
    QualityStatus.REGRESSED: (
        AdoptionOutcome.RESTORE, ADOPTION_ROW_RESTORE_REGRESSION,
    ),
}

#: Where #2602 splits the table's one PASSED cell, by the fourth axis.
#:
#: The cell above resolves to ``KEEP`` — which used to be the end of the story
#: — and this decides whether that keep is TERMINAL. Only the passing cell
#: consults the STATUS: a MISSED round keeps iterating however flat the axis
#: says the result is (it has outstanding targets by construction), and a
#: REGRESSED one restores before this table is ever reached. The one fact that
#: crosses to the missing cell is the spent BUDGET, and it crosses as the
#: axis's reason rather than as its status — see :func:`decide_adoption`.
#:
#: A second MAPPING for the same reason the first one is one: a lookup with no
#: default means a third :class:`~.contracts.IterationHeadroom` member raises
#: ``KeyError`` at its first call rather than silently landing on whichever
#: branch came last.
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
    """Keep, keep-and-iterate, restore, or escalate — the #2537/#2602 table.

    **What this MODIFIES, and why.** #2291's table was keyed on
    ``(realization, benefit)`` and had one keep cell: a round had to *prove* it
    improved the speaker or the graph came off. The 2026-08-15 JTS3 cycle-4
    round is what that costs. Its capture was usable, its realization tracked,
    its measured pooled residual went 3.304 → 0.915 dB, its uncommanded level
    residual pointed *quieter* — and because no comparable entry baseline made
    the benefit provable and the intervention carried a boost, the fail-closed
    cell reverted it to a previous state whose own measurement nobody had. The
    owner's ruling (2026-08-15): *we're looking for the least bad MEASURED tune.
    reverting to an unknown measured state seems dumb… the first application is
    not the end point, it is just the start.*

    **That table is still here, unchanged in what it reads.** It is
    :data:`_QUALITY_TABLE` — same nine cells, same two keys, same nine causes —
    and what changed is what a non-keep cell RESOLVES TO. Two axes join it, and
    neither is a new way to grade a correction: one asks whether there was
    evidence at all, one asks whether the result is a hazard. Three separable
    questions, each with its own evaluator above:

    * :func:`evaluate_evidence_trust` — did we measure the state we applied?
    * :func:`evaluate_applied_safety` — is that state safe to leave on?
    * :func:`evaluate_round_quality` — how good is it, and what is left to fix?
    * :func:`evaluate_iteration_headroom` — could a next round do better?

    **What #2602 MODIFIES, and why.** The fourth axis is the owner's ruling of
    2026-08-16: *in-tolerance is not done.* Row 1 used to be the end of every
    good series — a round that realized its prediction and measured flatter
    stopped, whatever was left. The round-3 review is what that costs: inside
    every spec band, tweeter "largely in range but still not flat", and a
    2.37 dB step between 250-2000 Hz and 8000-16000 Hz that no reference choice
    moves. So row 1 SPLIT: it still means "passed, and the series is over", and
    :data:`~.contracts.ADOPTION_ROW_KEEP_ITERATING` is the same passing round
    with a flatter result still in reach.

    **Nothing else moved with #2602.** Its split is confined to the one cell
    (:data:`_PASSED_ROWS`) that used to be unconditionally terminal, and every
    other stop the table had is still exactly where it was: a measured
    regression restores, an unmeasured state restores, a hazard restores, and
    a failed restore escalates. In particular **headroom can never keep a graph
    the other axes said to take off** — it is read after all three of them,
    and only on the branch they all passed.

    **What #2656 MODIFIES, and why.** One fact now crosses to the MISSED cell:
    the spent round budget. The ethos names three series-enders — *only the
    round budget, the plateau, and the safety class end a series*
    (``docs/audio-commissioning-roadmap.md``) — with no row exception, and
    until this the budget had one. A MISSED round never read the fourth axis at
    all, so a series that kept missing kept being offered another round with no
    round left to spend: a gate walked 40 consecutive MISSED rounds and every
    one of them said keep-for-iteration. The bound that existed lived on the
    done screen's button, which a headless driver never presses.

    **Keyed on the axis's REASON, not its status, and that is the scope.** The
    budget crosses; the two plateau stops do not. #2537 chose MISSED-iterates
    deliberately — a missing round has outstanding targets by construction, so
    "we stopped improving" is not a reason to stop trying — and that choice is
    untouched below the cap. :data:`HEADROOM_CAP_REACHED` is minted in exactly
    one place, by the one comparison that owns the budget
    (:func:`evaluate_iteration_headroom`, which checks it FIRST so the reason
    names the fact that actually ended the series). Re-deriving
    ``ordinal >= cap`` here would make this a second enforcer of a rule that
    has one — the same reason
    :func:`~.coordinator.series_position_from_state` refuses to clamp.

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
      trust / safety / quality / headroom: the four axis verdicts. **Verdicts,
        not bare statuses**, and that is a deliberate change from the pre-#2537
        signature. The reason a row fires under IS the deciding axis's own
        reason — "the boost realized above its declared bound", "the spec band
        is out of tolerance" — so taking statuses and minting a parallel cause
        vocabulary here would put a second owner on every one of them. This
        function still decides nothing an evaluator did not: it selects which
        axis speaks.

        On both passing rows the axis that speaks is **headroom**, not quality,
        and that is the point of the split rather than an accident of it. Row 1
        no longer means only "this round was good" — it means "this round was
        good AND the series is finished" — so the receipt has to carry WHICH
        ending it was, and "the objectives are inside the plateau" and "three
        rounds is the limit" are two very different sentences to hand a
        household. Quality's own reason has not gone anywhere; it rides on the
        quality axis, which the receipt records beside this decision.
      boosted: does the applied intervention contain a boost? Computed by the
        host with the shipped predicate
        (``camilla_yaml.linearization_has_boost``); this module keeps no second
        copy of that rule. Read **only** on the untrusted row — see
        :data:`ADOPTION_UNPROVEN_BOOST`.
      rollback_available: can the host actually restore the entry graph? The
        flow's capability idiom is seam presence — ``STAGE_VERIFY_CAPABILITIES``
        provides ``CAPABILITY_ROLLBACK``, and ``bind_v2_stage_seams`` binds the
        rollback seam only for a stage that declares it
        (:mod:`jasper.web.correction_crossover_v2`). The parameter exists so the
        decision *sees* seam presence rather than assuming it: a host that binds
        no rollback must get a different answer, not a restore instruction
        nothing can carry out.
      restore_failed: a restore was attempted and did not complete.

    Safety, stated plainly because this decides whether a graph stays on a
    speaker:

    * **The restore trigger set NARROWED for quality and WIDENED for hazard,
      and both directions are intended.** Gone as restore triggers: a
      realization that missed tolerance, and an unprovable benefit under a
      boost. Added: a boost measured above its declared bound, an uncommanded
      shift measured LOUDER than declared, a clipped capture, and every
      previously-``user_decision`` untrusted cell. Nothing that was a hard stop
      for a hazard stopped being one. A spec band out of tolerance was never a
      restore trigger and still is not — see :func:`evaluate_round_quality`.
    * **A measured regression still restores.** The owner's ruling turns on
      *unknown* previous states; a regression is the case where the previous
      state's measurement is exactly the evidence, so going back is going back
      to a measured tune. That is
      :data:`~.contracts.ADOPTION_ROW_RESTORE_REGRESSION`, the fifth row the
      ruling's four did not enumerate and its principle requires.
    * **An unmeasured applied state never stays.** The untrusted row restores
      even though nothing accuses the graph, because "least bad MEASURED tune"
      cannot include a state nobody measured.
    * **Safety is checked BEFORE trust.** Both rows restore, so the order only
      decides which name the receipt carries — and naming the hazard beats
      naming the absence when both are true (a clipped capture is both). A
      hazard read off weak evidence lands on the conservative side by
      construction.
    * **A restore we cannot perform is not a restore.** When the evidence says
      the graph must come off and no rollback anchor exists, the answer is
      ``recovery_required`` — a loud operator path — never a ``restore`` the
      host has no way to execute and never a ``keep``.
    * **A failed restore outranks everything.** Checked first: the speaker is
      then in neither the entry graph nor the intended one, and no later row
      can describe that better.
    * **The fourth axis chooses the sentence, never the graph.** Every one of
      its answers leaves the speaker on the graph the cell above it already
      chose — ``KEEP`` and ``KEEP_FOR_ITERATION`` are the same state, differing
      only in whether another round is coming — so it selects the receipt and
      the sentence, not the DSP. A headroom evaluator that returned nonsense
      could ask for a round nobody needs, or end a series a round early; it
      could not leave an unsafe, unmeasured, or regressed graph on a speaker,
      because all three of those rows return before it is read.
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
        # #2602: the one cell that used to be terminal, asked whether it is.
        # Keyed off the OUTCOME the table above resolved to rather than off
        # ``quality.status`` directly, so the passing cell has exactly one
        # definition and this branch cannot drift from it.
        outcome, row = _PASSED_ROWS[headroom.status]
        return AdoptionDecision(outcome=outcome, reason=headroom.reason, row=row)
    if headroom.reason == HEADROOM_CAP_REACHED:
        # #2656: the budget ends a MISSED series too. Reached only from the
        # iterating cell — the passing one returned above through
        # ``_PASSED_ROWS[EXHAUSTED]``, which is the same ending by the same
        # reason — so this is the missing half of one rule, not a second one.
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

    Mirrors the shipped rule at the flow's delta-probe rollback seam, which
    already reports the verdict's own reason when the graph came off and
    ``correction_rollback_failed`` when it did not. Here the branch is one
    step earlier — before the attempt rather than after it — because a
    decision to restore with no anchor to restore *to* is not a decision the
    host can carry out.

    The ROW is the same either way: the rule fired, and only its execution was
    impossible. A receipt that renamed the row on a missing anchor would say a
    different rule applied.
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

#: What the household is told a graded round came to. Four names, and the
#: domain owns them even though it is the web host's ``_post_apply_grade``
#: that picks one: they are the *vocabulary* of a commissioning result, in the
#: same sense as :data:`ADOPTION_REALIZED_AND_IMPROVED` above, and the domain
#: renderer has to speak them.
#:
#: They lived in ``jasper.web.correction_crossover_v2`` until #2662's health
#: check, and that was a layering inversion with a measurable cost. The
#: renderer that turns a graded round into screen copy —
#: :mod:`jasper.active_speaker.crossover_envelope_v2` — may not import
#: ``jasper.web`` (the package's one-way rule, enforced by
#: ``test_crossover_v2_journey.py``), so it re-typed all four as bare string
#: literals in twelve places with nothing holding the two sets equal. Owning
#: them here lets both the host that writes one and the renderer that reads it
#: import the same symbol.
#:
#: **These are not** :class:`~.contracts.AdoptionOutcome`. That enum decides
#: what happens to the GRAPH for one round; these four name what a whole
#: commission's post-apply grade came to for a PERSON, and a session can grade
#: ``RESULT_KEEP_PREVIOUS`` off inputs no single round's adoption decision saw.
#: The two are deliberately separate, and #2662 records that the pair has no
#: reconciliation — naming the split is not the same as closing it.
RESULT_VERIFIED_TARGET = "verified_target"
RESULT_VERIFIED_BEST_EVALUATED = "verified_best_evaluated"
RESULT_KEEP_PREVIOUS = "keep_previous"
#: Shares its value with the host's ``GRADE_INCONCLUSIVE``, which answers the
#: neighbouring question ("did the check finish?") about the same round. The
#: collision is real and load-bearing to know about: a bare ``"inconclusive"``
#: in the renderer cannot be attributed to one of the two by its value alone,
#: which is why the conventions test that pins this vocabulary skips exactly
#: this string and says so.
RESULT_INCONCLUSIVE = "inconclusive"
