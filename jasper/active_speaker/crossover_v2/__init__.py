# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover-v2 domain modules for the intervention loop.

This package imports no ``jasper.web`` and nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`; the flow imports these modules
and the reverse import is what the migration exists to prevent. Only
:mod:`.contracts` is re-exported below — several of the rest pull ``numpy`` and
the measurement stack at import.

The list is COMPLETE and ``test_package_enumeration_contract.py`` fails by name
when it stops being: every module on disk is named here.

* :mod:`.contracts` — the immutable domain values, with their construction-time
  invariants and fingerprints.
* :mod:`.intervention` — the side-effect-free prescription planner.
* :mod:`.plan_assembly` — the composed prediction of a linearized branch sum,
  and the frozen plan it lands in.
* :mod:`.proposal` — the :class:`~.contracts.InterventionProposal` a round
  receipt identifies, assembled.
* :mod:`.verification` — realization / benefit / spec grading, the adoption
  decision over them, and one VERIFY capture's own record.
* :mod:`.round_evidence` — the two measurements a round compares, reduced.
* :mod:`.tuning_scope` — the comparability fingerprint a round banks at entry:
  the graph's content hashed over the layers it measures through, preference
  EQ excluded.
* :mod:`.round_inputs` — where one round's evidence inputs are, for a banked
  tree and for a live session bundle on the box.
* :mod:`.round_views` — the round-grading comparison views.
* :mod:`.frequency_view` — retained round packets translated into the neutral
  :mod:`jasper.active_speaker.frequency_view` web/LLM contract.
* :mod:`.durable_state` — the durable crossover-v2 state document: the phase
  snapshot a session banks, which keys survive into the next session, and the
  readers that take it apart again.
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
* :mod:`.topology_prescription` — ONE crossover corner and order, pinned for
  one round: the request gate, its bounds, and the durable read-back.
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
* :mod:`.planning` — one candidate assembled: the eligibility gate, the planner
  request its own sections imply, and the emitted candidate.
* :mod:`.admission` — one position's attempt ledger, and whether the next
  ``begin_capture`` may start on it.
* :mod:`.capture_plan` — the walk this session will do, decided before anything
  plays: how many positions, where they are, what the screen says at each, and
  which program every capture index runs.
* :mod:`.sweep_spec` — the validated sweep spec a session opens on: the plan,
  the 48 kHz mono capture format, and the consent surface.
* :mod:`.position_cycle` — N takes at ONE pose: how they are staged, and how
  they read back.
* :mod:`.capture_dispatch` — which screens an anchor capture (CHECK, MEASURE,
  VERIFY) must clear, and in what order.
* :mod:`.capture_source` — the capture-source seam: what any capture provider
  owes the session.
* :mod:`.attempt_grading` — the tuning-attempt ledger's constants: the ungraded
  status and the two improvement thresholds.
* :mod:`.commanded` — what an apply asks the summed response to CHANGE: the
  applied graph's predicted sum minus that of the graph it replaces.
* :mod:`.evidence_packet` — one round's banked evidence, gathered into one
  document a reader can answer from.
* :mod:`.handoff_doors` — the request-time doors that describe a handoff
  between two branches, and how each refuses a speaker with no crossover
  region.
* :mod:`.harmonic_evidence` — H2/H3 read out of one round's banked MEASURE
  captures, and filed.
* :mod:`.ring_projection` — a banked round re-projected into the capture-ring
  layout, out of WAVs and records the bundle already has.
* :mod:`.feature_classifier` — what KIND of feature is that, measured from a
  round's own banked captures.
* :mod:`.feature_optics` — how a feature is read off a magnitude curve: the
  smoothing and detrend fractions, the spans its size and width are read over,
  the window lead, and the minimum-phase section that synthesizes one. The
  shared bottom the classifier, the gate sweep and the close reference read
  through, so no two of them read one feature differently.
* :mod:`.close_reference` — a close capture corrected to the far distance,
  sub-sample aligned and subtracted, so each spec band says whether the far
  read was the speaker or the room.
* :mod:`.feature_classification` — what KIND of feature is that, read from a
  banked verdict and nothing else.
* :mod:`.refusal_copy` — what the household is told when a round refuses: the
  reason codes, their copy and retry budgets, and the verdict that carries them.
* :mod:`.operator_notes` — everything a human typed, gathered into one labelled
  artifact that no code path reads for a decision.
* :mod:`.diagnostics` — the commissioning journal: the log fields a capture's
  numbers reduce to, and the emitters that write one line per consumed capture.
* :mod:`.delta_probe_run` — running the delta probe on one round: the CHANGE
  and STATE axes, the four optional accounting terms, and the journal line the
  verdict is read off.
* :mod:`.session` — one tuning session: the three lifetimes it opens once, and
  ``measure`` over them.
* :mod:`.session_seams` — what that session needs from outside itself: the
  session graph, the volume claim, the record store, and the play transaction.
* :mod:`.session_graph` — the graph seam filled: one measurement graph
  installed once per session and proven before each stimulus.
* :mod:`.volume_claim` — the volume seam filled: one session-measurement claim
  on the ranked owner, holding the handle the seam does not carry.
* :mod:`.playback_transaction` — ready → admit → lock → play → restore, a named
  boundary INSIDE ``measure`` rather than a fifth verb.
* :mod:`.program_transaction` — the play seam filled: one stimulus through
  ``play_program``, reporting the stage it WATCHED rather than one it assumed.
* :mod:`.composition` — the engine stood up around a host: the seam binder, the
  play-seam plumbing, and the graph-is-live proof, importable without
  :mod:`jasper.web` so a second front end can construct the same engine.
* :mod:`.door` — the kernel path an operator door rides onto a held speaker:
  the interlock, the isolation window, the volume plan and the measurement
  graph, opened in one order and given back in its mirror.
* :mod:`.measurement_phase` — which stimulus a measurement KIND plays, in the
  flow's phase words: the one place those two vocabularies meet.
* :mod:`.measure_spec` — what one ``measure`` asks for, and the named stubs for
  the mic-only regimes the engine has not built yet.
* :mod:`.delay_landscape` — the inter-driver delay, computed then confirmed:
  two banked transfers complex-summed across ``null_walk``'s whole grid to
  propose a coordinate without playing a note, and the grader that decides
  whether the acoustic confirmation agreed with it.
* :mod:`.forward_model` — what the speaker would measure for a candidate
  nothing has played: a round's two banked solos summed through one candidate's
  filters, trims, delay and polarity, offline, and the predicted-vs-measured
  delta where the round also banked a VERIFY sum.
* :mod:`.round_captures` — a round's summed captures, each bound to the program
  its BYTES were played through and deconvolved.
* :mod:`.gate_sweep` — room or speaker, decided on a ladder of gate windows:
  one round's captures read at every rung and every declared pose, with the
  window's own bias subtracted by a matched null model.
* :mod:`.record_store` — the record seam filled: one durable writer over the
  write-once evidence bundle and the session's own state file, and the kind
  table that says where each banked artifact lands.
* :mod:`.record_index` — selecting banked takes: seven columns rescanned from
  the take files on every read, so a reader can ask for one instead of globbing
  a directory. No file, no authority — the takes decide.

Only :mod:`.contracts` is re-exported below. Several of the rest pull
``numpy`` and the measurement stack at import, so re-exporting them would make
every importer of this package pay for work it is not running; all of them are
imported by module path instead.

The list above is deliberately unnumbered.  A stated count goes stale the next
time a module arrives or leaves — as one did in #2291 Phase 5c-iii — and a
count that disagrees with the list is worse than no count at all.  It is,
however, COMPLETE: every module in this package appears above.  A list that
quietly goes partial is worse than no list, because a reader takes the
omission for "no such thing".

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
