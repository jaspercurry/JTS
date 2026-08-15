# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two measurements one correction round compares, and the margin (#2291).

#2291 Phase 3c's half of the honest loop that is neither a verdict nor a
capture: turning **one summed capture** into one side of a before→after
comparison, and holding the one number that says how big a difference has to
be before the round is allowed to call it a change.

The defect this closes is stated in the issue as a missing measurement, not a
missing verdict. The 2026-08-10 jts3 round retained CHECK/MEASURE, the lateral
candidate-selection walk, VERIFY, and five post-apply cloud positions — and
still could not say whether the speaker got better, because none of that
evidence was *the same summed acoustic question, at the same program and level,
before and after the graph change*. So Phase 3c adds exactly one capture
(``PHASE_ENTRY_BASELINE``, at the measurement mark, immediately before apply,
replaying the VERIFY program) and this module is what makes that capture and
the post-apply VERIFY comparable to each other.

**What this module does, and the one decision it makes.** It reduces a
:class:`~jasper.audio_measurement.program_analysis.ProgramAnalysis` to a
:class:`~.verification.MeasurementComparand` — the shape
:func:`~.verification.evaluate_benefit` grades — and, when it holds both sides,
it **unions their exclusion masks**. That union is the one judgement call here,
and :func:`~.verification.evaluate_benefit` names it as the caller's to make:
equal masks mean equal graded bins by construction, so the residual cannot fall
merely because the honesty screen grew. Union rather than intersection because
a bin either capture could not trust is a bin neither comparison should be
graded on.

Everything else is deferred:

* **comparability** is the evaluator's, and it is answered by ``program_id``
  equality — a SHA-256 over the whole excitation schedule including every
  segment's ``gain_db`` and ``effective_peak_dbfs``
  (:func:`jasper.audio_measurement.program._program_id`). Two captures that
  share it share the program *and* the level, cryptographically. That is why
  the entry baseline must be built by the VERIFY program builder rather than by
  a lookalike: equality of that id is the whole comparability check.
* **the curve reduction** is the shipped owners', in the shipped order
  (:func:`~jasper.audio_measurement.spatial_combine.decimate_curve_to_analysis_grid`
  then :func:`~jasper.audio_measurement.analysis.smooth_fractional_octave` at
  the spec fraction) — the same two steps, same owners,
  ``crossover_v2_flow.spec_report_for_predicted_sum`` already applies to put one
  curve in front of :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`.
* **the verdict** is :mod:`.verification`'s. Nothing here decides improved,
  regressed, or indeterminate.

**What the mask here is, and is not.** It is this capture's own
gate-validity clamp — bins below ``DriverResponse.validity_floor_hz``, which
are an artifact of a truncated gate window rather than a measurement. It is
*not* the spatial cloud's interference screen
(:func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
over a combined group), because a single at-the-mark capture is not a group and
there is nothing to screen across. So a residual computed here is optimistic
against the cloud's absolute spec verdict. That does not weaken the *difference*
this module exists to support: both sides are screened the same way and then
share one union, and the difference of two same-frame residuals is the claim.
The absolute spec answer stays the cloud's, which is exactly the separation
:func:`~.verification.evaluate_spec` and :func:`~.verification.evaluate_benefit`
keep apart.

No ``jasper.web`` import, no filesystem, no clock: the host supplies the graph
fingerprint, the mark, and the timestamp, and the host persists the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import numpy as np

from ..flat_spec import FlatSpecReport, evaluate_flat_spec, spec_convergence_residual
from .contracts import (
    AdoptionDecision,
    BenefitStatus,
    CaptureValidity,
    CrossoverV2ContractError,
    EvidenceTrust,
    QualityStatus,
    RealizationStatus,
    ResponseCurve,
    RoundReceipt,
    SafetyStatus,
    SpecStatus,
    VerificationResult,
    _text,
)
from .verification import MeasurementComparand, Verdict

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Same rule as :mod:`.verification`: ``program_analysis`` is a 5,500-line
    # scipy-backed module, and a reduction helper should not drag it into every
    # import of the package. Only three attributes are read at runtime.
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

__all__ = [
    "BENEFIT_CURVE_MAX_BINS",
    "ENTRY_BASELINE_KIND",
    "MEASURED_BENEFIT_MARGIN_DB",
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

#: How much flatter the speaker must measure before the round may say so.
#:
#: **Provisionally equal to
#: :func:`~jasper.active_speaker.attempts_loop.material_improvement_db`
#: (0.5 dB), and deliberately NOT that constant.** They answer different
#: questions and only happen to agree today:
#:
#: * ``material_improvement_db`` is model-vs-hardware error — "the gap between
#:   what the correction model predicts and what the hardware realizes on
#:   JTS3", in its own words. It bounds a *prediction*.
#: * this bounds two *measurements* of the same speaker taken minutes apart
#:   through the same program, mic, and analyzer. That is capture
#:   repeatability, and it is a strictly different quantity — it could be much
#:   smaller (same graph, same room, same mic, one program apart) or much
#:   larger (a mic nudged between the two captures).
#:
#: Inheriting the model number silently would have been the cheaper edit and
#: the dishonest one: the margin decides whether a measured regression is
#: "clear" enough to pull a graph off a household's speaker, and a threshold
#: whose justification is about something else is a threshold nobody can
#: defend when it fires.
#:
#: **What the repository HAS measured, and what it has not.** The 2026-07-31
#: fixed-mic repeat study on jts3 is a real capture-to-capture study of exactly
#: the right shape — one unchanged applied profile, the UMIK-2 bolted in place
#: and never moved, sixteen back-to-back captures at the shipped operating
#: point, pushed through the production analyzer. Its numbers are banked
#: in-repo at ``tests/test_active_speaker_attempts_loop.py`` (``BANKED_P95_DB``
#: 0.08508, ``BANKED_MEDIAN_DB`` 0.05183) and summarized in
#: :mod:`~jasper.active_speaker.attempts_loop`'s header. Two things follow:
#:
#: * **It grades a different metric.** Those are consecutive-pair deviations of
#:   ``max_db_notch_excluded``, not of the pooled spec residual this margin
#:   bounds — the same mismatch
#:   :func:`~jasper.active_speaker.attempts_loop.decide_next` refuses a
#:   comparison over (``REASON_FLOOR_METRIC_MISMATCH``). The replay CLI that
#:   adopts floors says so in as many words: *no repeat study has measured a
#:   floor for this metric.*
#: * **The nearest pooled numbers say 0.5 dB is conservative, not calibrated.**
#:   Two back-to-back post-apply pairs about a minute apart agreed on the
#:   pooled scalar to 0.040 dB and 0.006 dB, against a per-bin floor of roughly
#:   0.15 dB rms. So this margin sits an order of magnitude above the noise it
#:   is guarding against — which errs the safe way (a real small improvement is
#:   called :attr:`~.contracts.BenefitStatus.INDETERMINATE` rather than noise
#:   being called an improvement) but does mean the round will decline to claim
#:   wins it could honestly claim. The house rule for turning a measured p95
#:   into a claim floor is
#:   :data:`~jasper.active_speaker.attempts_loop.CLAIM_FLOOR_P95_MULTIPLE`
#:   (2×); applied to that study's pooled-rms arm it would land near 0.05 dB.
#:
#: **TODO(#2291, Definition-of-Done hardware run — owner: the jts3 pass on the
#: DoD runbook):** capture the same at-the-mark VERIFY program twice in a row
#: against an unchanged graph, difference the two pooled
#: :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` values,
#: repeat, and set this to 2× that distribution's p95 through the same house
#: rule. Until that run exists this value is an assumption wearing its own
#: name, which is the point of the name.
MEASURED_BENEFIT_MARGIN_DB = 0.5

#: Resolution of both sides of the benefit comparison.
#:
#: The entry baseline crosses the durable stage bridge (stage 1 captures it,
#: stage 2 grades against it), so it is bounded like every other curve that
#: does — this is the same 512 the host applies to ``verify_priors.
#: predicted_sum`` and ``verify_priors.commanded_delta``, and for the same
#: reason. It is named here rather than imported because
#: :mod:`jasper.web.correction_crossover_v2` is the wrong direction for this
#: package to import, and because this ceiling governs *both* sides of a
#: comparison rather than one side's persistence budget: the post-apply curve
#: is reduced to it too, so the two land on one grid by construction instead of
#: by luck.
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
    known until both sides are in hand (see :func:`benefit_comparands`). This
    type carries the capture's OWN screen, which is the input to that union.
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
    no summed response, a degenerate curve. The evaluator turns a ``None`` side
    into :attr:`~.contracts.BenefitStatus.INDETERMINATE` with a reason naming
    which side was missing, which is the answer — a comparison with one side is
    not a comparison.

    The two reduction steps and their order are
    ``crossover_v2_flow.spec_report_for_predicted_sum``'s, not this module's:
    block-average onto the analysis grid first (the smoother is an
    O(bins x window) Python loop and a raw grid costs seconds on a Pi), then
    1/3-octave smooth. Doing it in the other order, or with a different
    smoothing fraction, would produce a curve
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` grades
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
        # crash to propagate into a household decision — the same bounded
        # family, for the same reason, as ``spec_report_for_predicted_sum``.
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
    below ``gating.f_valid_floor_hz`` the response is an artifact of a
    truncated gate window, so those bins must not decide a verdict either way.
    An absent or non-finite floor screens nothing — "no evidence of a floor" is
    not "the floor is at zero", and over-screening would shrink the graded
    denominator this comparison depends on.
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

    Persisted by the host between stage 1 (which captures it, immediately
    before the household applies) and stage 2 (which grades against it), so it
    is JSON-shaped by construction. The graph fingerprint travels with it
    because the receipt's whole job is to let a *later* round bind the
    currently-active profile as its own entry graph: a baseline curve with no
    record of which graph produced it cannot do that.
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

        ``None`` rather than a raise, and rather than a partially-trusted
        record: a state file from a build before this key shipped, a truncated
        write, and a hand-edited file all mean the same thing to the round —
        there is no comparable baseline — and that already has an honest
        verdict (:data:`~.verification.BENEFIT_BASELINE_UNAVAILABLE`). Length
        agreement between the two curve arrays and the mask is checked here
        because :class:`~.verification.MeasurementComparand` would otherwise
        raise on it inside a verdict computation.
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
    assembly decision :func:`~.verification.evaluate_benefit` explicitly leaves
    to its caller, taken here once so no other surface may take it differently.
    A bin either capture could not trust is dropped from both, which keeps the
    denominators identical and the residual difference honest.

    Comparability is NOT decided here. A program, mark, or grid disagreement is
    passed straight through to the evaluator, which names which one broke; a
    caller that silently resampled one side onto the other's grid would be
    manufacturing the comparability the round is supposed to be checking. So
    when the grids differ the two per-capture masks ride unchanged and the
    evaluator returns
    :data:`~.verification.BENEFIT_GRID_MISMATCH`.
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
    collapsed :class:`~.contracts.VerificationResult` on purpose: the contract
    carries the four *statuses*, and the verdicts carry the numbers each one
    was read from. The host logs the verdicts (so a support read can see WHY,
    not only WHAT) and puts the contract on the receipt.
    """

    capture: Verdict[CaptureValidity]
    realization: Verdict[RealizationStatus]
    benefit: Verdict[BenefitStatus]
    spec: Verdict[SpecStatus]
    result: VerificationResult
    #: The three axes :func:`~.verification.decide_adoption` read (#2537),
    #: kept beside the four verdicts they compose for exactly the same reason:
    #: the adoption decision carries the row and the deciding axis's reason,
    #: and these carry the numbers all three were read from.
    trust: Verdict[EvidenceTrust]
    safety: Verdict[SafetyStatus]
    quality: Verdict[QualityStatus]
    adoption: AdoptionDecision
    #: The post-apply pooled spec residual, lower-is-better, or ``None``.
    #: Separated out because it has a second consumer: it is the acoustic
    #: grade :func:`~jasper.active_speaker.attempts_loop.decide_next` differences
    #: across attempts. One computation, two readers — the benefit verdict and
    #: the attempts ledger — rather than two that can disagree.
    post_residual_db: float | None = None
    post_residual_bins: int | None = None

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
        }

    def axes(self) -> dict[str, Any]:
        """The three adoption axes alone, for the receipt's ``round_axes``.

        Separated from :meth:`to_dict` because the receipt carries these and
        not the four verdicts underneath them: the verdicts already ride on the
        receipt's ``verification`` field, and a receipt with both would state
        the same statuses twice.
        """

        return {
            "trust": self.trust.to_dict(),
            "safety": self.safety.to_dict(),
            "quality": self.quality.to_dict(),
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
) -> RoundEvaluation:
    """Grade one round: the four questions, the three axes, then the table.

    The composition #2291 Phase 3b left for its caller, taken here once so
    every surface that grades a round grades it the same way. It decides
    nothing the evaluators do not — the table is
    :func:`~.verification.decide_adoption`'s, the four answers are the four
    functions', and this adds only the ordering between them.

    **An unusable capture short-circuits, rather than being graded and then
    overwritten.** :func:`~.verification.verification_result` already collapses
    the other three statuses when the capture is unusable — but computing them
    first and discarding them would leave the *logged* evidence claiming a
    benefit that no usable capture supports. So the other three are not
    computed at all: they return their no-evidence verdicts carrying the
    capture's own reason, and what is logged is what was actually decided.

    ``spec_report`` is the post-apply spatial cloud's report when the tier
    produced one, and ``None`` otherwise (#2160's honest wire: the failure
    that used to be disclosure-only is now the SPEC verdict's input). It
    **cannot change the adoption** — spec is "any" in every row of #2291's
    table, and #2537 kept it that way for a second reason as well (the
    trusted-floor intersection; see
    :func:`~.verification.evaluate_round_quality`). What #2537 added is that
    each failing band rides into the receipt as a next-round target, which is
    disclosure and not a gate.

    Args:
      post_analysis: the post-apply VERIFY capture's analysis.
      entry_baseline: the pre-apply capture, rehydrated across the stage
        bridge. ``None`` means the round has no comparable "before" and the
        benefit is :attr:`~.contracts.BenefitStatus.INDETERMINATE` — which is
        what every round before this one silently was.
      tracking: ``ProgramAnalysis.verify_tracking``, the realization
        comparator's home.
      realization_tolerance_db: the flow's shipped ``VERIFY_TOLERANCE_DB``.
        Passed rather than imported so this module holds no threshold the
        flow already owns.
      reference_mark: the position identity both captures were taken at.
      boosted / rollback_available / restore_failed: the adoption modifiers;
        see :func:`~.verification.decide_adoption` for each one's scope.
      delta_probe: the round's
        :class:`~jasper.active_speaker.delta_probe.DeltaProbeMap`, or ``None``
        when the session never ran one (#2537). It is an *optional* input and
        an absent one is not a hazard: see
        :func:`~.verification.evaluate_applied_safety` for why an ungraded
        probe reports no finding rather than an unsafe one, and
        :func:`~.verification.evaluate_round_quality` for why an ungraded probe
        still cannot let a round call itself PASSED.
      margin_db: defaults to this module's own
        :data:`MEASURED_BENEFIT_MARGIN_DB` — the owner supplying its own
        constant, so there is no call site free to pass a different bar.
    """

    from .verification import (
        decide_adoption,
        evaluate_applied_safety,
        evaluate_benefit,
        evaluate_capture_validity,
        evaluate_evidence_trust,
        evaluate_realization,
        evaluate_round_quality,
        evaluate_spec,
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
        post_residual = (
            None
            if after is None
            else _post_residual(after, benefit_reason=benefit.reason)
        )

    result = verification_result(
        capture=capture, realization=realization, benefit=benefit, spec=spec
    )
    # The three axes are composed from the FOUR VERDICT OBJECTS, not from
    # ``result``'s collapsed statuses (#2537): a row's reason is the deciding
    # axis's reason, and ``result`` keeps only the statuses plus one joined
    # string. Reading it here would force the axes to re-derive a cause the
    # verdicts already carry.
    trust = evaluate_evidence_trust(capture=capture, realization=realization)
    safety = evaluate_applied_safety(probe=delta_probe, integrity=integrity)
    quality = evaluate_round_quality(
        realization=realization, benefit=benefit, spec=spec,
        probe=delta_probe, spec_report=spec_report,
    )
    adoption = decide_adoption(
        trust=trust,
        safety=safety,
        quality=quality,
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
        adoption=adoption,
        post_residual_db=None if post_residual is None else post_residual[0],
        post_residual_bins=None if post_residual is None else post_residual[1],
    )


def _post_residual(
    post: MeasurementComparand, *, benefit_reason: str
) -> tuple[float, int] | None:
    """The post side's own pooled residual, for the attempts ledger.

    Deliberately independent of whether a *comparison* was possible: the
    attempts loop differences consecutive ATTEMPTS, not this round's before
    and after (its kernel reads only the last two entries, because a fixed
    early baseline accumulates drift comparable to the measurement floor
    within ~15 attempts). So a round with no comparable entry baseline still
    has an honest acoustic grade to bank — the benefit verdict is
    indeterminate, and the ledger is not.

    Computed through the same shipped evaluator the benefit verdict uses, so
    the two numbers cannot disagree about what "the post-apply residual" is.
    """

    del benefit_reason  # the reason belongs to the verdict, not to the grade
    try:
        report = evaluate_flat_spec(
            np.asarray(post.curve.hz, dtype=np.float64),
            np.asarray(post.curve.db, dtype=np.float64),
            np.asarray(post.exclusion_mask, dtype=bool),
        )
    except ValueError:
        return None
    residual = spec_convergence_residual(report)
    if not residual.evaluable or residual.rms_db is None:
        return None
    return float(residual.rms_db), int(residual.n_bins)


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
) -> RoundReceipt:
    """Assemble #2291's immutable round receipt from the round's own facts.

    A named assembler rather than a host-side literal, for the reason the
    contract exists at all: a receipt built by hand at a call site is a
    receipt whose field set can quietly diverge from the issue's list.

    **Identities, not payloads.** ``entry_baseline`` on the receipt is the
    baseline's *identity* — program, mark, graph fingerprint, when, and the
    retained artifact it can be re-read from — not its 512-point curve. The
    curve already lives in two places that outlive this record (the retained
    capture and the durable ``verify_priors``), and copying it here would make
    the receipt large, make its fingerprint move with a decimation change, and
    give the curve a third owner. Same rule for ``post_measurement``.

    The commanded delta has no field of its own on
    :class:`~.contracts.RoundReceipt`; its identity rides in
    ``evidence_identities``, which is where the issue's "evidence/version
    identities" belong.  So does the applied candidate's fingerprint, since
    #2392 moved ``proposal_fingerprint`` off it: the caller puts it there, and
    the reason it must is that the receipt would otherwise carry no candidate
    identity at all once the proposal took that field over.

    ``proposal_fingerprint_kind`` is passed through unvalidated on purpose —
    :class:`~.contracts.RoundReceipt` owns the closed vocabulary
    (:data:`~.contracts.PROPOSAL_FINGERPRINT_KINDS`) and re-checking it here
    would be a second owner of the same rule.
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
        # The three axes the row was read off (#2537) — banked with the
        # decision, because a receipt saying "keep, and here is what to fix"
        # is only actionable to the NEXT round if the targets travel with it.
        round_axes=evaluation.axes(),
        restore_result=restore_result,
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
