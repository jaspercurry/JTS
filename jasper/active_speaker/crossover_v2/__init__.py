# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover-v2 domain contracts and the planner facade over them (#2291).

The strangler destination package for the v2 intervention loop.  Phase 1 ships
two modules:

* :mod:`.contracts` — the immutable domain values (candidate acoustic context,
  intervention proposal, plan refusal, verification result, adoption decision,
  round receipt) with their construction-time invariants and fingerprints.
* :mod:`.planner_facade` — assembles a proposal from the existing conductor's
  candidate output, computing nothing of its own.

Dependency direction: this package imports no ``jasper.web`` and nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`.  The flow imports these
contracts; the reverse import is what the migration exists to prevent.  The
remaining modules in #2291's target layout (capture plan, spatial, intervention,
verification, journey, host) arrive with the phase that needs them — the issue
is explicit that the diagram is not a scaffolding order.
"""

from __future__ import annotations

from .contracts import (
    PLAN_REFUSAL_REASONS,
    AdoptionDecision,
    AdoptionOutcome,
    BenefitStatus,
    CandidateAcousticContext,
    CaptureValidity,
    CrossoverV2ContractError,
    InterventionProposal,
    PlanRefusal,
    RealizationStatus,
    ResponseCurve,
    RoundReceipt,
    SpecStatus,
    TrimStrategy,
    VerificationResult,
)
from .planner_facade import (
    PROPOSAL_CREATED_EVENT,
    PROPOSAL_REFUSED_EVENT,
    build_intervention_proposal,
    plan_intervention_proposal,
    trim_strategy_for_outcome,
)

__all__ = [
    "PLAN_REFUSAL_REASONS",
    "PROPOSAL_CREATED_EVENT",
    "PROPOSAL_REFUSED_EVENT",
    "AdoptionDecision",
    "AdoptionOutcome",
    "BenefitStatus",
    "CandidateAcousticContext",
    "CaptureValidity",
    "CrossoverV2ContractError",
    "InterventionProposal",
    "PlanRefusal",
    "RealizationStatus",
    "ResponseCurve",
    "RoundReceipt",
    "SpecStatus",
    "TrimStrategy",
    "VerificationResult",
    "build_intervention_proposal",
    "plan_intervention_proposal",
    "trim_strategy_for_outcome",
]
