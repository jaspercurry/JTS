# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Assemble the :class:`InterventionProposal` a round receipt identifies (#2392).

**This module computes nothing.** Every number in the proposal it returns was
already computed by the candidate path that calls it; this is the assembler
that gathers them into one immutable object with one fingerprint, so the
durable round receipt can name *the proposal that was made* instead of the
candidate that happened to be committed.

**Why it lives here and not on the session.** The one caller is
:meth:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session.commit_intervention_proposal`,
and putting the assembly inline there would add a pure, side-effect-free
derivation to the 9,000-line file #2291 has spent five phases hollowing.  The
in-repo precedent is exact:
:func:`~jasper.active_speaker.crossover_v2.round_evidence.build_round_receipt`
is a named assembler in this package that the coordinator calls with values it
already holds, for the reason stated there — "a receipt built by hand at a call
site is a receipt whose field set can quietly diverge from the issue's list".
The same is true of a proposal, and a proposal now feeds that receipt.

**Where the context comes from — and where it must not.** The candidate
acoustic context is derived from the candidate's **own** ``source_preset``, via
:func:`jasper.active_speaker.branch_chain.sections_by_role`, which is already
the repository's single derivation of role → sections.  It is never read from
the session's ``_fc_hz``.  That is the whole point: on 2026-08-10 the
candidate's preset said 1,648.7 Hz while the session field still said
2,000 Hz, and reading the candidate's own preset is what makes the two
impossible to confuse.

**Assembly failure is not a session failure, and that is now load-bearing
rather than provisional.**  The caller is the commit seam: it performs
irreversible acts (``publish_candidate``) for a candidate the household has
already confirmed.  A proposal that cannot be assembled must therefore cost the
round its *proposal identity*, never its candidate.
:func:`plan_intervention_proposal` returns a :class:`PlanRefusal` instead of
raising, and the receipt then records the candidate fingerprint under an
explicitly-named kind — see
:data:`~.contracts.PROPOSAL_FINGERPRINT_KINDS`.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from jasper.log_event import log_event

from ..branch_chain import CrossoverSection, sections_by_role
from .contracts import (
    PLAN_REFUSAL_REASONS,
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

# The exceptions an assembly can raise from malformed planner output. An
# enumeration rather than a blind ``except Exception`` (ruff BLE, and the
# repository's frozen broad-except budget), and the same family this package's
# sibling port guards name.
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
    """Map the persisted ``linearization_outcome`` onto an honest strategy.

    **Why the strategy is derived from the artifact string rather than read off
    the live :class:`~.intervention.TrimDecision`.** The proposal is assembled
    at the ONE commit seam, and the walk that reaches it holds a
    :class:`~.candidates.LinearizationState` — which retains the realized level
    match but **not** the trim decision.  The decision is a by-product of the
    fit that the state deliberately does not carry, so the fact actually in
    hand at the seam is the string the build stamped onto the candidate, and
    this function derives from that.

    The derivation used to serve a second commit route as well — an
    alternative-Fc selection holding only a serialized
    :class:`~jasper.active_speaker.fc_selector.FcCandidateEvaluation`, which is
    why the artifact string was the one fact BOTH routes shared.  That route
    closed with the corner hunt on 2026-08-21 (``docs/tuning-master-plan.md``
    ticket 2.3); the derivation is unchanged, because the surviving route never
    held the decision either.

    **``"trim_rejected"`` is precise, and provably so.**
    :attr:`~.intervention.TrimDecision.outcome` returns ``"trim_rejected"`` if
    and only if :attr:`~.intervention.TrimDecision.beyond_sanity_margin`, and
    :func:`~.intervention.decide_trim` commits the anchor — strategy
    :attr:`TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT` — on exactly
    that branch.  The two are the same bit read twice, not an inference, and
    ``test_the_drift_outcome_string_and_the_drift_strategy_are_the_same_bit``
    pins the equivalence in both directions.

    **``"fitted"`` is not precise, and says so.** Which pair won the realized-
    level grading is decided *after* the margin test and is not encoded in the
    string, so the honest answer is
    :attr:`TrimStrategy.COMMITTED_PAIR_UNRECORDED` — deliberately not narrowed
    to ``ANCHORED_COMMITTED``/``RESOLVED_COMMITTED`` by guessing.
    """

    outcome = str(linearization_outcome or "")
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


def build_intervention_proposal(
    candidate: Any,
    *,
    predicted_response_after: Any = None,
    commanded_delta: Any = None,
    accountability: Mapping[str, Any] | None = None,
    realized_branch_level: Mapping[str, Any] | None = None,
    evidence_identities: Mapping[str, Any] | None = None,
    diagnostic_findings: Sequence[Mapping[str, Any]] | None = None,
) -> InterventionProposal:
    """Gather one already-planned candidate into its proposal contract.

    Raises :class:`CrossoverV2ContractError` when the planner's output cannot
    satisfy the contract.  Callers inside the live commit seam should use
    :func:`plan_intervention_proposal`, which converts that into a refusal.

    The parameters are the values the commit seam actually holds.  FIVE of the
    proposal's fields are left at their contract defaults rather than filled
    with a near-enough substitute, because
    :class:`InterventionProposal`'s own docstring says a field that cannot be
    stated is empty *honestly*:

    * ``predicted_response_before``, ``predicted_spec_before`` and
      ``predicted_spec_after`` — the commit seam does not hold them.  The walk
      installs its spec report out-of-band, so this assembler is never handed
      one, and a proposal may not quote a report it was not given.
    * ``anchored_trim_db`` and ``alternative_trim_db`` — the contract's own
      placeholders, ``None`` until a phase returns them as data (their field
      docstrings say so).  The call below is the only
      ``InterventionProposal(...)`` in ``jasper/`` and passes neither keyword,
      so no shipped proposal carries them.  ``tests/test_crossover_v2_contracts``
      does pass both, constructing the dataclass directly to pin what they
      contribute to the fingerprint — a contract test, not a shipped proposal.
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
            predicted_response_after=predicted_response_after,
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
    """Classify one assembly failure onto the closed refusal vocabulary.

    **By type, never by prose** (#2307 gate note N5). The first cut read the
    exception's message for phrases like ``"disagrees with"``, which made the
    household-facing reason a function of wording that no test owned: rephrase
    a validator's message and a ``candidate_fc_disagreement`` silently becomes
    a generic ``contract_invalid``. :class:`~.contracts.CrossoverV2ContractError`
    carries its own ``refusal_reason``, so the classification travels with the
    raise instead of being re-derived here.
    """

    detail = f"{type(exc).__name__}: {exc}"
    if candidate is None:
        return PlanRefusal(reason="no_candidate", detail=detail)
    reason = getattr(exc, "refusal_reason", "contract_invalid")
    # A non-contract exception (``KeyError`` on malformed planner output, say)
    # has no ``refusal_reason``, and an unrecognised one would fail
    # ``PlanRefusal``'s own closed-vocabulary check — which is the right
    # failure for a bug, and the wrong one to take a household's session down
    # with. Fall back to the generic member instead.
    if reason not in PLAN_REFUSAL_REASONS:
        reason = "contract_invalid"
    return PlanRefusal(reason=reason, detail=detail)
