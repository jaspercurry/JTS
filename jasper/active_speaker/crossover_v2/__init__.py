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
* :mod:`.intervention` — the side-effect-free prescription planner.
* :mod:`.proposal` — the :class:`~.contracts.InterventionProposal` a round
  receipt identifies, assembled.
* :mod:`.verification` — realization / benefit / spec grading and the adoption
  decision over them.
* :mod:`.round_evidence` — the two measurements a round compares, reduced.
* :mod:`.round_views` — the round-grading comparison views a laptop campaign
  had been re-deriving by hand.
* :mod:`.round_anchor` — what an apply displaced, what it put live, and whether
  it still is.
* :mod:`.durable_state` — the durable crossover-v2 state document: every key it
  carries, which of them survive into the next session, and the readers that
  take it apart again.
* :mod:`.journey` — the phase walk, its transitions, and the stage capability
  declarations.
* :mod:`.coordinator` — the round's verify-then-adopt tail, behind typed ports.
* :mod:`.programs` — what a session plays, how loud, and for which phase.
* :mod:`.priors` — what the analyzer is told about each capture, and what it
  is deliberately not told.
* :mod:`.spatial` — what a capture-consuming phase decides about one take.
* :mod:`.candidates` — what one candidate build produced, as values.
* :mod:`.accountability` — whether a built candidate may be proposed at all.
* :mod:`.fc_sweep` — whether a crossover corner is admissible for this
  speaker's declarations, and what its preset looks like re-cornered there.
* :mod:`.topology_prescription` — ONE crossover corner and order, pinned for one
  round: the request gate, its admissibility bounds, and the durable read-back.
* :mod:`.driver_prescription` — ONE driver's full-band shape correction,
  prescribed from outside this process.
* :mod:`.blend_prescription` — ONE blend-region shape correction, prescribed
  from outside this process.
* :mod:`.alignment_prescription` — ONE inter-driver delay, and optionally its
  polarity basin, prescribed from a named measurement.
* :mod:`.prescription_spool` — ONE accepted prescription, waiting for the round
  it was written for.
* :mod:`.blend_correction` — the blend region's summed-response-owned shape
  correction.
* :mod:`.planning` — one candidate assembled: the eligibility gate, the
  planner request its own sections imply, and the emitted candidate.
* :mod:`.admission` — one position's attempt ledger, and whether the next
  ``begin_capture`` may start on it.
* :mod:`.capture_plan` — the walk this session will do, decided before anything
  plays: how many positions, where they are, what the screen says at each, and
  which program every capture index runs.
* :mod:`.position_cycle` — N takes at ONE pose: how they are staged, and how
  they read back.
* :mod:`.capture_dispatch` — which screens an anchor capture (CHECK, MEASURE,
  VERIFY) must clear, and in what order.
* :mod:`.capture_source` — the capture-source seam: what any capture provider
  owes the conductor.
* :mod:`.attempt_grading` — whether a VERIFY capture is a new *tuning* attempt,
  and whether the loop may grade it.
* :mod:`.commanded` — what an apply asks the summed response to CHANGE: the
  applied graph's predicted sum minus the predicted sum of the graph it
  replaces, which is the delta probe's commanded axis.
* :mod:`.evidence_packet` — one round's banked evidence, gathered into one
  document a reader can answer from.
* :mod:`.harmonic_evidence` — H2/H3 read out of one round's banked MEASURE
  captures, and filed.
* :mod:`.feature_classifier` — what KIND of feature is that, measured from a
  round's own banked captures.
* :mod:`.feature_classification` — what KIND of feature is that, read from a
  banked verdict and nothing else.
* :mod:`.refusal_copy` — what the household is told when a round refuses: the
  reason codes, their copy and retry budgets, and the verdict that carries them.
* :mod:`.operator_notes` — everything a human typed, gathered into one labelled
  artifact that no code path reads for a decision.

The engine skeleton (``docs/REFACTOR-TUNING-2026-08.md`` §3 wave 1) — one
session over three lifetimes and ruling S1's four verbs, which both front ends
call and neither extends:

* :mod:`.session` — one tuning session: the three lifetimes it opens once, and
  ``measure`` · ``analyze`` · ``recommend`` · ``save`` over them.
* :mod:`.session_seams` — what that session needs from outside itself: the
  session graph, the volume claim, the record store, and the play transaction.
* :mod:`.session_graph` — the graph seam filled: one measurement graph installed
  once per session and proven before each stimulus, instead of a config swap and
  a duck around every capture.
* :mod:`.volume_claim` — the volume seam filled: one session-measurement claim
  on the ranked owner, holding the handle the seam does not carry.
* :mod:`.playback_transaction` — ready → admit → lock → play → restore, a named
  boundary INSIDE ``measure`` rather than a fifth verb.
* :mod:`.program_transaction` — the play seam filled: one stimulus through
  ``play_program``, reporting the stage it WATCHED rather than one it assumed.
* :mod:`.composition` — the engine stood up around a host: the seam binder,
  the play-seam plumbing, and the graph-is-live proof, importable without
  :mod:`jasper.web` so a second front end can construct the same engine.
* :mod:`.measurement_phase` — which stimulus a measurement KIND plays, in the
  flow's phase words: the one place those two vocabularies meet.
* :mod:`.measure_spec` — what one ``measure`` asks for, and the named stubs for
  the mic-only regimes the engine has not built yet (ruling S12).
* :mod:`.prior_bank` — a previous session's bank, read back: the "before" a
  candidate check grades against, and what that bank already disclosed.
* :mod:`.analysis_units` — what ``analyze`` can run: fifteen named analyses,
  the ``ProgramAnalysis`` fields each owns, and the gate that names the input a
  bank would have to carry for it to run at all.
* :mod:`.analysis_walk` — that table walked over a bank: what one record
  carries, one call into the analysis layer, each admitted unit's own fields
  projected out, and a named reason for every unit the bank could not feed.
* :mod:`.record_store` — the record seam filled: one durable writer over the
  write-once evidence bundle and the session's own state file, and the kind
  table that says where each banked artifact lands.
* :mod:`.record_index` — the little measurement database: six columns over the
  banked takes so a reader can ask for one instead of globbing a directory.
  Derived, rebuildable by rescanning, and never an authority.

Offline evaluation, deliberately not a search:

* :mod:`.forward_model` — what a candidate nothing has played WILL measure,
  from per-driver plants and the shipped filter arithmetic, together with the
  :class:`~jasper.active_speaker.crossover_v2.forward_model.XoverCandidate`
  value it predicts for. Corners are DECLARED by the operator; this module
  predicts what a variation of one would measure, at zero capture cost, and
  nothing here ranks candidates or recommends a corner.

Only :mod:`.contracts` is re-exported below, and :mod:`.forward_model` is
deliberately not added to it: it pulls ``numpy`` and the measurement stack at
import, so re-exporting it would make every importer of this package pay for a
prediction it is not running. The rest are imported by module path for the
same reason.

The list above is deliberately unnumbered.  A stated count goes stale the next
time a module arrives or leaves — as one did in #2291 Phase 5c-iii — and a
count that disagrees with the list is worse than no count at all.  It is,
however, COMPLETE: every module in this package appears above, and
``test_package_enumeration_contract.py`` fails by name when one does not.  A
list that quietly goes partial is worse than no list, because a reader takes
the omission for "no such thing" — this one had drifted to fourteen unnamed
modules before that guard existed.

Dependency direction: this package imports no ``jasper.web`` and nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`.  The flow imports these
modules; the reverse import is what the migration exists to prevent.  Pinned by
``test_crossover_v2_journey.test_no_domain_module_imports_the_host_or_the_legacy_flow``,
which walks every module here.
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
