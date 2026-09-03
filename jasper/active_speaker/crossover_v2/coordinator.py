# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round coordinator: grade a round, act on the adoption table, restore if
the table says restore, and bank the receipt.

The module in the round tail that changes the speaker — it calls seams that act. It
holds no session state (every input an argument, every output a return value),
reaches no host object beyond :class:`RoundPorts`, and owns no household
vocabulary: a refusal leaves as a :class:`RoundRefusal` kind the flow maps to a
:mod:`.refusal_copy` code."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES,
)
from jasper.log_event import log_event

from .contracts import ENTRY_GRAPH_FINGERPRINT_UNKNOWN, AdoptionOutcome
from .journey import PHASE_VERIFY
from .round_evidence import (
    EntryBaseline,
    RoundEvaluation,
    build_round_receipt,
    evaluate_round,
)
from .verification import FlatnessObjectives, decide_adoption

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import ProgramAnalysis
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        AlignmentPrescription,
    )
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        TopologyPrescription,
    )
    from jasper.active_speaker.flat_spec import FlatSpecReport, GradedSpec

logger = logging.getLogger(__name__)

#: The refusal kinds :func:`run_round` returns. The reason code and its
#: household sentence belong to :mod:`.refusal_copy`'s ``REASON_REGISTRY``.
REFUSAL_RESTORED = "restored"
REFUSAL_ROLLBACK_FAILED = "rollback_failed"

#: Every kind above, so the flow's mapping can be checked for completeness.
REFUSAL_KINDS = frozenset({REFUSAL_RESTORED, REFUSAL_ROLLBACK_FAILED})

#: The exception family four of the five seam calls are guarded against: losing
#: the round's verdict is worse than reporting it with the seam marked
#: unavailable. :func:`entry_graph_fingerprint` deliberately omits
#: ``AttributeError`` from its own guard — it fills provenance, never a gate.
_SEAM_ERRORS = (
    OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError,
)


# --- ports: the five host capabilities a round needs ---


@dataclass(frozen=True)
class RoundPorts:
    """The seams a round calls, and nothing else the host can do.

    Each is optional and each absence has a decided answer in the readers
    below; ``None`` never means "skip the question".
    """

    #: Put the previous graph back. Takes the adoption reason, returns success.
    rollback: Callable[[str], bool] | None = None
    #: Is a prior candidate recorded to go back to? (the state half of "can we
    #: restore")
    rollback_available: Callable[[], bool] | None = None
    #: Does the APPLIED intervention put energy in? (the applied-profile SSOT)
    applied_boosts: Callable[[], bool] | None = None
    #: Name the graph a capture was measured through.
    entry_graph_fingerprint: Callable[[], str] | None = None
    #: Bank the receipt; returns the stored artifact's fingerprint.
    publish_round_receipt: Callable[[Mapping[str, Any]], str] | None = None


# --- seam readers: each states its own fail direction ---


def applied_boosts(ports: RoundPorts, *, session_id: str) -> bool:
    """Does the applied intervention put energy IN?

    Asked of the HOST, never of a session's own state: the stage that grades a
    round is a fresh process with no candidate of its own. Fails CLOSED at both
    levels — an unbound seam and a raising one both answer "boosted".
    """
    seam = ports.applied_boosts
    if seam is None:
        return True
    try:
        return bool(seam())
    except _SEAM_ERRORS:
        log_event(
            logger, "correction.crossover_v2_round_boost_unreadable",
            level=logging.WARNING, session_id=session_id, exc_info=True,
        )
        return True


def rollback_available(ports: RoundPorts, *, session_id: str) -> bool:
    """Can this host actually put the previous sound back?

    BOTH-AND: the ``rollback`` seam is bound (a process fact) AND a prior
    candidate is recorded (a state fact). Either half alone answers
    :func:`~.verification.decide_adoption`'s question wrongly. Fails closed on
    both halves, which routes the adoption table to ``recovery_required``.
    """
    if ports.rollback is None:
        return False
    seam = ports.rollback_available
    if seam is None:
        return False
    try:
        return bool(seam())
    except _SEAM_ERRORS:
        log_event(
            logger, "correction.crossover_v2_rollback_available_failed",
            level=logging.WARNING, session_id=session_id, exc_info=True,
        )
        return False


def entry_graph_fingerprint(ports: RoundPorts, *, session_id: str) -> str:
    """Which graph this capture was measured through, or the unknown word.

    Never raises and never gates — see :data:`ENTRY_GRAPH_FINGERPRINT_UNKNOWN`.
    """
    seam = ports.entry_graph_fingerprint
    if seam is None:
        return ENTRY_GRAPH_FINGERPRINT_UNKNOWN
    try:
        value = str(seam() or "")
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        log_event(
            logger, "correction.crossover_v2_entry_graph_fingerprint_failed",
            level=logging.WARNING, session_id=session_id, exc_info=True,
        )
        return ENTRY_GRAPH_FINGERPRINT_UNKNOWN
    value = value.strip()
    return value or ENTRY_GRAPH_FINGERPRINT_UNKNOWN


def _post_measurement_identity(
    analysis: "ProgramAnalysis | None", *, reference_mark: str, phase: str,
) -> dict[str, Any] | None:
    """What the post-apply side WAS, as identity rather than payload."""
    if analysis is None:
        return None
    return {
        "program_id": str(getattr(analysis, "program_id", "") or ""),
        "reference_mark": reference_mark,
        "phase": phase,
    }


# --- the round's inputs and its answer ---


@dataclass(frozen=True)
class RoundEvidence:
    """Everything one round is graded from, as it stood when grading began."""

    session_id: str
    #: The instrument this session ran, for the receipt's evidence identities.
    tier: str
    #: The post-apply VERIFY capture — the round's "after".
    post_analysis: "ProgramAnalysis | None"
    #: The pre-apply summed sweep at the same mark — the round's "before".
    entry_baseline: EntryBaseline | None
    #: The post-apply spatial cloud's spec report, or ``None`` on a tier that
    #: walks no cloud (which the evaluator reads as "no report", not a pass).
    spec_report: "FlatSpecReport | None"
    #: What this round proposed, for the receipt: the
    #: :class:`~.contracts.InterventionProposal`'s fingerprint whenever the
    #: session has one, and the applied candidate's when it does not.
    proposal_fingerprint: str
    #: Did stage 1's commanded delta cross the bridge? (identity, not payload)
    commanded_delta_present: bool
    #: The realization tolerance the round grades against.
    realization_tolerance_db: float
    #: The mark both captures were taken at.
    reference_mark: str
    #: Which of the two the field above is
    #: (:data:`~.contracts.PROPOSAL_FINGERPRINT_KINDS`).
    proposal_fingerprint_kind: str
    #: The applied candidate's own fingerprint, for the receipt's evidence
    #: identities.
    candidate_fingerprint: str
    #: The round's :class:`~jasper.active_speaker.delta_probe.DeltaProbeMap`,
    #: or ``None`` when the session ran none. Feeds
    #: :func:`~.verification.evaluate_applied_safety`, the adoption table's
    #: only hard stop, so it is stated rather than defaulted.
    delta_probe: Any | None
    #: 1-based position of this round in the household's flattening series —
    #: what :data:`~.round_evidence.ROUND_SERIES_CAP` is checked against.
    round_ordinal: int
    #: What the PREVIOUS round of this series measured on the two objectives,
    #: read off the receipt that round banked — ``None`` for the first round.
    previous_objectives: FlatnessObjectives | None
    #: The trusted floor THIS round's objectives were graded against, and the
    #: one the previous round's were. Together they let the headroom axis
    #: refuse a cross-floor movement comparison instead of reading a
    #: gate-length artefact as progress. ``None`` is "no evidence the frame
    #: moved", which withholds a refusal rather than fabricating a number.
    trusted_floor_hz: float | None = None
    previous_trusted_floor_hz: float | None = None
    #: Which epoch of the ordinal sequence ``round_ordinal`` counts in — see
    #: :attr:`SeriesPosition.ordinal_epoch`, where it is resolved. ``0`` means
    #: no reset recorded.
    round_ordinal_epoch: int = 0
    #: The post-apply cloud's per-position residuals, role-labelled, as
    #: JSON-shaped rows. Empty on a tier that walks no post-apply cloud.
    position_residuals: tuple[Mapping[str, Any], ...] = ()
    #: The post-apply cloud's flat-spec evaluation with its graded curve and
    #: MERGED honesty mask — the blend correction's evidence. ``None`` on a
    #: tier that walks no post-apply cloud, and absent evidence prescribes
    #: nothing.
    graded_spec: "GradedSpec | None" = None
    #: The blend correction the post-apply capture actually rode, read off the
    #: APPLIED candidate. ``None`` is "could not be established" and refuses to
    #: prescribe; ``()`` is "it rode none", which every first round honestly
    #: is. The two are not interchangeable: an assumed-empty incumbent
    #: double-counts the correction the capture was taken through.
    applied_blend_correction: tuple[Mapping[str, Any], ...] | None = None
    #: The region residual the PREVIOUS round of this series read, or ``None``
    #: for the first round — absent only ever lets the loop keep prescribing.
    previous_blend_residual_db: float | None = None
    #: The inter-driver delay PRESCRIPTION this round's candidate was built
    #: from, or ``None`` for a round whose delay the aligner chose on its own.
    #: Banked verbatim as provenance, never graded here.
    alignment_prescription: "AlignmentPrescription | None" = None
    #: WHICH commitment produced the round's delay
    #: (:data:`~jasper.audio_measurement.program_analysis.ALIGNMENT_COMMITMENTS`),
    #: or ``""`` when no candidate was committed. A reachable rail (an
    #: ``ALIGNMENT_OK`` estimate with no scorable band) commits the estimator's
    #: seed while the round still carries the prescribed candidate's name, so
    #: the outcome is banked beside the request.
    alignment_objective: str = ""
    #: The crossover corner + order this round was PINNED to, or ``None`` for a
    #: round that ran the speaker's commissioned crossover. Banked verbatim as
    #: provenance, never graded here.
    topology_prescription: "TopologyPrescription | None" = None


@dataclass(frozen=True)
class RoundRefusal:
    """A round-driven refusal, as a *kind* the flow maps to its own code.

    ``kind`` is :data:`REFUSAL_RESTORED` or :data:`REFUSAL_ROLLBACK_FAILED`.
    ``rollback_anchor_available`` is recorded here, never re-derived at render
    time: the record can change between the round and the screen, and the
    screen must describe the round.
    """

    kind: str
    #: The adoption reason a successful restore was made for.
    cause: str = ""
    rollback_anchor_available: bool | None = None


@dataclass(frozen=True)
class RoundDecision:
    """What the round decided, and what it left behind.

    ``refusal is None`` means the caller's own capture verdict stands. A round
    whose grading raised returns every field at its default, which is the state
    the caller started in. What a restore DID is not here: its owners are the
    receipt's ``restore_result`` and the ``crossover_v2_round_restore`` line.
    """

    evaluation: RoundEvaluation | None = None
    refusal: RoundRefusal | None = None
    receipt_identity: dict[str, Any] | None = None


# --- the coordinator ---


def run_round(evidence: RoundEvidence, ports: RoundPorts) -> RoundDecision:
    """Grade one round, act on the adoption table, and bank the receipt.

    Called once per session, on an ACCEPTED capture; the fire-once guard
    belongs to the caller, the only party that knows the capture was accepted.
    Fail-soft: a grading failure logs and returns an empty decision, leaving
    the caller's own verdict untouched. The receipt is written LAST so it
    records what the round actually did, a restore's result included.
    """
    try:
        evaluation = evaluate_round(
            post_analysis=evidence.post_analysis,
            entry_baseline=evidence.entry_baseline,
            spec_report=evidence.spec_report,
            tracking=getattr(evidence.post_analysis, "verify_tracking", None),
            realization_tolerance_db=evidence.realization_tolerance_db,
            reference_mark=evidence.reference_mark,
            boosted=applied_boosts(ports, session_id=evidence.session_id),
            rollback_available=rollback_available(
                ports, session_id=evidence.session_id,
            ),
            delta_probe=evidence.delta_probe,
            # The cap and the plateau bar are NOT passed: their single
            # definitions are ``evaluate_round``'s own defaults, so no call
            # site can run a longer series than the ruling allows.
            round_ordinal=evidence.round_ordinal,
            previous_objectives=evidence.previous_objectives,
            trusted_floor_hz=evidence.trusted_floor_hz,
            previous_trusted_floor_hz=evidence.previous_trusted_floor_hz,
            graded_spec=evidence.graded_spec,
            applied_blend_correction=evidence.applied_blend_correction,
            previous_blend_residual_db=evidence.previous_blend_residual_db,
        )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError,
            AttributeError, IndexError, ZeroDivisionError):
        log_event(
            logger, "correction.crossover_v2_round_grade_failed",
            level=logging.WARNING, session_id=evidence.session_id, exc_info=True,
        )
        return RoundDecision()
    _log_round(evaluation, session_id=evidence.session_id)
    # Rebound rather than reused: a failed restore re-grades through the same
    # table, and both the refusal and the receipt must record the decision the
    # round ended on.
    evaluation, refusal, restore_result = _act_on_adoption(
        evaluation, evidence, ports,
    )
    receipt_identity = _write_round_receipt(
        evaluation, evidence, ports, restore_result=restore_result,
    )
    return RoundDecision(
        evaluation=evaluation, refusal=refusal, receipt_identity=receipt_identity,
    )


def _log_round(evaluation: RoundEvaluation, *, session_id: str) -> None:
    """The round's whole answer, as one journal line."""
    record = evaluation.to_dict()
    log_event(
        logger, "correction.crossover_v2_round_graded",
        session_id=session_id,
        adoption=evaluation.adoption.outcome.value,
        # The adoption table's seven rows share four outcomes, so ``adoption``
        # alone cannot say which rule fired. The row is what a journal is
        # grepped for.
        row=evaluation.adoption.row,
        reason=evaluation.adoption.reason,
        capture=record["verdicts"]["capture"]["status"],
        realization=record["verdicts"]["realization"]["status"],
        benefit=record["verdicts"]["benefit"]["status"],
        spec=record["verdicts"]["spec"]["status"],
        trust=record["axes"]["trust"]["status"],
        safety=record["axes"]["safety"]["status"],
        # The only axis whose reason rides this line beside its status:
        # ``safe`` has two readings — the realized-energy check looked and
        # found nothing, or it could not look at all.
        safety_reason=record["axes"]["safety"]["reason"],
        quality=record["axes"]["quality"]["status"],
        headroom=record["axes"]["headroom"]["status"],
        round_ordinal=evaluation.headroom.evidence.get("round_ordinal"),
        post_residual_db=evaluation.post_residual_db,
        post_residual_bins=evaluation.post_residual_bins,
        blend=None if evaluation.blend is None else evaluation.blend.reason,
        blend_filters=(
            None if evaluation.blend is None else len(evaluation.blend.filters)
        ),
        evidence={**record["verdicts"], **record["axes"]},
    )


def _act_on_adoption(
    evaluation: RoundEvaluation, evidence: RoundEvidence, ports: RoundPorts,
) -> tuple[RoundEvaluation, RoundRefusal | None, dict[str, Any] | None]:
    """Turn the adoption outcome into what the household gets.

    The table already decided; this carries it out and never re-decides.

    * ``KEEP`` and ``KEEP_FOR_ITERATION`` — the caller's verdict stands and the
      graph stays live; what differs between them is the receipt.
    * ``RESTORE`` — fire the rollback seam (once-guarded on the host side, so
      the delta probe's own rollback and this one cannot both run), then refuse
      under the cause's own code. A failed restore is re-graded through the
      SAME table with ``restore_failed=True``.
    * ``RECOVERY_REQUIRED`` — refuse loudly under the rollback-failed code.
    """
    outcome = evaluation.adoption.outcome
    if outcome in (AdoptionOutcome.KEEP, AdoptionOutcome.KEEP_FOR_ITERATION):
        return evaluation, None, None
    if outcome is AdoptionOutcome.RESTORE:
        restored, restore_result = _run_round_restore(
            evaluation.adoption.reason, evidence, ports,
        )
        if restored:
            return (
                evaluation,
                RoundRefusal(
                    kind=REFUSAL_RESTORED, cause=evaluation.adoption.reason,
                ),
                restore_result,
            )
        return (
            _regrade_after_failed_restore(evaluation, evidence, ports),
            # A prior candidate was recorded and the restore against it did
            # not complete, so going back is still a real remedy.
            RoundRefusal(
                kind=REFUSAL_ROLLBACK_FAILED, rollback_anchor_available=True,
            ),
            restore_result,
        )
    # RECOVERY_REQUIRED — the table already knew no restore was possible (no
    # prior candidate recorded), so nothing is attempted here.
    log_event(
        logger, "correction.crossover_v2_round_recovery_required",
        level=logging.ERROR, session_id=evidence.session_id,
        reason=evaluation.adoption.reason, rollback_anchor_available=False,
    )
    return (
        evaluation,
        RoundRefusal(
            kind=REFUSAL_ROLLBACK_FAILED, rollback_anchor_available=False,
        ),
        {
            "attempted": False,
            "restored": False,
            "reason": evaluation.adoption.reason,
        },
    )


def _regrade_after_failed_restore(
    evaluation: RoundEvaluation, evidence: RoundEvidence, ports: RoundPorts,
) -> RoundEvaluation:
    """Re-run the table with ``restore_failed=True``, or keep what we had.

    The speaker is still on the APPLIED graph: every reachable failure shape
    leaves it there, which is what makes the ``restore_failed`` row right — the
    intended graph is live and unverified with its automatic remedy spent.
    Re-grading rather than editing the decision keeps
    :func:`~.verification.decide_adoption` the only producer of an
    :class:`~.contracts.AdoptionDecision`.
    """
    try:
        adoption = decide_adoption(
            trust=evaluation.trust,
            safety=evaluation.safety,
            quality=evaluation.quality,
            # The SAME verdict, not a re-evaluation: a failed restore changes
            # what the speaker is running, not how much headroom was measured.
            headroom=evaluation.headroom,
            boosted=applied_boosts(ports, session_id=evidence.session_id),
            rollback_available=rollback_available(
                ports, session_id=evidence.session_id,
            ),
            restore_failed=True,
        )
    except (TypeError, ValueError, KeyError):
        log_event(
            logger, "correction.crossover_v2_round_regrade_failed",
            level=logging.WARNING, session_id=evidence.session_id, exc_info=True,
        )
        return evaluation
    regraded = RoundEvaluation(
        capture=evaluation.capture,
        realization=evaluation.realization,
        benefit=evaluation.benefit,
        spec=evaluation.spec,
        result=evaluation.result,
        trust=evaluation.trust,
        safety=evaluation.safety,
        quality=evaluation.quality,
        headroom=evaluation.headroom,
        adoption=adoption,
        post_residual_db=evaluation.post_residual_db,
        post_residual_bins=evaluation.post_residual_bins,
        blend=evaluation.blend,
        region_benefit=evaluation.region_benefit,
    )
    _log_round(regraded, session_id=evidence.session_id)
    return regraded


def _run_round_restore(
    cause: str, evidence: RoundEvidence, ports: RoundPorts,
) -> tuple[bool, dict[str, Any]]:
    """Fire the rollback seam for an adoption-driven restore.

    The ONLY caller of that seam, and the restore is NOT idempotent: a
    completed one re-stamps the displaced candidate as the new previous one, so
    a second ask would put the just-removed graph back. The host's closure is
    once-guarded as well — see ``bind_delta_probe_rollback`` in
    :mod:`jasper.web.correction_crossover_v2`.
    """
    restored = False
    error = ""
    if ports.rollback is not None:
        try:
            restored = bool(ports.rollback(cause))
        except _SEAM_ERRORS as exc:
            error = str(exc)
    result = {
        "attempted": True,
        "restored": restored,
        "reason": cause,
        "error": error,
        "seam_bound": ports.rollback is not None,
    }
    log_event(
        logger, "correction.crossover_v2_round_restore",
        level=logging.INFO if restored else logging.ERROR,
        session_id=evidence.session_id, reason=cause, restored=restored,
        seam_bound=ports.rollback is not None, error=error,
    )
    return restored, result


def _write_round_receipt(
    evaluation: RoundEvaluation,
    evidence: RoundEvidence,
    ports: RoundPorts,
    *,
    restore_result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Assemble the round receipt and hand it to the publishing seam.

    ``round_id`` is the stage-2 relay session id: one graded post-apply session
    is one round, and a recovery re-verify writes its own receipt rather than
    amending this one.

    The IDENTITY survives what the ARTIFACT does not: it is assembled from the
    evaluation and returned on every path, because
    :func:`series_position_from_state` reads the series' only memory back off
    it. Only the two fingerprint fields depend on the artifact, and ``""``
    means "no artifact was banked". The artifact write is fail-soft — it never
    reverses a verdict, refuses a capture, or crashes the capture path.
    """
    identity = _round_identity(evaluation, evidence)
    seam = ports.publish_round_receipt
    if seam is None:
        # No publishing capability; the series still has to remember the
        # round, so the identity goes back with empty fingerprints.
        return identity
    baseline = evidence.entry_baseline
    try:
        receipt = build_round_receipt(
            round_id=evidence.session_id,
            evaluation=evaluation,
            entry_baseline=baseline,
            # The graph the "before" was measured THROUGH — the baseline's own
            # stamp, not "what is live now".
            entry_graph_fingerprint=(
                baseline.graph_fingerprint if baseline is not None
                else ENTRY_GRAPH_FINGERPRINT_UNKNOWN
            ),
            rollback_anchor={
                "available": rollback_available(
                    ports, session_id=evidence.session_id,
                ),
            },
            proposal_fingerprint=evidence.proposal_fingerprint,
            proposal_fingerprint_kind=evidence.proposal_fingerprint_kind,
            applied_graph_fingerprint=entry_graph_fingerprint(
                ports, session_id=evidence.session_id,
            ),
            post_measurement=_post_measurement_identity(
                evidence.post_analysis,
                reference_mark=evidence.reference_mark,
                phase=PHASE_VERIFY,
            ),
            restore_result=restore_result,
            round_measurements=_round_measurements(evidence, evaluation),
            evidence_identities={
                "session_id": evidence.session_id,
                "tier": evidence.tier,
                "entry_baseline_artifact": (
                    baseline.artifact_ref if baseline is not None else ""
                ),
                "commanded_delta_present": evidence.commanded_delta_present,
                # Written unconditionally, ``""`` included: an absent key would
                # be a second way of saying "unknown".
                "candidate_fingerprint": evidence.candidate_fingerprint,
            },
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        fingerprint = seam(receipt.to_dict())
    except _SEAM_ERRORS:
        # ERROR, not WARNING: a fail-soft path whose only trace is a WARNING is
        # one nobody reads.
        log_event(
            logger, "correction.crossover_v2_round_receipt_failed",
            level=logging.ERROR, session_id=evidence.session_id, exc_info=True,
        )
        return identity
    identity["artifact_fingerprint"] = str(fingerprint or "")
    identity["receipt_fingerprint"] = receipt.fingerprint
    log_event(
        logger, "correction.crossover_v2_round_receipt",
        session_id=evidence.session_id, round_id=evidence.session_id,
        artifact_fingerprint=str(fingerprint or ""),
        receipt_fingerprint=receipt.fingerprint,
        adoption=evaluation.adoption.outcome.value,
        proposal_fingerprint_kind=receipt.proposal_fingerprint_kind,
    )
    return identity


#: The two adoption outcomes that leave the round's own graph playing.
_GRAPH_KEPT_OUTCOMES = frozenset({
    AdoptionOutcome.KEEP, AdoptionOutcome.KEEP_FOR_ITERATION,
})


def _round_kept_its_graph(evaluation: RoundEvaluation) -> bool:
    """Is the speaker still on the graph this round measured?"""

    return evaluation.adoption.outcome in _GRAPH_KEPT_OUTCOMES


def _round_identity(
    evaluation: RoundEvaluation, evidence: RoundEvidence,
) -> dict[str, Any]:
    """What the round decided, plus the series' memory of it.

    Built from the evaluation alone, so it exists whether or not an artifact
    was banked. The two fingerprint fields start empty and are filled in only
    by a successful publish.
    """

    return {
        "round_id": evidence.session_id,
        "artifact_fingerprint": "",
        "receipt_fingerprint": "",
        # Here rather than only in the banked artifact: fetching a bundle to
        # answer this would make a live surface depend on evidence storage.
        "adoption": evaluation.adoption.outcome.value,
        "row": evaluation.adoption.row,
        "reason": evaluation.adoption.reason,
        # The series' own memory, read off the headroom verdict's evidence
        # rather than recomputed.
        "round_ordinal": evaluation.headroom.evidence.get("round_ordinal"),
        # Read off the EVIDENCE rather than the headroom verdict beside it:
        # the headroom axis does not branch on the epoch and must not start.
        "round_ordinal_epoch": evidence.round_ordinal_epoch,
        "objectives": evaluation.headroom.evidence.get("objectives"),
        "trusted_floor_hz": evaluation.headroom.evidence.get("trusted_floor_hz"),
        # Disclosure: nothing reads it back, and the adoption table does not
        # branch on it.
        "spec": dict(evaluation.spec.evidence) or None,
        # The blend prescription for the NEXT round, read back by
        # ``series_position_from_state`` below. ``None`` when the round did not
        # KEEP its graph: a prescription is derived through a specific
        # incumbent, and a restored round threw that graph away.
        "blend": (
            None
            if evaluation.blend is None or not _round_kept_its_graph(evaluation)
            else {
                "filters": [dict(f) for f in evaluation.blend.filters],
                "residual_db": (
                    None if evaluation.blend.reading is None
                    else evaluation.blend.reading.residual_db
                ),
            }
        ),
    }


def _round_measurements(
    evidence: RoundEvidence, evaluation: RoundEvaluation,
) -> dict[str, Any]:
    """The round's own measured numbers, for the receipt's third mapping.

    Three instruments, all optional, none graded here: the delta probe's
    band-resolved realization, the post-apply cloud's per-position residuals,
    and the blend region's commanded-vs-realized pair. All ride HERE, with the
    numbers, rather than on ``round_axes`` — that mapping is the four adoption
    axes and every value in it is a ``Verdict``.

    The blend record is NOT nested under ``realization.bands.crossover``: that
    band is the graded tier below ``DELTA_PROBE_HF_SPLIT_HZ`` and is not
    derived from any Fc. Same word, different band.

    Never raises: a probe that cannot answer costs one optional mapping.
    """

    measurements: dict[str, Any] = {}
    realization = _probe_realization(evidence.delta_probe)
    if realization is not None:
        measurements["realization"] = realization
    if evidence.position_residuals:
        measurements["position_residuals"] = [
            dict(row) for row in evidence.position_residuals
        ]
    blend = evaluation.blend
    # Banked only when there WAS a crossover region to speak about; once
    # there is a band the record always rides, emitted or not.
    if blend is not None and blend.band_hz is not None:
        record = blend.to_dict()
        region_benefit = evaluation.region_benefit
        if region_benefit is not None:
            # Labelled because the record carries two residuals over the same
            # band, referenced differently and so numerically different.
            record["region_benefit"] = {
                "instrument": "region_local_reference",
                **region_benefit.to_dict(),
            }
        measurements["blend"] = record
    # Provenance, banked verbatim, and deliberately absent from
    # ``_round_identity``: each candidate of a delay sweep is prescribed
    # explicitly, and a receipt that carried one forward would re-run a
    # candidate nobody asked for.
    prescription = evidence.alignment_prescription
    if prescription is not None:
        objective = str(evidence.alignment_objective or "")
        measurements["alignment_prescription"] = {
            **prescription.to_dict(),
            # ``committed`` is derived here, at the single site that banks
            # either, so the two cannot disagree. ``None`` is a third answer
            # and is not "no": a round whose fit never committed has no
            # objective to report.
            "objective": objective,
            "committed": (
                None if not objective
                else objective in ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES
            ),
        }
    # No ``committed`` bit beside it: a topology pin is not requested of the
    # fit at all — the request boundary opened the session at the pinned
    # corner and order.
    topology = evidence.topology_prescription
    if topology is not None:
        measurements["topology_prescription"] = topology.to_dict()
    return measurements


def _probe_realization(probe: Any) -> dict[str, Any] | None:
    """The probe's ``realization`` block, verbatim, or ``None``."""

    if probe is None:
        return None
    to_dict = getattr(probe, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        record = to_dict()
    except _SEAM_ERRORS:
        return None
    if not isinstance(record, Mapping):
        return None
    realization = record.get("realization")
    return dict(realization) if isinstance(realization, Mapping) else None


# --- the series' own memory ---


@dataclass(frozen=True)
class SeriesPosition:
    """Where the next round sits in the household's flattening series.

    What :func:`~.verification.evaluate_iteration_headroom` cannot derive from
    one round's evidence: how many rounds have run and what the last measured.
    One record read in one step, so an ordinal cannot disagree with the
    objectives beside it.
    """

    #: 1-based position of the round about to be graded.
    ordinal: int
    #: What the previous round measured, or ``None`` when there was none.
    previous_objectives: FlatnessObjectives | None
    #: The blend correction the NEXT round should apply, or ``None`` for "no
    #: instruction". A TOTAL, not a delta: the whole correction, incumbent
    #: included, applied verbatim rather than composed with anything.
    #:
    #: ``None`` and ``()`` are different instructions and the apply path turns
    #: on the difference: ``()`` is "apply no blend correction"; ``None`` is
    #: "this series has no instruction", on which the next candidate derives
    #: its correction from the APPLIED graph instead of reverting to nothing.
    previous_blend_correction: tuple[Mapping[str, Any], ...] | None = None
    #: The region residual the previous round read, or ``None``. In this pair
    #: so a residual and a prescription cannot come from different rounds.
    previous_blend_residual_db: float | None = None
    #: The frame those objectives were graded in, or ``None`` for no previous
    #: round or a tier that banked no floor. In this pair for the same reason.
    previous_trusted_floor_hz: float | None = None
    #: Which EPOCH of the ordinal sequence this round's ``ordinal`` counts in;
    #: ``0`` is a box whose sequence has never been reset. Both doors that
    #: replace durable state wholesale while leaving a measured graph on the
    #: speaker increment it —
    #: :func:`~jasper.web.correction_crossover_v2_republish.handle_v2_republish`
    #: and :func:`~jasper.web.correction_crossover_v2.reset_v2_journey_state`'s
    #: applied branch — because the ``round_receipt`` they drop is the
    #: sequence's only memory.
    ordinal_epoch: int = 0

    @classmethod
    def first(cls, *, ordinal_epoch: int = 0) -> "SeriesPosition":
        """The opening round — nothing has run, nothing was measured.

        ``ordinal_epoch`` is threaded through rather than defaulted: every path
        resolving to the first round is one where the sequence restarted, and a
        republish must still be able to say "ordinal 1, epoch 2".
        """

        return cls(
            ordinal=1, previous_objectives=None, previous_trusted_floor_hz=None,
            previous_blend_correction=None, ordinal_epoch=ordinal_epoch,
        )


#: Durable-state key holding :attr:`SeriesPosition.ordinal_epoch`. Top-level
#: rather than inside ``round_receipt``: the epoch has to survive exactly the
#: write that DROPS the receipt — a republish's whole-dict replacement.
ROUND_ORDINAL_EPOCH_STATE_KEY = "round_ordinal_epoch"


def round_ordinal_epoch_from_state(raw: Any) -> int:
    """The ordinal sequence's epoch, or ``0`` for "no reset recorded".

    ``0`` for every unreadable shape: claiming a reset that did not happen is
    the direction that fabricates. ``bool`` is rejected before ``int`` because
    it subclasses it — a hand-edited ``true`` must not publish as epoch 1.
    """

    if not isinstance(raw, Mapping):
        return 0
    epoch = raw.get(ROUND_ORDINAL_EPOCH_STATE_KEY)
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        return 0
    return epoch


def series_position_from_state(raw: Any) -> SeriesPosition:
    """Resolve the next round's series position from durable journey state.

    The reader for the keys :func:`_write_round_receipt` writes, kept beside
    that writer so the two cannot drift apart.

    Every unreadable shape resolves to the FIRST round: starting over offers a
    household up to three more rounds it may not need, while assuming the cap
    was reached would silently refuse to iterate at all.

    A previous ordinal at or past the cap still returns ``ordinal + 1``, not a
    clamp — the headroom axis is the one place the cap is enforced.
    """

    epoch = round_ordinal_epoch_from_state(raw)
    receipt = raw.get("round_receipt") if isinstance(raw, Mapping) else None
    if not isinstance(receipt, Mapping):
        return SeriesPosition.first(ordinal_epoch=epoch)
    previous_ordinal = receipt.get("round_ordinal")
    if not isinstance(previous_ordinal, int) or isinstance(previous_ordinal, bool):
        return SeriesPosition.first(ordinal_epoch=epoch)
    if previous_ordinal < 1:
        return SeriesPosition.first(ordinal_epoch=epoch)
    objectives = receipt.get("objectives")
    if not isinstance(objectives, Mapping):
        # The ordinal is still usable and the objectives are not, so the
        # round runs with no movement to judge rather than a fabricated zero.
        # The floor goes with them: a frame for absent objectives is not a
        # fact.
        return SeriesPosition(
            ordinal=previous_ordinal + 1,
            previous_objectives=None,
            previous_blend_correction=_blend_from_receipt(receipt),
            previous_blend_residual_db=_blend_residual_from_receipt(receipt),
            previous_trusted_floor_hz=None,
            ordinal_epoch=epoch,
        )
    return SeriesPosition(
        ordinal=previous_ordinal + 1,
        ordinal_epoch=epoch,
        previous_objectives=FlatnessObjectives(
            tilt_db=_optional_db(objectives.get("tilt_db")),
            ripple_db=_optional_db(objectives.get("ripple_db")),
        ),
        # Absent on a round that did not keep its graph: "no instruction",
        # which the apply path reads as "derive from the applied graph" rather
        # than as "revert to nothing".
        previous_blend_correction=_blend_from_receipt(receipt),
        previous_blend_residual_db=_blend_residual_from_receipt(receipt),
        previous_trusted_floor_hz=_optional_db(receipt.get("trusted_floor_hz")),
    )


def _blend_from_receipt(
    receipt: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...] | None:
    """The blend instruction a banked receipt carries, or ``None`` for none.

    ``None`` for an absent key and for a record this reader cannot vouch for:
    an unreadable instruction must not be able to REMOVE an adopted correction
    any more than it can invent one. An explicit ``[]`` is a real instruction —
    "apply no blend correction" — and survives as ``()``.
    """

    from .blend_correction import blend_filters_from_mapping

    blend = receipt.get("blend")
    if not isinstance(blend, Mapping):
        return None
    return blend_filters_from_mapping(blend.get("filters"))


def _blend_residual_from_receipt(receipt: Mapping[str, Any]) -> float | None:
    """The region residual the previous round read, or ``None``.

    ``None`` resolves to "keep prescribing", the direction that cannot
    silently freeze a series.
    """

    blend = receipt.get("blend")
    if not isinstance(blend, Mapping):
        return None
    return _optional_db(blend.get("residual_db"))


def _optional_db(value: Any) -> float | None:
    """A finite float from persisted JSON, or ``None``.

    NaN and infinity are rejected: they sail through every comparison in the
    headroom axis and make a plateau look unreachable forever.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
