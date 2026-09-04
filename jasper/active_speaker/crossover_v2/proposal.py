# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Assemble the :class:`InterventionProposal` a round receipt identifies (#2392).

This module computes nothing; it gathers values the candidate path already
holds into one immutable object with one fingerprint. Two rules it must not
break: the acoustic context comes from the candidate's OWN ``source_preset``
and never from the session's ``_fc_hz`` (the two can disagree), and a proposal
that cannot be assembled costs the round its proposal identity, never its
candidate — hence :func:`plan_intervention_proposal` refuses instead of raising.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.log_event import log_event

from ..branch_chain import CrossoverSection, sections_by_role
from .contracts import (
    LINEARIZATION_OUTCOME_SINGLE_BRANCH,
    PLAN_REFUSAL_REASONS,
    CandidateAcousticContext,
    CrossoverV2ContractError,
    InterventionProposal,
    PlanRefusal,
    TrimStrategy,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .candidates import LinearizationState

__all__ = [
    "PROPOSAL_CREATED_EVENT",
    "PROPOSAL_REFUSED_EVENT",
    "build_intervention_proposal",
    "plan_intervention_proposal",
    "trim_strategy_for_outcome",
]

logger = logging.getLogger(__name__)

PROPOSAL_CREATED_EVENT = "correction.crossover_v2_intervention_proposal"
PROPOSAL_REFUSED_EVENT = "correction.crossover_v2_intervention_proposal_refused"

# Enumerated rather than a blind ``except Exception`` (ruff BLE; the frozen
# broad-except budget).
_ASSEMBLY_ERRORS = (
    ArithmeticError,
    AttributeError,
    IndexError,
    KeyError,
    RuntimeError,
    TypeError,
    ValueError,
)


def trim_strategy_for_outcome(
    linearization_outcome: Any, *, linearization: LinearizationState | None = None,
) -> tuple[TrimStrategy, str]:
    """State the committed strategy, from the build's own record where it has one.

    :class:`~.candidates.LinearizationState` carries what
    :func:`~.intervention.decide_trim` chose, so a build that hands over its own
    state is quoted rather than second-guessed. An assembly given only the
    candidate falls back to the ``linearization_outcome`` string, and that
    string does not encode which pair won the realized-level grading:
    ``"fitted"`` stays :attr:`TrimStrategy.COMMITTED_PAIR_UNRECORDED` rather
    than being narrowed by guessing.
    """

    recorded = None if linearization is None else linearization.trim_strategy
    drift_db = None if linearization is None else linearization.anchor_drift_db
    if recorded is not None and drift_db is not None:
        return recorded, (
            f"the build recorded {recorded.value} as the committed pair; the "
            f"ripple scan sat {drift_db:.3f} dB from the level-preserving "
            "anchor."
        )
    outcome = str(linearization_outcome or "")
    if outcome == LINEARIZATION_OUTCOME_SINGLE_BRANCH:
        return (
            TrimStrategy.NO_PAIR_TO_TRIM,
            "the speaker has one branch, so there is no inter-driver trim to "
            "commit; its linearization filters ship at a fixed 0 dB.",
        )
    if outcome == "fitted":
        return (
            TrimStrategy.COMMITTED_PAIR_UNRECORDED,
            "a trim pair was committed; the ripple scan stayed within the "
            "sanity margin. The candidate's linearization outcome does not "
            "record which pair won the realized-level grading.",
        )
    if outcome == "trim_rejected":
        return (
            TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT,
            "the ripple scan drifted beyond the sanity margin, so it was "
            "rejected and the level-preserving anchored pair was committed.",
        )
    return (
        TrimStrategy.NOT_FITTED,
        f"no linearization trim pair was fitted (outcome {outcome!r})."
        if outcome
        else "no linearization trim pair was fitted.",
    )


def _candidate_sections(candidate: Any) -> dict[str, tuple[CrossoverSection, ...]]:
    """The candidate's own sections, from its own preset. One derivation only."""

    preset = getattr(candidate, "source_preset", None)
    regions = getattr(preset, "crossover_regions", ()) or ()
    return sections_by_role(regions)


def _candidate_roles(candidate: Any) -> tuple[str, ...]:
    """The branches this candidate carries, read off its own committed trims.

    ``MeasuredCrossoverCandidate`` refuses a trim map that does not cover
    exactly the preset's driver roles, so the map states the shape.
    """
    trims = getattr(candidate, "role_attenuations_db", None)
    return tuple(trims) if isinstance(trims, Mapping) else ()


def build_intervention_proposal(
    candidate: Any,
    *,
    predicted_response_after: Any = None,
    commanded_delta: Any = None,
    accountability: Mapping[str, Any] | None = None,
    realized_branch_level: Mapping[str, Any] | None = None,
    evidence_identities: Mapping[str, Any] | None = None,
    diagnostic_findings: Sequence[Mapping[str, Any]] | None = None,
    linearization: LinearizationState | None = None,
) -> InterventionProposal:
    """Gather one already-planned candidate into its proposal contract.

    Raises :class:`CrossoverV2ContractError` when the planner's output cannot
    satisfy the contract; the live commit seam uses
    :func:`plan_intervention_proposal`, which converts that into a refusal. The
    five fields left at their contract defaults are the ones the commit seam
    does not hold — a proposal may not quote a spec report it was never given.
    ``linearization`` is this build's :class:`~.candidates.LinearizationState`;
    an assembly without it falls back to the candidate's own artifact string
    for the trim strategy.
    """

    if candidate is None:
        raise CrossoverV2ContractError("a proposal needs a measured candidate")
    # The 1-way-main exemption (no corner to own) belongs to the context type,
    # not here.
    context = CandidateAcousticContext.for_candidate(
        _candidate_sections(candidate), roles=_candidate_roles(candidate),
    )
    strategy, rationale = trim_strategy_for_outcome(
        getattr(candidate, "linearization_outcome", ""), linearization=linearization,
    )
    return InterventionProposal(
        candidate=candidate,
        context=context,
        evidence_identities=evidence_identities,
        predicted_response_after=predicted_response_after,
        commanded_delta=commanded_delta,
        trim_strategy=strategy,
        trim_rationale=rationale,
        realized_branch_level=realized_branch_level,
        linearization_filters=getattr(candidate, "linearization", None),
        excluded_regions=getattr(candidate, "exclusion_evidence", None),
        accountability=accountability,
        diagnostic_findings=diagnostic_findings,
    )


def plan_intervention_proposal(
    candidate: Any,
    *,
    session_id: str = "",
    predicted_response_after: Any = None,
    commanded_delta: Any = None,
    accountability: Mapping[str, Any] | None = None,
    realized_branch_level: Mapping[str, Any] | None = None,
    evidence_identities: Mapping[str, Any] | None = None,
    diagnostic_findings: Sequence[Mapping[str, Any]] | None = None,
    linearization: LinearizationState | None = None,
) -> InterventionProposal | PlanRefusal:
    """:func:`build_intervention_proposal`, but a refusal instead of a raise.

    Emits exactly one event either way. Parameters stay spelled out rather than
    ``**kwargs``: ``TypeError`` is in :data:`_ASSEMBLY_ERRORS`, so a misspelled
    keyword would come back as a ``contract_invalid`` refusal instead of a
    type error at the call site.
    """

    try:
        proposal = build_intervention_proposal(
            candidate,
            predicted_response_after=predicted_response_after,
            commanded_delta=commanded_delta,
            accountability=accountability,
            realized_branch_level=realized_branch_level,
            evidence_identities=evidence_identities,
            diagnostic_findings=diagnostic_findings,
            linearization=linearization,
        )
    except _ASSEMBLY_ERRORS as exc:
        refusal = _refusal_for(exc, candidate)
        log_event(
            logger,
            PROPOSAL_REFUSED_EVENT,
            level=logging.WARNING,
            session_id=session_id,
            reason=refusal.reason,
            detail=refusal.detail,
            fc_hz=refusal.fc_hz,
        )
        return refusal
    log_event(
        logger,
        PROPOSAL_CREATED_EVENT,
        session_id=session_id,
        proposal_fingerprint=proposal.fingerprint,
        candidate_fc_hz=(
            None if proposal.fc_hz is None else round(proposal.fc_hz, 6)
        ),
        trim_strategy=proposal.trim_strategy.value,
        candidate_fingerprint=proposal.candidate_fingerprint,
    )
    return proposal


def _refusal_for(exc: Exception, candidate: Any) -> PlanRefusal:
    """Classify one assembly failure onto the closed refusal vocabulary.

    By exception type and its ``refusal_reason`` attribute, never by message
    prose (#2307 gate note N5).
    """

    detail = f"{type(exc).__name__}: {exc}"
    if candidate is None:
        return PlanRefusal(reason="no_candidate", detail=detail)
    reason = getattr(exc, "refusal_reason", "contract_invalid")
    # An unrecognised reason would fail ``PlanRefusal``'s closed-vocabulary
    # check and take the session down; fall back to the generic member.
    if reason not in PLAN_REFUSAL_REASONS:
        reason = "contract_invalid"
    return PlanRefusal(reason=reason, detail=detail)
