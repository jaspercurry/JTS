# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round coordinator: what happens once a round's evidence is complete.

Phase 5's first vertical.  The issue's sequence is *capture → plan → apply →
verify → adopt*; this module owns its **tail** — grade the round, act on the
adoption table, restore if the table says restore, and bank the receipt — and
grows forward from there as the remaining organs move.

**Why the tail first.**  It is the leg whose collaborators are already pure:
:mod:`.round_evidence` grades, :mod:`.verification` decides, and
:mod:`.contracts` carries.  What was left on
:class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Conductor` was the
*sequencing* between them plus five seam calls — nine methods and four scratch
fields whose only reader was each other.  Sequencing is exactly what a
coordinator is, so it moved here whole rather than being split across a class
that also owns capture choreography.

**What this module is, that its siblings are not.**  Every other module in this
package is side-effect-free.  This one calls seams and writes journal lines,
because "restore the previous graph and record what happened" is not a pure
question.  The boundary it keeps instead is narrower and stated here so it can
be checked: it holds **no session state** (every input arrives as an argument,
every output leaves as a return value), it reaches **no host object** (only the
five callables in :class:`RoundPorts`), and it owns **no household vocabulary**
— a refusal leaves as :class:`RoundRefusal`, a typed *kind*, and the flow maps
it to the ``REASON_REGISTRY`` code whose copy the household reads.  That last
line is why :func:`run_round` can live outside the flow at all: the reason
codes are the flow's, and importing them back would be the reverse dependency
this package exists to prevent.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.log_event import log_event

from .contracts import ENTRY_GRAPH_FINGERPRINT_UNKNOWN, AdoptionOutcome
from .journey import PHASE_VERIFY
from .round_evidence import (
    EntryBaseline,
    RoundEvaluation,
    build_round_receipt,
    evaluate_round,
)
from .verification import decide_adoption

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import ProgramAnalysis
    from jasper.active_speaker.flat_spec import FlatSpecReport

logger = logging.getLogger(__name__)

#: The refusal kinds :func:`run_round` can return. Kinds rather than reason
#: codes: the code, and the sentence it renders, belong to the flow's
#: ``REASON_REGISTRY``.
REFUSAL_RESTORED = "restored"
REFUSAL_ROLLBACK_FAILED = "rollback_failed"

#: The exception family every seam call is guarded against. Wide on purpose and
#: for one reason: losing the round's verdict is strictly worse than reporting
#: it with the seam's own answer marked unavailable. Each guard below states
#: which way it fails and why.
_SEAM_ERRORS = (
    OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError,
)


# --------------------------------------------------------------------------- #
# ports — the five host capabilities a round needs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoundPorts:
    """The seams a round calls, and nothing else the host can do.

    A narrowed copy of the five relevant fields of
    :class:`~jasper.active_speaker.crossover_v2_flow.V2FlowSeams` rather than
    that object itself, so the coordinator's reach is its type signature: it
    cannot play a program, publish a candidate, or retain a position, because
    it has no name for those. Each is optional and each absence has a *decided*
    answer — see the readers below; ``None`` never means "skip the question."
    """

    #: Put the previous graph back. Takes the adoption reason, returns success.
    rollback: Callable[[str], bool] | None = None
    #: Is there a valid anchor to put back? (the state half of "can we restore")
    rollback_available: Callable[[], bool] | None = None
    #: Does the APPLIED intervention put energy in? (the applied-profile SSOT)
    applied_boosts: Callable[[], bool] | None = None
    #: Name the graph a capture was measured through.
    entry_graph_fingerprint: Callable[[], str] | None = None
    #: Bank the receipt; returns the stored artifact's fingerprint.
    publish_round_receipt: Callable[[Mapping[str, Any]], str] | None = None


# --------------------------------------------------------------------------- #
# seam readers — each states its own fail direction
# --------------------------------------------------------------------------- #


def applied_boosts(ports: RoundPorts, *, session_id: str) -> bool:
    """Does the applied intervention put energy IN? (#2291 ``boosted``)

    **Asked of the HOST, not of any conductor's own state, and that is the
    whole point.** This originally read a ``_candidate`` field assigned in
    exactly one place, stage 1's commit. The stage that GRADES a round is a
    different process with a fresh conductor and no ctor parameter for a
    candidate, so the read was ``None`` on every shipped round, ``boosted`` was
    always ``False``, and #2318's fail-closed cell was unreachable: a boosted
    intervention with unprovable benefit ended ``accepted=True`` with the graph
    live. The test suite was green over it because its harness injected a
    candidate the production path never supplies.

    The seam answers from the applied-profile SSOT — the one owner of "what did
    we apply" — through the shipped
    :func:`~jasper.active_speaker.camilla_yaml.linearization_has_boost`, so
    there is still no second definition of "boost" on this speaker.

    Fails CLOSED at both levels: an unbound seam and a raising one both answer
    "boosted", so an intervention nobody can inspect is restored rather than
    left driving a driver on evidence nobody has.
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
    """Can this host actually put the previous sound back? (#2291)

    **BOTH-AND**, and each half answers a different question:

    * the ``rollback`` seam is bound — a *process* fact, the flow's own
      capability idiom (``STAGE_VERIFY_CAPABILITIES`` provides
      ``CAPABILITY_ROLLBACK``). A single-stage or future caller may reach this
      decision with no seam at all.
    * a valid anchor exists — a *state* fact, owned by the host's
      ``rollback_available`` seam, which reads the very predicate
      ``handle_v2_restore`` refuses on.

    Either half alone gives a wrong answer to the question
    :func:`~.verification.decide_adoption` is actually asking. Seam-only says
    yes on a speaker whose durable state carries no ``pre_apply_profile``: the
    round would issue a ``restore`` instruction Undo then refuses, and the
    household would be told the old sound was coming back when nothing could
    bring it. Anchor-only ignores that some callers cannot restore at all.

    Fails closed on both halves — an unbound anchor seam, or one that raises,
    means "not available", which routes the adoption table to
    ``recovery_required`` (loud, operator-visible) rather than to a restore that
    cannot happen.
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

    Never raises and never gates — see :data:`ENTRY_GRAPH_FINGERPRINT_UNKNOWN`
    for the three honest ways the host cannot answer, and why the answer is a
    word rather than an empty string.
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


def post_measurement_identity(
    analysis: "ProgramAnalysis | None", *, reference_mark: str, phase: str,
) -> dict[str, Any] | None:
    """What the post-apply side WAS, as identity rather than payload.

    The same rule :func:`~.round_evidence.build_round_receipt` applies to the
    entry baseline: the curve has owners that outlive the receipt, so the
    receipt names it instead of copying it.
    """
    if analysis is None:
        return None
    return {
        "program_id": str(getattr(analysis, "program_id", "") or ""),
        "reference_mark": reference_mark,
        "phase": phase,
    }


# --------------------------------------------------------------------------- #
# the round's inputs and its answer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoundEvidence:
    """Everything one round is graded from, stated once by its caller.

    A frozen argument bundle rather than a live view of the conductor: the
    round is graded exactly once, from the evidence as it stood at that moment,
    and a bundle makes that literally true instead of merely intended.
    """

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
    #: The applied candidate's identity, for the receipt.
    proposal_fingerprint: str
    #: Did stage 1's commanded delta cross the bridge? (identity, not payload)
    commanded_delta_present: bool
    #: The realization tolerance the round grades against.
    realization_tolerance_db: float
    #: The mark both captures were taken at.
    reference_mark: str


@dataclass(frozen=True)
class RoundRefusal:
    """A round-driven refusal, as a *kind* the flow maps to its own code.

    ``kind`` is :data:`REFUSAL_RESTORED` or :data:`REFUSAL_ROLLBACK_FAILED`.
    ``rollback_anchor_available`` travels with the second because that code
    covers two situations whose household sentences differ — a restore that
    failed (Undo can still help) and a restore that was never possible (Undo
    refuses on the very predicate that routed here). The fact is recorded here,
    never re-derived at render time: the anchor can change between the round and
    the screen, and the screen must describe the round.
    """

    kind: str
    #: The adoption reason a successful restore was made for.
    cause: str = ""
    rollback_anchor_available: bool | None = None


@dataclass(frozen=True)
class RoundDecision:
    """What the round decided, and what it left behind.

    ``refusal is None`` means the caller's own capture verdict stands — the
    ``KEEP`` and ``USER_DECISION`` arms, which deliberately do not manufacture a
    refusal out of "we could not tell."

    Every field is what the caller should now hold, including on the failure
    paths: a round whose grading raised returns this object with every field at
    its default, which is precisely the state the caller started in.
    """

    evaluation: RoundEvaluation | None = None
    refusal: RoundRefusal | None = None
    restore_result: dict[str, Any] | None = None
    receipt_identity: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# the coordinator
# --------------------------------------------------------------------------- #


def run_round(evidence: RoundEvidence, ports: RoundPorts) -> RoundDecision:
    """Grade one round, act on the adoption table, and bank the receipt.

    Called once per session, by whichever trigger finds the post-apply evidence
    complete — the end of VERIFY on a tier that walks no post-apply cloud, the
    close of that cloud on a tier that does. The fire-once guard belongs to the
    caller, which is the only party that knows a capture was *accepted*: both
    triggers require one, because VERIFY and a position group each carry a retry
    budget and grading a rejected capture would burn the guard on evidence the
    household then replaced.

    **Fail-soft, and the fail direction matters:** a grading failure logs and
    returns an empty decision, so the caller's own verdict stands untouched. The
    round's job is to add an honest answer, and a bug in the grader must not
    cost the household the verdict the measurement gate already reached — least
    of all by turning an accepted capture into a refusal.

    Order is load-bearing. The receipt is written LAST so it records what the
    round actually DID — including a restore's result, which the adoption arm is
    what produces.
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
    # table, and both the refusal below and the receipt must record the decision
    # the round ended on rather than the one it started from.
    evaluation, refusal, restore_result = _act_on_adoption(
        evaluation, evidence, ports,
    )
    receipt_identity = _write_round_receipt(
        evaluation, evidence, ports, restore_result=restore_result,
    )
    return RoundDecision(
        evaluation=evaluation,
        refusal=refusal,
        restore_result=restore_result,
        receipt_identity=receipt_identity,
    )


def _log_round(evaluation: RoundEvaluation, *, session_id: str) -> None:
    """The round's whole answer, as one journal line.

    ``to_dict`` carries the four verdicts WITH their evidence, not just the
    collapsed statuses: a support read needs to know why a benefit was
    indeterminate, and the statuses alone cannot say.
    """
    record = evaluation.to_dict()
    log_event(
        logger, "correction.crossover_v2_round_graded",
        session_id=session_id,
        adoption=evaluation.adoption.outcome.value,
        reason=evaluation.adoption.reason,
        capture=record["verdicts"]["capture"]["status"],
        realization=record["verdicts"]["realization"]["status"],
        benefit=record["verdicts"]["benefit"]["status"],
        spec=record["verdicts"]["spec"]["status"],
        post_residual_db=evaluation.post_residual_db,
        post_residual_bins=evaluation.post_residual_bins,
        evidence=record["verdicts"],
    )


def _act_on_adoption(
    evaluation: RoundEvaluation, evidence: RoundEvidence, ports: RoundPorts,
) -> tuple[RoundEvaluation, RoundRefusal | None, dict[str, Any] | None]:
    """Turn the adoption outcome into what the household gets.

    Only ever reached for an ACCEPTED capture (see :func:`run_round`), so every
    bullet below describes a round whose post-apply measurement stands, and
    #2291's acting half applies exactly where it was meant to: the round that
    would otherwise have been reported as a SUCCESS.

    The table already decided; this carries it out and never re-decides. In
    particular there is no branch here that can keep a graph the table said to
    restore — a realization pass does not override a measured regression, and
    the only way that guarantee holds is by not writing the branch that would
    break it.

    * ``KEEP`` — the caller's verdict stands.
    * ``RESTORE`` — fire the rollback seam (once-guarded on the host side, so
      the delta probe's own rollback and this one cannot both run), then refuse
      under the cause's own code. A successful restore keeps the "the previous
      sound has been put back" promise; a failed one is re-graded through the
      SAME table with ``restore_failed=True``, which is what turns it into
      ``RECOVERY_REQUIRED`` — the receipt then says the speaker is in neither
      graph, because it is.
    * ``RECOVERY_REQUIRED`` — refuse LOUDLY under the flow's rollback-failed
      code, whose copy already tells the household the correction is still
      applied and Undo is on screen. No new screen: this is the shape the delta
      probe's refusal established.
    * ``USER_DECISION`` — the capture verdict stands, and nothing here claims
      success. The round's own answer rides the journal and the receipt; this
      path deliberately does not manufacture a refusal out of "we could not
      tell", which would report a failure that did not happen.
    """
    outcome = evaluation.adoption.outcome
    if outcome is AdoptionOutcome.KEEP or outcome is AdoptionOutcome.USER_DECISION:
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
            # An anchor existed — the restore was attempted against it and did
            # not complete — so Undo is still a real remedy.
            RoundRefusal(
                kind=REFUSAL_ROLLBACK_FAILED, rollback_anchor_available=True,
            ),
            restore_result,
        )
    # RECOVERY_REQUIRED — the table already knew no restore was possible (no
    # anchor), so nothing is attempted here; the record says why, and the copy
    # must not point at an Undo that refuses on the same fact.
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

    **The speaker is still on the APPLIED graph**, which is what the household
    copy says ("the newer tuning is STILL APPLIED"). Both reachable failure
    shapes leave it there: a ``CrossoverV2Refused`` never touches DSP, and
    ``restore_applied_baseline_profile`` is an atomic transaction that leaves
    the live config alone when it does not complete.

    The table's ``restore_failed`` row still describes it: what makes that row
    right is that the intended graph is live and unverified with its automatic
    remedy spent. Re-grading rather than editing the decision in place keeps
    :func:`~.verification.decide_adoption` the only thing that ever produces an
    :class:`~.contracts.AdoptionDecision`.
    """
    try:
        adoption = decide_adoption(
            realization=evaluation.result.realization,
            benefit=evaluation.result.benefit,
            spec=evaluation.result.spec,
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
        adoption=adoption,
        post_residual_db=evaluation.post_residual_db,
        post_residual_bins=evaluation.post_residual_bins,
    )
    _log_round(regraded, session_id=evidence.session_id)
    return regraded


def _run_round_restore(
    cause: str, evidence: RoundEvidence, ports: RoundPorts,
) -> tuple[bool, dict[str, Any]]:
    """Fire the rollback seam for an adoption-driven restore.

    Shares the host's once-guarded closure with the delta probe, so a Full
    session in which both ask for a restore attempts exactly one — and the
    second asker is handed the FIRST attempt's outcome rather than
    ``handle_v2_restore``'s "nothing is applied to undo" refusal, which would
    otherwise be reported as a failed rollback and told to the household as
    "the correction is still applied" when it is not.
    """
    restored = False
    error = ""
    if ports.rollback is not None:
        try:
            restored = bool(ports.rollback(cause))
        except _SEAM_ERRORS as exc:
            # Same widened family and same reasoning as the delta probe's
            # refusal: this call sits outside the cloud pipeline's wrap, and
            # losing the verdict is strictly worse than reporting it with the
            # restore marked failed.
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
    """Assemble #2291's receipt and hand it to the publishing seam.

    ``round_id`` is the stage-2 relay session id: one graded post-apply session
    is one round. A recovery re-verify runs under a NEW relay session and
    therefore writes its OWN receipt rather than amending this one — which is
    the honest shape, because it is a different measurement of a different
    speaker state, and a receipt that could be amended would not be a receipt.

    **Fail-soft, deliberately and in both directions.** A receipt that could not
    be built or written is a journal line and nothing more; it never reverses a
    verdict, never refuses a capture, and never crashes the capture path. The
    verdict is what protects the household's speaker; the receipt is what lets
    someone reconstruct why afterwards, and losing the second must not cost the
    first.
    """
    seam = ports.publish_round_receipt
    if seam is None:
        return None
    baseline = evidence.entry_baseline
    try:
        receipt = build_round_receipt(
            round_id=evidence.session_id,
            evaluation=evaluation,
            entry_baseline=baseline,
            # The graph the "before" was measured THROUGH — the baseline's own
            # stamp, not "what is live now", which post-apply is the applied
            # graph below.
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
            applied_graph_fingerprint=entry_graph_fingerprint(
                ports, session_id=evidence.session_id,
            ),
            post_measurement=post_measurement_identity(
                evidence.post_analysis,
                reference_mark=evidence.reference_mark,
                phase=PHASE_VERIFY,
            ),
            restore_result=restore_result,
            evidence_identities={
                "session_id": evidence.session_id,
                "tier": evidence.tier,
                "entry_baseline_artifact": (
                    baseline.artifact_ref if baseline is not None else ""
                ),
                "commanded_delta_present": evidence.commanded_delta_present,
            },
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        fingerprint = seam(receipt.to_dict())
    except _SEAM_ERRORS:
        # ERROR, not WARNING, and earned by demonstrated history: this is the
        # exact event that would have fired on every shipped round for a whole
        # phase while nobody looked. A dead ``_candidate`` read emptied
        # ``proposal_fingerprint``, the receipt contract refused it, and this
        # handler swallowed the loss — a fail-soft path whose only trace is a
        # WARNING is one nobody reads. Its sibling, the no-anchor recovery path,
        # is already ERROR.
        log_event(
            logger, "correction.crossover_v2_round_receipt_failed",
            level=logging.ERROR, session_id=evidence.session_id, exc_info=True,
        )
        return None
    identity = {
        "round_id": evidence.session_id,
        "artifact_fingerprint": str(fingerprint or ""),
        "receipt_fingerprint": receipt.fingerprint,
    }
    log_event(
        logger, "correction.crossover_v2_round_receipt",
        session_id=evidence.session_id, round_id=evidence.session_id,
        artifact_fingerprint=str(fingerprint or ""),
        receipt_fingerprint=receipt.fingerprint,
        adoption=evaluation.adoption.outcome.value,
    )
    return identity
