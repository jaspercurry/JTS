# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Four independent verification verdicts, and adoption as data (#2291).

**The defect this module exists to make impossible.** On 2026-08-10 a jts3
round reported VERIFY tracking the model to within 1.291 dB (tolerance
1.5 dB) while the post-apply cloud failed all three spec bands, worst
+7.727 dB at 428.44 Hz — and the run read as *passed*. One overloaded
pass/fail let a realization answer stand in for an acoustic one. So the four
questions are computed by four functions here, each answering only its own,
and :func:`decide_adoption` combines their *statuses* — never their
internals — through a table it does not get to reinterpret.

The four, in the issue's words:

* **Capture validity** — was the post-apply capture usable at all?
* **Realization** — did the applied graph produce the change we commanded?
* **Benefit** — did the measured speaker get better than it was?
* **Spec** — is the result inside the target envelope?

Spec is an *outcome*, never a proxy for benefit: "realized, improved, and
still out of spec" is a valid keep, and "realized while the speaker
regressed" is a valid restore. Both fit in the data model because the
statuses are independent.

**This module invents no DSP and owns no threshold.** Every number it
reports is lifted from a shipped primitive:

* the benefit residual is
  :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` over
  :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` — the same
  pooled spec residual the pre-apply prediction gate already grades
  model-vs-model (``crossover_v2_flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB``,
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

**Nothing calls any of this yet.** #2291 Phase 3c wires these functions into
the journey/host: it computes the verdicts, logs their ``to_dict()``
payloads, and applies the decision. This module emits no logs, reads no
clock, touches no file, and imports neither :mod:`jasper.web` nor
:mod:`jasper.active_speaker.crossover_v2_flow` — same inputs, same outputs,
every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Iterable, Mapping, TypeVar

import numpy as np

from ..flat_spec import (
    FlatSpecReport,
    evaluate_flat_spec,
    spec_convergence_residual,
    spec_flatness_gauge,
)
from .contracts import (
    AdoptionDecision,
    AdoptionOutcome,
    BenefitStatus,
    CaptureValidity,
    CrossoverV2ContractError,
    RealizationStatus,
    ResponseCurve,
    SpecStatus,
    VerificationResult,
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
    "MeasurementComparand",
    "REALIZATION_NO_COMPARATOR",
    "REALIZATION_NO_TRACKING",
    "REALIZATION_OUT_OF_TOLERANCE",
    "REALIZATION_WITHIN_TOLERANCE",
    "SPEC_BAND_OUT_OF_TOLERANCE",
    "SPEC_IN_TOLERANCE",
    "SPEC_NO_EVALUABLE_BAND",
    "SPEC_NO_REPORT",
    "SPEC_PARTIAL_COVERAGE",
    "TRACKING_COMPARATOR_KEY",
    "Verdict",
    "decide_adoption",
    "evaluate_benefit",
    "evaluate_capture_validity",
    "evaluate_realization",
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
    """

    status: StatusT
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "evidence": dict(self.evidence),
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
    sibling instrument for this same question, and it is deliberately **not**
    joined in here: two instruments need a precedence policy, and no defect
    has yet located what that policy should be. Phase 3c can route its
    verdict in when one has been. Naming the cut rather than inventing the
    policy.
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
# 5. adoption — the issue's table, as data
# --------------------------------------------------------------------------


class _Intent(str, Enum):
    """What the evidence says should happen to the applied graph.

    One step short of an :class:`~.contracts.AdoptionOutcome`: the table
    below reads only the two graded statuses, and the modifiers that follow
    (is it boosted, can we actually restore) turn an intent into an outcome.
    Separating them is what lets the table stay a literal transcription of
    the issue.
    """

    KEEP = "keep"
    RESTORE = "restore"
    UNPROVEN = "unproven"


#: #2291's adoption table, transcribed. Every (realization, benefit) pair
#: exists here — all nine — so a combination cannot fall through to a
#: default, and a new enum member fails the exhaustiveness test rather than
#: silently landing on one. Read against the issue's rows:
#:
#: * ``matched | improved | pass or fail | keep`` — the one keep row. Spec is
#:   "pass or fail", which is why spec is not a key: "improved but out of
#:   spec" is an honest first pass.
#: * ``failed | any | any | restore`` — all three ``FAILED`` rows.
#: * ``matched or unavailable | clearly regressed | any | restore``.
#: * ``unavailable | indeterminate | any | do not claim success``.
#: * ``any | indeterminate ... | any | do not claim success`` — so every
#:   indeterminate row is UNPROVEN, including ``matched``.
#:
#: ``unavailable | improved`` is UNPROVEN because the keep row names
#: ``matched`` and this table does not widen it: the speaker measured
#: better, but with no realization evidence the round cannot say the graph
#: it applied is why. Not claiming success is the conservative reading of a
#: table whose whole purpose is to stop success being claimed.
_ADOPTION_INTENTS: Mapping[tuple[RealizationStatus, BenefitStatus], _Intent] = {
    (RealizationStatus.MATCHED, BenefitStatus.IMPROVED): _Intent.KEEP,
    (RealizationStatus.MATCHED, BenefitStatus.REGRESSED): _Intent.RESTORE,
    (RealizationStatus.MATCHED, BenefitStatus.INDETERMINATE): _Intent.UNPROVEN,
    (RealizationStatus.UNAVAILABLE, BenefitStatus.IMPROVED): _Intent.UNPROVEN,
    (RealizationStatus.UNAVAILABLE, BenefitStatus.REGRESSED): _Intent.RESTORE,
    (RealizationStatus.UNAVAILABLE, BenefitStatus.INDETERMINATE): _Intent.UNPROVEN,
    (RealizationStatus.FAILED, BenefitStatus.IMPROVED): _Intent.RESTORE,
    (RealizationStatus.FAILED, BenefitStatus.REGRESSED): _Intent.RESTORE,
    (RealizationStatus.FAILED, BenefitStatus.INDETERMINATE): _Intent.RESTORE,
}

ADOPTION_REALIZED_AND_IMPROVED = "realized_and_improved"
ADOPTION_REALIZATION_FAILED = "realization_failed"
ADOPTION_MEASURED_REGRESSION = "measured_regression"
ADOPTION_UNPROVEN = "benefit_unproven"
ADOPTION_UNPROVEN_BOOST = "unproven_boost_failed_closed"
ADOPTION_RESTORE_FAILED = "restore_failed"
ADOPTION_NO_ROLLBACK_ANCHOR = "restore_required_without_rollback_anchor"

#: The reason each restoring intent states, so a receipt says *why* the
#: graph came off rather than only that it did.
_RESTORE_REASONS: Mapping[tuple[RealizationStatus, BenefitStatus], str] = {
    (RealizationStatus.MATCHED, BenefitStatus.REGRESSED): ADOPTION_MEASURED_REGRESSION,
    (
        RealizationStatus.UNAVAILABLE,
        BenefitStatus.REGRESSED,
    ): ADOPTION_MEASURED_REGRESSION,
}


def decide_adoption(
    *,
    realization: RealizationStatus,
    benefit: BenefitStatus,
    spec: SpecStatus,
    boosted: bool,
    rollback_available: bool,
    restore_failed: bool = False,
) -> AdoptionDecision:
    """Keep, restore, ask, or escalate — #2291's table plus two modifiers.

    Args:
      realization: the realization verdict's status.
      benefit: the measured-benefit verdict's status.
      spec: the spec verdict's status. **Deliberately not a factor** — every
        row of the issue's table reads "any" for spec, because spec is an
        outcome and not a proxy for benefit. It is a required argument so a
        caller must state it (and so a receipt carries it), and a test pins
        that permuting it changes no decision. If it ever *should* matter,
        that is a table change with evidence attached, not a quiet read.
      boosted: does the applied intervention contain a boost? Computed by the
        host with the shipped predicate (``camilla_yaml._linearization_has_boost``
        — any filter whose numeric ``gain`` exceeds 0 dB); this module keeps
        no second copy of that rule.
      rollback_available: can the host actually restore the entry graph?
        The flow's capability idiom is seam presence (``V2FlowSeams.rollback
        is not None``), and #2291 Phase 3c binds it in the stage that runs
        VERIFY — today only stage 1 binds it and only stage 2 reaches the
        probe, which is the wiring defect this parameter exists to let the
        decision *see* rather than assume away.
      restore_failed: a restore was attempted and did not complete.

    Safety, stated plainly because this decides whether a graph stays on a
    speaker:

    * **A realization pass never overrides a benefit regression.** The
      ``(MATCHED, REGRESSED)`` cell restores. That cell is the 2026-08-10
      shape, and it is the reason this table is keyed on both statuses
      instead of short-circuiting on a tracking pass.
    * **Indeterminate never claims success.** Every ``INDETERMINATE`` cell is
      UNPROVEN, which can only become ``user_decision``, ``restore``, or
      ``recovery_required`` — never ``keep``.
    * **A boosted intervention fails closed when unproven.** An unverified
      *cut* can wait for a household to decide; an unverified *boost* is
      energy we put into a driver and cannot show was warranted, so it comes
      off. This is why ``boosted`` promotes UNPROVEN to a restore rather
      than leaving it a question.
    * **A restore we cannot perform is not a restore.** When the evidence
      says the graph must come off and no rollback anchor exists, the answer
      is ``recovery_required`` — a loud operator path — never a ``restore``
      the host has no way to execute and never a ``keep``. That is also
      #2291's "the first-ever correction round refuses to apply if no valid
      rollback anchor exists", answered after the fact rather than before it.
    * **A failed restore outranks everything.** Checked first: the speaker is
      then in neither the entry graph nor the intended one, and no later row
      can describe that better.
    """

    for name, value, kind in (
        ("realization", realization, RealizationStatus),
        ("benefit", benefit, BenefitStatus),
        ("spec", spec, SpecStatus),
    ):
        if not isinstance(value, kind):
            raise CrossoverV2ContractError(f"{name} must be a {kind.__name__}")

    if restore_failed:
        return AdoptionDecision(
            outcome=AdoptionOutcome.RECOVERY_REQUIRED, reason=ADOPTION_RESTORE_FAILED
        )

    intent = _ADOPTION_INTENTS[(realization, benefit)]
    if intent is _Intent.KEEP:
        return AdoptionDecision(
            outcome=AdoptionOutcome.KEEP, reason=ADOPTION_REALIZED_AND_IMPROVED
        )
    if intent is _Intent.RESTORE:
        reason = _RESTORE_REASONS.get(
            (realization, benefit), ADOPTION_REALIZATION_FAILED
        )
        return _restore_or_recover(reason, rollback_available=rollback_available)
    if boosted:
        return _restore_or_recover(
            ADOPTION_UNPROVEN_BOOST, rollback_available=rollback_available
        )
    return AdoptionDecision(
        outcome=AdoptionOutcome.USER_DECISION, reason=ADOPTION_UNPROVEN
    )


def _restore_or_recover(
    reason: str, *, rollback_available: bool
) -> AdoptionDecision:
    """A restore the host can run, or the escalation when it cannot.

    Mirrors the shipped rule at the flow's delta-probe rollback seam, which
    already reports the verdict's own reason when the graph came off and
    ``correction_rollback_failed`` when it did not. Here the branch is one
    step earlier — before the attempt rather than after it — because a
    decision to restore with no anchor to restore *to* is not a decision the
    host can carry out.
    """

    if rollback_available:
        return AdoptionDecision(outcome=AdoptionOutcome.RESTORE, reason=reason)
    return AdoptionDecision(
        outcome=AdoptionOutcome.RECOVERY_REQUIRED,
        reason=f"{ADOPTION_NO_ROLLBACK_ANCHOR}:{reason}",
    )
