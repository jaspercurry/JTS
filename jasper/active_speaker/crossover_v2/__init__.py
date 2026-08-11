# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover-v2 domain modules for the intervention loop (#2291).

The strangler destination package.  Each module arrived with the phase that
needed it — the issue is explicit that its target diagram is not a scaffolding
order — so this list is what exists, not a plan:

* :mod:`.contracts` — the immutable domain values (candidate acoustic context,
  intervention proposal, plan refusal, verification result, adoption decision,
  round receipt) with their construction-time invariants and fingerprints.
* :mod:`.planner_facade` — assembles a proposal from the existing conductor's
  candidate output, computing nothing of its own.
* :mod:`.intervention` — the side-effect-free prescription planner.
* :mod:`.verification` — realization / benefit / spec grading and the adoption
  decision over them.
* :mod:`.round_evidence` — the two measurements a round compares, reduced.
* :mod:`.journey` — the phase walk, its transitions, and the stage capability
  declarations.
* :mod:`.coordinator` — the round's verify-then-adopt tail, behind typed ports.
* :mod:`.programs` — what a session plays, how loud, and for which phase.
* :mod:`.priors` — what the analyzer is told about each capture, and what it
  is deliberately not told.
* :mod:`.spatial` — what a capture-consuming phase decides about one take.
* :mod:`.candidates` — what one candidate build produced, as values.
* :mod:`.accountability` — whether a built candidate may be proposed at all.
* :mod:`.fc_sweep` — which crossover corners this speaker may be asked about,
  what each costs to score, and which one the evidence recommends.

Only :mod:`.contracts` and :mod:`.planner_facade` are re-exported below; the
rest are imported by module path, which is also what keeps a caller that wants
one of them from paying for all of them.

Dependency direction: this package imports no ``jasper.web`` and nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`.  The flow imports these
modules; the reverse import is what the migration exists to prevent.  Pinned by
``test_crossover_v2_journey.test_no_domain_module_imports_the_host_or_the_legacy_flow``,
which walks every module here.
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
