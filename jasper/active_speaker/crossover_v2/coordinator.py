# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round coordinator: what happens once a round's evidence is complete.

Phase 5's first vertical.  The issue's sequence is *capture → plan → apply →
verify → adopt*; this module owns its **tail** — grade the round, act on the
adoption table, restore if the table says restore, and bank the receipt.  The
legs before it belong to
:class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session`.

**Why the tail first.**  It is the leg whose collaborators are already pure:
:mod:`.round_evidence` grades, :mod:`.verification` decides, and
:mod:`.contracts` carries.  What was left on
:class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session` was the
*sequencing* between them plus five seam calls — nine methods and four scratch
fields whose only reader was each other.  Sequencing is exactly what a
coordinator is, so it moved here whole rather than being split across a class
that also owns capture choreography.

**What this module is, that its siblings are not.**  It is the only one that
CHANGES THE SPEAKER: "restore the previous graph and record what happened" is
not a pure question, so this module calls seams that act.  (Emitting a log line
is not the distinction — :mod:`.planning` and :mod:`.vocabulary` both write one
too.  This module's own text said "every other module in this package is
side-effect-free" until #2291 Phase 5c-iii; that was too strong, and the
narrower claim is the one worth checking.)  The boundary it keeps is stated
here so it can be checked: it holds **no session state** (every input arrives
as an argument, every output leaves as a return value), it reaches **no host
object** (only the five callables in :class:`RoundPorts`), and it owns **no
household vocabulary**
— a refusal leaves as :class:`RoundRefusal`, a typed *kind*, and the flow maps
it to the ``REASON_REGISTRY`` code whose copy the household reads.  That last
line is why :func:`run_round` can live outside the flow at all.  Its original
form argued that the codes were the flow's and importing them back would invert
the dependency; since #2291 Phase 5c-ii they are :mod:`.vocabulary`'s, a
sibling, so the import would be legal now.  The rule stands on its own without
that argument: a module that decides answers with a kind, and one that renders
copy is doing a different job.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

import logging
import math
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
from .verification import FlatnessObjectives, decide_adoption

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import ProgramAnalysis
    from jasper.active_speaker.flat_spec import FlatSpecReport, GradedSpec

logger = logging.getLogger(__name__)

#: The refusal kinds :func:`run_round` can return. Kinds rather than reason
#: codes: the code, and the sentence it renders, belong to
#: :mod:`.vocabulary`'s ``REASON_REGISTRY``.
REFUSAL_RESTORED = "restored"
REFUSAL_ROLLBACK_FAILED = "rollback_failed"

#: Every kind above, so the flow's mapping can be checked for completeness
#: rather than trusted. A kind added here without an arm there is a wiring
#: defect, and the flow says so loudly instead of answering with another kind's
#: household sentence.
REFUSAL_KINDS = frozenset({REFUSAL_RESTORED, REFUSAL_ROLLBACK_FAILED})

#: The exception family four of the five seam calls are guarded against. Wide on
#: purpose and for one reason: losing the round's verdict is strictly worse than
#: reporting it with the seam's own answer marked unavailable. Each guard below
#: states which way it fails and why.
#:
#: :func:`entry_graph_fingerprint` deliberately does NOT use this tuple — its
#: guard omits ``AttributeError``, exactly as it did on the conductor. The name
#: it fills is provenance, never a gate, so a narrower catch there costs a
#: fingerprint rather than a verdict; the difference is preserved rather than
#: harmonised because harmonising it would be a behaviour change smuggled in as
#: tidying.
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

    **Asked of the HOST, not of any session's own state, and that is the
    whole point.** This originally read a ``_candidate`` field assigned in
    exactly one place, stage 1's commit. The stage that GRADES a round is a
    different process with a fresh session and no ctor parameter for a
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


def _post_measurement_identity(
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

    A frozen argument bundle rather than a live view of the session: the
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
    #: What this round proposed, for the receipt. Since #2392 this is the
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
    #: (:data:`~.contracts.PROPOSAL_FINGERPRINT_KINDS`). REQUIRED, like the
    #: receipt field it feeds: this type has exactly one production constructor
    #: and no test constructs it, so a default protects nobody — it only buys a
    #: caller the ability to mislabel a write-once artifact by omission, which
    #: the closed vocabulary cannot catch because ``"candidate"`` is a legal
    #: word. Stating it is one line at the one call site.
    proposal_fingerprint_kind: str
    #: The applied candidate's own fingerprint, which rides into the receipt's
    #: evidence identities. It used to BE ``proposal_fingerprint``; #2392 gave
    #: that field to the proposal, and a receipt that dropped the candidate
    #: identity on the way would have lost a fact it used to carry. Required
    #: for the reason directly above.
    candidate_fingerprint: str
    #: The round's
    #: :class:`~jasper.active_speaker.delta_probe.DeltaProbeMap`, or ``None``
    #: when the session ran none (#2537). REQUIRED, for the same reason
    #: ``proposal_fingerprint_kind`` above is and one stronger: this type has
    #: exactly one production constructor, and the field feeds
    #: :func:`~.verification.evaluate_applied_safety` — the adoption table's
    #: only hard stop. A default would let a caller silence that stop by
    #: forgetting a keyword, which is the cheapest possible way to lose a
    #: safety check. ``None`` is a legitimate value and must be stated.
    delta_probe: Any | None
    #: 1-based position of this round in the household's flattening series
    #: (#2602) — what :data:`~.round_evidence.ROUND_SERIES_CAP` is checked
    #: against.
    #:
    #: REQUIRED, on the same reasoning as the field above rather than a weaker
    #: version of it: a default of 1 would be *silently wrong* on every round
    #: after the first, so a caller that forgot the keyword would produce a
    #: series that never reaches its cap and a household told to measure again
    #: forever. That failure looks exactly like working software. One line at
    #: the one call site buys the guarantee that the ordinal is always
    #: something the host actually resolved.
    round_ordinal: int
    #: What the PREVIOUS round of this series measured on #2602's two
    #: objectives, read off the durable receipt that round banked — or ``None``
    #: for the series' first round. Required for the same reason: "there was no
    #: previous round" and "nobody looked" must not be the same keyword.
    previous_objectives: FlatnessObjectives | None
    #: The trusted floor THIS round's objectives were graded against, and the
    #: one the previous round's were (#2609 SF5). Together they let the
    #: headroom axis refuse a cross-floor movement comparison instead of
    #: reading a gate-length artefact as progress.
    #:
    #: **Defaulted, unlike the three required fields above, and the line is
    #: the fail direction rather than the number of call sites.** A forgotten
    #: ``round_ordinal`` is silently WRONG on every round after the first; a
    #: forgotten floor is merely ABSENT, and absent is a value the reader
    #: already handles honestly — no floor means no evidence the frame moved,
    #: so the comparison proceeds exactly as it did before SF5. A default that
    #: can only withhold a refusal is safe; one that can fabricate a number is
    #: not.
    trusted_floor_hz: float | None = None
    previous_trusted_floor_hz: float | None = None
    #: The post-apply cloud's per-position residuals, role-labelled, as
    #: JSON-shaped rows (§4.2). Banked on the receipt so the next bite can tell
    #: a position-INVARIANT miss — ours, a model or level defect — from a
    #: position-DEPENDENT one, which is the room. Empty on a tier that walks no
    #: post-apply cloud, and defaulted for the reason directly above: an empty
    #: tuple and an unsupplied one both mean "no positions to report".
    position_residuals: tuple[Mapping[str, Any], ...] = ()
    #: The post-apply cloud's flat-spec evaluation with its graded curve and
    #: MERGED honesty mask (decision 10) — the blend correction's evidence.
    #: ``None`` on a tier that walks no post-apply cloud, and defaulted for the
    #: same fail-direction reason the floors above are: absent evidence
    #: prescribes nothing, which is the safe answer, while a fabricated one
    #: would prescribe a filter from a mask nobody screened.
    graded_spec: "GradedSpec | None" = None
    #: The blend correction the post-apply capture actually rode, read off the
    #: APPLIED candidate. ``None`` is "could not be established" and refuses to
    #: prescribe; ``()`` is "it rode none", which every first round honestly is.
    #:
    #: **Defaulted to ``None`` rather than ``()`` deliberately.** The two are
    #: not interchangeable here: assuming an empty incumbent when the real one
    #: is unknown double-counts the correction the capture was actually taken
    #: through, which is the precise shape #2653 reverted for the level datum.
    #: A caller that forgets this keyword gets a refusal, not a wrong number.
    applied_blend_correction: tuple[Mapping[str, Any], ...] | None = None


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
    ``KEEP`` and ``KEEP_FOR_ITERATION`` arms, which deliberately do not
    manufacture a refusal out of "it is not perfect yet."

    Every field is what the caller should now hold, including on the failure
    paths: a round whose grading raised returns this object with every field at
    its default, which is precisely the state the caller started in.

    **What a restore DID is deliberately not here.** It has two owners already —
    the receipt's ``restore_result`` and the ``crossover_v2_round_restore``
    journal line — and the session had no reader for it once this module took
    the receipt.  Returning it anyway would put a third copy in the caller's
    hands with nothing to check it against.
    """

    evaluation: RoundEvaluation | None = None
    refusal: RoundRefusal | None = None
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
            delta_probe=evidence.delta_probe,
            # #2602's axis. The cap and the plateau bar are NOT passed: their
            # single definitions are ``evaluate_round``'s own defaults, so
            # there is no call site free to run a longer series than the
            # ruling allows. What the host supplies is the two facts only it
            # can know — where in the series this round sits, and what the
            # previous one measured.
            round_ordinal=evidence.round_ordinal,
            previous_objectives=evidence.previous_objectives,
            # #2609 SF5's frame, carried beside the objectives it scoped.
            trusted_floor_hz=evidence.trusted_floor_hz,
            previous_trusted_floor_hz=evidence.previous_trusted_floor_hz,
            # Decision 10's two inputs, forwarded rather than re-derived: the
            # cloud evidence the correction is solved from, and the incumbent
            # the capture rode.
            graded_spec=evidence.graded_spec,
            applied_blend_correction=evidence.applied_blend_correction,
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
        evaluation=evaluation, refusal=refusal, receipt_identity=receipt_identity,
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
        # WHICH rule fired, beside what it decided (#2537). Three of the seven
        # rows restore and four keep the graph — and the four share only two
        # outcomes between them, since #2602 and #2656 each split a cell — so
        # ``adoption`` alone cannot say, and the reason travels from whichever
        # axis spoke. The row is the stable thing to grep a journal for.
        row=evaluation.adoption.row,
        reason=evaluation.adoption.reason,
        capture=record["verdicts"]["capture"]["status"],
        realization=record["verdicts"]["realization"]["status"],
        benefit=record["verdicts"]["benefit"]["status"],
        spec=record["verdicts"]["spec"]["status"],
        trust=record["axes"]["trust"]["status"],
        safety=record["axes"]["safety"]["status"],
        quality=record["axes"]["quality"]["status"],
        # #2602: WHETHER another round is coming, and where this one sat in
        # the series — the two facts a support read needs to tell "the series
        # stopped here" apart from "the series is still going". The tilt and
        # ripple behind them ride in ``evidence`` below like every other axis's
        # numbers.
        headroom=record["axes"]["headroom"]["status"],
        round_ordinal=evaluation.headroom.evidence.get("round_ordinal"),
        post_residual_db=evaluation.post_residual_db,
        post_residual_bins=evaluation.post_residual_bins,
        evidence={**record["verdicts"], **record["axes"]},
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
    * ``KEEP_FOR_ITERATION`` — the caller's verdict also stands, and the graph
      stays live (#2537). The round's outstanding targets ride the journal and
      the receipt's ``round_axes``; this path deliberately does not manufacture
      a refusal out of "it is not perfect yet", which would report a failure
      that did not happen and revert the least-bad MEASURED tune the household
      has. Same arm as ``KEEP`` because they leave the speaker in the same
      state — what differs is the receipt, which is where the difference
      belongs.
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
    the live config alone when it does not complete. ``decide_adoption`` and
    :class:`~.contracts.RoundReceipt` still describe this state as "neither the
    entry graph nor the intended one" — the abstract worst case, not this one,
    and contradicted by the sentence the household reads. Left for their owners
    rather than edited from here; do not propagate it back into this one.

    The table's ``restore_failed`` row still describes it: what makes that row
    right is that the intended graph is live and unverified with its automatic
    remedy spent, not the stronger claim above. Re-grading rather than editing the decision in place keeps
    :func:`~.verification.decide_adoption` the only thing that ever produces an
    :class:`~.contracts.AdoptionDecision`.
    """
    try:
        adoption = decide_adoption(
            trust=evaluation.trust,
            safety=evaluation.safety,
            quality=evaluation.quality,
            # The SAME verdict, not a re-evaluation: a failed restore changes
            # what the speaker is running, not how much headroom the round
            # measured. Re-grading it here would ask the fourth axis a question
            # no new measurement had answered.
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
    )
    _log_round(regraded, session_id=evidence.session_id)
    return regraded


def _run_round_restore(
    cause: str, evidence: RoundEvidence, ports: RoundPorts,
) -> tuple[bool, dict[str, Any]]:
    """Fire the rollback seam for an adoption-driven restore.

    **The ONLY caller of that seam, and that is a guarantee rather than an
    observation about the call graph.** The delta probe used to restore from a
    seam of its own, so a Full session could ask twice; ``handle_v2_restore``
    is not idempotent, and the second asker read its "nothing is applied to
    undo" refusal as a failed rollback and told the household "the correction
    is still applied" when it was not. That seam is deleted — the probe reports
    and this function acts on the table's decision — and the flow is pinned
    against growing a second one back.

    The host's closure is still once-guarded, and it should stay that way: the
    guard is a property of the binding rather than of this caller's discipline,
    which is precisely the assumption whose failure produced the false
    sentence. See ``bind_delta_probe_rollback`` in
    :mod:`jasper.web.correction_crossover_v2`.
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

    **The IDENTITY survives what the ARTIFACT does not, and that split is
    #2609.** This identity is also the series' only memory:
    :func:`series_position_from_state` reads ``round_ordinal``, ``objectives``,
    and (since SF5) ``trusted_floor_hz`` back off it. It used to be returned
    only on the success path, so an unbound or raising publish seam cost the
    series its place in the count as well as its artifact: every round then read
    as round 1, which disables the round cap AND the plateau stop, and the gate
    drove twelve rounds proving it.

    So the identity is assembled from the EVALUATION — which is in hand
    whatever the seam does — and returned on every path. Only the two
    fingerprint fields depend on the artifact, and they say so by being empty:
    ``""`` means "this round decided what it says it decided, and no artifact
    was banked", which is a fact worth carrying rather than a hole. Nothing
    downstream reads a fingerprint as proof the round happened; the row, the
    outcome, and the reason are what the screens and the next round read.

    **The artifact write is still fail-soft, deliberately and in both
    directions.** A receipt that could not be built or written never reverses a
    verdict, never refuses a capture, and never crashes the capture path. The
    verdict is what protects the household's speaker; the artifact is what lets
    someone reconstruct why afterwards, and losing the second must not cost the
    first — nor, now, the series its memory.
    """
    identity = _round_identity(evaluation, evidence)
    seam = ports.publish_round_receipt
    if seam is None:
        # No publishing capability on this host. The round still happened and
        # the series still has to remember it, so the identity goes back with
        # empty fingerprints rather than as ``None``.
        return identity
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
            # What the round MEASURED and nothing graded — the next bite's
            # command inputs. Assembled here rather than in the assembler for
            # the reason the assembler states: the instruments are the
            # caller's, and deriving these there would give them a second
            # owner.
            round_measurements=_round_measurements(evidence, evaluation),
            evidence_identities={
                "session_id": evidence.session_id,
                "tier": evidence.tier,
                "entry_baseline_artifact": (
                    baseline.artifact_ref if baseline is not None else ""
                ),
                "commanded_delta_present": evidence.commanded_delta_present,
                # The applied candidate, kept on the record now that the
                # ``proposal_fingerprint`` field above names the proposal
                # instead (#2392). Written unconditionally, including as ``""``
                # — an absent key would be a second way of saying "unknown"
                # alongside the empty string, and this map is read by a human
                # off a banked artifact.
                "candidate_fingerprint": evidence.candidate_fingerprint,
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
        return identity
    identity["artifact_fingerprint"] = str(fingerprint or "")
    identity["receipt_fingerprint"] = receipt.fingerprint
    log_event(
        logger, "correction.crossover_v2_round_receipt",
        session_id=evidence.session_id, round_id=evidence.session_id,
        artifact_fingerprint=str(fingerprint or ""),
        receipt_fingerprint=receipt.fingerprint,
        adoption=evaluation.adoption.outcome.value,
        # Which regime wrote this receipt, in the journal as well as in the
        # artifact (#2392) — the journal is where a session looks before it
        # fetches the bundle.
        proposal_fingerprint_kind=receipt.proposal_fingerprint_kind,
    )
    return identity


def _round_identity(
    evaluation: RoundEvaluation, evidence: RoundEvidence,
) -> dict[str, Any]:
    """What the round decided, plus the series' memory of it.

    Built from the evaluation alone, so it exists whether or not an artifact
    was banked — see :func:`_write_round_receipt` for why that independence is
    the point. The two fingerprint fields start empty and are filled in only by
    a successful publish.
    """

    return {
        "round_id": evidence.session_id,
        "artifact_fingerprint": "",
        "receipt_fingerprint": "",
        # WHAT the round decided, WHICH row decided it, and the deciding axis's
        # own reason (#2537), beside the pointers to where the full receipt
        # landed. The done screen needs the outcome to know whether it owes a
        # "kept, and here is what is still off" caveat, and a driver chaining
        # rounds needs the row — fetching a bundle artifact to answer either
        # would make a live surface depend on evidence storage.
        "adoption": evaluation.adoption.outcome.value,
        "row": evaluation.adoption.row,
        "reason": evaluation.adoption.reason,
        # #2602: the series' own memory, and the reason it is HERE rather than
        # only inside the banked artifact. The next round needs all three — the
        # ordinal to know where the cap is, the objectives to know whether the
        # series is still moving, and (SF5) the floor to know whether those
        # objectives are even comparable — and it resolves them from this
        # durable identity, which the host already carries forward across
        # sessions. Fetching a bundle artifact to answer "should we run another
        # round" would make the decision depend on evidence storage.
        #
        # Read off the headroom verdict's own evidence rather than recomputed,
        # so the numbers banked for the next round are byte-for-byte the ones
        # this round decided on.
        "round_ordinal": evaluation.headroom.evidence.get("round_ordinal"),
        "objectives": evaluation.headroom.evidence.get("objectives"),
        "trusted_floor_hz": evaluation.headroom.evidence.get("trusted_floor_hz"),
        # Decision 10's prescription for the NEXT round, carried on the same
        # durable record the objectives are — because it is the same kind of
        # fact (what this round learned that only the next one can use) and
        # because a series that remembered its objectives from one record and
        # its prescription from another would be remembering two pasts. Read
        # back by ``series_position_from_state`` directly below.
        "blend": (
            None if evaluation.blend is None
            else [dict(f) for f in evaluation.blend.filters]
        ),
    }


def _round_measurements(
    evidence: RoundEvidence, evaluation: RoundEvaluation,
) -> dict[str, Any]:
    """The round's own measured numbers, for the receipt's third mapping.

    Three instruments, all optional, none graded here:

    * the delta probe's band-resolved realization (#2649) — read off the
      probe's own ``to_dict`` so the receipt banks exactly what the probe
      published rather than a reshaped copy of it. Absent on a session that ran
      no probe, and absent on a probe from a build before the band-resolved
      report shipped; both are honest absences and neither is an error.
    * the post-apply cloud's per-position residuals (§4.2), already
      JSON-shaped by the caller.
    * the blend region's commanded-vs-realized pair (decision 10/11) — the
      filters prescribed for the next round, the incumbent they were derived
      from, the damping, and what the incumbent actually achieved in the
      region. Decision 11 makes that pair deterministic forever no matter who
      eventually prescribes, which is why it is banked with the numbers rather
      than only logged.

      **Its reason code rides here, with the numbers, not on ``round_axes``.**
      ``round_axes`` is the four ADOPTION axes and every value in it is a
      ``Verdict``; a blend reason is neither an adoption axis nor a verdict, and
      a fifth key of a different shape would read as one — which decision 10
      explicitly forbids ("not a new safety class"). Keeping the reason beside
      the numbers is also what lets a reader tell "the region was already
      clean" from "the instrument refused" in one place.

      **Deliberately NOT nested under ``realization.bands.crossover``.** That
      band is the graded tier below ``DELTA_PROBE_HF_SPLIT_HZ``
      (``[953.5, 9999.98] Hz`` on the series-1 rig) and its own comment says it
      is named for what it contains rather than derived from any Fc — which
      this one is. Same word, different band.

    Never raises. A probe object that cannot answer costs the receipt one
    optional mapping, and losing the whole receipt over it would be exactly the
    trade this module refuses everywhere else.
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
    # Banked only when there WAS a crossover region to speak about. A round on
    # a tier that walks no cloud, or one whose absolute claim was never
    # evaluated, has no blend question — and a record saying so would be the
    # same false claim the empty position-residual list above is refused for:
    # that the question was asked and answered. Once there IS a band, the
    # record always rides, emitted or not, because "the region was already
    # clean" and "the instrument refused" are then genuinely different answers.
    if blend is not None and blend.band_hz is not None:
        record = blend.to_dict()
        region_benefit = evaluation.region_benefit
        if region_benefit is not None:
            record["region_benefit"] = region_benefit.to_dict()
        measurements["blend"] = record
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


# --------------------------------------------------------------------------
# the series' own memory (#2602)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesPosition:
    """Where the next round sits in the household's flattening series (#2602).

    The two facts :func:`~.verification.evaluate_iteration_headroom` cannot
    derive from one round's evidence, because they are properties of the
    SERIES: how many rounds have already run, and what the last one measured.

    Deliberately a pair rather than two loose arguments threaded side by side:
    they are read from one durable record, in one step, and an ordinal that
    disagreed with the objectives beside it would be a series remembering two
    different pasts.
    """

    #: 1-based position of the round about to be graded.
    ordinal: int
    #: What the previous round measured, or ``None`` when there was none.
    previous_objectives: FlatnessObjectives | None
    #: The blend correction the NEXT round should apply (decision 10), or
    #: ``None`` when the previous round prescribed none / there was no previous
    #: round. A TOTAL, not a delta: the whole correction, incumbent included,
    #: so the candidate build applies it verbatim rather than composing it with
    #: anything.
    previous_blend_correction: tuple[Mapping[str, Any], ...] = ()
    #: The frame those objectives were graded in (#2609 SF5), or ``None`` for
    #: no previous round, a receipt written before SF5, or a round whose tier
    #: banked no floor. Travels in this pair rather than beside it for the
    #: reason the pair exists at all: a floor read from one record and
    #: objectives from another would be a series remembering two different
    #: pasts, which is precisely the artefact SF5 exists to refuse.
    previous_trusted_floor_hz: float | None = None

    @classmethod
    def first(cls) -> "SeriesPosition":
        """The opening round — nothing has run, nothing was measured."""

        return cls(
            ordinal=1, previous_objectives=None, previous_trusted_floor_hz=None,
        )


def series_position_from_state(raw: Any) -> SeriesPosition:
    """Resolve the next round's series position from durable journey state.

    The READER for the two keys :func:`_write_round_receipt` writes, and it
    lives beside that writer on purpose: the receipt identity is the only place
    a series' history survives between sessions, so the code that parses it and
    the code that emits it must be impossible to drift apart. A reader in
    another module would be a second owner of this shape.

    **Every unreadable shape resolves to the FIRST round**, and the direction is
    deliberate. A corrupt, absent, or older-build receipt means the series'
    history is gone, and the two possible defaults are not symmetric: starting
    over offers a household up to three more rounds it might not need, while
    assuming the cap was reached would silently refuse to iterate at all — the
    exact behaviour #2602 exists to remove, restored by a bad byte on disk.

    **The cap is unconditional since #2609, and this is the other half of
    that.** The identity this reads is now assembled from the round's own
    evaluation and returned whether or not the artifact write succeeded (see
    :func:`_write_round_receipt`), so a broken evidence store costs the series
    its artifacts and not its count. What still resolves to the first round is
    genuinely lost history: no state file, a corrupt one, or a host that never
    persisted the identity at all.

    A previous ordinal at or past the cap still returns ``ordinal + 1``, not a
    clamp: the headroom axis is the one place the cap is enforced, and a reader
    that quietly clamped here would be a second enforcer of the same rule.
    """

    receipt = raw.get("round_receipt") if isinstance(raw, Mapping) else None
    if not isinstance(receipt, Mapping):
        return SeriesPosition.first()
    previous_ordinal = receipt.get("round_ordinal")
    if not isinstance(previous_ordinal, int) or isinstance(previous_ordinal, bool):
        return SeriesPosition.first()
    if previous_ordinal < 1:
        return SeriesPosition.first()
    objectives = receipt.get("objectives")
    if not isinstance(objectives, Mapping):
        # A receipt from before #2602 knows its ordinal only if a #2602 build
        # wrote it, so this is a partially-written or hand-edited record. The
        # ordinal is still usable and the objectives are not: the round runs
        # with no movement to judge, which is exactly the first-round reading
        # of the plateau stop and never a fabricated zero. The floor goes with
        # them — a frame for objectives nobody has is not a fact.
        return SeriesPosition(
            ordinal=previous_ordinal + 1,
            previous_objectives=None,
            previous_blend_correction=_blend_from_receipt(receipt),
            previous_trusted_floor_hz=None,
        )
    return SeriesPosition(
        ordinal=previous_ordinal + 1,
        previous_objectives=FlatnessObjectives(
            tilt_db=_optional_db(objectives.get("tilt_db")),
            ripple_db=_optional_db(objectives.get("ripple_db")),
        ),
        # Absent on every receipt written before decision 10, and on every
        # round that prescribed nothing — both mean "apply no blend
        # correction", which is the honest reading and the safe one.
        previous_blend_correction=_blend_from_receipt(receipt),
        # Absent on every receipt written before SF5, which the headroom axis
        # reads as "no evidence the frame moved" — the same non-refusing
        # direction an unknown floor has everywhere else.
        previous_trusted_floor_hz=_optional_db(receipt.get("trusted_floor_hz")),
    )


def _blend_from_receipt(receipt: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The blend correction the next round should apply, from a banked receipt.

    Unreadable in ANY way resolves to ``()`` — apply no blend correction. That
    is the safe direction and the only one: this list becomes emitted biquads,
    so a half-parsed record must produce no filter rather than a filter nobody
    can vouch for. Cuts-only is re-proved at the emitter regardless; this reader
    refuses earlier so a malformed record never reaches it.

    ``()`` is also what "the previous round prescribed nothing" writes, and the
    two do not need telling apart HERE: both mean the same graph. They are told
    apart on the receipt, whose ``blend.reason`` says which arm fired.
    """

    from .blend_correction import blend_filters_from_mapping

    filters = blend_filters_from_mapping(receipt.get("blend"))
    return () if filters is None else filters


def _optional_db(value: Any) -> float | None:
    """A finite float from persisted JSON, or ``None``.

    ``None`` for anything that is not a real number — including a NaN or an
    infinity a corrupt write could leave behind, which would otherwise sail
    through every comparison in the headroom axis and make a plateau look
    unreachable forever.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
