# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Four verification verdicts, three adoption axes, and the table (#2291, #2537).

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
just the start.* So the four verdicts now compose into **three adoption axes**
— :func:`evaluate_evidence_trust`, :func:`evaluate_applied_safety`,
:func:`evaluate_round_quality` — and :func:`decide_adoption` selects one of
five rows from those. Keeping an imperfect measured result and handing its
misses forward is a first-class outcome
(:attr:`~.contracts.AdoptionOutcome.KEEP_FOR_ITERATION`); the hard stops are
reserved for the safety class and for evidence that does not exist.

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

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Iterable, Mapping, TypeVar

import numpy as np

from ..delta_probe import VERDICT_LEVEL_MISMATCH, VERDICT_MATCHED
from ..flat_spec import (
    FlatSpecReport,
    evaluate_flat_spec,
    spec_convergence_residual,
    spec_flatness_gauge,
)
from .contracts import (
    ADOPTION_ROW_KEEP,
    ADOPTION_ROW_KEEP_FOR_ITERATION,
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
    "MeasurementComparand",
    "REALIZATION_NO_COMPARATOR",
    "REALIZATION_NO_TRACKING",
    "REALIZATION_OUT_OF_TOLERANCE",
    "REALIZATION_WITHIN_TOLERANCE",
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
    "evaluate_realization",
    "evaluate_round_quality",
    "evaluate_spec",
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
# 5. evidence trust — the axis that gates the other two
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
      honest first pass. A test pins that permuting spec changes no adoption
      decision, and that pin is load-bearing.
    * *The spec verdicts available today are not yet honest enough to decide
      on.* They are computed over the raw 250 Hz-2 kHz band with **no
      intersection against the session's own trusted floor** (357.1 Hz on a 7 ms
      gate), so a best-of-N series keyed on them would rank rounds partly on
      sub-trusted-floor evidence the same session's delta probe already refuses
      to grade — and which the E4 sweep measured moving ~2 dB with gate length
      alone. That intersection is a separate filed fix, and it must land before
      any axis is allowed to DECIDE on a spec verdict. Carrying spec as
      disclosure costs nothing and inherits none of it.

    So a round can be PASSED with targets outstanding, and that is not a
    contradiction: the status answers "did this round realize and improve the
    speaker", the targets answer "what should the next one chase". They are
    different questions, and #2291's whole design is about not letting one
    answer stand in for another.
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

    return Verdict(quality, reason, {
        "targets": targets,
        "spec_bands": _failing_spec_bands(spec_report),
    })


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
# 8. adoption — the three axes, as a table
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


def decide_adoption(
    *,
    trust: Verdict[EvidenceTrust],
    safety: Verdict[SafetyStatus],
    quality: Verdict[QualityStatus],
    boosted: bool,
    rollback_available: bool,
    restore_failed: bool = False,
) -> AdoptionDecision:
    """Keep, keep-and-iterate, restore, or escalate — the #2537 table.

    **What this replaces, and why.** #2291's table was keyed on
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

    So the axes changed. Not "did we prove it helped", but three separable
    questions, each with its own evaluator above:

    * :func:`evaluate_evidence_trust` — did we measure the state we applied?
    * :func:`evaluate_applied_safety` — is that state safe to leave on?
    * :func:`evaluate_round_quality` — how good is it, and what is left to fix?

    The five rows, by their :data:`~.contracts.ADOPTION_ROWS` identifiers:

    ====================================== ================================
    row                                    outcome
    ====================================== ================================
    ``row1_trusted_safe_passed``           ``KEEP``
    ``row2_trusted_safe_missed``           ``KEEP_FOR_ITERATION``
    ``row3_unsafe``                        ``RESTORE``
    ``row4_untrusted_evidence``            ``RESTORE``
    ``row5_trusted_safe_regressed``        ``RESTORE``
    ====================================== ================================

    Args:
      trust / safety / quality: the three axis verdicts. **Verdicts, not bare
        statuses**, and that is a deliberate change from the pre-#2537
        signature. The reason a row fires under IS the deciding axis's own
        reason — "the boost realized above its declared bound", "the spec band
        is out of tolerance" — so taking statuses and minting a parallel cause
        vocabulary here would put a second owner on every one of them. This
        function still decides nothing an evaluator did not: it selects which
        axis speaks.
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
      realization that missed tolerance, an unprovable benefit under a boost, a
      spec band out of tolerance. Added: a boost measured above its declared
      bound, an uncommanded shift measured LOUDER than declared, a clipped
      capture, and every previously-``user_decision`` untrusted cell. Nothing
      that was a hard stop for a hazard stopped being one.
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
    """

    for name, value, kind in (
        ("trust", trust, EvidenceTrust),
        ("safety", safety, SafetyStatus),
        ("quality", quality, QualityStatus),
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
