# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover-v2 domain modules for the intervention loop.

Imports no ``jasper.web`` and nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`; the flow imports these
modules, and the reverse import is what the migration exists to prevent
(pinned in ``test_crossover_v2_journey.py``). Only :mod:`.contracts` is
re-exported below — the rest pull ``numpy`` and the measurement stack at
import, so importers reach them by module path instead.
"""

from __future__ import annotations

from .contracts import (
    PLAN_REFUSAL_REASONS,
    PROPOSAL_FINGERPRINT_KINDS,
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

__all__ = [
    "PLAN_REFUSAL_REASONS",
    "PROPOSAL_FINGERPRINT_KINDS",
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
]
