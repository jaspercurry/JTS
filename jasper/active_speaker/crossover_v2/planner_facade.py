# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Assemble an :class:`InterventionProposal` from today's planner output (#2291).

**This module computes nothing.** Every number in the proposal it returns was
already computed by ``CrossoverV2Conductor``'s existing candidate path; this is
the wrapper that gathers them into one immutable object with one fingerprint,
so callers can consume a proposal instead of reading a scatter of ``_last_*``
scratch fields.  #2291 Phase 2 replaces the *inside* of the planner; the shape
of what it returns is fixed here first, deliberately, so that replacement has
something to be compared against.

**Where the context comes from — and where it must not.** The candidate
acoustic context is derived from the candidate's **own** ``source_preset``, via
:func:`jasper.active_speaker.branch_chain.sections_by_role`, which is already
the repository's single derivation of role → sections.  It is never read from
the conductor's session ``_fc_hz``.  That is the whole point: on 2026-08-10 the
candidate's preset said 1,648.7 Hz while the session field still said
2,000 Hz, and reading the candidate's own preset is what makes the two
impossible to confuse.

**Phase 1 is additive, so assembly failure is not a session failure.** Nothing
consumes the proposal yet, so a contract violation here must not take down a
household's measurement that would otherwise have succeeded.
:func:`plan_intervention_proposal` therefore returns a :class:`PlanRefusal`
instead of raising, and logs it.  From Phase 2, when the proposal *is* the
prescription, that same refusal becomes a real refusal of the intervention.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from jasper.log_event import log_event

from ..branch_chain import CrossoverSection, sections_by_role
from .contracts import (
    CandidateAcousticContext,
    CrossoverV2ContractError,
    InterventionProposal,
    PlanRefusal,
    TrimStrategy,
)

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

# The exceptions an assembly can raise from malformed planner output. Mirrors
# the tuple ``_sweep_fc_candidates`` already uses around ``_evaluate_fc_
# candidate`` rather than a blind ``except Exception`` (ruff BLE, and the
# repository's frozen broad-except budget).
_ASSEMBLY_ERRORS = (
    ArithmeticError,
    AttributeError,
    IndexError,
    KeyError,
    RuntimeError,
    TypeError,
    ValueError,
)


def trim_strategy_for_outcome(linearization_outcome: Any) -> tuple[TrimStrategy, str]:
    """Map today's ``linearization_outcome`` onto an honest strategy + rationale.

    The persisted outcome encodes exactly one bit — whether the ripple scan
    drifted past ``LINEARIZATION_TRIM_SANITY_MARGIN_DB`` from the anchor — and
    says nothing about which pair the level grading then committed.  So this
    returns a ``COMMITTED_PAIR_UNRECORDED*`` strategy for a fitted candidate:
    that is the complete truth available from the artifact, and it is
    deliberately not narrowed to :attr:`TrimStrategy.ANCHORED_COMMITTED` or
    :attr:`TrimStrategy.RESOLVED_COMMITTED` by guessing.  #2291 Phase 2 returns
    the winning pair as data and the precise members become reachable.
    """

    outcome = str(linearization_outcome or "")
    if outcome == "fitted":
        return (
            TrimStrategy.COMMITTED_PAIR_UNRECORDED,
            "a trim pair was committed; the ripple scan stayed within the "
            "sanity margin. The artifact does not record which pair won the "
            "realized-level grading (#2291 Phase 2).",
        )
    if outcome == "trim_rejected":
        return (
            TrimStrategy.COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT,
            "the ripple scan drifted beyond the sanity margin and a trim pair "
            "was still committed. Despite the legacy 'trim_rejected' outcome "
            "name, no trim was rejected; the artifact does not record which "
            "pair won the realized-level grading (#2291 Phase 2).",
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


def build_intervention_proposal(
    candidate: Any,
    *,
    predicted_response_before: Any = None,
    predicted_response_after: Any = None,
    predicted_spec_before: Mapping[str, Any] | None = None,
    predicted_spec_after: Mapping[str, Any] | None = None,
    commanded_delta: Any = None,
    accountability: Mapping[str, Any] | None = None,
    realized_branch_level: Mapping[str, Any] | None = None,
    evidence_identities: Mapping[str, Any] | None = None,
    diagnostic_findings: Sequence[Mapping[str, Any]] | None = None,
) -> InterventionProposal:
    """Gather one already-planned candidate into its proposal contract.

    Raises :class:`CrossoverV2ContractError` when the planner's output cannot
    satisfy the contract.  Callers inside the live conductor should use
    :func:`plan_intervention_proposal`, which converts that into a refusal.
    """

    if candidate is None:
        raise CrossoverV2ContractError("a proposal needs a measured candidate")
    context = CandidateAcousticContext.from_sections(_candidate_sections(candidate))
    strategy, rationale = trim_strategy_for_outcome(
        getattr(candidate, "linearization_outcome", "")
    )
    return InterventionProposal(
        candidate=candidate,
        context=context,
        evidence_identities=evidence_identities,
        predicted_response_before=predicted_response_before,
        predicted_response_after=predicted_response_after,
        predicted_spec_before=predicted_spec_before,
        predicted_spec_after=predicted_spec_after,
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
    predicted_response_before: Any = None,
    predicted_response_after: Any = None,
    predicted_spec_before: Mapping[str, Any] | None = None,
    predicted_spec_after: Mapping[str, Any] | None = None,
    commanded_delta: Any = None,
    accountability: Mapping[str, Any] | None = None,
    realized_branch_level: Mapping[str, Any] | None = None,
    evidence_identities: Mapping[str, Any] | None = None,
    diagnostic_findings: Sequence[Mapping[str, Any]] | None = None,
) -> InterventionProposal | PlanRefusal:
    """:func:`build_intervention_proposal`, but a refusal instead of a raise.

    Emits exactly one stable event either way, so a session always says what it
    proposed or why it could not.

    The parameters are spelled out rather than forwarded as ``**kwargs`` on
    purpose: ``TypeError`` is in :data:`_ASSEMBLY_ERRORS`, so a misspelled
    keyword would otherwise be caught here and returned as a
    ``contract_invalid`` refusal — a programming error wearing a domain
    verdict's clothes. Explicit parameters keep mypy checking the call site.
    """

    try:
        proposal = build_intervention_proposal(
            candidate,
            predicted_response_before=predicted_response_before,
            predicted_response_after=predicted_response_after,
            predicted_spec_before=predicted_spec_before,
            predicted_spec_after=predicted_spec_after,
            commanded_delta=commanded_delta,
            accountability=accountability,
            realized_branch_level=realized_branch_level,
            evidence_identities=evidence_identities,
            diagnostic_findings=diagnostic_findings,
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
        candidate_fc_hz=round(proposal.fc_hz, 6),
        trim_strategy=proposal.trim_strategy.value,
        candidate_fingerprint=proposal.candidate_fingerprint,
    )
    return proposal


def _refusal_for(exc: Exception, candidate: Any) -> PlanRefusal:
    detail = f"{type(exc).__name__}: {exc}"
    if candidate is None:
        return PlanRefusal(reason="no_candidate", detail=detail)
    message = str(exc)
    if "different crossover corners" in message or "disagrees with" in message:
        return PlanRefusal(reason="candidate_fc_disagreement", detail=detail)
    if "at least one crossover section" in message:
        return PlanRefusal(reason="no_crossover_sections", detail=detail)
    return PlanRefusal(reason="contract_invalid", detail=detail)
