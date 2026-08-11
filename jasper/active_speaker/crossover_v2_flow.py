# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor — phase orchestration (Wave 5a).

``docs/crossover-measurement-productization-design.md`` §5 replaces the legacy
per-driver distributed transaction with a **conductor**: the Pi compiles one
excitation program per phase, plays it as one continuous stream, and analyzes
``(program, capture) → analysis`` as a pure function. This module owns the
phase state machine that drives the relay session — 16 captures at the FULL
tier's shipped defaults (7 on the express tier, ``TIER_EXPRESS``), since the
spatial cloud replaced the original three:

    CHECK → gain solve → MEASURE → the pre-apply position group → fit +
      candidate → APPLYING (auto) → VERIFY → the post-apply position group
      → done

**Owner decision (2026-07-27): the fit is the last thing before the apply.**
The candidate used to be built the moment MEASURE was accepted, which put it
eight captures BEFORE the pre-apply cloud whose honesty verdict it is supposed
to consume — so the two optional cloud terms in ``compose_envelope`` had no
reachable production caller. Building it at the group close instead lets the
fit correct the envelope around the interference the cloud identified and
refuse to fill it (flat-linearization plan, interpretation call (A)). MEASURE
keeps every trust gate it owned: they read the analysis, not the candidate, so
a session doomed at sweep two still fails at sweep two rather than after a nine
-position walk. A session with no pre-apply group (the pre-cloud 3-entry shape
this class still defaults to) has nothing to wait for and still builds at
MEASURE, with the same accept, the same payload keys and the same apply timing
it had before the move — its ``candidate.json`` does gain an always-empty
``exclusion_evidence`` key, which leaves the fingerprint unchanged.
See :meth:`CrossoverV2Conductor._measure_verdict`.

**Owner ruling (2026-07-20): no human mid-flow Apply gate.** A hardware
session proved the prior REVIEW/APPLY human tap a dead end — phone-only
users cannot bounce to a second browser tab, and "apply this?" is
unanswerable the moment after measuring (the household has no basis to
judge). A trusted candidate (all quality gates pass, including
:data:`ALIGNMENT_CONFIDENCE_TRUST_FLOOR`, promoted here from a review-screen
nudge to a hard gate) is applied by the conductor itself; an untrusted one is
rejected with guidance to re-measure, never a question. See
[docs/HANDOFF-crossover-measurement-v2.md](../../docs/HANDOFF-crossover-measurement-v2.md)
gotcha #18.

It is deliberately I/O-free: every side effect (playback, analysis, evidence
publish, apply-gate observation) crosses an INJECTED seam
(:class:`V2FlowSeams`), exactly as :func:`jasper.active_speaker.program_playback.play_program`
and :class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` inject
their DSP / volume seams. That keeps the whole state walk fixture-testable with
fake seams, and lets Wave 6 bind the real CamillaController-backed playback, the
``analyze_program_capture`` call, the verified-WAV source, and the
``commissioning_service`` publish/apply chain without touching this logic.

The conductor exposes the three ``run_capture_plan`` callbacks
(:meth:`authorize_begin`, :meth:`on_armed`, :meth:`consume_capture`) plus the
lifecycle hooks the flow needs (:meth:`note_apply_complete`,
:meth:`snapshot`/:meth:`hydrate` for phase persistence + session binding). One
journey spans TWO relay sessions since the two-stage split (work order D1/D2,
issue #1806), each a heterogeneous ``CapturePlan``: **stage 1** is check /
measure / the pre-apply position group (10 entries at the full tier's shipped
defaults, 6 on express), and **stage 2** is verify / the post-apply position
group (6 at Full, 1 on express, which omits the group entirely). See
"position-group choreography" below. **Nothing is applied inside a session** —
stage 1 ends on the household's explicit set-completion signal, which closes
the group and publishes a candidate they then review and choose to apply on
jts.local. VERIFY's soft hold behind :class:`CaptureBeginDeferred` is retained
machinery that no shipped session reaches (D10): stage 1 has no VERIFY index
and stage 2 is constructed already-applied.

**Failure taxonomy (§5.10).** Terminal verdicts are internal reason codes, not
screens: :data:`REASON_REGISTRY` maps each code to one of the four screen
templates, its owning phase, and its retry budget. The conductor decides the
code + accepted verdict; the envelope (:mod:`jasper.active_speaker.crossover_envelope_v2`)
renders the template. A woofer-repeat level disagreement REUSES
``drift_baselines_disagree`` — never a new user-facing code (§5.2).
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Mapping,
    Protocol,
    Sequence,
)

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only, and imported lazily at the one runtime use site
    # (``_consume_entry_baseline``): ``crossover_v2.round_evidence`` reaches
    # :mod:`jasper.active_speaker.flat_spec` through
    # :mod:`jasper.active_speaker.crossover_v2.verification`, and this module
    # already imports ``flat_spec`` lazily everywhere else for that reason.
    from jasper.active_speaker.crossover_v2.coordinator import RoundPorts
    from jasper.active_speaker.crossover_v2.round_evidence import (
        EntryBaseline,
        MeasuredResponse,
    )

from jasper.active_speaker.attempts_loop import (
    PROVENANCE_REALIZED,
    REASON_ATTEMPT_NOT_COMPARABLE,
    STOP_EVIDENCE,
    AttemptBudget,
    AttemptIntegrity,
    AttemptRecord,
    FloorStats,
    LoopDecision,
    decide_next,
)
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_MODEL_ERROR,
    VERDICT_SPATIALLY_COSTLY,
    DeltaProbeMap,
    classify_delta_probe,
    spatial_cost_from_group_spreads,
)
from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    chain_response,
    crossover_response_complex,
    radiating_band_hz,
    sections_by_role,
)
from jasper.active_speaker.crossover_v2 import priors as _priors
from jasper.active_speaker.crossover_v2 import programs as _programs
from jasper.active_speaker.crossover_v2 import spatial as _spatial
from jasper.active_speaker.crossover_v2.contracts import (
    ENTRY_GRAPH_FINGERPRINT_UNKNOWN as _ENTRY_GRAPH_FINGERPRINT_UNKNOWN,
)
from jasper.active_speaker.crossover_v2.contracts import (
    CandidateAcousticContext,
    InterventionProposal,
    PlanRefusal,
)

# #2291 Phase 2 moved the prescription policy — the two Layer-1a constants, the
# σ-composition table and gate, and three small pure derivations — into the
# planner module, because a pure planner cannot import this one (the dependency
# runs one way: flow → crossover_v2, never back). They are re-exported here
# under their historical private names so every existing importer keeps
# resolving to the single definition rather than growing a second copy.
#
# The Phase 2b cutover deleted the legacy fitter, and with it this module's own
# last reads of ``compose_sigma_db`` and ``rounded_band_hz``. Rather than keep
# two imports alive purely as a door for outside callers, those callers now
# import from the module that OWNS them; only what this module still reads is
# imported here.
from jasper.active_speaker.crossover_v2.intervention import (
    LINEARIZATION_MIN_PAIRED_OCCURRENCES,
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    JournalRecord,
    LinearizationPlan,
    driver_response_by_role as _driver_response_by_role,
    measure_validity_floor_hz as _measure_validity_floor_hz,
    plan_linearization,
    request_from_analysis,
)
from jasper.active_speaker.crossover_v2.journey import (
    CAPTURE_PHASES,
    GROUP_PHASES,
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOSING,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_REVIEW,
    PHASE_VERIFY,
    CommissionJourney,
    JourneyPlan,
)
from jasper.active_speaker.crossover_v2.planner_facade import (
    plan_intervention_proposal,
)
from jasper.active_speaker.fc_selector import (
    EVAL_REFUSED_BUDGET,
    EVAL_REFUSED_UNFITTABLE,
    FcCandidateEvaluation,
    FcSelection,
    fc_comparison_complete,
    select_fc,
)
from jasper.active_speaker.camilla_yaml import role_polarity
from jasper.active_speaker.linearization_fit import (
    linearization_filters_by_role,
    worst_headroom_cost_db,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
    KIND_SWEEP,
    STIMULUS_KINDS,
    VERIFY_PILOT_ROLE,
    ExcitationProgram,
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    INTEGRITY_CHECK_SWEEP_HEARD,
    CaptureIntegrity,
    GainPlan,
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    RealizedLevelMatch,
    overlap_band_hz,
    summed_model_residual_delay_us,
)
from jasper.capture_relay.session import CaptureBeginDeferred, CaptureBeginRefused
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# phase vocabulary — owned by crossover_v2.journey, imported at the top
# --------------------------------------------------------------------------- #
#
# The phase names, their canonical capture order (``CAPTURE_PHASES``) and which
# of them are position groups (``GROUP_PHASES``) moved to
# :mod:`jasper.active_speaker.crossover_v2.journey` in #2291 Phase 4: a journey
# is made of them, and it cannot import this module (the strangler destination
# does not import the monolith it replaces). They are re-exported from here —
# the same objects, not copies — so every ``from ...crossover_v2_flow import
# PHASE_CHECK`` keeps working, and ``__all__`` below still lists them.
#
# The phase-ADJACENT constants stay here with the concern that owns them:
# ``_INDEX_PHASE`` and ``CAPTURE_PLAN_TARGET`` describe the relay capture plan,
# ``SUMMED_SWEEP_PHASES`` selects an excitation program, and
# ``PRE_CLOUD_CAPTURE_PHASES`` records what a session ran before the position
# groups shipped. None of those answers "where is this round".

# Where the pre-apply cloud's close has got to. Read by the wizard through
# durable state; see :attr:`V2ConductorSnapshot.cloud_close`.
CLOUD_CLOSE_NONE = ""
CLOUD_CLOSE_AWAITING_CONFIRM = "awaiting_confirm"
CLOUD_CLOSE_RUNNING = "running"

# The absolute VERIFY tracking error used by both the live attempts loop and
# the offline repeat-floor replay. Lower is better: zero is the model's
# prediction of perfect realization, while the analyzer's value is what the
# applied speaker actually realized.
ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED = "max_db_notch_excluded"

# A flow-owned status, deliberately not a synthetic kernel decision. The
# kernel requires a real FloorStats and the store returns ``None`` until an
# offline repeat study adopts one, so the honest live result is ungraded — no
# invented floor and no improvement claim.
ATTEMPT_REASON_NO_FLOOR = "ungraded_no_floor"
ATTEMPT_INTEGRITY_UNAVAILABLE = "capture_integrity_unavailable"

# Capture-plan index → phase. APPLYING is a control-page phase (no capture)
# that sits between MEASURE-accepted and VERIFY-armed, so it has no index.
# This is the pre-cloud 3-entry layout, kept as the fallback for a conductor
# constructed with no explicit ``index_phase_map``; the shipped session builds
# its map through ``build_v2_cloud_index_phase_map``.
_INDEX_PHASE = {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY}
_PHASE_INDEX = {phase: index for index, phase in _INDEX_PHASE.items()}
CAPTURE_PLAN_TARGET = 3

# This flow's own capture retry budget: the total admission attempts a v2
# session may spend across its entries, including retaken captures.
#
# It is deliberately NOT `capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`. Both
# builders below passed that ceiling verbatim while the two happened to be
# equal, which silently conflated a TRANSPORT limit (how many blob keys the
# relay Worker will store for one session) with a POLICY choice (how many
# retakes this measurement offers a household). Raising the transport ceiling
# to 32 for multi-position capture plans separated them, and this constant
# holds the shipped value so the 3-entry and 1-entry flows keep emitting the
# exact same `max_attempts` on the wire. Changing it is a product decision
# about retries, not a consequence of the relay's capacity.
CAPTURE_PLAN_MAX_ATTEMPTS = 8

# What a session ran before the position groups shipped. Durable state written
# then carries no ``session_phases`` field, and it came from a session that ran
# exactly these three — so this, not the (now longer) ``CAPTURE_PHASES``, is the
# honest fallback for reading such a state. Reading a pre-cloud state against
# the full tuple would report a household mid-"cloud_measure" in a session that
# never had one.
PRE_CLOUD_CAPTURE_PHASES = (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY)

# Re-exported. It selects an excitation PROGRAM rather than a place in the walk,
# so #2291 Phase 5a-ii moved it to
# :mod:`jasper.active_speaker.crossover_v2.programs` alongside the composer whose
# min-cap clamp is the only level guard for all four of its members — see that
# module for why ``PHASE_ENTRY_BASELINE``'s membership is a correctness
# condition rather than an efficiency.
SUMMED_SWEEP_PHASES = _programs.SUMMED_SWEEP_PHASES

#: WHERE the two sides of #2291's before→after comparison were measured.
#:
#: The mark is the one spot CHECK asks the household to stand the microphone on
#: and MEASURE names ("this spot is the mark"), and both the entry baseline and
#: the post-apply VERIFY are taken there. ``program_id`` equality cannot see
#: position — a capture a metre away replays the identical program — so
#: :class:`~jasper.active_speaker.crossover_v2.verification.MeasurementComparand`
#: carries this second identity and
#: :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_benefit`
#: refuses a pair whose marks disagree.
#:
#: **One owner, deliberately.** Both sides must stamp the SAME string or every
#: round grades
#: :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_MARK_MISMATCH`,
#: so the post-apply side imports this constant rather than spelling the
#: literal a second time. It is a stable identity, not a coordinate: nothing
#: measures where the mark physically is, and the flow makes no claim that two
#: sessions' marks are the same place — only that within ONE round the mic did
#: not move between the two captures, which is what the round's own
#: choreography (baseline last in stage 1, VERIFY first in stage 2, no prompted
#: move between them) is for.
REFERENCE_MARK_DESIGN_AXIS = "design_axis_mark"

#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.contracts`, which
#: owns it alongside the two receipt fields it fills (#2291 Phase 5).  Every
#: ``flow.ENTRY_GRAPH_FINGERPRINT_UNKNOWN`` read keeps resolving to that one
#: object; see the contract for why the sentinel is a word rather than ``""``.
ENTRY_GRAPH_FINGERPRINT_UNKNOWN = _ENTRY_GRAPH_FINGERPRINT_UNKNOWN

# --------------------------------------------------------------------------- #
# position-group choreography (flat-linearization PR-3b)
# --------------------------------------------------------------------------- #
#
# docs/flat-linearization-plan.md fundamental 1: "Spatial multi-capture is THE
# measurement... N≈8–12 gated sweeps at guided positions (≥10 cm spread for HF
# null decorrelation; ≥~30 cm spread to support the LF edge)". These constants
# are the product's realisation of that fundamental.

# Total MIC POSITIONS in the pre-apply cloud, MEASURE's design-axis anchor
# included — so the plan emits ``N − 1`` additional prompted positions after
# MEASURE.
#
# Read that literally: the cloud carries ``N − 1`` SUMMED CURVES, not N. The
# anchor is a per-driver MEASURE capture, so ``_analyze_measure`` produces no
# ``summed_response`` for it to contribute and only a modelled
# ``predicted_sum``. The same holds for the post-apply group below, where
# VERIFY's anchor DOES capture a summed sweep but is consumed by the tracking
# verdict rather than joined to the group.
#
# 9 is chosen so that ``N − 1`` = 8 CURVES, which is what
# docs/flat-linearization-plan.md fundamental 1's "N≈8–12 gated sweeps" floor
# actually asks for (adjudication 3a, 2026-07-26: the first draft shipped 8
# positions ⇒ 7 curves, meeting the floor in positions but not in the thing
# that gets combined). Beyond that floor it is a WALL-CLOCK choice, not a
# statistical optimum: S0's stability work (6-of-10 subsets,
# docs/flat-linearization-plan.md "S0 executed") says more positions is
# strictly better, and the session-length ceiling is what stops us at 9. Treat
# it as a constant, never as a promise about accuracy.
DEFAULT_CLOUD_MEASURE_POSITIONS = 9
# The floor a caller may configure. Below 6 the cloud stops decorrelating HF
# nulls well enough to be worth the extra session minutes, and
# ``CLOUD_POSITION_PROMPTS``' wide-offset guarantee (below) is specified
# against exactly this number.
MIN_CLOUD_MEASURE_POSITIONS = 6
# The ceiling a caller may configure. Sized so the worst-case plan still fits
# the relay's blob-index space — see ``assert_cloud_plan_fits_relay_capacity``,
# which is the executable form of that claim.
#
# **12 → 11 when #2291's entry baseline landed**, because that claim is what
# this number IS, and one more stage-1 entry left the old value one blob index
# over. The arithmetic the guard runs, spelled out:
#
#     cloud_plan_max_attempts(N, M=6)                  = 1 + N + 6 + 2 + 5
#     + len(LATERAL_POSE_PROMPTS)                      = 6
#     + the entry baseline                             = 1
#     ------------------------------------------------------------------
#     N=12 -> 33, N=11 -> 32 = MAX_CAPTURE_PLAN_ATTEMPTS
#
# Nothing shipped changes: ``DEFAULT_CLOUD_MEASURE_POSITIONS`` is 9 and stage 1
# does not run the pre-apply cloud at all (``STAGE1_INCLUDES_CLOUD_MEASURE``).
# What is spent is one step of configuration headroom — the cheapest of the
# three ways to pay, the other two being a household-visible retake
# (``CLOUD_RETAKE_ALLOWANCE``) and a lockstep raise of the relay Worker's own
# ceiling, which is a deployed cross-system contract and not something a flow
# change gets to assume.
MAX_CLOUD_MEASURE_POSITIONS = 11
# Total MIC POSITIONS in the post-apply cloud, VERIFY's anchor included — so
# the plan emits ``M − 1`` additional prompted positions after VERIFY, and the
# group combines ``M − 1`` curves (see the positions-are-not-curves note
# above: VERIFY's own summed capture is consumed by the tracking verdict, which
# is a different question than "is the speaker flat"). Smaller than the
# pre-apply cloud on purpose: the post-apply pass grades a correction the
# pre-apply cloud already constrained, and it is paid at the END of a long
# session where operator patience is the binding resource.
DEFAULT_CLOUD_VERIFY_POSITIONS = 6
# The floor a caller may configure for the POST-apply group. It exists for the
# same reason ``MIN_CLOUD_MEASURE_POSITIONS`` does and is enforced the same way:
# both groups walk ``CLOUD_POSITION_PROMPTS`` from the front, so a group that
# stops before the second wide offset carries no ~30 cm-class spread at all and
# silently voids fundamental 1's LF-edge guarantee — which
# ``test_cloud_prompts_front_load_the_wide_offsets`` states as a property of the
# TABLE, not of the default. Until this floor existed, ``M = 2`` was accepted
# and quietly broke that claim.
#
# DERIVED from the table (``_min_positions_for_two_wide_offsets``), never a
# literal: reordering the prompts must move the floor with them, not leave a
# stale number behind.
MIN_CLOUD_VERIFY_POSITIONS = 5

# How many wider-spread RETAKES of the group's last position the
# geometry-locked check may ask for, once per group.
#
# Retakes rather than appended positions for ONE reason, and it is the protocol
# rather than the physics: the relay runner completes a set at exactly
# ``capture_target`` accepted captures with ``index == accepted_count + 1``, so
# rejecting a capture is the only lever that keeps a plan alive at the same
# index — appending would need a variable-length plan the shipped runner cannot
# express.
#
# A "replacing is better physics" argument was made and WITHDRAWN under review
# (2026-07-26): the reviewer computed the power-mean counterexample, where
# APPENDING a wide position to a clustered cloud fills a −15 dB null further
# than replacing does (−6.1 dB vs −7.7 dB) and lowers ``clustered_fraction``
# more besides. Replacing is what the protocol permits, not what the estimator
# prefers; if the runner ever grows variable-length sets, appending is the
# better answer.
#
# Bounded on purpose: `geometry.locked` is a "spread the mic further" hint, not
# a failure, and an unbounded loop against a genuinely position-invariant
# defect (S0's source-fixed horn-rim comb — see the plan doc's "S0 executed"
# §b) would never terminate, because no amount of mic movement decorrelates a
# source-fixed null. Two retakes, then proceed and RECORD the verdict — it
# lands in the journal and the durable v2 state's `cloud` block. PR-4 carries
# it further: `_geometry_guidance_copy`'s plain-language guidance rides the
# envelope's own `cloud` key and `/state`'s compact projection
# (`crossover_v2_status_block`) — but no household-facing surface renders it
# yet (zero JS/asset changes in PR-4). PR-7 renders it.
GEOMETRY_RETRY_POSITIONS = 2

# Retake headroom a cloud plan carries ABOVE its entry count and its geometry
# retries. Deliberately the same ABSOLUTE spare the shipped 3-entry flow has
# always had (``CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET`` = 5), not the
# same RATIO: `capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`' own sizing note
# says longer sets getting proportionally fewer retakes each "is the intended
# direction — a 21-position session that needs 11 retakes has a problem retries
# will not fix."
CLOUD_RETAKE_ALLOWANCE = CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET

# --------------------------------------------------------------------------- #
# The bounded-retry ruling (owner, 2026-08-03, issue #2086)
# --------------------------------------------------------------------------- #
#
# One prompted position gets its PLANNED capture plus at most this many EXTRA
# attempts, POOLED across everyone who can ask for one: the household's "Try
# again" and voluntary retakes, and the conductor's own geometry retakes. In the
# owner's words: *"We need a finite bound: do up to three more measurements to
# see if we can get a read. If we still can't, attribute it to X."*
#
# What this replaces: a per-REASON-CODE budget (``ReasonSpec.retry_budget``,
# 0-2) measured against a cumulative per-slot attempt count. Two measured
# defects fell out of that shape and killed two live sessions on 2026-08-03
# (#2083 entries 4 and 6):
#
# * an ACCEPTED capture left the counter standing, so one voluntary retake of a
#   healthy position could start at zero headroom;
# * each reason code held its OWN counter while all of them drew on the same
#   plan attempts and the same operator, so alternating conditions walked the
#   meter to 12 attempts behind a screen still reading "step 6".
#
# One pooled counter with an honest surface is the whole replacement. It is
# deliberately NOT derived from ``ReasonSpec.retry_budget``: the point of the
# ruling is that the bound belongs to the POSITION and the household's patience,
# not to whichever condition happened to fire last.
MAX_EXTRA_ATTEMPTS_PER_POSITION = 3

# Who asked for one extra attempt. Pooled against the single bound above (the
# ruling is explicit that the bound is shared), but recorded separately so the
# count the household reads is truthful about who spent what — "the speaker used
# 2 extra tries; you have 1 left" rather than a bare total that reads as though
# the household burned them.
#
# The split is observed at the REJECTION that kept the plan alive, never at the
# relay's ``retake`` flag: a geometry rung does not travel the retake path at
# all (it rejects a good capture so the runner stays on the same index — see
# ``GEOMETRY_RETRY_POSITIONS``' own note), so every geometry rung on 2026-08-03
# was authorized with ``retake=false``. Reading the flag would have attributed
# every system-forced take to the household.
ATTEMPT_INITIATOR_HOUSEHOLD = "household"
ATTEMPT_INITIATOR_SPEAKER = "speaker"

# The fewest RESOLVED positions a cloud group can close with and still produce a
# usable claim, so a position the flow gives up on degrades the group instead of
# ending the session (ruling item 3: "continue the phase if it can proceed with
# the positions it has").
#
# MEASURED, not chosen: the group close itself has no position floor at all
# (``_close_cloud_group`` never compares ``len(positions)`` to anything), and
# ``combine_cloud_positions`` tolerates any non-empty group. The binding
# constraint is downstream, in the fit —
# ``linearization_envelope.position_stability_limit`` raises ``ValueError`` for
# ``n_positions < 2``, because a cross-position spread across fewer than two
# positions is undefined. So two is where "can proceed" genuinely stops.
#
# Deliberately NOT ``MIN_CLOUD_MEASURE_POSITIONS`` / ``MIN_CLOUD_VERIFY_POSITIONS``
# (6 / 5): those are PLAN-DECLARATION floors — how many positions the household
# is asked to walk — enforced once by ``_validated_cloud_counts`` before any
# capture happens. Reusing them at runtime would have killed the 2026-08-03
# verify, which was running usefully at 4-of-6 when it died. Between this floor
# and the declared one the claim is degraded, and degradation is DISCLOSED (the
# geometry verdict's ``n_positions`` / ``thin_evidence`` already ride the
# envelope), not gated.
MIN_RESOLVED_CLOUD_POSITIONS = 2


# The offset class that carries fundamental 1's LF edge. A move at or past
# this distance is "wide"; everything shorter only decorrelates HF nulls.
# DERIVED FROM THE PHYSICS, not from the copy: the parent plan's two-path
# inversion side-finding needs ~30 cm-class spread before the cloud's LF
# common-mode bounce lift starts converging, and ~10 cm before HF nulls
# decorrelate at all (:data:`MIN_CLOUD_OFFSET_CM`). Because
# :attr:`CloudPositionPrompt.wide` is computed from this constant rather than
# hand-set per row, an editor who narrows a wide prompt's distance moves the
# derived group floors with it and
# ``test_cloud_prompts_front_load_the_wide_offsets`` fails loudly — the
# adjustment is refused by construction, not by a second copy of the rule.
WIDE_OFFSET_MIN_CM = 30.0
# The shortest prompted move that is still a move: below this the position is
# not decorrelating anything and is costing a household a session minute.
MIN_CLOUD_OFFSET_CM = 10.0
# How far the geometry-locked retake rungs ask the operator to go. Past every
# position in the table (its widest is 60 cm), because a retake at a distance
# the cloud already sampled is not a wider spot; and no further than that,
# because a desk-scale setup has to be able to reach it — issue #1874's open
# question is whether the lock's threshold suits tabletop clouds at all, and
# this retake must not answer it by walking a household out of the room.
GEOMETRY_RETRY_OFFSET_CM = 75.0

# The named question each prompted position answers (McCarthy's mic-position
# vocabulary, attribution-stage plan §5 promotion queue item 1). Persisted with
# the position so the attribution stage can consume a labelled sample instead
# of an anonymous member of an average; profile-independent, so both listening
# profiles read the same labels.
#
#   ONAX  — inside the design-axis window (lateral offset < WIDE_OFFSET_MIN_CM)
#   OFFAX — out at the coverage edge (lateral offset >= WIDE_OFFSET_MIN_CM)
#   XOVR  — vertical offset: the axis the woofer/tweeter crossover lobes on,
#           which is the mechanism M8 needs a labelled sample of
#
# WHAT A CONSUMER MUST NOT ASSUME: a cloud carries every role. Roles come from
# the walked PREFIX of the table, so the Full tier's 8 prompted positions
# sample all three, but EXPRESS's 4 sample {onax, offax} ONLY — its walk stops
# before the first vertical move. That is by design (express is the shorter
# instrument, §1.3), so an attribution consumer reads the roles a group
# actually has and reports the absent one as unsampled, never as null evidence.
POSITION_ROLE_ONAX = "onax"
POSITION_ROLE_OFFAX = "offax"
POSITION_ROLE_XOVR = "xovr"
POSITION_ROLES = (POSITION_ROLE_ONAX, POSITION_ROLE_OFFAX, POSITION_ROLE_XOVR)


def format_position_distance(offset_cm: float) -> str:
    """One prompted distance, in inches with the metric value beside it.

    Units are a RECORDED OWNER RULING being superseded by a newer one. The
    body-part register (hand-widths, forearms) came from the 2026-07-25 studio
    session, where numeric prompts had proved unusable; the 2026-07-28 field
    session on issue #1805 withdrew it — *"drop body-part units — prompts
    should use inches and/or meters"*. The newer field ruling wins, and both
    units ride every prompt because a household reading this is standing in a
    room with whatever tape measure it owns.

    Centimetres rather than metres at this scale: every prompted move is
    between 0.1 m and 0.6 m, and "0.12 m LEFT" is worse copy than "12 cm
    LEFT" for the same number.
    """
    inches = round(float(offset_cm) / 2.54)
    return f"{inches:g} in ({float(offset_cm):g} cm)"


@dataclass(frozen=True)
class CloudPositionPrompt:
    """One prompted mic move in a position group.

    Split into ``headline`` + ``detail`` by the flow-simplification redesign
    (§2.1): the step screen shows ONE imperative sentence where the counter
    used to be, with at most one short supporting clause under it. Before the
    split this was a single 2-3 sentence paragraph rendered as muted 0.9 rem
    body text under a headline that was just a counter — the inversion the
    owner asked for. ``detail`` may be empty; ``text`` re-joins the two for
    the durable evidence sidecar, which wants the whole instruction the
    operator actually followed rather than only its first sentence.

    ``offset_cm`` is the DISTANCE FROM THE MARK the pose states, and it is the
    row's load-bearing datum rather than decoration: ``wide`` is computed from
    it (see :data:`WIDE_OFFSET_MIN_CM`), so the ~30 cm-class guarantee cannot
    be voided by editing copy alone. ``role`` names the question the position
    answers (:data:`POSITION_ROLES`).

    ``CLOUD_POSITION_PROMPTS`` is ORDERED to put two wide moves inside the
    first ``MIN_CLOUD_MEASURE_POSITIONS - 1`` offsets — pinned by test, because
    an editor reordering this table for readability would silently delete the
    LF half of the measurement.
    """

    headline: str
    detail: str = ""
    offset_cm: float = 0.0
    role: str = POSITION_ROLE_ONAX

    @property
    def wide(self) -> bool:
        """Whether this move carries the plan's ~30 cm-class LF-edge offset.

        Derived, never stored: a row whose distance is edited below the class
        stops being wide in the same edit, which is what makes the floors
        below re-derive instead of going stale.
        """
        return float(self.offset_cm) >= WIDE_OFFSET_MIN_CM

    @property
    def text(self) -> str:
        """Headline + detail as one string — the evidence sidecar's ``prompt``.

        The sidecar is the only durable statement of WHERE a curve was
        measured, so it records the complete instruction, not the screen's
        headline slot alone.
        """
        return f"{self.headline} {self.detail}".strip() if self.detail else self.headline


def _pose(
    template: str,
    offset_cm: float,
    role: str,
    detail: str = "",
    **bearing: str,
) -> CloudPositionPrompt:
    """One table row: an ABSOLUTE pose whose copy is generated from its number.

    ``template`` carries a ``{d}`` slot filled by
    :func:`format_position_distance` plus the bearing words the row supplies,
    so a row's stated distance and its ``offset_cm`` cannot drift apart — the
    number is the source and the sentence is derived from it. Two shared
    templates rather than eleven hand-written sentences is also what keeps
    every row an absolute pose: there is no per-row prose in which a relative
    delta could reappear.

    Refuses at IMPORT TIME below the HF-decorrelation floor, for the same
    reason ``wide`` is derived: a move too short to decorrelate anything is a
    session minute spent on nothing, and the floor is enforced rather than
    documented.

    ``ValueError`` rather than :class:`CrossoverV2FlowError` deliberately —
    the table below is built while this module is still executing, and that
    class is not defined until much further down, so raising it here would give
    the editor this guard exists for a ``NameError`` instead of the message.
    """
    if float(offset_cm) < MIN_CLOUD_OFFSET_CM:
        raise ValueError(
            f"a prompted cloud move must be at least {MIN_CLOUD_OFFSET_CM:g} cm "
            f"to decorrelate HF nulls, got {offset_cm:g} cm"
        )
    if role not in POSITION_ROLES:
        raise ValueError(
            f"cloud position role must be one of {POSITION_ROLES}, got {role!r}"
        )
    return CloudPositionPrompt(
        headline=template.format(
            d=format_position_distance(offset_cm), **bearing
        ),
        detail=detail,
        offset_cm=offset_cm,
        role=role,
    )


# Every wide row's supporting clause. Stepping in as you go out keeps the
# microphone about as far from the speaker as the mark is, which is the
# equidistance precondition any later position-pair level comparison needs
# (attribution-stage plan G8) — an unequal path length makes a level
# difference distance-contaminated rather than axial.
_WIDE_LATERAL_DETAIL = (
    "Step a little toward the speaker as you go out, so you stay about as far "
    "from it as the mark is, and keep the microphone pointed at it."
)
_VERTICAL_DETAIL = "Keep the microphone pointed at the speaker."

# The prompt table, in the order a group walks it.
#
# Copy provenance and the RULING THAT SUPERSEDED IT: the validated reference is
# the S0 kit's ``_prompt_position`` table
# (captures/flat-linearization-20260725/s0-kit/s0_capture.py), whose
# hand-width/forearm language was an owner request from the 2026-07-25 studio
# session after numeric prompts ("move the mic 10 cm left") proved unusable
# standing next to a speaker holding a mic stand. The 2026-07-28 field session
# (issue #1805) withdrew that register — *"drop body-part units — prompts
# should use inches and/or meters"* — so distances are numeric again, in both
# units, and the 2026-07-25 ruling no longer governs this table. Copy stays
# hardware-blind: no horn, no JTS3, nothing that assumes a particular cabinet.
#
# EVERY ROW IS AN ABSOLUTE POSE, never a delta on the previous one (owner
# field ruling, 2026-07-29 on issue #1806): "raise one hand" then "now move two
# hands left" leaves a household guessing whether the raise survived. Each row
# states the complete target — distance, bearing, and height — measured from
# THE MARK, which is also the guidance half of issue #1874: ambiguous relative
# deltas plausibly produce the clustering that trips the geometry lock.
#
# The actor is THE MICROPHONE, not "the phone" (same owner ruling): households
# measure with a phone, a laptop, or a calibrated USB mic, and the device is
# incidental to the instruction.
#
# ONE ordered table serves both groups: the pre-apply group uses
# ``[:N - 1]`` and the post-apply group ``[:M - 1]``, so whichever group ends
# soonest still gets the front-loaded spread. That is why the FIRST TWO wide
# moves sit at offsets 3 and 4 (1-based) rather than at the end, where the S0
# kit (which always ran all ten) could afford to put them — the later rows
# carry wide offsets too, but only a group long enough to reach them walks
# them, so they cannot be what the guarantee rests on. The left/right
# alternation is deliberately UNCHANGED: reordering this table moves two
# derived numbers (``MIN_CLOUD_VERIFY_POSITIONS`` and
# ``express_cloud_measure_positions()``), and what made the alternation read as
# weird in the field was the ambiguous relative phrasing, which the absolute
# poses above remove.
_LATERAL_POSE = "Move the microphone {d} to the {side} of the mark, at mark height."
_VERTICAL_POSE = "Move the microphone back over the mark, {d} {updown} mark height."

CLOUD_POSITION_PROMPTS: tuple[CloudPositionPrompt, ...] = (
    _pose(_LATERAL_POSE, 12.0, POSITION_ROLE_ONAX, side="LEFT"),
    _pose(_LATERAL_POSE, 12.0, POSITION_ROLE_ONAX, side="RIGHT"),
    _pose(
        _LATERAL_POSE, 40.0, POSITION_ROLE_OFFAX,
        side="LEFT", detail=_WIDE_LATERAL_DETAIL,
    ),
    _pose(
        _LATERAL_POSE, 40.0, POSITION_ROLE_OFFAX,
        side="RIGHT", detail=_WIDE_LATERAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 12.0, POSITION_ROLE_XOVR,
        updown="ABOVE", detail=_VERTICAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 12.0, POSITION_ROLE_XOVR,
        updown="BELOW", detail=_VERTICAL_DETAIL,
    ),
    _pose(_LATERAL_POSE, 25.0, POSITION_ROLE_ONAX, side="LEFT"),
    _pose(_LATERAL_POSE, 25.0, POSITION_ROLE_ONAX, side="RIGHT"),
    _pose(
        _LATERAL_POSE, 60.0, POSITION_ROLE_OFFAX,
        side="LEFT", detail=_WIDE_LATERAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 40.0, POSITION_ROLE_XOVR,
        updown="ABOVE", detail=_VERTICAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 40.0, POSITION_ROLE_XOVR,
        updown="BELOW", detail=_VERTICAL_DETAIL,
    ),
)

# --- R16 lateral evidence (plan §4.4) --------------------------------------- #
#
# §4.4: "the smallest direction reuses the existing ±12 cm and ±40 cm left/right
# moves". So the walk is DERIVED from the table above rather than restating its
# copy — and by PREDICATE, not by slice index, so reordering that table for
# readability cannot silently swap which poses the lateral walk asks for.
_LATERAL_POSE_OFFSETS_CM = (12.0, 40.0)

# The walk OPENS and CLOSES at the mark. Both rows bypass ``_pose``: its
# ≥ MIN_CLOUD_OFFSET_CM floor guarantees a prompted move DECORRELATES HF nulls,
# and these two exist to CORRELATE with each other.
#
# Why an at-mark pose at all, when the anchor MEASURE just ran there: the
# anchor's evidence is COMPOSED to the configured crossover the moment it is
# analyzed (§4.2), while a pose is kept neutral so the consumer can compose it
# per candidate. The two are not comparable curves, so a drift bracket drawn
# between them would be comparing a composition to its absence. One extra sweep
# buys a design-axis sample in the SIDES' own fidelity class and makes the
# closing bracket an exact same-instrument repeat of the opening one.
LATERAL_MARK_PROMPT = CloudPositionPrompt(
    headline="Leave the microphone on the mark — one more sweep from here.",
    detail="Nothing to move yet.",
    offset_cm=0.0,
    role=POSITION_ROLE_ONAX,
)
LATERAL_MARK_RETURN_PROMPT = CloudPositionPrompt(
    headline="Last one: put the microphone back on the mark.",
    detail="Same spot, same height, pointed at the speaker.",
    offset_cm=0.0,
    role=POSITION_ROLE_ONAX,
)

LATERAL_POSE_PROMPTS: tuple[CloudPositionPrompt, ...] = (
    (LATERAL_MARK_PROMPT,)
    + tuple(
        prompt for prompt in CLOUD_POSITION_PROMPTS
        if prompt.role != POSITION_ROLE_XOVR
        and float(prompt.offset_cm) in _LATERAL_POSE_OFFSETS_CM
    )
    + (LATERAL_MARK_RETURN_PROMPT,)
)

# Import-time guard, same register as ``_pose``'s: the derivation above must
# yield exactly one LEFT and one RIGHT at each declared offset, bracketed by the
# two at-mark poses. A cloud-table edit that drops or duplicates one of those
# four fails the import rather than shipping a lopsided walk whose left/right
# disagreement term is meaningless.
if len(LATERAL_POSE_PROMPTS) != 2 * len(_LATERAL_POSE_OFFSETS_CM) + 2:
    raise ValueError(
        "the lateral walk must derive exactly one LEFT and one RIGHT pose at "
        f"each of {_LATERAL_POSE_OFFSETS_CM} cm, bracketed by the two at-mark "
        f"poses, got {len(LATERAL_POSE_PROMPTS)} poses"
    )

# What the household reads during the apply hold, and the same entry's fallback
# screen body. It carries a REPOSITION instruction because the pre-apply cloud
# ends at a wide offset while VERIFY's tracking comparator is only meaningful
# back on the design axis — the hold is the walk-back window.
VERIFY_ANCHOR_HOLD_MESSAGE = (
    "Applying the measured crossover to your speaker. While that finishes, put "
    "the microphone back on the mark — same spot, same height, pointed at the "
    "speaker."
)

# The one sentence the 1-entry re-verify re-arm leads with, on BOTH of its
# surfaces (the consent screen's steps and the plan entry's own instruction).
# Flow-simplification §2.4: the 2026-07-27 hardware session abandoned this
# recovery because nothing on screen said it was one sweep rather than another
# walk. Kept as a constant so the two surfaces cannot drift apart.
REVERIFY_NO_REWALK_HEADLINE = (
    "One sweep, back at the mark — you do NOT need to redo the walk."
)

# What the geometry-locked retake asks for. Two rungs, so a second retake is a
# genuinely different instruction rather than the same sentence twice.
#
# Carries the SAME register as the position table (issue #1805's 2026-07-28
# ruling): numeric distances in both units, absolute poses measured from the
# mark, and "the microphone" as the actor. The second rung's height is stated
# rather than left as "a little higher or lower than before" — a household
# asked to break a spatial tie should not be the one deciding which way.
CLOUD_GEOMETRY_RETRY_PROMPTS: tuple[str, ...] = (
    "Same measurement, wider spot: move the microphone "
    f"{format_position_distance(GEOMETRY_RETRY_OFFSET_CM)} to the LEFT of the "
    "mark, at mark height, still pointed at the speaker.",
    "One more, wider still: move the microphone "
    f"{format_position_distance(GEOMETRY_RETRY_OFFSET_CM)} to the RIGHT of the "
    f"mark and {format_position_distance(WIDE_OFFSET_MIN_CM)} ABOVE mark "
    "height.",
)


def _min_positions_for_two_wide_offsets() -> int:
    """Smallest group size whose walked offsets include two WIDE moves.

    DERIVED from :data:`CLOUD_POSITION_PROMPTS`, never hardcoded: the whole
    point of the wide-offset guarantee is that it survives someone reordering
    that table, and a literal here would be the first thing to go stale if they
    did. A group of size ``g`` walks offsets ``[:g - 1]``, so the answer is one
    past the index of the second wide prompt.
    """
    wide = [i for i, prompt in enumerate(CLOUD_POSITION_PROMPTS) if prompt.wide]
    if len(wide) < 2:
        raise CrossoverV2FlowError(
            "CLOUD_POSITION_PROMPTS must supply at least two wide offsets — "
            "fundamental 1's LF edge needs ~30 cm-class spread"
        )
    return wide[1] + 2


# What happens AFTER the walk, in one clause. The pre-apply group hands the
# household a decision; the post-apply group ends the journey. Deliberately
# promises no tune in either case — stage 1 measures, and whether anything is
# applied is the household's call on the next screen.
CLOUD_WALK_SHAPE_TAIL = "Afterwards you decide what to do about what JTS heard."
CLOUD_WALK_SHAPE_TAIL_POST_APPLY = (
    "Afterwards the speaker page shows how the tune did."
)

# The granularity the orientation's REACH is quoted at, and the reason it is
# rounded UP rather than quoted exactly.
#
# A reach that is not a true ceiling is worse than no number at all: a
# household that cleared exactly the quoted space and is then prompted past it
# has been mis-set by the one sentence meant to prevent that. TWO things push a
# position's real displacement past the offset its prompt states:
#
#   * the wide rows' equidistance step-in (``_WIDE_LATERAL_DETAIL``) — after
#     stepping toward the speaker the capsule sits on a CHORD, so a stated
#     40 cm lateral move really lands ~40.9 cm from the mark at the placement
#     copy's nominal 1 m; and
#   * ``CLOUD_GEOMETRY_RETRY_PROMPTS``, which is deliberately "past every
#     position in the table" (75 cm, and ~80.8 cm on rung 2).
#
# The first is absorbed by rounding up to the next whole step — never merely
# to the stated maximum, which is why the arithmetic below is STRICTLY
# greater. The second is NOT absorbed: inflating the everyday number to cover
# a retake most sessions never see would destroy the sentence's
# space-planning value, so the retake is acknowledged in its own short clause
# instead. ``tests/test_crossover_v2_conductor.py`` re-derives both bounds and
# fails if either stops holding.
CLOUD_WALK_REACH_ROUNDING_CM = 10.0


def cloud_walk_reach_cm(positions: int) -> float:
    """The ceiling the orientation quotes for a walk of ``positions`` captures.

    DERIVED from the same ``[:positions - 1]`` slice of
    :data:`CLOUD_POSITION_PROMPTS` the walk is prompted from, then rounded UP
    to the next whole :data:`CLOUD_WALK_REACH_ROUNDING_CM` — *strictly* up, so
    the result is never merely equal to a stated offset and therefore absorbs
    the wide rows' step-in chord. See that constant for why both halves matter.
    """
    walked = max(0, int(positions) - 1)
    if walked > len(CLOUD_POSITION_PROMPTS):
        raise CrossoverV2FlowError(
            f"cloud walk shape needs {walked} position prompts but "
            f"CLOUD_POSITION_PROMPTS supplies {len(CLOUD_POSITION_PROMPTS)}"
        )
    return cloud_walk_reach_cm_of(CLOUD_POSITION_PROMPTS[:walked])


def cloud_geometry_retry_reach_cm() -> float:
    """How far from the mark the geometry-locked retake can send the operator.

    Rung 2 is a COMPOUND pose — :data:`GEOMETRY_RETRY_OFFSET_CM` sideways *and*
    :data:`WIDE_OFFSET_MIN_CM` up — so its displacement is the hypotenuse, not
    either leg. Derived here rather than inside the test so the orientation's
    honesty clause and the prompts it is honest about read one number.
    """
    return max(
        GEOMETRY_RETRY_OFFSET_CM,
        math.hypot(GEOMETRY_RETRY_OFFSET_CM, WIDE_OFFSET_MIN_CM),
    )


def cloud_walk_shape(positions: int, *, post_apply: bool = False) -> str:
    """The walk's SHAPE in one sentence, for the pre-session orientation screen.

    **This replaces the enumerated preview** (issue #1941 R1). Work order D7
    (issues #1804 + #1805) put the whole walk on the consent screen so a
    household would not discover it one prompt at a time — the right intent,
    and this keeps it. What it withdraws is D7's PRESENTATION: at the Full
    tier, a second list of TEN items (eight prompted moves plus a lead and a
    tail) totalling ~250 words, stacked under a 73-word placement block,
    before the first tone. The owner's 2026-07-30 field note is the whole
    argument — *"crazy dense with the 10 steps all spelled out. The user
    doesn't know what's gonna happen next, let alone 10 things from now."*

    So the household is told the two things that list was actually being used
    to convey — **how far from the mark this gets** (can I do this where I am
    standing?) and **that they will be prompted** (I do not need to hold every
    move in my head) — and then the walk spoon-feeds itself, one position per
    screen, which is what the per-entry screens already do.

    The distance is DERIVED from the same ``[:positions - 1]`` slice of the
    same table :func:`build_v2_capture_plan` and
    :func:`build_v2_verify_capture_plan` prompt from, and formatted by the same
    :func:`format_position_distance` the prompts themselves use — so the
    orientation cannot describe a reach the walk does not have, and a reordered
    or narrowed table moves this sentence with it. ``post_apply`` selects
    stage 2's tail; the prompted moves are the same table either way, because
    both groups walk it from the front.
    """
    return _walk_shape(cloud_walk_reach_cm(positions), post_apply=post_apply)


def walk_shape_for(*, cloud_positions: int, lateral: bool) -> str:
    """The orientation sentence for a stage-1 session's ACTUAL groups (R16).

    One sentence for whichever groups run, quoting the FURTHEST reach of any of
    them — a household needs to know how much room the whole session wants, and
    two sentences quoting two ceilings would just make them pick one.
    """
    reach = max(
        cloud_walk_reach_cm(cloud_positions) if cloud_positions else 0.0,
        (
            cloud_walk_reach_cm_of(LATERAL_POSE_PROMPTS) if lateral else 0.0
        ),
    )
    return _walk_shape(reach)


def cloud_walk_reach_cm_of(prompts: Sequence[CloudPositionPrompt]) -> float:
    """:func:`cloud_walk_reach_cm`'s rounding rule over an explicit table."""
    if not prompts:
        return 0.0
    step = CLOUD_WALK_REACH_ROUNDING_CM
    furthest = max(float(p.offset_cm) for p in prompts)
    return math.floor(furthest / step) * step + step


def _walk_shape(reach: float, *, post_apply: bool = False) -> str:
    if not reach:
        # A group with no prompted moves is not a walk and gets no shape line —
        # Express's stage 2 is one held-still sweep at the mark, whose consent
        # screen already leads with REVERIFY_NO_REWALK_HEADLINE.
        return ""
    tail = (
        CLOUD_WALK_SHAPE_TAIL_POST_APPLY if post_apply
        else CLOUD_WALK_SHAPE_TAIL
    )
    # The retake clause is CONDITIONAL on the retake actually reaching past the
    # quoted ceiling, so the sentence never carries a caveat it does not need.
    # Today it always does (75 cm / ~80.8 cm against a 50 cm walk), but a
    # narrowed retake should drop the clause rather than keep a stale one.
    beyond = (
        " though a redo can ask for one step further out,"
        if cloud_geometry_retry_reach_cm() > reach
        else ""
    )
    return (
        f"Every spot is within {format_position_distance(reach)} of the "
        f"mark,{beyond} and you will be told each one when it is time — "
        f"nothing to memorise now. {tail}"
    )


# --------------------------------------------------------------------------- #
# commission tiers (flow-simplification §1)
# --------------------------------------------------------------------------- #

# The two named plan SHAPES a household can consent to. A tier is not a
# loosened floor — it is a distinct, validated (N, M) pair with its own rules,
# so ``MIN_CLOUD_MEASURE_POSITIONS`` (the FULL tier's validated floor) never
# moves to accommodate express.
TIER_FULL = "full"
TIER_EXPRESS = "express"
TIERS = (TIER_FULL, TIER_EXPRESS)
DEFAULT_TIER = TIER_FULL

# Express's post-apply group: VERIFY's design-axis anchor and nothing else. An
# ``M = 1`` plan emits NO cloud-verify entries, so express makes no
# cross-position post-apply claim at all — it verifies tracking at the mark
# (``VERIFY_TOLERANCE_DB``, unchanged) and says so. See the degraded-claims
# table in docs/flat-linearization-flow-simplification-plan.md §1.3.
EXPRESS_CLOUD_VERIFY_POSITIONS = 1


def express_cloud_measure_positions() -> int:
    """Express's pre-apply group size — DERIVED, never the literal 5.

    Express walks the shortest prompted cloud that still contains BOTH of
    :data:`CLOUD_POSITION_PROMPTS`' wide (~30 cm-class) moves, which is
    exactly what :func:`_min_positions_for_two_wide_offsets` computes and
    exactly why :data:`MIN_CLOUD_VERIFY_POSITIONS` is derived the same way: if
    the table's wide moves are ever reordered, express must move with them
    rather than silently ship one-wide and void fundamental 1's LF-edge
    guarantee.
    """
    return _min_positions_for_two_wide_offsets()


@dataclass(frozen=True)
class V2PlanShape:
    """The RESOLVED (tier, N, M) triple — one value, threaded everywhere.

    Before this existed, ``prepare_v2_session`` called
    :func:`build_v2_session_spec` and :func:`build_v2_cloud_index_phase_map`
    with independent defaults and passed counts to neither: two functions that
    MUST agree, agreeing only by luck. Resolving once and threading the result
    closes that desync hazard by construction — the plan the phone is handed
    and the index→phase map the conductor walks are derived from the same
    object or they are not built at all.
    """

    tier: str
    cloud_measure_positions: int
    cloud_verify_positions: int

    @property
    def measure_capture_target(self) -> int:
        """Accepted captures STAGE 1 runs (``1 + N``) — CHECK plus the
        pre-apply cloud (MEASURE's design-axis anchor plus ``N − 1`` prompted
        positions). 10 at the Full tier's shipped defaults, 6 for express.

        Stage 1 ends at the group-close confirm and applies nothing (two-stage
        commission work order D1), so it carries no post-apply entry at all.
        """
        return 1 + self.cloud_measure_positions

    @property
    def verify_capture_target(self) -> int:
        """Accepted captures STAGE 2 runs (``M``) — VERIFY's anchor plus
        ``M − 1`` prompted post-apply positions. 6 at Full, 1 for express
        (whose whole post-apply check is the anchor at the mark).
        """
        return self.cloud_verify_positions

    @property
    def capture_target(self) -> int:
        """Accepted captures the WHOLE JOURNEY runs (``1 + N + M``).

        No single session emits this any more — since the two-stage split it is
        the sum of two sessions' targets (:attr:`measure_capture_target` and
        :attr:`verify_capture_target`), which is what the tier chooser's
        household-facing "N measurements" claim is about: the household is
        choosing both stages when it picks a tier.
        """
        return self.measure_capture_target + self.verify_capture_target

    @property
    def measure_max_attempts(self) -> int:
        """Stage 1's admission budget (its entries + geometry retakes + spare)."""
        return (
            self.measure_capture_target
            + GEOMETRY_RETRY_POSITIONS
            + CLOUD_RETAKE_ALLOWANCE
        )

    @property
    def verify_max_attempts(self) -> int:
        """Stage 2's admission budget, derived exactly like stage 1's."""
        return (
            self.verify_capture_target
            + GEOMETRY_RETRY_POSITIONS
            + CLOUD_RETAKE_ALLOWANCE
        )

    @property
    def max_attempts(self) -> int:
        """The whole journey's admission budget — the CONSERVATIVE bound.

        Kept as the sum rather than ``max(measure, verify)`` because both its
        consumers — :func:`assert_cloud_plan_fits_relay_capacity`, and
        ``jasper-doctor``'s OWN check via :func:`cloud_plan_max_attempts` —
        ask "can the relay carry what this flow needs"; the sum is
        strictly larger than either stage's own budget, so a guard that passes
        on it passes on both. It is deliberately NOT what either session emits
        — those read :attr:`measure_max_attempts` / :attr:`verify_max_attempts`.
        """
        return self.capture_target + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE

    @property
    def has_cloud_verify_group(self) -> bool:
        """Whether this shape emits a post-apply position GROUP at all.

        ``False`` for express (``M = 1``): VERIFY's anchor is the last entry,
        so the plan's end-screen copy rides IT rather than a group tail.
        """
        return self.cloud_verify_positions > 1


def normalize_tier(tier: Any) -> str:
    """Allowlist a household-supplied tier id; empty/absent means FULL.

    Deliberately strict about the value and lenient about absence: an unset
    tier is every pre-tier caller (and the wizard before PR-U3 ships its
    chooser), which must keep getting the full instrument; an UNKNOWN tier is
    a caller asking for an instrument this build does not have, which must
    fail loudly rather than silently measure something else.
    """
    name = str(tier or "").strip().lower()
    if not name:
        return DEFAULT_TIER
    if name not in TIERS:
        raise CrossoverV2FlowError(
            f"unknown commission tier {name!r} (expected one of {', '.join(TIERS)})"
        )
    return name


def resolve_plan_shape(
    tier: Any = None,
    *,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> V2PlanShape:
    """Resolve (and validate) one plan shape from a tier and optional counts.

    Express admits EXACTLY (:func:`express_cloud_measure_positions`,
    :data:`EXPRESS_CLOUD_VERIFY_POSITIONS`) — it is a named shape, not a
    configurable range, so an explicit count that disagrees is a caller bug
    rather than a preference. Full keeps the shipped ranges
    (``MIN_CLOUD_MEASURE_POSITIONS..MAX_CLOUD_MEASURE_POSITIONS``,
    ``M >= MIN_CLOUD_VERIFY_POSITIONS``).
    """
    name = normalize_tier(tier)
    if name == TIER_EXPRESS:
        n = express_cloud_measure_positions()
        m = EXPRESS_CLOUD_VERIFY_POSITIONS
        for label, wanted, got in (
            ("cloud_measure_positions", n, cloud_measure_positions),
            ("cloud_verify_positions", m, cloud_verify_positions),
        ):
            if got is not None and int(got) != wanted:
                raise CrossoverV2FlowError(
                    f"the express tier is a fixed shape: {label} must be "
                    f"{wanted}, got {int(got)}"
                )
        # Still routed through the shared table-length check below, so a
        # shortened prompt table fails here rather than at entry-build time.
        _validated_cloud_counts(
            cloud_measure_positions=n, cloud_verify_positions=m, tier=name,
        )
        return V2PlanShape(
            tier=name, cloud_measure_positions=n, cloud_verify_positions=m,
        )
    n, m = _validated_cloud_counts(
        cloud_measure_positions=(
            DEFAULT_CLOUD_MEASURE_POSITIONS
            if cloud_measure_positions is None
            else cloud_measure_positions
        ),
        cloud_verify_positions=(
            DEFAULT_CLOUD_VERIFY_POSITIONS
            if cloud_verify_positions is None
            else cloud_verify_positions
        ),
        tier=name,
    )
    return V2PlanShape(tier=name, cloud_measure_positions=n, cloud_verify_positions=m)


def _shape_from_kwargs(
    plan_shape: V2PlanShape | None,
    *,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> V2PlanShape:
    """One resolved shape from either a pre-resolved value or loose kwargs.

    Passing both is refused rather than silently preferring one: the whole
    point of :class:`V2PlanShape` is that two surfaces cannot disagree about
    the shape, and a caller handing over two sources of truth has already lost
    that guarantee.
    """
    loose = (tier, cloud_measure_positions, cloud_verify_positions)
    if plan_shape is not None:
        if any(value is not None for value in loose):
            raise CrossoverV2FlowError(
                "pass either plan_shape or explicit tier/position counts, "
                "never both"
            )
        return plan_shape
    return resolve_plan_shape(
        tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )


# --------------------------------------------------------------------------- #
# R17 Fc candidate set (plan §4.2 / #1894 / #1675)
# --------------------------------------------------------------------------- #

# How many Fc values the selector may PROPOSE besides the configured one. The
# ratified direction is "at most five safe candidates" (#1894); this is the
# proposed half of that, so the evaluated set is at most six.
MAX_PROPOSED_FC_CANDIDATES = 5

# Why each Fc was refused. Named codes, never a bare number, because every one
# of these is a household- or operator-actionable declaration rather than an
# internal detail.
FC_REJECT_AT_OR_BELOW_FLOOR = "at_or_below_declared_floor"
FC_REJECT_ABOVE_LOWER_DRIVER_BAND = "above_lower_driver_band"
FC_REJECT_BEAMING = "beaming_above_ka_ceiling"
FC_REJECT_OUTSIDE_SEARCH_BAND = "outside_declared_search_band"


@dataclass(frozen=True)
class FcCandidateSet:
    """The Fc values the selector may evaluate, and why the others are out.

    ``configured_hz`` is ALWAYS in ``candidates`` (plan §9.8: the configured
    path stays the golden mode until a multi-candidate path proves equivalence
    and then improvement), even when it would fail a bound below. That is not
    an exemption from safety — the declared floor and the lower driver's band
    are hard, and a configured Fc outside THOSE is a broken declaration the
    session refuses long before here. It is an exemption from the BEAMING
    prior, which #1675 defines as guidance to warn on, not a fence.

    ``limits`` carries the derived bounds for disclosure, so a household or an
    operator can see what the search was allowed to consider rather than being
    handed a verdict with no visible reasoning.
    """

    configured_hz: float
    candidates: tuple[float, ...]
    rejected: tuple[tuple[float, str], ...]
    limits: dict[str, float]

    @property
    def alternatives(self) -> tuple[float, ...]:
        """Everything the selector could move TO — the set minus configured."""
        return tuple(fc for fc in self.candidates if fc != self.configured_hz)


def fc_candidate_set(
    *,
    configured_hz: float,
    hf_hard_floor_hz: float,
    lower_driver_hard_ceiling_hz: float,
    search_band_hz: tuple[float, float] | None = None,
    lower_driver_diameter_mm: float | None = None,
    count: int = MAX_PROPOSED_FC_CANDIDATES,
) -> FcCandidateSet:
    """Derive the bounded LR4 Fc candidate set from DECLARATIONS only.

    Four bounds, each traceable to something a person confirmed:

    * **strictly above** ``hf_hard_floor_hz`` — the operator-confirmed minimum
      for the HF driver. Strict because #1654 measured the edge case: at an Fc
      equal to the floor the candidate's own handoff lands exactly on the
      evidence band's edge, so it cannot be scored honestly even though the
      sweep now reaches it;
    * at or below the lower driver's declared hard ceiling;
    * inside the declared search band when one is declared;
    * at or below the **beaming ceiling** from the lower driver's declared
      diameter (:func:`~jasper.active_speaker.branch_chain.beaming_onset_hz`)
      — a PROPOSAL bound only, per the paragraph in :class:`FcCandidateSet`.

    Proposals are spaced geometrically, because a crossover argument is a
    per-octave one and an arithmetic grid would crowd the top of the range.

    Returns an empty ``candidates`` only when the configured value itself is
    inadmissible; the caller turns that into the ordinary
    ``no_admissible_candidate`` refusal rather than guessing a crossover.
    """
    from jasper.active_speaker.branch_chain import beaming_onset_hz

    lo = float(hf_hard_floor_hz)
    hi = float(lower_driver_hard_ceiling_hz)
    limits: dict[str, float] = {
        "declared_floor_hz": lo,
        "lower_driver_ceiling_hz": hi,
    }
    if search_band_hz is not None:
        limits["search_lo_hz"], limits["search_hi_hz"] = (
            float(search_band_hz[0]), float(search_band_hz[1]),
        )
        hi = min(hi, float(search_band_hz[1]))
        lo = max(lo, float(search_band_hz[0]) - _FC_GRID_EPS_HZ)
    if lower_driver_diameter_mm is not None:
        ceiling = beaming_onset_hz(float(lower_driver_diameter_mm))
        limits["beaming_ceiling_hz"] = ceiling
        hi = min(hi, ceiling)

    rejected: list[tuple[float, str]] = []
    proposed: list[float] = []
    if hi > lo and count > 0:
        # Geometric interior points, excluding both ends: the floor is refused
        # by the strictness rule above and the ceiling is a bound rather than a
        # recommendation, so neither is a value to propose.
        step = (hi / lo) ** (1.0 / (int(count) + 1))
        proposed = [round(lo * step ** (i + 1), 1) for i in range(int(count))]

    candidates = [float(configured_hz)]
    for fc in proposed:
        reason = _fc_rejection(
            fc, hf_hard_floor_hz, lower_driver_hard_ceiling_hz,
            search_band_hz, limits.get("beaming_ceiling_hz"),
        )
        if reason is None:
            candidates.append(fc)
        else:
            rejected.append((fc, reason))
    return FcCandidateSet(
        configured_hz=float(configured_hz),
        candidates=(
            float(configured_hz),
            *sorted(set(candidates) - {float(configured_hz)}),
        ),
        rejected=tuple(rejected),
        limits=limits,
    )


# Half a display digit: the grid rounds proposals to 0.1 Hz, so a search-band
# edge comparison must not refuse a value it just rounded onto that edge.
_FC_GRID_EPS_HZ = 0.05

#: One-time serial candidate-sweep wall budget. The capture page separately
#: owns a 90 s end-to-end result wait, leaving 20 s for anchor analysis, result
#: publication, polling, and loaded-Pi variance. Post-P0.1 live-Pi all-six
#: timing is unverified; this is the bounded deployment ceiling.
FC_SWEEP_COMPUTE_BUDGET_S = 70.0

def _fc_refusal(fc_hz: float, reason: str) -> FcCandidateEvaluation:
    """A candidate that produced no score, carrying WHY — never a silent drop."""
    empty = np.zeros(0, dtype=np.float64)
    return FcCandidateEvaluation(
        fc_hz=float(fc_hz), freqs_hz=empty, branch_operator_by_role={},
        anchor_sum_db=empty, scoring_band_hz=None, refusal=reason,
    )


def _fc_rejection(
    fc_hz: float,
    hf_hard_floor_hz: float,
    lower_driver_hard_ceiling_hz: float,
    search_band_hz: tuple[float, float] | None,
    beaming_ceiling_hz: float | None,
) -> str | None:
    """The FIRST bound ``fc_hz`` violates, hardest first, or ``None``."""
    if fc_hz <= float(hf_hard_floor_hz):
        return FC_REJECT_AT_OR_BELOW_FLOOR
    if fc_hz > float(lower_driver_hard_ceiling_hz):
        return FC_REJECT_ABOVE_LOWER_DRIVER_BAND
    if search_band_hz is not None and not (
        float(search_band_hz[0]) - _FC_GRID_EPS_HZ
        <= fc_hz
        <= float(search_band_hz[1]) + _FC_GRID_EPS_HZ
    ):
        return FC_REJECT_OUTSIDE_SEARCH_BAND
    if beaming_ceiling_hz is not None and fc_hz > float(beaming_ceiling_hz):
        return FC_REJECT_BEAMING
    return None


@dataclass(frozen=True)
class FcSearchBand:
    """Which declared search band binds the candidate set, and who narrowed it.

    ``band_hz`` is ``None`` when no proposal may be made at all; the caller
    then evaluates the configured Fc alone, which is an honest verdict rather
    than a failure (plan §9.8).

    ``lo_role`` / ``hi_role`` name a role whose own declaration set each
    surviving edge. They are the whole point of this type: with one number per
    edge and no owner, a household that has declared a stale band sees only
    "nothing was proposed" and has nowhere to go. With the owner named, the
    disclosure can say WHICH driver's declaration is the binding one — the
    operator edits that declaration, which is where the fact lives.

    There is deliberately no "do the roles disagree?" boolean. In a two-way the
    intersection is narrower than somebody's declaration almost every time, so
    such a flag would read ``True`` on nearly every session and mean nothing;
    and a flag comparing only the two EDGE OWNERS misses the live jts3 case
    outright, where both roles declare the same upper limit and the
    disagreement is entirely on the lower one. The two role names carry the
    actionable fact without either failure mode.
    """

    band_hz: tuple[float, float] | None
    lo_role: str | None
    hi_role: str | None
    undeclared_roles: tuple[str, ...]


def resolve_fc_search_band(
    declared_band_hz_by_role: Mapping[str, tuple[float, float] | None],
) -> FcSearchBand:
    """Intersect the participating roles' declared crossover search bands.

    **The rule, stated once.** A two-way crossover at ``Fc`` puts BOTH drivers
    at ``Fc`` — the lower driver is low-passed there and the upper driver is
    high-passed there — so an ``Fc`` is only proposable when EVERY participating
    role's declaration admits it. The binding band is therefore the
    intersection, and it is the fail-closed direction: a tweeter's declared low
    limit is an excursion claim, and the cost of honouring a stale one is a
    proposal not made, while the cost of ignoring it is a driver asked to cross
    below what its declaration permits.

    **A participating role with no declared band yields ``None``** — no
    proposal, disclosed via ``undeclared_roles``. ``crossover_search_band_hz``
    is a required declaration (``driver_safety._target_issues`` refuses a
    target without one), so absence here is an anomaly, and the safe reading of
    an anomaly is "this role has told us nothing about where it may be
    crossed", never "this role permits everything".

    **An empty intersection also yields ``None``** with both edge owners still
    named, because "your woofer says at-or-below 1500 and your tweeter says
    at-or-above 2000" is precisely the actionable sentence, and losing the two
    role names to a bare ``None`` would throw it away.

    Not a safety gate on its own: :func:`fc_candidate_set` still applies the
    declared floor, the lower driver's ceiling, and the ka prior on top of
    whatever this returns. This narrows what may be PROPOSED; it never widens
    it, and it has no say over the configured Fc, which is always evaluated.
    """
    lo_hz = -math.inf
    hi_hz = math.inf
    lo_role: str | None = None
    hi_role: str | None = None
    undeclared: list[str] = []
    for role in sorted(declared_band_hz_by_role):
        band = declared_band_hz_by_role[role]
        if band is None:
            undeclared.append(role)
            continue
        role_lo, role_hi = float(band[0]), float(band[1])
        # Strict ">" / "<" keep the FIRST role to set an edge as its owner, so
        # two roles declaring the same limit name the one that sorts first
        # rather than whichever happened to be iterated last. Sorted iteration
        # above is what makes that deterministic; a tie means both roles
        # declared that limit, so either name is equally true.
        if role_lo > lo_hz:
            lo_hz, lo_role = role_lo, role
        if role_hi < hi_hz:
            hi_hz, hi_role = role_hi, role
    if undeclared or lo_role is None or hi_role is None or lo_hz >= hi_hz:
        return FcSearchBand(
            band_hz=None, lo_role=lo_role, hi_role=hi_role,
            undeclared_roles=tuple(undeclared),
        )
    return FcSearchBand(
        band_hz=(lo_hz, hi_hz), lo_role=lo_role, hi_role=hi_role,
        undeclared_roles=(),
    )


def relay_plan_attempts_required(
    *,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> int:
    """Relay blob indexes one journey needs — the SINGLE producer of that fact.

    Both consumers read it — :func:`assert_cloud_plan_fits_relay_capacity` with
    the WORST-CASE counts ("any shape this flow can be configured into") and
    ``jasper-doctor``'s ``check_capture_relay`` with the shipped defaults ("what
    THIS Pi will actually run"). The two questions differ; the arithmetic must
    not, so they pass arguments here rather than each adding their own terms.

    R16's walk counts **when its flag is on** — 23 → 29 at the doctor's defaults
    the moment it flips, no second edit. Before one producer, the guard added
    the poses unconditionally and the doctor did not, so a flipped build would
    under-report by six and pass a Pi whose Worker ceiling sat in [23, 28]:
    green in the diagnostic, refused mid-walk.

    #2291's entry baseline is one more stage-1 entry and counts on exactly the
    same terms — flag-guarded here so the guard and the doctor cannot disagree
    about it either.
    """
    return (
        cloud_plan_max_attempts(
            cloud_measure_positions=cloud_measure_positions,
            cloud_verify_positions=cloud_verify_positions,
        )
        + (len(LATERAL_POSE_PROMPTS) if STAGE1_INCLUDES_LATERAL else 0)
        + (1 if STAGE1_INCLUDES_ENTRY_BASELINE else 0)
    )


def assert_cloud_plan_fits_relay_capacity() -> None:
    """Raise unless the WORST-CASE cloud plan fits the relay's index space.

    The relay stores one blob per admitted attempt at ``capture_index =
    attempt - 1``, so ``capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`` bounds
    entries PLUS retakes for a whole session. That ceiling was sized (PR-3a)
    from the choreography constants above; this function is the executable
    statement of the dependency, so raising ``MAX_CLOUD_MEASURE_POSITIONS`` or
    ``DEFAULT_CLOUD_VERIFY_POSITIONS`` past what the relay can carry fails
    here — loudly, in a hardware-free test — instead of stranding an operator
    mid-cloud when a blob index is refused.
    """
    from jasper.capture_relay.spec import MAX_CAPTURE_PLAN_ATTEMPTS

    # R16's walk and #2291's entry baseline are stage-1 entries too. Flag-aware
    # via the shared producer below, so this and jasper-doctor can never
    # disagree about the number.
    entries = (
        cloud_capture_target(
            cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
            cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
        )
        + (len(LATERAL_POSE_PROMPTS) if STAGE1_INCLUDES_LATERAL else 0)
        + (1 if STAGE1_INCLUDES_ENTRY_BASELINE else 0)
    )
    if entries + GEOMETRY_RETRY_POSITIONS > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise CrossoverV2FlowError(
            f"worst-case cloud plan needs {entries + GEOMETRY_RETRY_POSITIONS} "
            f"relay blob indexes but the relay ceiling is "
            f"{MAX_CAPTURE_PLAN_ATTEMPTS}"
        )
    attempts = relay_plan_attempts_required(
        cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    if attempts > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise CrossoverV2FlowError(
            f"worst-case cloud plan's attempt budget {attempts} exceeds the "
            f"relay ceiling {MAX_CAPTURE_PLAN_ATTEMPTS}"
        )


def _validated_cloud_counts(
    *,
    cloud_measure_positions: int,
    cloud_verify_positions: int,
    tier: str = DEFAULT_TIER,
) -> tuple[int, int]:
    """Validate one (N, M) pair AGAINST ITS TIER's rules.

    The FULL tier keeps the shipped ranges verbatim. Express is checked
    against its own derived shape instead — the range rules would reject it
    (that is the point: express is a distinct named plan, not a loosened
    floor), and :func:`resolve_plan_shape` has already pinned N and M to the
    derived constants before calling here. What both tiers share is the
    prompt-table length check below, which is a property of the TABLE.
    """
    n = int(cloud_measure_positions)
    m = int(cloud_verify_positions)
    if tier != TIER_EXPRESS:
        if not MIN_CLOUD_MEASURE_POSITIONS <= n <= MAX_CLOUD_MEASURE_POSITIONS:
            raise CrossoverV2FlowError(
                f"cloud_measure_positions must be "
                f"{MIN_CLOUD_MEASURE_POSITIONS}..{MAX_CLOUD_MEASURE_POSITIONS}, got {n}"
            )
        if m < MIN_CLOUD_VERIFY_POSITIONS:
            raise CrossoverV2FlowError(
                f"cloud_verify_positions must be at least "
                f"{MIN_CLOUD_VERIFY_POSITIONS}, got {m}"
            )
    # Both groups index the SAME prompt table, so the longer of the two bounds
    # how many offsets it must supply.
    offsets_needed = max(n, m) - 1
    if offsets_needed > len(CLOUD_POSITION_PROMPTS):
        raise CrossoverV2FlowError(
            f"cloud group needs {offsets_needed} position prompts but "
            f"CLOUD_POSITION_PROMPTS supplies {len(CLOUD_POSITION_PROMPTS)}"
        )
    return n, m


def cloud_capture_target(
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> int:
    """Accepted captures one cloud session runs: CHECK + the two groups.

    ``1 + N + M`` — CHECK, then the pre-apply cloud (MEASURE's anchor plus
    ``N − 1`` prompted positions), then the post-apply cloud (VERIFY's anchor
    plus ``M − 1``). 16 at the full tier's shipped defaults, 7 for express.
    """
    return _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    ).capture_target


def cloud_plan_max_attempts(
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> int:
    """This flow's retry budget for a cloud plan (a POLICY number).

    Entries + the bounded geometry retakes + ``CLOUD_RETAKE_ALLOWANCE``. Kept
    separate from ``capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`` (the relay's
    TRANSPORT ceiling) for the reason ``CAPTURE_PLAN_MAX_ATTEMPTS`` states:
    conflating the two is how a transport change silently becomes a product
    change. 23 at the full tier's shipped defaults, 14 for express.
    """
    return _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    ).max_attempts


# One owner for "does stage 1 capture a pre-apply cloud?". R15 (#2106) says NO.
# `prepare_v2_session` runs the plan and `tier_display_info` quotes it, so the
# chooser cannot advertise a walk the session does not take (#2098's pattern).
STAGE1_INCLUDES_CLOUD_MEASURE = False

# R16 (plan §4.4, Gate 0 2026-08-05): stage 1 walks the lateral poses after the
# anchor. Its own flag rather than a reuse of the one above, because the two
# groups answer different questions and are separately authorized — the
# pre-apply cloud stays off.
#
# **ON since R17.** Gate 0 pairs every producer with a CURRENT consumer, and
# R16's is R17's Fc selector — now landed, so the walk has one. It was held off
# through R16 because at the then-declared tweeter measurement floor (2000 Hz,
# equal to the configured Fc) every candidate below 2 kHz had its own handoff
# clamped out of ``overlap_band_hz`` and could not be honestly scored; #1654
# swept the HF driver to its declared 1600 Hz floor and removed that clamp.
# What the walk buys: the lateral robustness term in ``fc_selector.
# score_candidate``, which is the only evidence in the session that a
# candidate's handoff survives OFF the design axis.
# Applied at the PRODUCTION seams (``_stage1_capture_target``,
# ``prepare_v2_session``), not as a builder default — the two builders keep
# whatever a caller asks for, exactly like ``STAGE1_INCLUDES_CLOUD_MEASURE``.
STAGE1_INCLUDES_LATERAL = True

# #2291's minimum new measurement: stage 1 takes ONE summed sweep at the mark
# immediately before the household applies, so the round has a "before" to
# grade its "after" against. Its own flag beside the two above, and ON, for the
# reason the issue gives — without it every round's benefit verdict is
# ``entry_baseline_unavailable``, which is exactly the blind spot that let the
# 2026-08-10 jts3 round report success over a speaker three spec bands out.
#
# Gate 0's producer/consumer pairing is satisfied: the consumer
# (:mod:`jasper.active_speaker.crossover_v2.verification`'s benefit verdict and
# adoption table) is already merged, and the capture is what it has been
# waiting on.
#
# Applied at the PRODUCTION seams (``_stage1_capture_target``,
# ``prepare_v2_session``) exactly like its two siblings — the builders below
# keep whatever a caller asks for.
STAGE1_INCLUDES_ENTRY_BASELINE = True


def _stage1_capture_target(shape: Any) -> int:
    """Stage 1's REAL capture count, not the cloud-inclusive shape target."""
    return len(build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=STAGE1_INCLUDES_LATERAL,
        include_entry_baseline=STAGE1_INCLUDES_ENTRY_BASELINE,
    ))


def build_v2_cloud_index_phase_map(
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
    include_cloud_measure: bool = True,
    include_lateral: bool = False,
    include_entry_baseline: bool = False,
) -> dict[int, str]:
    """Capture-plan index → conductor phase for a STAGE-1 (measure) session.

    The relay drives 1-based indexes where ``index == accepted_count + 1``
    (``capture_relay.session._poll_capture_plan``), so this map is also the
    running order::

        1                    CHECK
        2                    MEASURE            (design-axis anchor)
        3 .. L+2             LATERAL            (L prompted poses, R16 §4.4)
        L+3 .. L+N+1         CLOUD_MEASURE      (N-1 prompted positions)
        (last)               ENTRY_BASELINE     (#2291's "before", at the mark)

    The lateral walk runs BEFORE any pre-apply cloud because it is the anchor's
    own robustness sample: it replays the anchor program, and the sooner it runs
    after the anchor the less the household and the room have had to drift.

    The entry baseline runs LAST — after the walk and after any cloud — because
    #2291 asks for the summed capture *immediately before apply*, and every
    entry it followed would otherwise be time the room and the microphone had
    to drift between the "before" and the graph change it is meant to bracket.
    It prompts the household back to the mark, so it is one held-still capture
    rather than a group.

    **There is deliberately no VERIFY entry** (two-stage commission work order
    D1, issue #1806). Stage 1 measures and stops at the group-close confirm;
    nothing is applied inside it, so nothing post-apply can be measured by it.
    The post-apply half is stage 2's own session and its own map — see
    :func:`build_v2_verify_index_phase_map`. VERIFY's absence here is what
    ``jasper.web.correction_crossover_v2._phase_from_state`` reads to resolve a
    measure-only session to the review interlude instead of "your speaker is
    tuned".

    Single source of truth: ``build_v2_capture_plan`` builds its entries from
    this same function, so an entry's prompt can never address a different
    phase than the conductor believes it is running.
    """
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    n = shape.cloud_measure_positions
    mapping = {1: PHASE_CHECK, 2: PHASE_MEASURE}
    nxt = 3
    if include_lateral:
        for offset in range(len(LATERAL_POSE_PROMPTS)):
            mapping[nxt + offset] = PHASE_LATERAL
        nxt += len(LATERAL_POSE_PROMPTS)
    if include_cloud_measure:
        for offset in range(n - 1):
            mapping[nxt + offset] = PHASE_CLOUD_MEASURE
        nxt += n - 1
    if include_entry_baseline:
        mapping[nxt] = PHASE_ENTRY_BASELINE
    return mapping


def build_v2_verify_index_phase_map(
    *,
    plan_shape: V2PlanShape | None = None,
) -> dict[int, str]:
    """Capture-plan index → conductor phase for a STAGE-2 (verify) session.

    ::

        1                    VERIFY             (design-axis anchor, at the mark)
        2 .. M               CLOUD_VERIFY       (M-1 prompted positions)

    ``plan_shape is None`` is the shipped 1-entry recovery re-verify —
    ``{1: PHASE_VERIFY}``, byte-identical to what ``prepare_v2_verify``
    hardcoded before the split. A shape supplies the tier's own post-apply walk
    (work order D2, owner-confirmed 2026-07-29): express is ``M = 1`` and so
    resolves to the same single-entry map; Full is the six-position spatial
    walk whose combined curve the after-chart, the post-apply spec verdict, and
    the delta probe all read.
    """
    m = 1 if plan_shape is None else plan_shape.verify_capture_target
    mapping = {1: PHASE_VERIFY}
    for offset in range(m - 1):
        mapping[2 + offset] = PHASE_CLOUD_VERIFY
    return mapping


# --------------------------------------------------------------------------- #
# failure taxonomy (§5.10)
# --------------------------------------------------------------------------- #

# The four screen templates W5 ships, each parameterized by reason copy.
TEMPLATE_SILENT_AUTO_RETRY = "silent_auto_retry"
TEMPLATE_FIX_AND_RETRY = "fix_and_retry"
TEMPLATE_HARD_STOP = "hard_stop"
TEMPLATE_SESSION_RESTART = "session_restart"
# Two special screens defined in §5.2 (not among the four generic templates).
TEMPLATE_VERIFY_FAIL = "verify_fail"
TEMPLATE_VOLUME_RECOVERY = "volume_recovery"

# Reason codes (internal — never a bare code reaches the household; the envelope
# renders each through its template copy).
REASON_AGC_BEHAVIORAL_FAIL = "agc_behavioral_fail"
# W6.12: the SAME captured-delta-vs-programmed-delta pilot mismatch
# ``REASON_AGC_BEHAVIORAL_FAIL`` names has a second, honest cause hardware
# round 4 proved distinct from the phone's own AGC: a loud ambient burst
# during the pilot pair corrupts the captured level just as effectively, with
# the phone's AGC verifiably off. ``_consume_check`` distinguishes the two
# using the CHECK gain solve's own SNR-floor verdict (``gain_plan.
# snr_floor_ok``, already computed against this exact capture's ambient bands
# independent of the linearity outcome) rather than blaming the phone's
# microphone when the room itself was the problem.
REASON_NOISY_ROOM_LINEARITY = "noisy_room_linearity"
# Issue #1810 (2026-07-28): the same discriminator W6.12 gave CHECK, for the
# phases CHECK's evidence cannot speak for. MEASURE / cloud / VERIFY each
# carry their own leading pilot pair, and since #1810 their own pre-pilot
# ambient window, so `analysis.pilot_snr_ok` is a real verdict there: False
# means the quiet pilot did not clear the room's own in-band floor by enough
# to trust ANY level comparison drawn from the pair. That is a statement
# about the room and the playback level — a loud room, a mic too far away, or
# (the session that exposed this) a freshly-applied correction that dropped
# the pilot band 14-18 dB and left the quiet pilot ~5 dB over the floor. It is
# NOT evidence about the phone's microphone, which is exactly what the copy
# said before this code existed. `_pilot_observations` reports
# ``linearity_ok`` as None — unknown — whenever the SNR guard fails (it forced
# True until issue #1838, which made an unreadable capture look like a PASS),
# so this branch is the only path that can fail on it, and every verdict below
# checks it BEFORE `REASON_AGC_BEHAVIORAL_FAIL`.
REASON_PILOT_LEVEL_COLLAPSE = "pilot_level_collapse"
REASON_SNR_FLOOR = "snr_floor"
REASON_CHANNEL_MAP_MISMATCH = "channel_map_mismatch"
REASON_CLIPPED = "clipped"
REASON_DRIFT_BASELINES_DISAGREE = "drift_baselines_disagree"
REASON_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"
REASON_LOCATE_FAILED = "locate_failed"
REASON_RELAY_TIMEOUT = "relay_timeout"
REASON_VOLUME_UNRESOLVED = "volume_unresolved"
# The play seam refused/failed the program (safety re-admission over-cap, a
# graph-restore failure, or a conductor program error) — distinct from a relay
# transport death (``relay_timeout``). After the W6.1 cap-aware composition a
# play-time refusal is unexpected (a bug, a tampered readback, or a genuinely
# infeasible profile), so it is terminal: hard-stop, budget 0.
REASON_PROGRAM_UNPLAYABLE = "program_unplayable"
# R15 (#2106): the program PLAYED — the offline evidence math refused. Design
# §4.2 divides the emitted measurement protection back out of the capture, and
# on a candidate-required bin that division is inadmissible when the protection
# attenuates more than 12 dB or the recovery would exceed 12 dB. Its own code
# exists for the #1820 reason: ``program_unplayable``'s copy claims JTS "could
# not play the measurement signal within the speaker's safe limits", which is
# simply not what happened, and its action (re-check driver details) does not
# reach the lever. Deterministic, so terminal — the same protection and the
# same crossover reproduce it exactly. The offending slug rides out in the
# refusal detail.
REASON_PROTECTION_NOT_SEPARABLE = "protection_not_separable"
# Sibling for the OTHER conditioning branch (panel SF3): `abs(P) < floor` does
# not involve `C`, so "change the crossover frequency" cannot clear it (#1820).
REASON_PROTECTION_SWEEP_TOO_LOW = "protection_sweep_too_low"
# Issue #1820 (2026-07-28): the ONE program refusal that is neither unexpected
# nor about levels, split back out of ``program_unplayable``'s collapse. The
# household changed a declared driver value (an enclosure kind, a sensitivity),
# which rotates the safety profile's fingerprint and so CLEARS its confirmation
# by design (``driver_safety.build_driver_safety_profile``) — a deterministic,
# self-inflicted, one-control-away state, not a level ceiling the speaker could
# not meet. Collapsed into ``program_unplayable`` it inherited that code's copy
# ("Re-check the driver details in speaker setup"), which is the one action that
# makes it WORSE: every edit rotates the fingerprint again. Its own code exists
# so the copy can name the actual exit and its ``next_action`` can point at it.
# Terminal (hard-stop, budget 0) for the same reason it is deterministic — a
# second identical measurement reproduces it exactly.
REASON_PROGRAM_PROFILE_NOT_CONFIRMED = "program_profile_not_confirmed"
# Its two siblings, added in the same issue's review round. "Confirm the safety
# limits" is only the honest action when there ARE visible limits to confirm and
# a control that confirms them. Two profile states fail both halves, and the
# session-open pre-flight can tell them apart because it holds the full
# ``DriverSafetyProfileEvaluation``:
#
#   * ``missing``    — no profile exists at all (never-saved / unreadable /
#                      pre-crossover draft). ``/sound/`` deliberately renders NO
#                      confirm control in this state, so telling the household
#                      to confirm names a button that is not on the page.
#   * ``incomplete`` — declared values are still missing.
#                      ``build_driver_safety_profile`` REFUSES a confirm while
#                      derived issues exist, so "Confirm" would 400 even if the
#                      household found the control.
#
# These have no ``ProgramAdmissionRefusal`` counterpart — the play-seam
# vocabulary carries one ``PROFILE_NOT_CONFIRMED`` slug for all three — so they
# are reachable only from the pre-flight, which is the point: the gate that has
# the evidence is the gate that names the action.
REASON_PROGRAM_PROFILE_MISSING = "program_profile_missing"
REASON_PROGRAM_PROFILE_INCOMPLETE = "program_profile_incomplete"
# Any OTHER host-side fault the session runner's catch-all cleanup arm caught
# (W6.1 gate: the seams raise open-endedly — CamillaUnavailable is a bare
# Exception, analyze/emit raise ValueError/RuntimeError, the held measurement
# window raises MeasurementWindowError — so an enumerated except list is how
# failures escape with the volume active and the phone frozen). Terminal for
# the session; the household's one action is to try again.
REASON_INTERNAL_ERROR = "internal_error"
REASON_VERIFY_OUT_OF_TOLERANCE = "verify_out_of_tolerance"
# Internal-only addition BEYOND the §5.10 table: §5.2's "inconclusive —
# re-verify" verdict (VERIFY's own detected first reflection forced a shorter
# gate than MEASURE's, so the overlay difference is not evidence about driver
# alignment). Renders through the same VERIFY-fail template — it is a distinct
# reason parameterizing that screen's copy, not a fifth screen.
REASON_VERIFY_INCONCLUSIVE = "verify_inconclusive"
# Measurement-honesty gate G3 (2026-07-22): a THIRD, distinct VERIFY-outcome
# reason — the phone's own input chain drifted between VERIFY attempts (see
# VERIFY_PILOT_TRANSFER_STEP_CEILING_DB below for the evidence), not the
# speaker going out of tolerance. Renders through the SAME verify_fail
# template as the two codes above (one more parameterization of that
# screen, not a fifth screen) with its own copy naming the actual cause.
REASON_VERIFY_LEVEL_SHIFT = "verify_level_shift"
# R18 / #1868: the applied result tracks the model but does NOT meet the
# candidate's own crossover target through the handoff — the case
# ``REASON_VERIFY_OUT_OF_TOLERANCE`` structurally cannot catch, since it grades
# measured-vs-model and a defect present in BOTH sides cancels. Its own code
# because the household's situation differs: the graph did what it was told,
# and the crossover as designed-and-aligned is what does not sum.
REASON_VERIFY_CROSSOVER_REGION = "verify_crossover_region"
# Owner ruling (2026-07-20): the alignment-estimator confidence floor that
# used to gate ONLY a review-screen nudge (informed consent, Apply stayed
# available regardless) is now a hard MEASURE-phase gate — see
# ALIGNMENT_CONFIDENCE_TRUST_FLOOR below. A household has no basis to judge a
# raw confidence number, so doubt becomes guidance ("move the mic"), never a
# question ("apply anyway?").
REASON_LOW_ALIGNMENT_CONFIDENCE = "low_alignment_confidence"
# The apply transaction came back blocked or raised. It was the conductor's
# OWN auto-apply until the two-stage split (D1); since then the only apply is
# the household's POST from the review screen, which persists its blocking
# issue through ``_persist_apply_blocked`` and answers the request directly.
# The code is retained: it is still the honest name for "the apply failed",
# and ``_persist_terminal_failure`` still scopes its §5.6 evidence reset away
# from it (an apply failure says nothing about the mic position).
REASON_APPLY_FAILED = "apply_failed"
# A deliberate phone Stop (CaptureAborted, abort_reason == "stopped") is not a
# relay-transport death — see the catch-all's exception classification in
# jasper.web.correction_crossover_v2. Reuses TEMPLATE_SESSION_RESTART's
# rendering shape (a fresh session is the only way forward either way) with
# honest copy instead of a manufactured "timed out" claim.
REASON_USER_STOPPED = "user_stopped"
# The deferred apply/"review" hold (CaptureBeginDeferred "awaiting_apply")
# expired before an apply completed. Distinct from a relay-transport death
# (relay_timeout) and a deliberate phone Stop (user_stopped): name the actual
# cause rather than a generic "the measurement link timed out" claim (#1605).
# Same TEMPLATE_SESSION_RESTART shape — a fresh session is the only way
# forward. RETAINED but unreached since the two-stage split (D10): no shipped
# session holds for an apply any more.
REASON_REVIEW_HOLD_TIMEOUT = "review_hold_timeout"
# Position-group choreography (flat-linearization PR-3b): the pre-apply cloud
# closed with `spatial_combine.assess_geometry` reporting `locked` — every
# position's echo estimate landed on the same tau, so the nulls are not moving
# and spatial averaging cannot fill them. NOT a bad capture: the capture is
# fine and the operator did nothing wrong. It is the one actionable thing the
# geometry instrument can say ("spread the mic further"), so the group asks for
# that position again from a wider spot, at most ``GEOMETRY_RETRY_POSITIONS``
# times, and then proceeds with the verdict RECORDED (journal + durable
# state; PR-4 carries it on the envelope and `/state` — no household-facing
# surface renders it yet, PR-7 renders it) rather than blocking a
# measurement on a defect no mic move can decorrelate.
REASON_CLOUD_GEOMETRY_LOCKED = "cloud_geometry_locked"
# Accountability assertions (linearization-integrity PR-L4). Both refuse a
# candidate at the confirm seam, so no proposal ever reaches the review screen
# and the speaker is never touched: the honest outcome of "we cannot show this
# makes your speaker better" is to leave it alone and say so.
#
# item 1 — the two drivers' realized levels, read on their own mirrored
# ±1-octave half-bands about Fc after the committed trim, sit further apart than
# REALIZED_LEVEL_MATCH_TOLERANCE_DB. A 2-way sums flat only when both branches
# hand off at the same level, so this is a tonal-balance defect that no amount
# of per-driver flattening can hide. Fired at ~9 dB on the 2026-07-27 JTS3
# profile the owner heard as dark. (It grades the HANDOFF, not the whole
# passband: a driver whose own band tilts while its half-band level is right is
# the fit's problem to catch, not this assertion's.)
REASON_DRIVER_LEVELS_DISAGREE = "driver_levels_disagree"
# item 2 — the PREDICTED post-apply response fails the flat spec and is not
# materially better than the measured pre-apply response. Applying it would
# spend the household's speaker on a change we can already show does not help.
REASON_CORRECTION_NOT_AN_IMPROVEMENT = "correction_not_an_improvement"
# Delta-probe verdicts (linearization-integrity PR-L5). Unlike the two above,
# these fire AFTER the apply — they are what the post-apply sweep found — so
# each one rolls the correction back before it names itself. The household is
# left on the sound they had, and told why, which is the difference between an
# automatic rollback and a silent one.
#
# The correction did not do what its own filters said it would: a chain defect
# (the shelf realized at a Q the fit never modelled is the archetype, and the
# reason this code exists permanently rather than as a one-off fix).
REASON_CORRECTION_MODEL_ERROR = "correction_model_error"
# The correction's shape landed but its depth did not — the driver delivered
# materially less level than it was asked for. A compression diagnostic.
REASON_CORRECTION_LEVEL_SHORTFALL = "correction_level_shortfall"
# The correction tracked at the measuring spot and made the room LESS even
# everywhere else: it fitted one position's interference rather than the
# speaker. The remedy is placement, not a different filter.
REASON_CORRECTION_SPATIALLY_COSTLY = "correction_spatially_costly"
# The probe found a defect AND the automatic rollback could not run (no
# rollback binding, a refused restore, or a seam that raised). The correction
# is therefore STILL APPLIED, and the copy has to say so — the household is
# listening to it right now, and telling them it was put back would be a false
# statement about their speaker. Mirrors the room-correction acceptance
# precedent: a failed automatic restore must continue to say the correction is
# still applied, and name the manual action.
REASON_CORRECTION_ROLLBACK_FAILED = "correction_rollback_failed"
# #2291's round verdict, and the one cause no code above can carry: the
# correction was applied, MEASURED at the same mark with the same program, and
# the speaker is measurably worse than it was before — so it came back off.
# Distinct from ``REASON_CORRECTION_NOT_AN_IMPROVEMENT``, whose copy says "it
# was not applied" and which grades a PREDICTED response before the apply, and
# distinct from the three delta-probe codes, which say the graph did not do
# what its own filters commanded. Here the graph did exactly what it was told
# and the room liked it less, which is a different sentence to the household
# and a different next step.
REASON_CORRECTION_MEASURED_REGRESSION = "correction_measured_regression"
# #2291's fail-closed boost. The benefit could not be measured — no comparable
# "before", or a capture that could not be compared — and the applied
# intervention puts energy INTO a driver. An unverified cut can wait for a
# household to decide; an unverified boost cannot, so it comes off. Its own
# code because "we could not tell, and erred toward your drivers" is a
# different and more honest sentence than "it measured worse".
REASON_CORRECTION_UNPROVEN_BOOST = "correction_unproven_boost"

def round_restore_reason(cause: str) -> str:
    """#2291 adoption cause → the code a SUCCESSFUL round restore surfaces.

    Only the three causes the table can reach with a ``restore`` intent exist,
    and each maps to the code whose copy states that cause truthfully. A
    realization failure IS the graph not doing what its own filters commanded,
    which is :data:`REASON_CORRECTION_MODEL_ERROR`'s existing sentence, so it
    is reused rather than duplicated.

    A function with a lazy import rather than a module-level dict, because
    :mod:`~jasper.active_speaker.crossover_v2.verification` reaches
    :mod:`~jasper.active_speaker.flat_spec`, which this module imports lazily
    everywhere for that reason.

    Anything unlisted falls back to the measured-regression code — the weakest
    true statement available for "the round asked for a restore". The mapping
    is exhaustive today and pinned by a test, so the fallback is a floor, not
    a branch anything reaches.
    """
    from jasper.active_speaker.crossover_v2.verification import (
        ADOPTION_MEASURED_REGRESSION,
        ADOPTION_REALIZATION_FAILED,
        ADOPTION_UNPROVEN_BOOST,
    )

    return {
        ADOPTION_MEASURED_REGRESSION: REASON_CORRECTION_MEASURED_REGRESSION,
        ADOPTION_REALIZATION_FAILED: REASON_CORRECTION_MODEL_ERROR,
        ADOPTION_UNPROVEN_BOOST: REASON_CORRECTION_UNPROVEN_BOOST,
    }.get(cause, REASON_CORRECTION_MEASURED_REGRESSION)


#: Delta-probe verdict → the reason code its rollback surfaces. Exhaustive
#: over :data:`delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`, pinned by a test.
#:
#: The stated intent has always been "a new NON-MATCHED verdict cannot ship
#: without a surface", and until #1811 the rollback set and the non-matched set
#: were the same thing, so equality here enforced it. ``level_mismatch`` is the
#: first verdict that is non-matched WITHOUT being a rollback, so the two sets
#: diverged and this mapping alone stopped covering the intent. The guard test
#: is now written against the non-matched set: a verdict that is not here must
#: prove it reaches a household some OTHER way (``level_mismatch`` does — the
#: persisted ``verify.delta_probe`` summary and the done screen's caveat
#: nudge), never merely by being absent from a rollback list.
DELTA_PROBE_REASON_BY_VERDICT: Mapping[str, str] = {
    VERDICT_MODEL_ERROR: REASON_CORRECTION_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL: REASON_CORRECTION_LEVEL_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY: REASON_CORRECTION_SPATIALLY_COSTLY,
}


def verify_inconclusive_cause(
    code: str | None, reflection_measured: bool | None,
) -> str:
    """WHY a verify check could not settle, as one household clause (#1974).

    **THE single writer of that clause**, because it renders on TWO screens —
    the verify_fail screen's reason copy and the done screen's ungraded
    verdict — and those two screens is exactly how the bug this fixes stayed
    invisible: each carried its own paraphrase of "the room reflection cut the
    window short", so neither could be corrected without the other being
    noticed. There is now one sentence and two framings of it.

    Two things produce the "inconclusive" outcome and they share no mechanism:

    * ``REASON_VERIFY_INCONCLUSIVE`` — VERIFY's own gate came out SHORTER than
      MEASURE's, so the two captures cannot be compared like for like. That is
      the whole of what the rule observed; WHY the window is short is a
      separate fact, and it is the one the old copy asserted without ever
      consulting. ``reflection_measured`` is that fact, taken from
      :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.gated_anything`
      — the single owner of "is the reflections claim true here", whose own
      docstring says it is true THERE and nowhere else. Across the whole
      2026-07-30 corpus it was False (issue #1966), i.e. the sentence people
      actually read was false on every capture that produced it.
    * ``REASON_VERIFY_LEVEL_SHIFT`` — the recording chain moved between
      attempts. No reflection and no window are involved at all, and this path
      never reaches the verify_fail screen's inconclusive copy (it has its own
      ReasonSpec); it reaches the DONE screen's, because that screen keys on
      the coarse outcome rather than the code.

    The two arguments go unknown for different reasons and get different
    answers, and the difference is load-bearing:

    * ``code=None`` — the record does not say WHICH verdict fired (a durable
      state written before this shipped). Nothing at all is established, so
      the clause is EMPTY: the caller states the outcome and stops, which is
      the honest rendering of an unrecorded cause.
    * ``reflection_measured=None`` — the verdict IS known, only its gate is
      not. That collapses into the no-reflection-claim branch below rather
      than emptying the clause, because the code alone already establishes the
      observation ("the window came out shorter than the tuning's") — that is
      what the rule measured, independent of any gate record. Emptying here
      would also break :func:`verify_inconclusive_message`, whose registry
      rendering passes exactly this and would otherwise read "The check was
      inconclusive — . Re-verify to try again."

    Returned without terminal punctuation: the caller owns the sentence it
    lands in.
    """
    if code == REASON_VERIFY_LEVEL_SHIFT:
        # Same vocabulary as REASON_VERIFY_LEVEL_SHIFT's own ReasonSpec below,
        # deliberately: one cause should not have two names depending on which
        # screen a household happens to be reading.
        return "the microphone's levels changed between measurements"
    if code != REASON_VERIFY_INCONCLUSIVE:
        return ""
    if reflection_measured:
        # The ONE state where blaming a reflection is true — and it says what
        # the comparison actually lost, not merely that a reflection existed.
        return (
            "a reflection reached the microphone sooner than it did during "
            "tuning, so there was less of the sound to compare"
        )
    # Reflection NOT measured, or not recorded. Both render the observation the
    # rule made and stop there: a window capped at the search ceiling proves
    # nothing about reflections, so naming one would be the same overstatement
    # in a new place. The precise gate state is disclosed a line below in
    # expert details, by ``gate_disclosure.describe_gate``.
    return "this measurement had less usable sound to compare than the tuning did"


def verify_inconclusive_diagnosis(reflection_measured: bool | None) -> str:
    """What VERIFY established, without advice about the next action."""
    cause = verify_inconclusive_cause(REASON_VERIFY_INCONCLUSIVE, reflection_measured)
    return f"The check was inconclusive — {cause}."


def verify_inconclusive_message(reflection_measured: bool | None) -> str:
    """``REASON_VERIFY_INCONCLUSIVE``'s household sentence. Single writer.

    The registry entry below holds this function's ``None`` (cause-unknown)
    rendering, so a caller with no gate record on hand — and every reader of
    ``REASON_REGISTRY`` — gets copy that is true rather than copy that guesses.
    The envelope re-renders it with the persisted fact when it has one.
    """
    return f"{verify_inconclusive_diagnosis(reflection_measured)} Re-verify to try again."


def locate_failed_diagnosis(pilot_heard: bool | None) -> str:
    """What the locator established, without advice about the next action."""
    if pilot_heard:
        return (
            "JTS could hear the speaker, but couldn't line up the test tones "
            "in the recording."
        )
    return "Couldn't hear the speaker clearly."


def locate_failed_message(pilot_heard: bool | None) -> str:
    """``REASON_LOCATE_FAILED``'s household sentence. Single writer (#2085).

    SELECTION, never composition — the same shape
    :func:`verify_inconclusive_message` above uses, and for the same reason:
    one code, two honest causes, and a registry that cannot hold one literal
    true of both.

    ``locate_failed`` fires when the correlator could not place this capture's
    stimuli (:func:`_stimulus_locate_ok`, :func:`_sweep_locate_confidence_ok`,
    or VERIFY's ``summed_sweep_heard`` integrity check — all three are
    locate-CONFIDENCE floors). Its copy asserted the one cause that would
    explain that on its own: the speaker was not audible, so check the volume
    and the microphone. The JTS3 session of 2026-08-03 measured that claim
    false three times in one sitting. Every one of those captures carried
    ``pilot_snr_ok=True`` — the leading pilot pair cleared the room's own
    in-band floor by 13.9-15.5 dB, direct evidence from THIS capture that the
    speaker was heard — while its sweeps scored 0.019-0.097 against a 0.3
    floor. A household told to check the volume then goes and changes the one
    thing the measurement had already proved was fine.

    **The copy names the operation that failed, and stops there.** Forensics
    on those same three WAVs found the audio pristine: the analyzer had
    anchored the timeline on ``pilot_lo`` — deliberately the quietest segment
    in the program — missed the anchor gate by an NCC margin of 0.005-0.049,
    snapped to ``pilot_hi`` instead, and put every subsequent sweep 1296.5 ms
    (exactly the pilot spacing) outside a +/-30 ms search window. Re-scored
    with a whole-capture search the same recordings give 0.67-0.82. So "the
    recording came back damaged" would have been a THIRD false sentence, told
    to households whose volume AND whose recording were both fine. What is
    true in every case — a corrupted capture and this mis-anchor alike — is
    that JTS could not line up the test tones. That is what the household is
    told. (The anchor itself is a separate fix in ``program_analysis``; this
    copy does not depend on it landing, and does not become wrong when it
    does.)

    ``pilot_heard`` is the discriminator:

    * ``True`` — the pilot pair was measurably heard, so "couldn't hear the
      speaker" is refuted BY THIS CAPTURE. The copy reports the lining-up
      failure and asks for one retry, asserting no cause for it.
    * ``False`` / ``None`` — the pilot failed too, or there is no pilot
      evidence at all. Then the level/microphone reading is either supported
      or simply unknown, and the original copy stands. The registry holds
      this rendering, so every reader of ``REASON_REGISTRY`` with no capture
      in hand gets copy that is true rather than copy that guesses.

    Deliberately keyed on the EVIDENCE, not on which gate fired. The three
    call sites above measure the same thing (a locate-confidence floor) and
    the falsifying fact is the same field on the same analysis, so keying on
    the site would let one measured situation produce two different sentences
    depending on which floor happened to be checked first — the drift this
    file already fixed once for the inconclusive copy (#1974).
    """
    diagnosis = locate_failed_diagnosis(pilot_heard)
    if pilot_heard:
        return f"{diagnosis} Try again."
    return f"{diagnosis} Check the volume and the microphone, then try again."


@dataclass(frozen=True)
class RetryableReasonCopy:
    """One retryable reason's diagnosis and still-available action.

    ``diagnosis`` is the observation that remains true after the slot's last
    extra attempt.  ``retry_action`` is appended only on surfaces where an
    attempt is still available.  Keeping both pieces in this one value lets
    :class:`ReasonSpec` expose the historical full ``message``/``banner``
    strings without duplicating the diagnosis in a terminal-copy registry.

    ``strip_before_join`` supports the two existing em-dash sentences: their
    standalone diagnosis ends with a period, while the retryable rendering
    removes that period before the dash.  The diagnosis itself remains a
    complete household sentence.
    """

    diagnosis: str
    retry_action: str
    joiner: str = " "
    strip_before_join: str = ""

    @property
    def message(self) -> str:
        diagnosis = self.diagnosis
        if self.strip_before_join and diagnosis.endswith(self.strip_before_join):
            diagnosis = diagnosis[: -len(self.strip_before_join)]
        return f"{diagnosis}{self.joiner}{self.retry_action}"


@dataclass(frozen=True)
class ReasonSpec:
    """One terminal verdict's template + budget + copy (§5.10)."""

    code: str
    template: str
    # RETRIABLE-OR-NOT, since the bounded-retry ruling (#2086) moved the COUNT
    # to :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION`. Zero still means "no extra
    # attempt can help" — a statement about the CONDITION (wiring is wrong, the
    # tuning would not have improved the speaker), not a budget — and those
    # codes still stop the moment they fire. Any non-zero value now says only
    # "retriable"; the specific 1 vs 2 no longer changes behaviour, because a
    # per-code count was exactly the fragmentation the ruling replaced (five
    # attempts at one position on 2026-08-03 came from three codes each holding
    # its own meter). Kept as an int rather than collapsed to a bool to keep
    # this change off every registry entry's line; see
    # :data:`NON_RETRIABLE_CODES`.
    retry_budget: int
    # Short banner shown while a transient code auto-retries (template 1). Empty
    # for codes whose template is a decision screen.
    banner: str
    # The fix/action copy the decision-screen template renders. One reason, one
    # action (the Language guide).
    message: str
    # Optional per-reason override for the HARD-STOP screen's action button
    # (issue #1820). Consulted by that template ONLY, because it is the one
    # screen whose default action is a generic destination ("Back to speaker
    # setup", ``/sound/``) rather than a semantically load-bearing control —
    # verify_fail owns Undo, session_restart owns Start over, fix_and_retry
    # owns Try again, and none of those may be replaced by copy data. A
    # hard-stop reason that knows the exact control which clears it declares
    # that control here so the household lands ON it instead of on the page
    # that contains it. Shape is the ``next_action`` mapping the envelope
    # emits: ``{"id", "label", "href"}``.
    next_action: Mapping[str, Any] | None = None
    # Structured only for retryable rows.  ``message``/``banner`` above is
    # derived from this value by :func:`_retriable_reason`, so the diagnosis
    # used at exhaustion and the diagnosis inside retry copy have one writer.
    retry_copy: RetryableReasonCopy | None = None


def _retriable_reason(
    code: str,
    template: str,
    retry_budget: int,
    copy: RetryableReasonCopy,
    *,
    auto_retry: bool = False,
) -> ReasonSpec:
    """Build a retryable registry row from one structured copy source."""
    if retry_budget <= 0:
        raise ValueError("a retryable reason needs a positive retry budget")
    return ReasonSpec(
        code,
        template,
        retry_budget,
        copy.message if auto_retry else "",
        "" if auto_retry else copy.message,
        retry_copy=copy,
    )


# The §5.10 table, as data. The envelope and the conductor both read it, so copy
# and budget never drift between the verdict and its screen.
REASON_REGISTRY: dict[str, ReasonSpec] = {
    REASON_AGC_BEHAVIORAL_FAIL: _retriable_reason(
        REASON_AGC_BEHAVIORAL_FAIL, TEMPLATE_FIX_AND_RETRY, 1,
        # Copy amended 2026-07-28 (issue #1810). It used to state the cause
        # outright — "Your phone's microphone changed its own levels
        # mid-measurement" — and the JTS3 session that filed the issue proved
        # that claim can be false: the pilot pair had collapsed into the room
        # floor, the only direct recording-chain evidence path
        # (``pilot_transfer_step_db``) was null, and the household was told to
        # go re-allow a microphone that had done nothing wrong. What this code
        # actually observes is that the captured two-pilot level delta did not
        # match the programmed one at a level where it should have. Two things
        # produce that — the phone's input chain riding gain, or the speaker's
        # own output compressing — so the copy names the observation and the
        # one action that helps either way. The definite mic accusation now
        # lives ONLY on REASON_VERIFY_LEVEL_SHIFT, which has the cross-attempt
        # transfer step to back it.
        RetryableReasonCopy(
            "The two test tones didn't come back at the levels JTS played them.",
            "Re-allow the microphone, then try again.",
        ),
    ),
    REASON_NOISY_ROOM_LINEARITY: _retriable_reason(
        REASON_NOISY_ROOM_LINEARITY, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The room got loud during that measurement.",
            "quiet it and try again.",
            joiner=" — ",
            strip_before_join=".",
        ),
    ),
    REASON_PILOT_LEVEL_COLLAPSE: _retriable_reason(
        REASON_PILOT_LEVEL_COLLAPSE, TEMPLATE_FIX_AND_RETRY, 1,
        # One reason, one action (the Language guide) — but the cause is
        # genuinely two-sided and naming only half of it would be the same
        # over-claim this code exists to stop. "Not your phone" is the point:
        # the household's previous experience of this failure was being told
        # to re-allow a microphone that was working.
        RetryableReasonCopy(
            "The test tones didn't rise clearly above the room — it was too "
            "loud, or the speaker too quiet, for this check.",
            "Quiet the room or move the microphone closer, then try again.",
        ),
    ),
    REASON_SNR_FLOOR: _retriable_reason(
        REASON_SNR_FLOOR, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The room is too loud right now, or the microphone is too far away.",
            "Quiet the room or move the microphone closer, then try again.",
        ),
    ),
    REASON_CHANNEL_MAP_MISMATCH: ReasonSpec(
        REASON_CHANNEL_MAP_MISMATCH, TEMPLATE_HARD_STOP, 0, "",
        # Fix 3 (W6.4): with Fix 1's band-relative discriminator this should
        # be rare and genuinely wiring, but the honest failure mode also
        # includes a very quiet/noisy room (the discriminator needs both a
        # driver's own band to rise over its ambient AND the other driver's
        # band to stay quiet) — name both causes rather than blaming wiring
        # unconditionally.
        "The drivers didn't play in the expected order — check the speaker "
        "wiring, or if the room is noisy, quiet it and try again.",
    ),
    REASON_CLIPPED: _retriable_reason(
        REASON_CLIPPED, TEMPLATE_SILENT_AUTO_RETRY, 1,
        RetryableReasonCopy(
            "That was a touch loud.",
            "measuring again a bit quieter.",
            joiner=" — ",
            strip_before_join=".",
        ),
        auto_retry=True,
    ),
    REASON_DRIFT_BASELINES_DISAGREE: _retriable_reason(
        REASON_DRIFT_BASELINES_DISAGREE, TEMPLATE_SILENT_AUTO_RETRY, 1,
        RetryableReasonCopy(
            "The capture glitched.",
            "measuring again.",
            joiner=" — ",
            strip_before_join=".",
        ),
        auto_retry=True,
    ),
    REASON_DELAY_EXCEEDS_SEARCH_WINDOW: _retriable_reason(
        REASON_DELAY_EXCEEDS_SEARCH_WINDOW, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The microphone may be off the spot in the picture.",
            "Re-check its placement, then try again.",
        ),
    ),
    REASON_LOCATE_FAILED: _retriable_reason(
        REASON_LOCATE_FAILED, TEMPLATE_FIX_AND_RETRY, 1,
        # NOT a literal (issue #2085). This code is a locate-CONFIDENCE floor,
        # and its copy named the one cause that would explain a miss on its own
        # — an inaudible speaker — on captures whose own pilot pair proved the
        # speaker was heard. The sentence has one writer now
        # (``locate_failed_message``, which also explains why the heard-speaker
        # branch names no cause at all); what the registry holds is its
        # no-pilot-evidence rendering, true for any reader with no capture in
        # hand. The relay verdict and the envelope both re-render it with the
        # measured fact.
        RetryableReasonCopy(
            locate_failed_diagnosis(None),
            "Check the volume and the microphone, then try again.",
        ),
    ),
    REASON_RELAY_TIMEOUT: ReasonSpec(
        REASON_RELAY_TIMEOUT, TEMPLATE_SESSION_RESTART, 0, "",
        # The old link is dead once the session collapses — do NOT tell the
        # household to "open the link again" (W6.10 fold-in: that link and its
        # QR are gone). Start over mints a FRESH session from this page.
        "The measurement link timed out. Start over from this page to measure "
        "again — the quick microphone check runs first.",
    ),
    REASON_VOLUME_UNRESOLVED: ReasonSpec(
        REASON_VOLUME_UNRESOLVED, TEMPLATE_VOLUME_RECOVERY, 0, "",
        "JTS could not confirm the listening volume was restored. Recover the "
        "safe volume before continuing.",
    ),
    REASON_PROGRAM_UNPLAYABLE: ReasonSpec(
        REASON_PROGRAM_UNPLAYABLE, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not play the measurement signal within the speaker's safe "
        "limits. Re-check the driver details in speaker setup, then measure "
        "again.",
    ),
    REASON_PROTECTION_SWEEP_TOO_LOW: ReasonSpec(
        REASON_PROTECTION_SWEEP_TOO_LOW, TEMPLATE_HARD_STOP, 0, "",
        "JTS played the measurement fine, but it swept this driver lower than "
        "the driver's own protection lets through, so the bottom of the sweep "
        "is too quiet to trust. Re-check this driver's protection settings in "
        "speaker setup, then measure again.",
    ),
    REASON_PROTECTION_NOT_SEPARABLE: ReasonSpec(
        REASON_PROTECTION_NOT_SEPARABLE, TEMPLATE_HARD_STOP, 0, "",
        "JTS played the measurement fine, but the safety limits it had to keep "
        "in place overlap the crossover you have set, so it cannot tell the two "
        "apart well enough to trust the result. Change the crossover frequency "
        "in speaker setup, then measure again.",
    ),
    REASON_PROGRAM_PROFILE_NOT_CONFIRMED: ReasonSpec(
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED, TEMPLATE_HARD_STOP, 0, "",
        # Issue #1820 defect 2: the copy this refusal used to inherit from
        # ``program_unplayable`` sent the household to "re-check the driver
        # details" — and re-checking (editing) them rotates the profile
        # fingerprint, which clears the confirmation again. That is a LOOP, not
        # a fix. This copy names the actual exit, warns why edits do not help,
        # and the ``next_action`` below lands ON the control rather than on the
        # page that hides it behind a disclosure.
        "This speaker's safety limits are not confirmed, so JTS did not play "
        "the measurement signal. Confirm the safety limits in speaker setup — "
        "changing a driver detail clears them — then measure again.",
        next_action={
            "id": "confirm_safety_limits",
            "label": "Confirm safety limits",
            # ``/sound/``'s Component setup card renders the hoisted confirm
            # control under this exact id when the profile needs confirmation
            # (deploy/assets/sound-profile/js/main.js), and its boot path opens
            # the owning step for this fragment. Both halves are pinned by
            # tests/test_sound_profile_confirm_deeplink.py.
            "href": "/sound/setup/#confirm-safety-limits",
        },
    ),
    REASON_PROGRAM_PROFILE_MISSING: ReasonSpec(
        REASON_PROGRAM_PROFILE_MISSING, TEMPLATE_HARD_STOP, 0, "",
        # NOT "confirm the safety limits": there is nothing to confirm and no
        # control to confirm it with. This is the state the pre-gate's original
        # copy was right about, kept for exactly this branch.
        "This speaker's driver details are not finished, so JTS has no safety "
        "limits to measure within. Finish the driver details in speaker setup, "
        "then measure again.",
        next_action={
            "id": "speaker_setup",
            "label": "Finish speaker setup",
            # No fragment: ``/sound/`` renders no confirm callout in this state,
            # so a deep link would land on nothing. The page opens on its own
            # first unfinished step, which IS the action.
            "href": "/sound/setup/",
        },
    ),
    REASON_PROGRAM_PROFILE_INCOMPLETE: ReasonSpec(
        REASON_PROGRAM_PROFILE_INCOMPLETE, TEMPLATE_HARD_STOP, 0, "",
        # Matches what ``/sound/``'s own callout says in this state — the two
        # surfaces name one action, and it is not "Confirm", which the server
        # would refuse while values are missing.
        "Some of this speaker's safety limits are still missing, so JTS did "
        "not play the measurement signal. Add them under Advanced in speaker "
        "setup, then confirm the limits and measure again.",
        next_action={
            "id": "add_safety_limits",
            "label": "Add the missing limits",
            # The callout DOES render for this state (button-less, naming the
            # add-the-values action), so the fragment lands on the explanation.
            "href": "/sound/setup/#confirm-safety-limits",
        },
    ),
    REASON_INTERNAL_ERROR: ReasonSpec(
        REASON_INTERNAL_ERROR, TEMPLATE_FIX_AND_RETRY, 0, "",
        "Something went wrong on the speaker during that measurement. "
        "Try again.",
    ),
    REASON_VERIFY_OUT_OF_TOLERANCE: _retriable_reason(
        REASON_VERIFY_OUT_OF_TOLERANCE, TEMPLATE_VERIFY_FAIL, 2,
        RetryableReasonCopy(
            "The result didn't quite match the prediction.",
            "Try again, or undo to restore the previous sound.",
        ),
    ),
    REASON_VERIFY_CROSSOVER_REGION: _retriable_reason(
        REASON_VERIFY_CROSSOVER_REGION, TEMPLATE_VERIFY_FAIL, 2,
        # Says what was measured, no diagnosis — a handoff dip can be
        # alignment, spacing, Fc, or the horn, and this cannot tell them apart.
        # The hint deliberately does NOT lead with "try again": a retry
        # re-checks the SAME applied graph and this defect is deterministic, so
        # that is a near-dead lever. It names the two that change the outcome.
        RetryableReasonCopy(
            "The two drivers didn't blend as designed where they hand over.",
            "Re-measure to fit it again, or undo to restore the previous sound.",
        ),
    ),
    REASON_VERIFY_INCONCLUSIVE: _retriable_reason(
        REASON_VERIFY_INCONCLUSIVE, TEMPLATE_VERIFY_FAIL, 2,
        # NOT a literal (issue #1974). This copy used to assert "the room
        # reflection cut the window short" on a verdict that never consulted
        # whether a reflection was found — and across the whole 2026-07-30
        # corpus none was. The sentence has one writer now
        # (``verify_inconclusive_message``), and what the registry holds is its
        # cause-unknown rendering: true for any reader with no gate record.
        # The envelope re-renders it with the persisted fact.
        RetryableReasonCopy(
            verify_inconclusive_diagnosis(None),
            "Re-verify to try again.",
        ),
    ),
    REASON_VERIFY_LEVEL_SHIFT: _retriable_reason(
        REASON_VERIFY_LEVEL_SHIFT, TEMPLATE_VERIFY_FAIL, 2,
        # The instrument is named device-agnostically (#1941 R4): the session
        # mic may be a UMIK-2 or a laptop, and #1924's field evidence is a
        # UMIK-2 session told its phone had drifted.
        #
        # ROUTING (#1924, the half R4 deferred). ONE string renders on TWO
        # surfaces where "try again" is a DIFFERENT control, so the copy has to
        # be true on both without discrediting either:
        #
        # * measurement page (``renderPlanRetry``) — the in-session re-arm,
        #   which re-compares against the SAME reference this attempt just
        #   failed against. A level that moved and stayed moved repeats here
        #   until the budget dies.
        # * wizard (``_verify_fail_envelope``) — a FRESH relay session, which
        #   since #1927 builds a fresh conductor and re-baselines, so this gate
        #   is structurally unreachable on its first attempt. Retry settles it
        #   in one capture.
        #
        # The old ending ("re-verify to try again") commanded the retry, which
        # is the phone's dead end. Naming only Re-measure/Undo would have been
        # the mirror-image error: it discredits a wizard button that works, and
        # the screen's visible primary IS "Try again". So the sentence states
        # the fact, CONTEXTUALIZES the retry rather than commanding or
        # dismissing it, and names the escalation conditionally — "if it
        # repeats" is honest on the wizard (it will not) and on the phone (it
        # may). Both escalations are already on the verify-fail screen.
        #
        # NOT an owner ruling: #1924's body offers remedies explicitly labelled
        # "not decisions", and the issue carries no ruling comment. This
        # wording is the pipeline's call, derived from #1927's mechanics above,
        # and is the owner's to change.
        RetryableReasonCopy(
            "The microphone's levels changed between measurements, so this "
            "check couldn't settle.",
            "Try again — if it repeats, re-measure, or undo to restore the "
            "previous sound.",
        ),
    ),
    REASON_LOW_ALIGNMENT_CONFIDENCE: _retriable_reason(
        REASON_LOW_ALIGNMENT_CONFIDENCE, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "Alignment is less certain at this mic position.",
            "Place the microphone about 1 m in front of the speaker at tweeter "
            "height, then measure again.",
        ),
    ),
    REASON_APPLY_FAILED: _retriable_reason(
        REASON_APPLY_FAILED, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "JTS could not apply the measured crossover automatically.",
            "Try again.",
        ),
    ),
    REASON_USER_STOPPED: ReasonSpec(
        REASON_USER_STOPPED, TEMPLATE_SESSION_RESTART, 0, "",
        "You stopped the measurement. Start over from this page when you're "
        "ready.",
    ),
    REASON_REVIEW_HOLD_TIMEOUT: ReasonSpec(
        REASON_REVIEW_HOLD_TIMEOUT, TEMPLATE_SESSION_RESTART, 0, "",
        "Applying the measured crossover took too long, so the measurement "
        "timed out before it could finish. Start over from this page to "
        "measure again — the quick microphone check runs first.",
    ),
    REASON_CLOUD_GEOMETRY_LOCKED: _retriable_reason(
        REASON_CLOUD_GEOMETRY_LOCKED, TEMPLATE_FIX_AND_RETRY,
        # RETRIABLE (any non-zero value; see ``ReasonSpec.retry_budget``). The
        # count kept here for readability is the conductor's own ceiling on
        # wider-spot asks — ``_close_cloud_group`` stops at
        # ``GEOMETRY_RETRY_POSITIONS`` — but it is no longer what admits the
        # retake: since the bounded-retry ruling (#2086) every rung spends one
        # of the POSITION's pooled extras, booked to the speaker rather than the
        # household. Before that, this code's own budget and every other code's
        # ran side by side on the same operator.
        GEOMETRY_RETRY_POSITIONS,
        # Copy names the ACTION, not the diagnosis — a household has no way to
        # judge "the echo estimates clustered". The per-attempt wider-spot
        # instruction rides the verdict payload's ``prompt`` field on top of
        # this (see ``_cloud_measure_group_verdict``).
        RetryableReasonCopy(
            "These spots were too close together to tell a real dip from an echo.",
            "Take this one from further out and we will use it instead.",
        ),
    ),
    # PR-L4. Both are HARD_STOP with budget 0: the defects are systematic, not
    # transient — a second identical measurement reproduces them — and both name
    # the one thing a household can actually act on, the declared driver details
    # the level frame is built from. Copy names the ACTION, not the arithmetic.
    REASON_DRIVER_LEVELS_DISAGREE: ReasonSpec(
        REASON_DRIVER_LEVELS_DISAGREE, TEMPLATE_HARD_STOP, 0, "",
        "The two drivers would not have ended up at matching levels, so JTS "
        "left your speaker alone. Re-check the driver details — sensitivity "
        "and any resistor pad — in speaker setup, then measure again.",
    ),
    REASON_CORRECTION_NOT_AN_IMPROVEMENT: ReasonSpec(
        REASON_CORRECTION_NOT_AN_IMPROVEMENT, TEMPLATE_HARD_STOP, 0, "",
        "The tuning JTS worked out would not have made this speaker measure "
        "better, so it was not applied. Re-check the driver details in speaker "
        "setup, then measure again.",
    ),
    # PR-L5 delta-probe rollbacks. All three are TEMPLATE_HARD_STOP with no
    # retry budget: the correction has already been undone, so "try again"
    # would re-run the same measurement into the same defect. Each names what
    # was restored FIRST — a household whose speaker just changed twice needs
    # to know where it ended up before it needs a diagnosis — and then the one
    # thing that would actually change the outcome. No hardware nouns, matching
    # the null-classification copy rule.
    REASON_CORRECTION_MODEL_ERROR: ReasonSpec(
        REASON_CORRECTION_MODEL_ERROR, TEMPLATE_HARD_STOP, 0, "",
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — so the previous sound has been put back. This "
        "usually means something in the chain is not behaving as described; "
        "re-check the driver details in speaker setup, then measure again.",
    ),
    REASON_CORRECTION_LEVEL_SHORTFALL: ReasonSpec(
        REASON_CORRECTION_LEVEL_SHORTFALL, TEMPLATE_HARD_STOP, 0, "",
        "Your speaker delivered noticeably less than the tuning asked it for, "
        "so the previous sound has been put back. Try measuring again at a "
        "lower listening volume.",
    ),
    REASON_CORRECTION_SPATIALLY_COSTLY: ReasonSpec(
        REASON_CORRECTION_SPATIALLY_COSTLY, TEMPLATE_HARD_STOP, 0, "",
        "The tuning helped at the measuring spot but made the sound less even "
        "elsewhere in the room, so the previous sound has been put back. "
        "Moving the speaker away from nearby walls and surfaces, then "
        "measuring again, is what changes this.",
    ),
    # #2291's measured regression. Same promise as the three above — and it is
    # true on the same terms: this row renders only when the restore actually
    # ran, and the failed-restore row below is what renders when it did not.
    # The remedy differs from its neighbours because the finding does: nothing
    # misbehaved, so there is no chain to re-check and no level to drop. The
    # honest next step is a different measurement, which usually means moving
    # the microphone or the speaker.
    REASON_CORRECTION_MEASURED_REGRESSION: ReasonSpec(
        REASON_CORRECTION_MEASURED_REGRESSION, TEMPLATE_HARD_STOP, 0, "",
        "JTS measured your speaker before and after the tuning, and it "
        "measured worse afterwards — so the previous sound has been put back. "
        "Nothing is broken; this room and this speaker position did not suit "
        "the tuning. Moving the speaker a little, or measuring from your usual "
        "listening spot, is what changes this.",
    ),
    # #2291's fail-closed boost, and the one row here that reports a
    # NON-finding. Says what JTS could not establish before what it did about
    # it, because the household's speaker changed twice and "why" is otherwise
    # unanswerable from the screen.
    REASON_CORRECTION_UNPROVEN_BOOST: ReasonSpec(
        REASON_CORRECTION_UNPROVEN_BOOST, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not measure whether this tuning improved your speaker, and "
        "it turns some parts up rather than only down — so the previous sound "
        "has been put back rather than leaving an unproven change driving your "
        "speaker harder. Measuring again, from your usual listening spot, is "
        "what settles it.",
    ),
    # The five rows above all promise "the previous sound has been put back",
    # which is only true when the rollback actually ran. When it did not, THIS
    # is the row that renders instead — same finding, opposite state of the
    # speaker, and it says so first.
    #
    # ONE row rather than three verdict-specific ones, deliberately. Splitting
    # it would let each keep its own remedy ("move the speaker away from
    # walls"), but that remedy is the SECOND thing this household needs: the
    # first is that a correction they are listening to right now was found
    # faulty and is still applied, and the action is Undo in all three cases.
    # Three near-duplicate rows for a state that should be rare is registry
    # bloat, and the specific finding is on the verdict itself
    # (``delta_probe.verdict``, in the payload and the journal) for whoever
    # needs it after the undo.
    REASON_CORRECTION_ROLLBACK_FAILED: ReasonSpec(
        REASON_CORRECTION_ROLLBACK_FAILED, TEMPLATE_HARD_STOP, 0, "",
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — but it could not put the previous sound back on "
        "its own, so the new tuning is STILL APPLIED. Tap Undo on the speaker "
        "page to restore the previous sound.",
    ),
}

# The transient codes whose first retry is automatic (a banner, no decision
# screen) per §5.10 template 1.
TRANSIENT_AUTO_RETRY_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items()
    if spec.template == TEMPLATE_SILENT_AUTO_RETRY
)

#: #2291 Phase 5a-iv: the capture-consuming ladders' refusal KINDS, mapped to
#: the codes whose copy the household reads.
#:
#: :mod:`jasper.active_speaker.crossover_v2.spatial` owns the ORDER those
#: ladders run in and returns a kind; this file owns the registry above, so the
#: sentence stays here. The same split :mod:`.crossover_v2.coordinator` makes
#: with :data:`~jasper.active_speaker.crossover_v2.coordinator.REFUSAL_KINDS`,
#: and it is a mapping rather than an identity because two kinds do NOT share
#: their code's name: a glitched timeline renders as
#: ``drift_baselines_disagree`` and a bent curve as ``agc_behavioral_fail``.
#:
#: Completeness is CHECKED, not trusted — see :func:`_screen_refusal_code` and
#: ``test_every_screen_kind_has_a_household_sentence``.
SCREEN_KIND_REASONS: dict[str, str] = {
    _spatial.SCREEN_LOCATE_FAILED: REASON_LOCATE_FAILED,
    _spatial.SCREEN_PILOT_LEVEL_COLLAPSE: REASON_PILOT_LEVEL_COLLAPSE,
    _spatial.SCREEN_LINEARITY_FAILED: REASON_AGC_BEHAVIORAL_FAIL,
    _spatial.SCREEN_CAPTURE_GLITCH: REASON_DRIFT_BASELINES_DISAGREE,
    _spatial.SCREEN_CLIPPED: REASON_CLIPPED,
}


def _screen_refusal_code(kind: str) -> str:
    """One screen kind's household code, LOUDLY on an unmapped one.

    A kind arriving here unmapped is a wiring defect — a new ladder step shipped
    without a sentence — and answering it with another kind's copy is the shape
    :meth:`CrossoverV2Conductor._round_refusal_for` already refuses. It still
    returns rather than raising, under the most conservative code available: the
    capture was screened and something was wrong with it, and losing that
    refusal to a mapping gap would be worse than naming it imprecisely for one
    release.
    """
    code = SCREEN_KIND_REASONS.get(kind)
    if code is not None:
        return code
    log_event(
        logger, "correction.crossover_v2_screen_kind_unmapped",
        level=logging.ERROR, kind=str(kind),
    )
    return REASON_LOCATE_FAILED


def correction_rollback_failed_message(rollback_anchor_available: bool | None) -> str:
    """``correction_rollback_failed``'s sentence, branched on the anchor.

    One code, two situations, and until #2291 one sentence — which pointed the
    wrong half at a control that cannot help it.

    * **A restore was attempted and did not complete** (``True``/``None``):
      there IS a stored previous sound, the automatic attempt failed, and Undo
      is a real remedy the household can press. Unchanged copy.
    * **There was never an anchor** (``False``): the adoption table routed here
      *because* no previous sound exists, and Undo refuses on that same
      predicate. Telling this household to tap it sends them to a dead end on
      the most ordinary case there is — a speaker's first-ever correction. So
      this arm names no Undo, states what is true about their speaker, and
      offers the two remedies that DO exist.

    ``None`` takes the Undo arm deliberately: an unestablished fact must not
    invent the more alarming claim ("nothing to go back to") about a speaker
    that may well have a perfectly good anchor.
    """
    if rollback_anchor_available is False:
        return (
            "The new tuning is still applied, and this speaker has no stored "
            "previous sound to go back to — this was its first measured "
            "crossover. You can measure again to try for a better result, or "
            "clear the tuning from the Sound page to return to the standard "
            "setup."
        )
    return (
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — but it could not put the previous sound back, "
        "so the newer tuning is STILL APPLIED. Tap Undo to restore the "
        "previous sound."
    )


def reason_message(
    code: str,
    spec: ReasonSpec,
    *,
    pilot_heard: bool | None = None,
    reflection_measured: bool | None = None,
    rollback_anchor_available: bool | None = None,
) -> str:
    """The household sentence for ``code``, given what the capture measured.

    **THE single copy selector**, because one failure is narrated on several
    surfaces that never see each other: the relay verdict the measurement page
    shows the moment a capture is refused
    (:meth:`PhaseVerdict.to_relay_dict`), the envelope jts.local serves for
    the persisted terminal failure
    (``crossover_envelope_v2._reason_message``), the apply-seam refusal, and
    :meth:`_refuse`'s accountability refusals. Two codes now choose their copy
    from evidence rather than holding a literal, and a household looking at
    two of those surfaces after ONE failure must not be handed two different
    accounts of it — which is exactly how the inconclusive copy's own bug
    stayed invisible for as long as it did (#1974). Adding a third
    evidence-keyed code means adding a branch HERE; a caller that renders
    ``spec.message`` directly re-opens the gap.

    Exhaustion is state-aware: :meth:`authorize_begin` keeps the diagnosis
    selected here but replaces retry advice with the terminal outcome. That is
    intentionally not whole-sentence equality. The observation must agree
    across surfaces; an action that is no longer available must not survive.

    ``spec`` is passed in rather than looked up so each caller keeps the
    existence guard it already had — ``REASON_REGISTRY[code]`` raising
    ``KeyError`` on an unregistered code is load-bearing in :meth:`_refuse`,
    whose whole purpose is that a refusal never ships a bare code where a
    household expects a sentence.

    Facts are keyword-only and each defaults to "not established", so a caller
    holding none of them gets the registry's own renderings — the same answer
    reading ``REASON_REGISTRY`` by hand would give.
    """
    if code == REASON_LOCATE_FAILED:
        return locate_failed_message(pilot_heard)
    if code == REASON_VERIFY_INCONCLUSIVE:
        return verify_inconclusive_message(reflection_measured)
    if code == REASON_CORRECTION_ROLLBACK_FAILED:
        return correction_rollback_failed_message(rollback_anchor_available)
    # ``or spec.banner`` for the silent-auto-retry codes, whose household text
    # IS the banner and whose ``message`` is empty by construction.
    return spec.message or spec.banner


def reason_diagnosis(
    code: str,
    spec: ReasonSpec,
    *,
    pilot_heard: bool | None = None,
    reflection_measured: bool | None = None,
) -> str:
    """The observation inside any retryable reason, without retry advice.

    The two evidence-keyed reasons select their diagnosis from this capture's
    facts. Every literal reason reads the diagnosis stored in its structured
    :class:`RetryableReasonCopy`; that same value also composes the registry's
    full retryable ``message``/``banner``. Exhaustion therefore preserves X
    for every retryable code without maintaining a second prose table.
    """
    if code == REASON_LOCATE_FAILED:
        return locate_failed_diagnosis(pilot_heard)
    if code == REASON_VERIFY_INCONCLUSIVE:
        return verify_inconclusive_diagnosis(reflection_measured)
    return spec.retry_copy.diagnosis if spec.retry_copy is not None else ""


# Conditions no extra attempt can clear — wiring in the wrong order, a tuning
# that would not have improved the speaker, a dead link. The bounded-retry
# ruling (#2086) is a CEILING on retries, not a floor: it stops the flow asking
# a household for a fifth take of the same spot, and it does not start it asking
# for a second take of something a second take cannot fix. These refuse on the
# next begin, with their own copy, exactly as they always have.
NON_RETRIABLE_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items() if spec.retry_budget == 0
)

# --------------------------------------------------------------------------- #
# tuning constants (PROVISIONAL pending W6 bench validation)
# --------------------------------------------------------------------------- #

# Re-exported from :mod:`jasper.active_speaker.crossover_v2.programs`, which
# owns the level policy it belongs to (#2291 Phase 5a-ii).
GAIN_CAP_BACKOFF_DB = _programs.GAIN_CAP_BACKOFF_DB
# Per gain-adjusted clip retry, drop the offending program's level by this much.
CLIP_RETRY_BACKOFF_DB = 3.0
# Re-exported; see ``crossover_v2.programs`` (#2291 Phase 5a-ii).
PILOT_LEVEL_DELTA_DB = _programs.PILOT_LEVEL_DELTA_DB
# A located stimulus below this correlation confidence reads as "couldn't hear
# the speaker" (locate_failed).
LOCATE_MIN_CONFIDENCE = 0.1
# VERIFY PASS: |measured sum − predicted sum| ≤ this over [Fc/2, 2·Fc] (§5.2),
# measured against the notch-excluded max (W6.7 ruling 1 —
# `program_analysis.VERIFY_NOTCH_EXCLUSION_DB`) rather than the raw max.
VERIFY_TOLERANCE_DB = 1.5


def verify_absolute_tolerance_db(band_hz: Sequence[float]) -> float | None:
    """How far the realized sum may sit from the candidate's own crossover
    target across ``band_hz``, in dB — or ``None``, no tolerance to apply.

    **Derived, never chosen.** The product already promises an absolute
    magnitude tolerance over this frequency range — ``flat_spec.SPEC_BANDS``,
    the adopted spec table — so this returns the LOOSEST entry the region
    overlaps and a crossover-region result is never held to a tighter bar than
    the speaker's own spec applies somewhere inside it. For the shipped 2 kHz
    two-way: ``max(1.5 [250–2k], 2.0 [2k–8k]) = 2.0 dB``. It inherits that
    table's S0-contingent status rather than restating a literal.

    Deliberately NOT :data:`VERIFY_TOLERANCE_DB`, which bounds
    measured-vs-MODEL; this bounds measured-vs-DESIGN. Same units, different
    question.

    Known contributor, not corrected for: rung P1 measured a frame tilt
    between VERIFY's in-room curve and its on-axis model, and part of that
    frame lands in this residual too. It is DISCLOSED beside the number
    (``_verify_frame_lines``) rather than removed, following this flow's
    standing rule that a measured tilt is evidence, not permission to re-grade.

    ``None`` when the region overlaps no specced band (a crossover high enough
    that the region is entirely ``flat_spec.BEST_EFFORT_ABOVE_HZ``, where the
    table itself declines): the caller records the claim not-evaluated rather
    than inventing a bar.
    """
    from jasper.active_speaker.flat_spec import SPEC_BANDS

    if len(band_hz) != 2:
        return None
    lo, hi = float(band_hz[0]), float(band_hz[1])
    overlapping = [tol for f_lo, f_hi, tol in SPEC_BANDS if f_lo < hi and lo < f_hi]
    return max(overlapping) if overlapping else None


# The prescribed on-axis mic distance the parallax correction assumes (§5.2).
MEASUREMENT_DISTANCE_M = 1.0
# Below this GCC-seed/capture confidence (see ``AlignmentEstimate.confidence``
# and ``confidence_source`` in ``program_analysis.py``), the conductor refuses
# to auto-apply and rejects
# MEASURE with ``REASON_LOW_ALIGNMENT_CONFIDENCE`` instead of building a
# candidate (owner ruling, 2026-07-20). Formerly
# ``crossover_envelope_v2.ALIGNMENT_CONFIDENCE_NUDGE_FLOOR`` — a review-screen
# nudge that left Apply available regardless ("informed consent, not a
# gate"). Moved here and promoted to a hard gate now that apply is automatic:
# there is no more human screen to hand the informed-consent judgment to.
# PROVISIONAL pending W6 bench distributions on confidence-vs-outcome
# correlation (unchanged from the prior nudge floor's own provisional status).
ALIGNMENT_CONFIDENCE_TRUST_FLOOR = 0.6
# Physical-plausibility backstop (Fix 3, 2026-07-21): the GCC estimator can
# return a CONFIDENTLY WRONG delay (a hardware run reported a confident
# −631 us against this preset's declared [50, 300] us delay_range_ms search
# bound) that still clears ALIGNMENT_CONFIDENCE_TRUST_FLOOR above — high GCC
# correlation confidence at the wrong lag is a real failure mode, not a
# hypothetical one. This margin is added on BOTH sides of the crossover
# region's declared ``delay_range_ms`` (a SEARCH bound per
# ``jasper.active_speaker.profile.CrossoverRegion``'s own docstring, not a
# hard physical limit) before a measured delay outside it is rejected, so a
# delay a little past the declared bound isn't treated the same as one
# wildly outside it. PROVISIONAL pending W6 bench validation, same status as
# the confidence floor above.
ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS = 0.1

# Measurement-honesty disclosure G1 (2026-07-22; converted from a refusal to a
# disclosure by owner ruling on 2026-08-03, issue #2087): a corrupted
# phone-chain MEASURE capture on 2026-07-22 hardware built a candidate whose
# ``predicted_ripple_db`` was 27.316 dB at an alignment confidence (0.703) that
# cleared ALIGNMENT_CONFIDENCE_TRUST_FLOOR above — the candidate auto-applied,
# then failed three VERIFYs at 5.3-6.7 dB. Every clean MEASURE that same day
# predicted 4.387-9.031 dB — 13 captures precisely: 4 on UMIK-2, 8 on
# iMM-6C, 1 accepted phone-chain measure. This composition is OWNED here;
# cite this comment rather than re-quoting a count (issue #2015 traced a
# since-corrected 12-capture, two-chain restatement elsewhere to a copy
# that dropped the phone measure). Primary source: that night's own
# retention sidecars, tabulated in ``captures/xover-e0-2026-07-21/
# honesty-guards-proof-20260722/REPORT.md``'s G1 table. This threshold sits ~6
# dB above the clean corpus's worst case and ~12 dB below the corrupt one —
# wide margin on both sides.
#
# **It is a DISCLOSURE TRIGGER, not a gate — the owner's 2026-08-03 ruling on
# #2087.** It refused captures until then, and the refusal was wrong in the way
# a hard quality ceiling is usually wrong: a household whose room and hardware
# simply sit above a corpus collected on better rigs was told to move a
# correctly-placed microphone, and the session died on the attempt meter. The
# live 2026-08-03 bench validation is the case that settled it — 15.244 dB
# refused 58 s after an identically-positioned 11.324 dB capture was accepted,
# both at alignment confidence ~0.677, so confidence was never the
# discriminator the reused reason code claimed it was. Crossing this threshold
# now means the session PROCEEDS and says so: the capture is accepted, the
# measured value is banked as a reservation, and the household reads one plain
# sentence on the screens that offer and report the tuning. Nothing about the
# threshold's calibration changed; only what crossing it does.
#
# What did NOT change, stated because a reader will ask: the trust floor, the
# delay-plausibility backstop, the SNR/linearity/glitch verdicts and every
# accountability gate below still REFUSE. This one number stopped being a veto
# because a bad ripple describes how well two branches can sum in this room on
# this rig — a thing the household cannot act on by moving anything — and not
# a defect in the capture that measuring again would fix.
#
# PROVISIONAL pending W6 bench validation, same status as every other
# MEASURE-phase threshold in this block.
MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB = 15.0

# Measurement-honesty gate G2 (2026-07-22): an ``event=outputd.xrun`` playback
# glitch on 2026-07-22 hardware shifted a MEASURE capture's three sweeps
# −25…−28 ms off their SCHEDULED slot with per-segment locate confidence
# 0.07-0.12 (the measured clean corpus's WORST capture ran ≤1.5 ms residual
# at ≥0.6926 confidence) while ``glitch_detected`` stayed False — the
# repeat-pair drift check (``_estimate_drift``) is structurally blind to a
# uniform whole-capture shift (its own residual guard demeans per role, so
# it only catches a WITHIN-driver desync), and ``_stimulus_locate_ok`` passed
# on the max() confidence across every located stimulus, so one good segment
# masked three bad sweeps (that max() is per-ROLE since #1838's D8, which
# narrows but does not close the hole — a role's own pilots can still be the
# segment that clears it, which is why this per-sweep floor exists). Both
# thresholds carry wide margin on both sides of the two clusters above.
# PROVISIONAL pending W6 bench validation.
#
# The two are read by DIFFERENT gates since #1838's D3: the residual ceiling
# by ``_sweep_schedule_ok`` (a glitch — silent auto-retry), the confidence
# floor by ``_sweep_locate_confidence_ok`` (too quiet — no retry).
#
# Both have a deliberate twin one layer down —
# ``program_analysis.SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`` /
# ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` — which apply the SAME two judgments to
# VERIFY's single ``KIND_SUMMED_SWEEP`` (issue #1971), a segment kind neither
# gate here has ever filtered for. ``program_analysis`` cannot import them
# from this module without inverting the dependency, so they are duplicated
# and pinned by tests/test_measurement_integrity_floor_contracts.py: a
# deliberate move of either number must update BOTH copies and that test.
SWEEP_SCHEDULE_RESIDUAL_CEILING_MS = 5.0
SWEEP_LOCATE_CONFIDENCE_FLOOR = 0.3

# Measurement-honesty gate G3 (2026-07-22): the gate's OWN metric (summed-
# pilot transfer step) measured the phone's input chain stepping 0.75-0.82
# dB across the dishonest 1.192 → 2.111 → 2.835 dB VERIFY attempt sequence on
# 2026-07-22 hardware, producing verdicts that read as "speaker out of
# tolerance" when the recorder was what changed — the one clean multi-
# attempt session on the same rig stepped ≤0.05 dB by that SAME metric. (A
# separate, coarser frequency-differential estimate of the same drift put it
# at ~0.56 dB — kept only as secondary corroborating context; the pilot-band
# numbers above are what this gate actually measures and are the primary
# evidence.) VERIFY replays the IDENTICAL program through the IDENTICAL
# applied graph on every attempt, so its own leading pilot pair's transfer
# (captured level minus programmed gain) should not move between attempts
# either — a step this large is the input chain moving, not the speaker.
# PROVISIONAL pending W6 bench validation.
VERIFY_PILOT_TRANSFER_STEP_CEILING_DB = 0.35

# Re-exported from :mod:`jasper.active_speaker.crossover_v2.programs`, which
# owns it and states why it has no switch and why both the phone's duration
# budget and the actual playback must read the SAME constant (#2291 Phase
# 5a-ii). The two capture-plan builders in this module are the other pair of
# readers.
COURTESY_PRELUDE_ENABLED = _programs.COURTESY_PRELUDE_ENABLED


class CrossoverV2FlowError(RuntimeError):
    """The v2 conductor could not form a safe phase transition."""


# --------------------------------------------------------------------------- #
# pure helpers (fixture-testable in isolation)
# --------------------------------------------------------------------------- #


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.programs`, which
#: owns it beside the three composers that are its only production callers
#: (#2291 Phase 5a-ii). Every existing ``flow.back_off_gain`` import resolves to
#: that one function.
back_off_gain = _programs.back_off_gain


def alignment_to_candidate_fields(
    analysis: ProgramAnalysis, *, woofer_role: str, tweeter_role: str,
) -> tuple[float | None, str | None, str | None]:
    """Map a MEASURE ``AlignmentEstimate`` to ``(delay_us, delay_role, polarity)``.

    Honours the analysis sign contract (design §5.6.5): its ``delay_us`` is
    ``(D_woofer − D_tweeter)``, so **positive ⇒ the tweeter arrived earlier and
    the tweeter branch is delayed**; negative ⇒ the woofer is delayed. The W4
    :class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverAlignment`
    wants a non-negative magnitude + the delayed role, so the sign is folded into
    the role choice. Returns ``(None, None, None)`` when there is no trustworthy
    alignment (missing, or the estimator clamped at the search-window edge), so
    the candidate falls back to a trims-only apply.
    """
    from jasper.active_speaker.crossover_alignment import (
        POLARITY_INVERT,
        POLARITY_KEEP,
    )

    est = analysis.alignment
    if est is None or est.status != ALIGNMENT_OK:
        return None, None, None
    delay_us = float(est.delay_us)
    if delay_us >= 0.0:
        role, magnitude = tweeter_role, delay_us
    else:
        role, magnitude = woofer_role, -delay_us
    polarity = POLARITY_INVERT if est.polarity == "inverted" else POLARITY_KEEP
    return magnitude, role, polarity


def _declared_alignment_delay_range_ms(
    source_preset: Any,
) -> tuple[Any, float, float] | None:
    """Return the single v2 region plus its valid declared delay range."""
    regions = getattr(source_preset, "crossover_regions", None)
    if not regions:
        return None
    region = regions[0]
    delay_range_ms = getattr(region, "delay_range_ms", None)
    if not (isinstance(delay_range_ms, (tuple, list)) and len(delay_range_ms) == 2):
        return None
    lo_ms, hi_ms = float(delay_range_ms[0]), float(delay_range_ms[1])
    if not (math.isfinite(lo_ms) and math.isfinite(hi_ms)) or lo_ms > hi_ms:
        return None
    return region, lo_ms, hi_ms


def alignment_delay_search_bounds_us(
    source_preset: Any,
    *,
    margin_ms: float = ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS,
) -> tuple[float, float] | None:
    """Flatness-search magnitude bounds from the preset's declaration.

    The range and margin are the same ones Fix 3's plausibility gate reads.
    ``delay_target_driver`` is optional until a delay has actually been applied,
    so it cannot orient a fresh measurement. The analysis uses the
    drift-corrected physical peak gap to orient and center one signed lobe
    inside these declared magnitude bounds; GCC remains confidence, polarity,
    and fallback evidence only.
    """
    declared = _declared_alignment_delay_range_ms(source_preset)
    if declared is None:
        return None
    _region, lo_ms, hi_ms = declared
    lo_ms = max(0.0, lo_ms - margin_ms)
    hi_ms += margin_ms
    return lo_ms * 1000.0, hi_ms * 1000.0


def alignment_delay_plausible(
    delay_us: float | None,
    source_preset: Any,
    *,
    margin_ms: float = ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS,
) -> bool:
    """True when ``|delay_us|`` falls inside the preset's declared crossover
    region ``delay_range_ms`` search bound (± ``margin_ms``), or when there is
    no declared bound / no delay to judge (nothing to gate on).

    Physical-plausibility backstop (Fix 3): see
    :data:`ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS`. Declaration-driven —
    reads the SAME ``delay_range_ms`` the crossover region already carries as
    a search bound (:class:`jasper.active_speaker.profile.CrossoverRegion`),
    never a hardcoded delay literal. The v2 conductor is scoped to a single
    2-way crossover region (``crossover_regions[0]``), matching every other
    single-region read in this module (e.g. ``resolve_conductor_context``).
    """
    if delay_us is None:
        return True
    declared = _declared_alignment_delay_range_ms(source_preset)
    if declared is None:
        return True
    _region, lo_ms, hi_ms = declared
    delay_ms = abs(float(delay_us)) / 1000.0
    return (lo_ms - margin_ms) <= delay_ms <= (hi_ms + margin_ms)


def _analysis_json(analysis: ProgramAnalysis) -> dict[str, Any]:
    """Compact JSON-safe evidence core for the measured candidate fingerprint.

    The W4 candidate freezes ``analysis`` as exact JSON data, so only the
    scalar verdicts travel — never the numpy response arrays. Enough to identify
    the exact measurement that authorized the candidate (§5.6/§5.8).
    """
    drift = analysis.drift
    align = analysis.alignment
    cand = analysis.candidate
    return {
        "schema_version": 1,
        "kind": "jts_program_analysis_evidence",
        "program_id": analysis.program_id,
        "epsilon_ppm": round(float(drift.epsilon_ppm), 3) if drift else None,
        "glitch_detected": bool(analysis.glitch_detected),
        "delay_us": round(float(align.delay_us), 3) if align else None,
        "alignment_seed_delay_us": (
            round(float(align.seed_delay_us), 3)
            if align and align.seed_delay_us is not None else None
        ),
        "polarity": align.polarity if align else None,
        "alignment_confidence": round(float(align.confidence), 4) if align else None,
        "alignment_confidence_source": align.confidence_source if align else None,
        "trim_db": (
            {k: round(float(v), 4) for k, v in cand.trim_db.items()} if cand else None
        ),
        # #1667: the band-average seed trim_db's ripple-optimal solve started
        # from — evidence only, so replay/forensics can always see both even
        # when the applied trim_db above coincides with it (the sanity-guard
        # fallback path).
        "trim_band_average_db": (
            {k: round(float(v), 4) for k, v in cand.trim_band_average_db.items()}
            if cand and cand.trim_band_average_db is not None else None
        ),
        "predicted_ripple_db": (
            round(float(cand.predicted_ripple_db), 4) if cand else None
        ),
        "alignment_seed_ripple_db": (
            round(float(cand.alignment_seed_ripple_db), 4)
            if cand and cand.alignment_seed_ripple_db is not None else None
        ),
        "flatness_improvement_db": (
            round(float(cand.flatness_improvement_db), 4)
            if cand and cand.flatness_improvement_db is not None else None
        ),
        "anchor_delay_us": (
            round(float(cand.anchor_delay_us), 3)
            if cand and cand.anchor_delay_us is not None else None
        ),
        "snap_delta_us": (
            round(float(cand.snap_delta_us), 3)
            if cand and cand.snap_delta_us is not None else None
        ),
        "snap_found": bool(cand.snap_found) if cand else None,
    }


def _stimulus_locate_ok(analysis: ProgramAnalysis) -> bool:
    """False when any ROLE's stimuli all failed the locate-confidence floor.

    D8 (issue #1838). This used to be ``max(confidences) >= floor`` over every
    stimulus segment in the capture, which is effectively no floor at all on a
    multi-driver program: one clearly-located segment anywhere cleared the
    gate for the whole capture, so a capture in which an entire driver was
    inaudible passed and went on to be analysed as if both drivers had been
    heard. Grouping by role first makes the gate mean what its name says.

    Per ROLE, not per SEGMENT, deliberately. A role's segments are not
    equally locatable by design — a two-level pilot pair's quiet side sits
    10 dB under its loud side and locates more coarsely — so requiring every
    segment to clear the floor would fail captures that are fine. One
    confidently-located stimulus is enough to say "this driver was heard";
    zero is not. Role-less stimuli (a summed sweep, which carries no role)
    group together and are held to the same rule.

    The stricter per-SWEEP floor that MEASURE also applies lives in
    :func:`_sweep_locate_confidence_ok`.
    """
    by_role: dict[str | None, float] = {}
    for loc in analysis.locations:
        if loc.kind not in STIMULUS_KINDS:
            continue
        best = by_role.get(loc.role)
        if best is None or loc.confidence > best:
            by_role[loc.role] = loc.confidence
    if not by_role:
        return False
    return all(best >= LOCATE_MIN_CONFIDENCE for best in by_role.values())


def _sweep_locate_confidence_ok(analysis: ProgramAnalysis) -> bool:
    """False when a MEASURE sweep was only weakly located — i.e. too quiet.

    Split out of :func:`_sweep_schedule_ok` by D3 (issue #1838). The two
    halves of that gate answer different questions and deserve different
    verdicts:

    * a sweep whose RESIDUAL is out of bounds landed off its scheduled slot —
      a timeline splice, a genuine capture glitch, retry;
    * a sweep the locator could barely find at all is not a splice. It is a
      capture too quiet to hear, and the fix is the level or the mic, not a
      retry of the same level.

    In session cap_-Us10xORVNlFa_dgi-sP7g the sweeps located at 0.0298
    against this 0.3 floor, the mis-located sweeps then produced a 1018-sample
    residual, and the residual tripped ``glitch_detected`` — so the household
    was told the capture glitched and the flow silently re-armed the same
    unwinnable level. Low SNR CAUSES the glitch signal; ordering this check
    ahead of it is what makes the reported cause the real one.

    Same ``KIND_SWEEP`` domain as :func:`_sweep_schedule_ok`: the leading
    pilot pair's short, quiet windows locate coarsely by design and would
    manufacture spurious fires here.

    That domain is MEASURE-only, and deliberately stays so. VERIFY's sweep is
    ``KIND_SUMMED_SWEEP``, and the same judgment is made for it one layer down
    by ``program_analysis._verify_capture_integrity`` (issue #1971) — where
    the capture's own record can also say which MEASURE-era checks could not
    run there at all.
    """
    return all(
        loc.confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        for loc in analysis.locations
        if loc.kind == KIND_SWEEP
    )


def _sweep_schedule_ok(analysis: ProgramAnalysis, sample_rate_hz: int) -> bool:
    """False when a MEASURE sweep landed off its scheduled slot
    (measurement-honesty gate G2, 2026-07-22 — the xrun detector; see
    :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS` for the evidence).

    Since D3 (issue #1838) this is the RESIDUAL half of G2 only. The
    locate-confidence half moved to :func:`_sweep_locate_confidence_ok`,
    which runs earlier and answers "too quiet" instead of "glitched" — see
    that function for why the two must not share a verdict.

    ``sample_rate_hz`` is deliberately the CALLER's own MEASURE program rate,
    not something read off ``analysis`` itself:
    ``analyze_program_capture`` HARD-REFUSES a capture whose sample rate
    disagrees with the program's own (``capture rate != program rate``,
    ``jasper.audio_measurement.program_analysis``), and the relay capture
    spec fixes every phone upload at ``REQUIRED_SAMPLE_RATE_HZ`` (48 kHz,
    ``jasper.capture_relay.spec``) — so no resampling ever runs between the
    phone's WAV and this analysis, and ``SegmentLocation.residual_samples``
    is always expressed in exactly that domain (the conductor's own composed
    program's ``sample_rate_hz``).

    Filtered to ``KIND_SWEEP`` only — mirrors ``_estimate_drift``'s exclusion
    of the leading pilot pair from residual/drift logic (their short/quiet
    windows locate more coarsely and would manufacture spurious fires here).
    VERIFY's ``KIND_SUMMED_SWEEP`` is out of this domain on purpose; see
    :func:`_sweep_locate_confidence_ok` for where its twin lives (#1971).
    No sweeps at all (nothing to judge) passes — the pre-existing
    ``_stimulus_locate_ok`` check, which runs earlier in ``_measure_verdict``'s
    ladder, already covers "nothing usable in this capture".
    """
    sweeps = [loc for loc in analysis.locations if loc.kind == KIND_SWEEP]
    if not sweeps:
        return True
    for loc in sweeps:
        residual_ms = abs(loc.residual_samples) / sample_rate_hz * 1000.0
        if residual_ms > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS:
            return False
    return True


def _sweep_schedule_diag_fields(
    analysis: ProgramAnalysis, sample_rate_hz: int,
) -> tuple[float | None, float | None]:
    """``(sweep_residual_ms_worst, sweep_locate_confidence_min)`` — diagnostic
    only, over the SAME ``KIND_SWEEP`` domain ``_sweep_schedule_ok`` and
    ``_sweep_locate_confidence_ok`` gate on (one figure each, since #1838's
    D3 split them), but never itself gates a verdict. ``sweep_residual_ms_worst`` is the
    SIGNED residual (not its magnitude) of whichever sweep has the largest
    absolute residual, so a reviewer sees which direction the schedule broke,
    not just how far. ``(None, None)`` when there are no sweeps to judge —
    mirrors ``_sweep_schedule_ok``'s own "nothing to judge" stance.
    """
    sweeps = [loc for loc in analysis.locations if loc.kind == KIND_SWEEP]
    if not sweeps:
        return None, None
    worst = max(sweeps, key=lambda loc: abs(loc.residual_samples))
    residual_ms_worst = worst.residual_samples / sample_rate_hz * 1000.0
    confidence_min = min(loc.confidence for loc in sweeps)
    return residual_ms_worst, confidence_min


def _capture_integrity_log_field(integrity: CaptureIntegrity | None) -> str:
    """One logfmt token for a VERIFY capture's integrity verdict (#1971).

    Three values a reader must be able to tell apart, which is why this is not
    a bool: ``unavailable`` (no record — a pre-#1971 analysis shape, never
    produced by the live analyze seam), ``ok`` (every evaluated check passed),
    or the comma-joined names of the checks that FAILED. The companion
    ``integrity_not_evaluated`` field carries what could not be checked at
    all, so "ok" never has to stand in for "nobody looked".
    """
    if integrity is None:
        return "unavailable"
    return ",".join(integrity.failed) if integrity.failed else "ok"


def _any_sweep_clipped(analysis: ProgramAnalysis) -> bool:
    return any(
        loc.clipped for loc in analysis.locations if loc.kind in STIMULUS_KINDS
    )


def _gate_window_ms(response: Any) -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def _band_edge(band: Any, index: int) -> float | None:
    """One edge of a persisted ``[lo, hi]`` band pair, or ``None``.

    For log lines that carry a band as two scalars (the shape
    ``_log_verify_diag``'s ``tracking_band_lo_hz``/``_hi_hz`` established)
    rather than one bracketed value logfmt would have to quote.
    """
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    edge = band[index]
    return float(edge) if isinstance(edge, (int, float)) else None


def _per_band_flatness_log_field(bands: Any) -> str:
    """One compact token per graded spec band, its own worst deviation from
    the SAME reference ``flatness_max_db`` above is stated against (issue
    #1857) -- so a log reader is never limited to the single band the gauge
    happened to flag as worst. A uniformly-off band drags the shared
    reference toward itself and can make an unrelated band's ordinary
    ripple read as the LARGER deviation; this is what let a #1857 corpus
    session's worst-band pointer read the woofer while the tweeter sat
    uniformly ~5 dB dark across its own passband, undetected by the single
    logged point. Same disclosure, and the same "unevaluable is not a
    fabricated verdict" skip rule, as
    ``crossover_envelope_v2._per_band_flatness_lines`` (the household-facing
    prose reading of the identical numbers) -- shaped for one logfmt token
    (``lo-hiHz:+dev.ddB:pass|fail``, semicolon-joined, no bracket or space
    for logfmt to quote) rather than a sentence. Disclosure only: every
    figure is copied from the SAME :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`
    ``flatness_max_db`` reads, nothing is recomputed, and no verdict moves.
    ``""`` (never a fabricated reading) when ``bands`` is absent or no band
    survives to be measured.
    """
    if not isinstance(bands, list):
        return ""
    parts: list[str] = []
    for band in bands:
        if not isinstance(band, Mapping) or not band.get("evaluable"):
            continue
        lo, hi = band.get("f_lo_hz"), band.get("f_hi_hz")
        deviation_db, passed = band.get("max_deviation_db"), band.get("passed")
        if (
            not isinstance(lo, (int, float)) or not isinstance(hi, (int, float))
            or not isinstance(deviation_db, (int, float))
            or isinstance(deviation_db, bool) or not isinstance(passed, bool)
        ):
            continue
        parts.append(
            f"{lo:.0f}-{hi:.0f}Hz:{deviation_db:+.2f}dB:{'pass' if passed else 'fail'}"
        )
    return ";".join(parts)


def _gate_floor_source(response: Any) -> str | None:
    """WHY ``_gate_window_ms`` is what it is — travels beside it everywhere.

    ``gating.FLOOR_MEASURED`` = a reflection onset was found and the window
    stops at it; ``gating.FLOOR_SEARCH_BOUND`` = the search reached
    ``gating.SEARCH_T_MAX_MS`` without finding one and the window was CAPPED
    there. Both print as the same ``gate_window_ms`` number, and the whole
    2026-07-30 corpus was the second state while every consumer read it as
    the first (issue #1966). ``None`` is an ungateable capture, never a
    guess. See ``program_analysis._gate_floor_source_of``, which does the
    same job for the retained-capture sidecar.
    """
    if response is None:
        return None
    source = response.gating.get("floor_source") if response.gating else None
    return str(source) if isinstance(source, str) else None


def _gate_disclosure(response: Any) -> str | None:
    """``_gate_floor_source`` and its floors, rendered as one sentence.

    Rendered, never composed here: the copy has a single writer,
    ``jasper.audio_measurement.gate_disclosure.describe_gate``, so the
    per-position evidence file and the retained-capture sidecar cannot
    describe the same gate two different ways.

    Imported inside the function, matching how this module reaches its
    other cross-package helpers — it deliberately does not import
    :mod:`~jasper.audio_measurement.gating` at module scope either, and
    reads a gating block purely as data.
    """
    if response is None or not getattr(response, "gating", None):
        return None
    from jasper.audio_measurement import gate_disclosure

    return gate_disclosure.describe_gate(response.gating)


def _gate_record(response: Any) -> dict[str, Any] | None:
    """The gate reduced to the two facts a household SCREEN needs, or ``None``.

    ``{"disclosure": <the sentence>, "reflection_measured": <bool>}``. Both are
    :mod:`~jasper.audio_measurement.gate_disclosure`'s own derivations, taken
    here at compose time — one is :func:`_gate_disclosure`'s sentence, the
    other is
    :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.gated_anything`,
    the single owner of "may this record claim reflections were removed".
    Neither is re-derived downstream.

    **A reduction, not the block.** What travels to the wizard's durable state
    is these two derived facts rather than the gating fragment itself, so the
    state file does not take a dependency on
    :mod:`~jasper.audio_measurement.gating`'s schema — that schema is versioned
    and moves (it went 1 -> 2 in R9), and a screen re-deriving copy from it
    would be a second place the two epistemic states could be collapsed back
    into one. A response with no gating block yields ``None``: absent stays
    absent, and no screen invents a gate that was never applied.
    """
    disclosure = _gate_disclosure(response)
    if disclosure is None:
        return None
    from jasper.audio_measurement import gate_disclosure

    return {
        "disclosure": disclosure,
        "reflection_measured": gate_disclosure.build_gate_disclosure(
            response.gating
        ).gated_anything,
    }


def _capture_wav_sha256(result: Any) -> str | None:
    """SHA-256 of a capture's WAV bytes, or ``None`` when there are none.

    The content **verifier** for a per-position take (attribution plan §6).
    ``None`` for any caller or test double that carries no bytes — an absent
    digest is honest, a fabricated one would not be, and nothing downstream
    treats absence as a match.
    """

    wav = getattr(result, "wav", None)
    if not isinstance(wav, (bytes, bytearray)):
        return None
    return hashlib.sha256(bytes(wav)).hexdigest()


def _verify_evidence_from_tracking(
    tracking: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The verify_fail expert-disclosure numbers (#1605): the notch-excluded
    max the tolerance gates on, the RMS, and the tolerance itself. Returns
    None when the gated max is not a real number — nothing meaningful to show
    behind the disclosure.

    **The graded band is NOT here** — it moved to
    :func:`_verify_graded_band_from_tracking` (issue #1868). It used to ride
    this block, which is persisted only for a NON-pass outcome, so the one
    fact that bounds what "Verified." actually means was visible on exactly
    the screens where the verdict had already failed. One owner, one place:
    the band is a property of the comparison, not of its failure.
    """
    max_db = tracking.get("max_db_notch_excluded")
    if not isinstance(max_db, (int, float)):
        return None
    rms_db = tracking.get("rms_db")
    return {
        "max_db": float(max_db),
        "rms_db": float(rms_db) if isinstance(rms_db, (int, float)) else None,
        "tolerance_db": float(VERIFY_TOLERANCE_DB),
    }


def _verify_graded_band_from_tracking(
    tracking: Mapping[str, Any],
) -> list[float] | None:
    """The frequency span VERIFY's tracking comparison actually graded.

    ``[lo, hi]``, or ``None`` when this capture never reached a tracking
    comparison (an early locate/level/gate refusal) — absent means "nothing
    was graded", never "graded everywhere".

    **Why it is disclosed on a PASS too** (issue #1868, panel item O5): the
    band is not the nominal Fc±1 octave. ``overlap_band_hz`` clamps its lower
    edge UP to the tweeter's actual MEASURE sweep floor, and
    ``_analyze_verify`` clamps it up again to the capture's own gate-derived
    validity floor. On the 2026-07-30 corpus that landed at
    ``[2000, 4000] Hz`` while the crossover-region defect the forensics
    locate sits at 1919 Hz — 81 Hz below the floor, structurally ungradeable.
    A "Verified." badge over an unstated band reads as "verified everywhere";
    stating the band makes the claim exactly as wide as the measurement.
    """
    band = tracking.get("tracking_band_hz")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    lo, hi = band
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return None
    return [float(lo), float(hi)]


#: The three states a plan §7 claim can be in. ``not_evaluated`` is
#: first-class and never collapses into the other two — R18's entire point is
#: that a claim nobody could grade must not read as one that passed.
CLAIM_PASS = "pass"
CLAIM_FAIL = "fail"
CLAIM_NOT_EVALUATED = "not_evaluated"
#: Why the two per-branch claims are never graded today: a VERIFY program plays
#: ONE mono summed sweep (``build_verify_program``'s ``KIND_SUMMED_SWEEP``), so
#: the capture holds no woofer-alone or HF-alone response to compare with its
#: candidate branch. Widening the capture plan is out of R18's ratified scope;
#: naming the gap rather than silently claiming it is what R18 owes.
CLAIM_NO_PER_BRANCH_CAPTURE = "no_per_branch_verify_capture"
#: A crossover-region band exists but ``flat_spec.SPEC_BANDS`` sets no
#: tolerance across it — see :func:`verify_absolute_tolerance_db`.
ABSOLUTE_NO_SPEC_TOLERANCE = "no_spec_tolerance_for_region"
CLAIM_NAMES = ("woofer_branch", "hf_branch", "integration", "absolute")


def _verify_claims(
    tracking: Mapping[str, Any], absolute: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The plan §7 claim record for one VERIFY capture — ONE producer.

    Four entries for §7's three claims: its third is two questions in one
    sentence ("the measured sum tracks the candidate **and** does not merely
    reproduce a model-predicted crossover null"), so ``integration`` is the
    tracking half and ``absolute`` the other. Every number is LIFTED from the
    record its own owner already computed — nothing re-graded — so a screen and
    a gate cannot quote different figures. The kernel supplies the absolute
    band and scalars; the tolerance and both verdicts are this module's,
    mirroring where ``VERIFY_TOLERANCE_DB`` already lives.
    """
    tracking_max = tracking.get("max_db_notch_excluded")
    band = (absolute or {}).get("band_hz")
    absolute_max = (absolute or {}).get("max_db")
    tolerance_db = (
        verify_absolute_tolerance_db(band) if isinstance(band, (list, tuple)) else None
    )
    absolute_claim: dict[str, Any]
    if not isinstance(absolute_max, (int, float)):
        # The kernel's own reason survives, never re-labelled: "no trusted
        # crossover region" and "no candidate target" stay distinguishable.
        absolute_claim = {
            "status": CLAIM_NOT_EVALUATED,
            "reason": str((absolute or {}).get("not_evaluated") or CLAIM_NOT_EVALUATED),
        }
    elif tolerance_db is None or not isinstance(band, (list, tuple)):
        absolute_claim = {
            "status": CLAIM_NOT_EVALUATED, "reason": ABSOLUTE_NO_SPEC_TOLERANCE,
        }
    else:
        absolute_claim = {
            "status": CLAIM_PASS if absolute_max <= tolerance_db else CLAIM_FAIL,
            "tolerance_db": float(tolerance_db),
            "band_hz": [float(band[0]), float(band[1])],
            **{k: _rounded((absolute or {}).get(k), 4)
               for k in ("max_db", "rms_db", "worst_db", "worst_hz")},
        }
    branch = {"status": CLAIM_NOT_EVALUATED, "reason": CLAIM_NO_PER_BRANCH_CAPTURE}
    return {
        "woofer_branch": dict(branch),
        "hf_branch": dict(branch),
        "integration": {
            "status": (
                CLAIM_NOT_EVALUATED if not isinstance(tracking_max, (int, float))
                else CLAIM_PASS if tracking_max <= VERIFY_TOLERANCE_DB
                else CLAIM_FAIL
            ),
            "max_db": _rounded(tracking_max, 4),
            "tolerance_db": float(VERIFY_TOLERANCE_DB),
            "band_hz": _verify_graded_band_from_tracking(tracking),
        },
        "absolute": absolute_claim,
    }


def _claims_log_field(claims: Mapping[str, Any]) -> str:
    """One logfmt token for the whole §7 claim record: ``name:state`` per
    claim, comma-joined, a not-evaluated one carrying its reason. So a corpus
    sweep can count what was actually judged instead of inferring it from a
    bare ``accepted=true``. ``""`` for an early refusal that graded nothing.
    """
    return ",".join(
        f"{name}:{claims[name].get('status')}"
        f"{'(%s)' % claims[name]['reason'] if claims[name].get('reason') else ''}"
        for name in CLAIM_NAMES
        if isinstance(claims.get(name), Mapping)
    )


def _rounded(value: Any, digits: int) -> float | None:
    """``round(value, digits)`` for a real number, ``None`` for anything else.

    Keeps a diagnostic line's absent values as ``None`` rather than letting a
    missing field become ``0.0`` — the same unknown-is-not-a-value rule every
    other field on that line follows.
    """
    return round(float(value), digits) if isinstance(value, (int, float)) else None


def _verify_frame_from_tracking(
    tracking: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The FRAME VERIFY's comparison spanned, and the residual both ways.

    Rung P1. VERIFY differences an ON-AXIS two-branch model against an IN-ROOM
    gated measurement — two instruments, and on the 2026-07-29 corpus a single
    −0.79 dB/octave tilt between them was 84 % of the flow's reported
    prediction error. ``program_analysis._analyze_verify`` fits that frame and
    reports the residual with it removed beside the raw one; this lifts both
    for the durable record. **Nothing is recomputed here** — every value is one
    of that analysis's own, exactly like :func:`_verify_evidence_from_tracking`
    beside it.

    Rendered on EVERY outcome, like :func:`_verify_graded_band_from_tracking`
    and for the same class of reason: a PASS is exactly the case where an
    unstated tilt lets a reader take instrument agreement for model agreement.

    ``None`` when no tracking comparison ran, or when it ran and the frame
    could not be fitted — absent means "no frame was measured", never "the
    frames matched". The tilt-removed keys are omitted individually rather than
    defaulted to their raw twins: a beside-number equal to its raw twin would
    read as "removing the frame changed nothing", which is a measurement, not
    an absence.

    ``max_db_tilt_removed`` is the twin of the NOTCH-EXCLUDED max, matching
    what :func:`_verify_evidence_from_tracking` already calls ``max_db`` on
    this same surface — on a household-facing record "the level error" has one
    meaning, the one the tolerance gates on, and a second spelling for it here
    would invite a reader to compare two numbers taken over different bin sets.

    **``pivot_hz``/``n_bins``/``band_hz`` travel too.** They are not decoration:
    a two-parameter fit over few bins or a narrow span is ill-conditioned, and
    :mod:`jasper.audio_measurement.frame_fit` deliberately reports that span
    rather than inventing a confidence policy — so a record that dropped them
    would state a tilt with no way to judge it. They also disclose WHICH bins
    the frame was estimated from: the span is the notch-excluded, validity-floor
    clamped set, narrower than the graded band whenever the prediction has a
    deep notch in it.
    """
    frame = tracking.get("frame")
    if not isinstance(frame, Mapping):
        return None
    offset_db = frame.get("offset_db")
    tilt = frame.get("tilt_db_per_octave")
    if not isinstance(offset_db, (int, float)) or not isinstance(tilt, (int, float)):
        return None
    out: dict[str, Any] = {
        "offset_db": float(offset_db),
        "tilt_db_per_octave": float(tilt),
    }
    pivot_hz = frame.get("pivot_hz")
    if isinstance(pivot_hz, (int, float)):
        out["pivot_hz"] = float(pivot_hz)
    n_bins = frame.get("n_bins")
    if isinstance(n_bins, int):
        out["n_bins"] = n_bins
    band_hz = frame.get("band_hz")
    if (
        isinstance(band_hz, (list, tuple))
        and len(band_hz) == 2
        and all(isinstance(edge, (int, float)) for edge in band_hz)
    ):
        out["band_hz"] = [float(band_hz[0]), float(band_hz[1])]
    tilt_removed = frame.get("tilt_removed")
    if isinstance(tilt_removed, Mapping):
        for key, source in (
            ("rms_db_tilt_removed", "rms_db"),
            ("max_db_tilt_removed", "max_db"),
        ):
            value = tilt_removed.get(source)
            if isinstance(value, (int, float)):
                out[key] = float(value)
    # The RAW pair the tilt-removed numbers sit beside (should-fix 1). Carried
    # here because the durable ``verify.evidence`` block — the other place these
    # live — is persisted only on a NON-pass outcome, so a passing screen would
    # otherwise render the frame-removed half of a comparison with nothing to
    # compare it to: the flattering number alone.
    raw = frame.get("raw")
    if isinstance(raw, Mapping):
        for key, source in (("rms_db_raw", "rms_db"), ("max_db_raw", "max_db")):
            value = raw.get(source)
            if isinstance(value, (int, float)):
                out[key] = float(value)
    return out


# (``_flatness_evidence_from_tracking`` lived here until the
# flat-linearization plan's PR-5. It repackaged one VERIFY capture's own
# grid-and-band-mean flatness number for the RESULT/verify_fail screens; that
# number is retired along with ``program_analysis._flatness_tracking``, and the
# flatness the household sees now comes from the cloud group's spec evaluation
# — ``assemble_cloud_group_result``'s ``flatness`` key, one construction, one
# owner. See that function and ``flat_spec.spec_flatness_gauge``.)


# --------------------------------------------------------------------------- #
# diagnostic-logging helpers (Part 1 — additive; feed no verdict)
# --------------------------------------------------------------------------- #
#
# Every CHECK/MEASURE/VERIFY capture logs its full numeric diagnostics on
# PASS *and* FAIL via ``log_event`` — previously only ``program_analysis.
# glitch`` carried a partial view (epsilon/residual/repeat-level, WARN-only,
# glitch captures only) and the ``crossover_v2_result`` line carried just the
# reason code, so a failed hardware run left no numbers to look at. These
# helpers read what ``ProgramAnalysis`` already computed; none of them derive
# a NEW number or influence any verdict.






def _pilot_by_role(analysis: ProgramAnalysis, role: str) -> Any | None:
    for pilot in analysis.pilots:
        if pilot.role == role:
            return pilot
    return None


def _pilot_transfer_by_role(analysis: ProgramAnalysis) -> dict[str, float]:
    """Per-role pilot transfer: captured hi level minus the programmed hi gain.

    Measurement-honesty gate G3's raw material (2026-07-22): VERIFY replays
    the identical program through the identical applied graph on every
    attempt, so this transfer should not move between attempts either — see
    :data:`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB`. Excludes any pilot whose
    ``programmed_hi_gain_db`` is unset (a legacy program built without
    ``leading_pilot_gains_db`` never threads it, per
    ``program_analysis.PilotObservation``'s docstring) — nothing to compare
    that pilot against.

    ``level_hi_dbfs`` safety note: ``PilotObservation``'s own docstring warns
    it "must never feed an ABSOLUTE-level consumer" (ambient subtraction
    shifts it by however much ambient power was removed). This use is safe
    for TWO independent reasons.

    (1) It is a RELATIVE cross-ATTEMPT comparison (this attempt's transfer
    minus the FIRST attempt's), never a true absolute-level read.

    (2) The ambient-difference confound the older version of this note
    deferred is now REAL but bounded far below the gate. Until issue #1810
    (2026-07-28) a VERIFY pilot pair had no ambient window at all, so
    subtraction was a literal no-op here; it now has a ~1 s pre-pilot window
    and subtraction is live. The bound: ``_verify_verdict`` refuses any
    attempt whose ``pilot_snr_ok`` is False BEFORE reaching the G3 block, so
    every attempt that gets here cleared ``PILOT_MIN_SNR_DB`` (≈12.4 dB) on
    the QUIET pilot — and the HI pilot sits a further
    ``PILOT_LEVEL_DELTA_DB`` (10 dB) above it, i.e. ≥22.4 dB in-band SNR. At
    that SNR the subtraction moves ``level_hi_dbfs`` by at most
    ``10·log10(1 − 10**−2.24)`` ≈ **0.025 dB**, so two admissible attempts can
    differ by at most ~0.05 dB from this term alone — an order of magnitude
    under :data:`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB` (0.35 dB). Lowering
    that ceiling toward ~0.1 dB, or raising ``PILOT_AMBIENT_WINDOW_S``'s trust
    without the SNR gate in front of it, is what would put this back in play.
    """
    return {
        pilot.role: pilot.level_hi_dbfs - pilot.programmed_hi_gain_db
        for pilot in analysis.pilots
        if pilot.programmed_hi_gain_db is not None
    }


def _driver_snr_fields(resp: Any | None) -> tuple[float | None, str | None]:
    """``(estimated_snr_db, verdict)`` from a driver's worst-relevant SNR band."""
    if resp is None or resp.snr is None:
        return None, None
    worst = resp.snr.get("worst_relevant") or {}
    return worst.get("estimated_snr_db"), worst.get("verdict")




# The finite stand-in logged for `_pilot_in_band_snr_db`'s ``-inf`` — "this
# pilot's measured power did not even exceed the ambient", i.e. the estimate
# is unusable rather than merely low. JSON has no infinity, and DROPPING the
# value is worse than substituting one: a two-role capture with one buried
# pilot and one clean one would log the CLEAN pilot's SNR beside
# ``pilot_snr_ok=False``, a diag row that contradicts itself and reproduces
# the very "verdict beside absent evidence" shape #1810 was filed about.
# -120 dB mirrors `program_analysis.DBFS_FLOOR`'s "off the scale" magnitude
# and keeps the field monotone-comparable, so `min(...)` still selects the
# worst pilot.
PILOT_SNR_UNUSABLE_DB = -120.0


def _worst_pilot_snr_db(analysis: ProgramAnalysis) -> float | None:
    """The lowest quiet-pilot in-band SNR across this capture's pilots.

    The number the ``pilot_snr_ok`` aggregate (an ``all(...)``) was
    thresholded from, so the diag line says HOW low, not just that it was.
    The two infinities `_pilot_in_band_snr_db` can return are treated
    differently on purpose:

    * ``+inf`` — "no ambient evidence to validate against". Not a
      measurement, so it is EXCLUDED. A capture where every pilot reads
      ``+inf`` (a legacy program with no room-listening window) logs
      ``None``; one where some pilots read ``+inf`` and others a real number
      logs the worst real number.
    * ``-inf`` — "the pilot never exceeded the ambient". That IS a
      measurement, and the most damning one, so it is substituted with
      :data:`PILOT_SNR_UNUSABLE_DB` rather than dropped.
    """
    values = [
        PILOT_SNR_UNUSABLE_DB if p.snr_db == -math.inf else p.snr_db
        for p in analysis.pilots
        if p.snr_db != math.inf
    ]
    return round(min(values), 2) if values else None


def _pilot_diag_fields(pilot: Any | None) -> dict[str, float | None]:
    """One pilot's linearity/SNR/channel-map diagnostics, ``None``-safe."""
    if pilot is None:
        return {
            "snr_db": None,
            "captured_delta_db": None,
            "programmed_delta_db": None,
            "channel_map_target_rise_db": None,
            "channel_map_cross_rise_db": None,
        }
    snr_db = pilot.snr_db
    target_rise = pilot.channel_map_target_rise_db
    cross_rise = pilot.channel_map_cross_rise_db
    return {
        "snr_db": round(snr_db, 2) if math.isfinite(snr_db) else None,
        "captured_delta_db": round(float(pilot.captured_delta_db), 3),
        "programmed_delta_db": round(float(pilot.programmed_delta_db), 3),
        "channel_map_target_rise_db": (
            round(target_rise, 3) if target_rise is not None else None
        ),
        "channel_map_cross_rise_db": (
            round(cross_rise, 3) if cross_rise is not None else None
        ),
    }


# --------------------------------------------------------------------------- #
# Layer-1a driver-linearization wiring (#1668 PR-C)
# --------------------------------------------------------------------------- #
#
# The fit engine (jasper.active_speaker.linearization_fit) and the envelope
# core (jasper.active_speaker.linearization_envelope) are pure, policy-free
# computation. Turning their outputs into a PRODUCT decision — σ-composition
# policy, the anchored trim, the re-solve and its sanity backstop — is
# `crossover_v2.intervention`'s job since #2291 Phase 2, and
# `LINEARIZATION_MIN_PAIRED_OCCURRENCES` / `LINEARIZATION_TRIM_SANITY_MARGIN_DB`
# / `_compose_sigma_db` are re-exported from there at the top of this module
# rather than defined twice. The σ tolerable-value table went with them and is
# NOT re-exported — its last reader here was `_compose_sigma_db` itself, so a
# re-export would have been an import nothing resolved; it is
# `intervention.SIGMA_TOLERABLE_DB`. This conductor still owns eligibility
# (mic tier + paired repeat count) and the accountability gate. See
# docs/active-speaker-tuning-layers-design.md "Layer 1a concretely".

# How far the two measured level estimates may disagree before the session is
# refused (linearization-integrity PR-L5). The estimates are the trim solve's
# power-band average on each side of Fc (`program_analysis.solve_branch_trims`)
# and the fit's median over each driver's own RADIATING band since #1929
# (`linearization_fit.driver_core_level_db`), reconciled by
# `solve_shared_level_frame` into one frame whose per-role offset IS their
# disagreement.
#
# DELIBERATELY the same number as `program_analysis.
# REALIZED_LEVEL_MATCH_TOLERANCE_DB`, and imported from it rather than written
# twice: both answer one question — do two estimates of where these drivers sit
# agree — and PR-L4 already derived 3.0 dB for it from this exact evidence (the
# 2026-07-27 profile that shipped 9-11 dB dark sat at 8.76 dB). A second number
# for the same question is how two instruments start disagreeing about what
# "agree" means.
#
# The FLOOR argument moved with #1929 and is no longer 1.08-1.30 dB. That range
# was PR-L3's measurement with the median over each driver's whole DECLARED
# capture span, which counted the driver's own crossover stopband as driver
# level. On archived run 5 — the capture this repo replays, in
# `tests/test_audio_measurement_program_analysis.py` — banding the median takes
# the same session's disagreement from 1.076 dB to 0.510 dB. The other four
# archived captures have not been re-measured under the band, so the honest
# statement is "the one capture we replay halved", not a new range.
#
# What the tolerance still does NOT buy is a small residual. A pair that is
# identical by construction still reads 0.910 dB, and the number climbs with
# ordinary driver shape at roughly 1.33 dB per dB/octave of woofer passband
# tilt (measured on the conductor fixture: 0.910 flat, 2.251 at -1 dB/oct,
# 3.574 at -2, 4.883 at -3), so a -2 dB/oct woofer — an unremarkable driver —
# refuses while the realized-level instrument reads 1.41 dB and passes.
#
# That gradient is the honest read of this constant: about 1.6 dB/oct of real
# passband tilt is the whole budget, because 0.910 dB is spent before the
# speaker contributes anything. #1929 removed one structural bias; it did not
# make the two estimators agree, and the next field refusal comes from what is
# left. Closing THAT is the comparator family's work (plan section 4 M7 /
# WO-4), and the frame-gate SEMANTICS ruling on #1866 is the next step of it.
#
# EXTERNAL FIELD EVIDENCE, not reproducible from this repo: an offline re-fit
# of the 2026-07-30 field bundle puts that session at 3.2307 dB under this
# banded estimator — still refused. Provenance and fidelity are recorded on
# #1870; the bundle is laptop-side and gitignored, so no test replays it and
# nothing here should be read as if one did. The archived-corpus numbers above
# ARE in-repo and are a different session's bytes — both true, neither derived
# from the other.
LEVEL_FRAME_AGREEMENT_TOLERANCE_DB = REALIZED_LEVEL_MATCH_TOLERANCE_DB

# How much the correction must improve ITS OWN two-branch model before a
# spec-failing prediction is allowed onto the speaker (linearization-integrity
# PR-L4 item 2). Both numbers are the pooled spec residual
# (`flat_spec.spec_convergence_residual`) of the RAW pre-fit and the LINEARIZED
# predicted sum, graded through the identical evaluator, in dB.
#
# The gate only bites when the prediction ALREADY fails the spec — a prediction
# that meets it needs no improvement argument, and gating an in-spec result on
# "how much did it improve" would refuse the flattest speakers hardest. So the
# question this threshold answers is narrow: *we can already see this will not
# reach spec — is it at least clearly moving the right way?*
#
# 0.5 dB, and the derivation changed with the frame (PR-L4 review B1). While
# this compared the model against the measured in-room cloud, the threshold had
# to absorb the whole cross-frame gap and was set at `SPEC_BANDS[0]`'s 1.5 dB
# for that reason — which, as the review demonstrated, made the verdict a
# function of the ROOM rather than the correction. Now that both terms are the
# same instrument (same branches, same grid, same evaluator, differing ONLY by
# the emitted filters) the comparison carries no measurement noise at all, so
# the threshold is a product-policy floor instead of a noise margin.
#
# 0.5 dB is that floor because it is this model's own measured tracking error:
# `crossover_v2.intervention.plan_linearization` records the complex-correction
# model tracking the real
# VERIFY summation to ~0.5 dB on JTS3 (the zero-phase model it replaced
# mistracked by ~2.0 dB). An improvement smaller than the gap between what we
# model and what the hardware realizes is not an improvement we can honestly
# claim, so it does not earn an apply.
PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5


# --------------------------------------------------------------------------- #
# seams + snapshot
# --------------------------------------------------------------------------- #

# Injected seams. The web host binds the production implementations
# (jasper.web.correction_crossover_v2); tests inject fakes.
PlayProgram = Callable[[str, ExcitationProgram], None]


class AnalyzeCapture(Protocol):
    """analyze(program, capture_result, priors, geometry, *, phase) → ProgramAnalysis.

    The second argument is the relay CaptureResult (wav + phone-reported
    device + setup — the production binding resolves the mic calibration
    from it; fakes may pass raw bytes). ``geometry`` is the conductor's
    declared MeasurementGeometry so the parallax correction actually reaches
    analyze_program_capture — a seam that dropped it would silently analyze
    with zero spacing.

    ``phase`` is REQUIRED and keyword-only: the CONDUCTOR's own flow phase
    (issue #1855) — NOT ``program.phase``. The two are different
    vocabularies: every cloud position plays ``self._verify_program`` (see
    ``program_for_phase``), so ``program.phase`` is always "verify" for
    PHASE_VERIFY, PHASE_CLOUD_MEASURE, and PHASE_CLOUD_VERIFY alike. A seam
    that derives a retained capture's label from ``program.phase`` mislabels
    every cloud position as "verify" — the exact bug #1855 fixed. No
    default: a binder or a refactor that drops ``phase=phase`` must fail at
    the call, not fall back silently to ``program.phase`` and reintroduce
    the mislabel. A plain ``Callable[[...], R]`` can't express a required
    keyword-only parameter, hence this Protocol — prior art in-package:
    ``jasper.active_speaker.playback.TonePlaybackBackend`` and
    ``jasper.active_speaker.commissioning_capture_producer.RegionCaptureOperation``.
    """

    def __call__(
        self,
        program: ExcitationProgram,
        result: Any,
        priors: MeasurementPriors,
        geometry: MeasurementGeometry,
        *,
        phase: str,
    ) -> ProgramAnalysis: ...


PublishCheck = Callable[[GainPlan, Mapping[str, Any]], None]
PublishCandidate = Callable[[Any], None]
ApplyGate = Callable[[], bool]
# Reads whether an apply hit a TERMINAL failure. Retained with the deferred
# VERIFY hold it feeds, and unreached by any shipped session since the
# two-stage split (D10) — its writer was the auto-apply worker thread —
# returns the reason code (e.g. REASON_APPLY_FAILED) or "" while still
# pending/never attempted. Distinct from ``apply_complete`` (success only) so
# ``authorize_begin`` can REFUSE the deferred VERIFY with an honest reason
# instead of holding forever toward a dishonest relay_timeout.
ApplyFailureGate = Callable[[], str]
class RecordModelError(Protocol):
    """Banks one model-predicted/realized pair outside the conductor."""

    def __call__(
        self,
        *,
        attempt_id: str,
        metric: str,
        predicted_db: float,
        realized_db: float,
        speaker_id: str,
        context: Mapping[str, Any],
    ) -> bool: ...


@dataclass(frozen=True)
class V2FlowSeams:
    """The conductor's injected I/O boundary (all side effects)."""

    play: PlayProgram
    analyze: AnalyzeCapture
    publish_check: PublishCheck
    publish_candidate: PublishCandidate
    apply_complete: ApplyGate
    apply_failed: ApplyFailureGate
    # Position-group evidence retention (PR-3b), called once per ACCEPTED cloud
    # capture with ``(position_id, capture_result, metadata)``. Optional so
    # every pre-cloud construction site (and every conductor unit test) stays
    # valid; ``None`` means the group runs with no durable per-position
    # artifact, which is the correct behaviour for a conductor with no evidence
    # store rather than a reason to fail a capture.
    retain_position: Callable[[str, Any, Mapping[str, Any]], None] | None = None
    # PR-4: the cloud honesty-pipeline bundle publisher, called once per
    # CLOSED group with ``(phase, cloud_group_result_dict)``. Optional for the
    # same reason ``retain_position`` is: every pre-PR-4 construction site
    # (and every conductor unit test) stays valid, and ``None`` means the
    # group's result is computed and readable via
    # :meth:`CrossoverV2Conductor.group_cloud_result` but not published as a
    # bundle artifact.
    publish_cloud: Callable[[str, Mapping[str, Any]], None] | None = None
    # #1866 frame-gate ruling: the banked level-frame disagreement, called at
    # most once per session with the flow's evidence record, from
    # :meth:`CrossoverV2Conductor._commit_measure_candidate` — AFTER
    # ``publish_candidate``, so the artifact the finding cites already exists.
    # Optional exactly like the two seams above: a conductor with no evidence
    # store still banks the number in its journal and still PROCEEDS, it just
    # writes no durable finding. That degraded mode is the ordinary state of
    # every conductor unit test and is not a reason to refuse a session.
    publish_findings: Callable[[Mapping[str, Any]], None] | None = None
    # PR-L5: undo the applied correction, called with the delta-probe reason
    # code when the post-apply map does not match. Returns True when the
    # previous profile was restored. Optional like the two seams above — a
    # conductor with no rollback binding still CLASSIFIES and refuses (the
    # household sees the verdict and the Undo button the failure screen
    # already offers), it just cannot press the button itself. That degraded
    # mode is disclosed on the verdict's own event, never silent.
    rollback: Callable[[str], bool] | None = None
    # #1811: the whole-band level move the APPLY made and did not command as
    # part of the correction's shape — the pre-split headroom the applied graph
    # charges for its own boost. Read at probe time (like ``apply_complete`` /
    # ``apply_failed``, off durable state) rather than passed at construction,
    # because the apply happens on a background thread AFTER this conductor is
    # built. Optional: ``None`` means "nothing known", which
    # ``classify_delta_probe`` treats honestly — the whole shift stays visible
    # as ``residual_offset_db`` instead of being silently claimed as accounted.
    applied_offset_db: Callable[[], float] | None = None
    # S3 attempts loop: called once for each newly accepted applied-candidate
    # VERIFY. Optional so a conductor without a durable host still grades its
    # in-memory attempt and every pre-wiring construction site remains valid.
    record_model_error: RecordModelError | None = None
    # #2291: which DSP graph the entry baseline was measured through, read at
    # accept time (like ``applied_offset_db``, not passed at construction —
    # the answer is a property of the moment the capture happened). Bound on
    # BOTH stages: "what is live right now" is not a stage asymmetry, and the
    # record's whole job is to let a LATER round bind the currently-active
    # profile as its own entry graph.
    #
    # Optional, and a raising call is caught by the one caller: a fingerprint
    # is provenance on a record, never a gate. A capture that measured the
    # speaker honestly must not be rejected because the host could not name the
    # graph it measured — the record then carries "" and says so.
    entry_graph_fingerprint: Callable[[], str] | None = None
    # #2291: is there a valid anchor to restore TO? The ANCHOR half of the
    # adoption table's ``rollback_available``; the SEAM half is ``rollback``
    # above being bound at all, and
    # :func:`~jasper.active_speaker.crossover_v2.coordinator.rollback_available`
    # ANDs them. Optional, and its absence reads as "cannot confirm an anchor"
    # rather than as "there is one" — see that function for why the pessimistic
    # direction is the safe one here.
    rollback_available: Callable[[], bool] | None = None
    # #2291/#2318: does the APPLIED graph put energy in? Read from the host at
    # grading time, because the grading conductor cannot answer it from its own
    # state — stage 2 never holds a candidate (see
    # :func:`~jasper.active_speaker.crossover_v2.coordinator.applied_boosts`
    # for the bug this closes). Optional, and its absence answers "boosted": an
    # intervention this process cannot inspect comes off rather than staying on
    # evidence nobody has.
    applied_boosts: Callable[[], bool] | None = None
    # #2291: publish the round receipt and return its artifact fingerprint.
    # The round coordinator builds the record (one assembler, in the pure
    # layer) and this seam owns WHERE it lands — the evidence bundle, write-once and
    # tamper-checked. Optional, and every failure is the host's to raise: the
    # caller treats a raise or a ``None`` as "no receipt was written", logs it,
    # and keeps the verdict. A receipt is the round's record, never its gate.
    publish_round_receipt: Callable[[Mapping[str, Any]], str] | None = None


@dataclass(frozen=True)
class V2ConductorSnapshot:
    """Durable phase state, bound to the relay session (§5.6).

    Persisted under the session's commissioning run; :meth:`CrossoverV2Conductor.hydrate`
    keeps the accepted phases only when the current session matches — a new
    session invalidates CHECK/MEASURE evidence (mic position is unverifiable
    across sessions).
    """

    session_id: str
    accepted_phases: tuple[str, ...] = ()
    applied: bool = False
    gain_plan_db: Mapping[str, float] | None = None
    candidate_fingerprint: str | None = None
    # The ordered phases THIS session actually runs — the subset of
    # ``CAPTURE_PHASES`` its ``index_phase_map`` addresses. Persisted so a
    # host reading only the durable state can tell "verify is the last phase
    # of a re-arm session" from "verify is followed by a post-apply cloud",
    # which the module-global tuple cannot express. Empty on state written
    # before PR-3b; readers fall back to ``CAPTURE_PHASES`` then.
    session_phases: tuple[str, ...] = ()
    # WHICH INSTRUMENT produced this session (:data:`TIER_FULL` /
    # :data:`TIER_EXPRESS`). Empty string means UNKNOWN — state written before
    # tiers existed, or a conductor constructed without one — and readers must
    # render it as unknown rather than assuming full, the same
    # unknown-vs-default discipline ``echo_band_provenance`` carries (issue
    # #1763): the two tiers make materially different claims (§1.3), so
    # guessing one would attach a post-apply cross-position claim to a result
    # that never measured across positions.
    tier: str = ""
    # WHERE the pre-apply cloud's close has got to, for the surfaces that have
    # to say something true while it is in flight (two-stage work order D1).
    # One of :data:`CLOUD_CLOSE_NONE` / :data:`CLOUD_CLOSE_AWAITING_CONFIRM` /
    # :data:`CLOUD_CLOSE_RUNNING`. Persisted because the wizard renders from
    # durable state alone: without it, "every stage-1 phase is accepted and
    # there is no candidate" reads identically at three very different moments
    # — the household holding a phone at the confirm screen, the fit running,
    # and a session that ended having produced nothing.
    cloud_close: str = ""
    # S3 attempt history is journey-scoped, not relay-session-scoped. A second
    # apply→VERIFY necessarily runs under a fresh relay session, so these
    # records survive :meth:`hydrate`'s session rebind while CHECK/MEASURE
    # evidence above correctly does not.
    attempt_history: tuple[AttemptRecord, ...] = ()
    last_attempt_decision: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "accepted_phases": list(self.accepted_phases),
            "applied": self.applied,
            "gain_plan_db": dict(self.gain_plan_db) if self.gain_plan_db else None,
            "candidate_fingerprint": self.candidate_fingerprint,
            "session_phases": list(self.session_phases),
            "tier": self.tier,
            "cloud_close": self.cloud_close,
            "attempt_history": [item.to_dict() for item in self.attempt_history],
            "last_attempt_decision": (
                dict(self.last_attempt_decision)
                if self.last_attempt_decision is not None else None
            ),
        }


def attempt_history_from_state(raw: Any) -> tuple[AttemptRecord, ...]:
    """Restore the conductor-owned attempt history from durable journey state.

    Invalid rows are dropped as unavailable history, never partially trusted.
    The floor is intentionally absent from this shape: it has one owner in
    :mod:`jasper.active_speaker.model_error_store` and is read afresh by the
    host when it constructs the conductor.
    """

    loop = raw.get("attempts_loop") if isinstance(raw, Mapping) else None
    rows = loop.get("history") if isinstance(loop, Mapping) else None
    if not isinstance(rows, list):
        return ()
    restored: list[AttemptRecord] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        integrity = row.get("integrity")
        if not isinstance(integrity, Mapping):
            continue
        try:
            record = AttemptRecord(
                attempt_id=str(row.get("attempt_id") or ""),
                metric=str(row.get("metric") or ""),
                provenance=str(row.get("provenance") or ""),
                integrity=AttemptIntegrity(
                    comparable=integrity.get("comparable") is True,
                    reasons=tuple(
                        str(reason) for reason in integrity.get("reasons", ())
                        if isinstance(reason, str) and reason
                    ),
                ),
                repeats_used=(
                    int(row["repeats_used"])
                    if isinstance(row.get("repeats_used"), int)
                    and not isinstance(row.get("repeats_used"), bool)
                    else 1
                ),
                grade_db=_attempt_optional_float(row.get("grade_db")),
                deviation_from_predecessor_db=_attempt_optional_float(
                    row.get("deviation_from_predecessor_db")
                ),
                n_graded_bins=(
                    _attempt_optional_positive_int(row.get("n_graded_bins"))
                ),
                predicted_remaining_improvement_db=_attempt_optional_float(
                    row.get("predicted_remaining_improvement_db")
                ),
                in_spec=(
                    row.get("in_spec")
                    if isinstance(row.get("in_spec"), bool) else None
                ),
                curve_refs=tuple(
                    str(ref) for ref in row.get("curve_refs", ())
                    if isinstance(ref, str) and ref
                ),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        restored.append(record)
    # The kernel's hard cap is the only live attempt budget. Older rows carry
    # no additional decision value and retaining them would grow Pi state for
    # no payoff.
    return tuple(restored[-AttemptBudget().hard_cap_attempts:])


def _attempt_optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _attempt_optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def attempt_record_from_verify(
    analysis: ProgramAnalysis, *, attempt_id: str,
) -> AttemptRecord:
    """Map one VERIFY analysis into the pure kernel's realized record (#2033).

    VERIFY necessarily leaves repeat-only checks ``not_evaluated`` because it
    contains one summed sweep. Their names still ride as reasons, but they do
    not make an otherwise clean capture incomparable. Any evaluated failure
    does, and carries both the failed and not-evaluated check names so the
    kernel's STOP_EVIDENCE record never loses what the analyzer knew.
    """

    integrity = analysis.capture_integrity
    if integrity is None:
        attempt_integrity = AttemptIntegrity(
            comparable=False, reasons=(ATTEMPT_INTEGRITY_UNAVAILABLE,),
        )
    else:
        reasons = tuple(dict.fromkeys((*integrity.failed, *integrity.not_evaluated)))
        attempt_integrity = AttemptIntegrity(
            comparable=not integrity.failed,
            reasons=reasons,
        )
    tracking = analysis.verify_tracking or {}
    frame = tracking.get("frame")
    frame = frame if isinstance(frame, Mapping) else {}
    return AttemptRecord(
        attempt_id=str(attempt_id),
        metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        provenance=PROVENANCE_REALIZED,
        integrity=attempt_integrity,
        grade_db=_attempt_optional_float(
            tracking.get(ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED)
        ),
        # ``frame.n_bins`` is produced from the exact validity-clamped,
        # notch-excluded mask VERIFY graded. Carrying it activates the
        # kernel's denominator-shrink refusal instead of letting a narrower
        # frequency set look like an acoustic improvement.
        n_graded_bins=_attempt_optional_positive_int(frame.get("n_bins")),
    )


@dataclass
class SlotAttempts:
    """One prompted position's attempt ledger (owner ruling #2086).

    The single meter for a slot. ``admitted`` counts every attempt the
    conductor let start — the first is the PLANNED capture and is free; each
    one after it spends an extra against
    :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION`, attributed to whoever asked.

    An ACCEPTED capture never adds to ``by_household``/``by_speaker`` beyond
    the extra its own admission already spent, and the planned capture adds
    nothing at all — so a position measured cleanly on the first take still has
    its full three extras available if the household chooses to redo it. That
    is the "accepted captures never consume retry budget" half of the ruling:
    before it, acceptance left the old cumulative counter standing and one
    voluntary retake of a healthy position could start at zero headroom.

    Mutable on purpose (the frozen ``PhaseVerdict`` next door is a value; this
    is per-session state the conductor advances).
    """

    admitted: int = 0
    by_household: int = 0
    by_speaker: int = 0

    @property
    def extras_used(self) -> int:
        return self.by_household + self.by_speaker

    @property
    def extras_left(self) -> int:
        return max(0, MAX_EXTRA_ATTEMPTS_PER_POSITION - self.extras_used)

    def spend(self, initiator: str) -> None:
        """Charge one extra attempt to ``initiator``.

        Callers gate on :attr:`extras_left` first; an unchecked overspend would
        be a bug, so it raises rather than silently capping — a meter that lies
        about its own total is the defect this class replaces.
        """
        if self.extras_left <= 0:
            raise CrossoverV2FlowError(
                "slot has no extra attempts left "
                f"({self.extras_used}/{MAX_EXTRA_ATTEMPTS_PER_POSITION})"
            )
        if initiator == ATTEMPT_INITIATOR_SPEAKER:
            self.by_speaker += 1
        else:
            self.by_household += 1

    def to_payload(self) -> dict[str, Any]:
        """The honest count, as the phone renders it (ruling item 2).

        Numbers only — the page composes the eyebrow ("Measurement 6 of 6 —
        extra try 2 of 3") because the §2.1 screen grammar makes the counter
        the page's slot, and a second sentence written here would be the same
        fact stated twice. ``by_speaker`` is what makes the count truthful
        about who spent what.
        """
        return {
            "used": self.extras_used,
            "allowed": MAX_EXTRA_ATTEMPTS_PER_POSITION,
            "left": self.extras_left,
            "by_speaker": self.by_speaker,
            "by_household": self.by_household,
        }


@dataclass(frozen=True)
class PhaseVerdict:
    """A consume verdict: the relay dict + the internal reason (if any)."""

    accepted: bool
    code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Whether THIS capture's leading pilot pair cleared the room's own in-band
    # floor — ``analysis.pilot_snr_ok``, carried verbatim including its ``None``
    # (no pilot evidence). The one fact ``locate_failed``'s copy branches on
    # (#2085): it is the direct, same-capture refutation of "couldn't hear the
    # speaker", so it has to reach the sentence. Carried on the verdict rather
    # than dug out of ``payload`` because it is decided at the gate, where the
    # analysis is in hand, and a typed field cannot be misspelled into silence.
    pilot_heard: bool | None = None
    # VERIFY's gate discriminator for ``verify_inconclusive`` (#1974/#2095),
    # paired with the verdict for the same reason ``pilot_heard`` is: terminal
    # exhaustion must repeat this capture's diagnosis, not the registry's
    # evidence-unknown fallback.
    reflection_measured: bool | None = None

    def to_relay_dict(self) -> dict[str, Any]:
        """The mapping ``consume_capture`` returns to ``run_capture_plan``.

        Always carries ``accepted``; a rejection adds the reason code + template
        + copy so the phone renders the right §5.10 screen. Every non-``accepted``
        field is relayed verbatim in the ``capture_result`` host event.

        ``reason`` comes from :func:`reason_message`, not from the registry
        entry directly, so a code whose honest sentence depends on what was
        measured renders that sentence HERE — on the surface the household is
        actually looking at when a capture is refused — and not only in the
        envelope served later. ``pilot_heard`` rides out beside it so the
        journal and the phone's own record can tell the two accounts apart
        without re-deriving the discriminator.
        """
        out: dict[str, Any] = {"accepted": self.accepted}
        if self.code is not None:
            spec = REASON_REGISTRY[self.code]
            out.update(
                code=self.code,
                template=spec.template,
                reason=reason_message(
                    self.code,
                    spec,
                    pilot_heard=self.pilot_heard,
                    reflection_measured=self.reflection_measured,
                ),
                banner=spec.banner,
                auto_retry=self.code in TRANSIENT_AUTO_RETRY_CODES,
                pilot_heard=self.pilot_heard,
            )
            if self.code == REASON_VERIFY_INCONCLUSIVE:
                out["reflection_measured"] = self.reflection_measured
        out.update(self.payload)
        return out


@dataclass(frozen=True)
class _CloudPosition:
    """One accepted position inside a group, retained for the group-end combine.

    ``response`` is the capture's ``ProgramAnalysis.summed_response`` — a
    ``program_analysis.DriverResponse`` carrying the calibrated, reflection-gated
    magnitude on a linear (rfftfreq) grid plus the matching complex TF. Holding
    the response rather than a pre-built
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture` is
    deliberate: PR-4 needs the same object for the per-position work the null
    gate and the spec curve do, and re-deriving it from a lossy intermediate
    would be the drift this seam exists to prevent.
    """

    position_id: str
    index: int
    attempt: int
    prompt: str
    wide: bool
    captured_at: float
    response: Any
    sample_rate_hz: int
    # The named question this position answers (:data:`POSITION_ROLES`), copied
    # off the prompt the operator was actually given. Persisted with the
    # position so the attribution stage reads a labelled sample rather than an
    # anonymous member of an average (attribution-stage plan §5 promotion queue
    # item 1). Defaulted so every construction site that predates roles — the
    # corpus and unit fixtures — stays valid unchanged.
    role: str = POSITION_ROLE_ONAX
    # PR-4: the contract-derived analysis bands this position's GROUP should be
    # combined/searched with — spatial_combine.combine_positions's own
    # ``echo_band_hz`` / ``signal_band_hz`` kwargs, echoed here rather than
    # threaded as a separate call-site argument. Carrying them on the position
    # (every position in one group shares the same conductor-derived values —
    # see ``CrossoverV2Conductor.__init__``) is what lets
    # :func:`combine_cloud_positions` derive the right bands from
    # ``positions`` alone, with no caller (``_close_cloud_group``'s single
    # combine, ``cloud_geometry_verdict``'s convenience wrapper) needing to
    # pass them explicitly or risk two call sites drifting apart.
    # ``None`` means "use the module defaults" — the pre-PR-4 behaviour, still
    # exercised by every corpus/unit test that builds a ``_CloudPosition``
    # without these two kwargs.
    echo_band_hz: tuple[float, float] | None = None
    signal_band_hz: tuple[float, float] | None = None


# --- R16 lateral evidence (plan §4.4) --------------------------------------- #
#
# One fixed log-spaced basis for every retained pose curve. Fixed rather than
# per-role so both branches land on the SAME frequencies and a consumer can sum
# them without resampling either; log-spaced because a crossover argument is a
# per-octave one. 1/12 octave is ~118 Hz at 2 kHz, which resolves a handoff
# region the plan itself calls a COARSE gate ("lateral samples remain a coarse
# gate", #1968) — this is not a polar measurement and must not be read as one.
LATERAL_EVIDENCE_BAND_HZ = (20.0, 20_000.0)
LATERAL_EVIDENCE_POINTS_PER_OCTAVE = 12


@dataclass(frozen=True)
class LateralPoseCurve:
    """One driver's NEUTRAL response at one pose, on the shared log basis.

    ``complex_tf`` holds ``M = plant * P`` — polarity-free, with NO
    configured-crossover composition applied (see
    ``CrossoverV2Conductor._lateral_priors``). §4.2's
    ``S_c = sign_c * M * C_c / P`` is the consumer's step, once per candidate.

    Values are SAMPLED at the nearest native bin, never interpolated or
    averaged: an interpolated complex value is a number no microphone produced,
    and a phase interpolated across a wrap is simply wrong. The frequencies
    actually sampled ride along for the same reason. ``band_hz`` is the role's
    driven sweep band — outside it there was no stimulus, so the samples are
    noise and a consumer must bound itself with this.
    """

    role: str
    freqs_hz: np.ndarray
    complex_tf: np.ndarray
    band_hz: tuple[float, float]
    validity_floor_hz: float | None


@dataclass(frozen=True)
class LateralPose:
    """One accepted pose in the lateral walk.

    Carries NO trim, delay, polarity or fit. That absence is the §4.4 contract
    ("re-solve trim or delay independently at every pose" is forbidden), and it
    is structural rather than a convention: there is no field here for a second
    solution to be written to.
    """

    pose_id: str
    index: int
    attempt: int
    prompt: str
    role: str
    offset_cm: float
    at_mark: bool
    captured_at: float
    curves: tuple[LateralPoseCurve, ...]

    def curve(self, role: str) -> LateralPoseCurve | None:
        for curve in self.curves:
            if curve.role == role:
                return curve
        return None


def _primary_sweep_bands(program: Any) -> dict[str, tuple[float, float]]:
    """Each role's PRIMARY sweep band, read off the program that played.

    ``kind == KIND_SWEEP`` matters because a v2 MEASURE program OPENS with a
    leading pilot pair, and a pilot carries a role and a band too — so a
    role-only match would take the pilot's, not the sweep's. Today those two
    bands are EQUAL (both derive from the same intersected ``RoleBand``), so
    this is not a live bug; it names which segment the retained curve's band
    describes, so the answer stays right if that coupling ever moves. Pinned by
    ``test_the_retained_band_reads_the_sweep_segment_not_a_pilot``.
    """
    bands: dict[str, tuple[float, float]] = {}
    for segment in program.segments:
        if segment.kind != KIND_SWEEP or segment.role is None:
            continue
        if segment.f1_hz is None or segment.f2_hz is None:
            continue
        bands.setdefault(segment.role, (float(segment.f1_hz), float(segment.f2_hz)))
    return bands


def lateral_evidence_grid_hz() -> np.ndarray:
    """The shared log basis every retained pose curve is sampled onto."""
    lo, hi = LATERAL_EVIDENCE_BAND_HZ
    octaves = math.log2(hi / lo)
    return np.geomspace(
        lo, hi, num=int(round(octaves * LATERAL_EVIDENCE_POINTS_PER_OCTAVE)) + 1,
    )


def lateral_pose_curve(
    response: Any, band_hz: tuple[float, float],
) -> LateralPoseCurve:
    """Sample one analyzed driver response onto the shared basis."""
    freqs = np.asarray(response.freqs_hz, dtype=np.float64)
    tf = np.asarray(response.complex_tf, dtype=np.complex128)
    # ``searchsorted`` + a one-step comparison is the nearest native bin on a
    # monotonically increasing rfft grid, without materialising an N x M
    # distance matrix (the analysis grid is hundreds of thousands of bins).
    grid = lateral_evidence_grid_hz()
    right = np.searchsorted(freqs, grid).clip(1, freqs.size - 1)
    left = right - 1
    take = np.where(
        np.abs(grid - freqs[left]) <= np.abs(freqs[right] - grid), left, right
    )
    return LateralPoseCurve(
        role=str(response.role),
        freqs_hz=freqs[take],
        complex_tf=tf[take],
        band_hz=(float(band_hz[0]), float(band_hz[1])),
        validity_floor_hz=response.validity_floor_hz,
    )


def cloud_position_capture(position: _CloudPosition) -> Any:
    """One retained position → a :class:`spatial_combine.PositionCapture`.

    **The PR-4 seam.** PR-3b calls the combiner for one thing — the geometry
    verdict — but the input assembly is the whole assembly, so PR-4's wider
    pipeline (``identify_interference_nulls`` → ``evaluate_flat_spec``) extends
    the consumer, never this builder.

    Regime of the ``ir`` field, stated exactly because ``detect_echo``'s answer
    depends on it: it is the inverse rFFT of the response's **gated, calibrated**
    complex transfer function — i.e. the impulse response AFTER
    ``deconv.direct_arrival_window`` and the adaptive reflection gate that
    ``program_analysis._driver_response`` applies, not the raw deconvolved IR.
    The direct arrival is therefore present (the window places it at a fixed
    pre-offset) and early secondary arrivals inside the gate survive, which is
    the region ``detect_echo`` windows itself down to; LATE room reflections
    beyond the gate are gone by construction. The S0 forensics ran the detector
    on the ungated IR instead — ``tests/test_crossover_v2_cloud_geometry_corpus.py``
    is the measurement that the two agree on the S0 corpus's geometry verdict,
    rather than an assumption that they must.
    """
    from jasper.audio_measurement.spatial_combine import PositionCapture

    response = position.response
    freqs = np.asarray(response.freqs_hz, dtype=float)
    magnitude = np.asarray(response.magnitude_db, dtype=float)
    complex_tf = np.asarray(response.complex_tf)
    # ``program_analysis._n_fft_for`` always returns a power of two (>= 8192),
    # so the analysis grid is an even-length rfft and ``n = 2*(bins-1)``
    # inverts it exactly rather than approximately.
    ir = np.fft.irfft(complex_tf, n=2 * (complex_tf.size - 1))
    return PositionCapture(
        position_id=position.position_id,
        freqs_hz=freqs,
        magnitude_db=magnitude,
        sample_rate=int(position.sample_rate_hz),
        ir=ir,
    )


def combine_cloud_positions(positions: Sequence[_CloudPosition]) -> Any:
    """Assemble a closed group and combine it — the whole PR-4 seam.

    Returns a :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`,
    or ``None`` when the group cannot be combined (no positions, or a malformed
    one). Called exactly ONCE per group-close event, from
    :meth:`CrossoverV2Conductor._close_cloud_group`: PR-3b reads one field off
    the result (``geometry``, via :func:`_geometry_verdict_from_combined`);
    PR-4's pipeline (:func:`assemble_cloud_group_result`) reads the rest of
    the SAME object. Never a second combine — see S3 review finding
    (2026-07-26): an earlier revision of this wiring called this function
    TWICE per close attempt (once through :func:`cloud_geometry_verdict` for
    the retry gate, once more from the pipeline) — measured seconds-per-combine
    (3-6 s across runs/hosts on the S0 ten-position corpus; interpreter-bound
    ``smooth_fractional_octave``, worse on a Pi 5 — N2 review finding,
    2026-07-27: an earlier "5.6-6.2 s" point figure did not reproduce across
    hosts, so this states the regime instead of a false-precision number).
    ``GEOMETRY_RETRY_POSITIONS = 2`` allows up to 3 close attempts per group
    (2 retries + the accepting close), so the pre-fix worst case was 3 × 2 =
    6 combines, not the earlier "4x" claim — real operator seconds for a
    claim (byte-for-byte determinism) that was true but not worth paying for.

    Never raises. A group's captures are already-accepted evidence and a
    combiner failure must not retroactively fail them, so an unusable cloud is
    a ``None`` the caller turns into an honest "unknown" rather than an
    exception that would strand the session.
    """
    from jasper.audio_measurement.spatial_combine import (
        DEFAULT_ECHO_BAND_HZ,
        combine_positions,
    )

    if not positions:
        return None
    # Every position in one group carries the SAME conductor-derived bands
    # (set once at construction — see ``_CloudPosition``'s docstring), so
    # reading them off the first position is reading the group's own bands,
    # not an arbitrary one. ``None`` (a position built before PR-4, or by a
    # caller that never declared a driver contract) falls back to the
    # module's own long-standing default, unchanged from pre-PR-4 behaviour.
    echo_band_hz = positions[0].echo_band_hz or DEFAULT_ECHO_BAND_HZ
    signal_band_hz = positions[0].signal_band_hz
    try:
        return combine_positions(
            [cloud_position_capture(p) for p in positions],
            echo_band_hz=echo_band_hz,
            signal_band_hz=signal_band_hz,
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log_event(
            logger, "correction.crossover_v2_cloud_combine_failed",
            level=logging.WARNING,
            positions=len(positions), error=str(exc),
        )
        return None


def _geometry_verdict_from_combined(
    combined: Any, n_positions: int,
) -> dict[str, Any]:
    """The geometry-verdict dict from an ALREADY-COMBINED result.

    Split out of :func:`cloud_geometry_verdict` (S3 review finding,
    2026-07-26) so :meth:`CrossoverV2Conductor._close_cloud_group` can
    combine a group's positions exactly ONCE and derive both the retry-gating
    verdict and the honest-instrument pipeline from that ONE object, rather
    than each deriving its own combine. A plain JSON-native dict, because the
    host persists it verbatim into the durable v2 state. ``locked`` is
    ``False`` on every degraded path — but the ``reason`` says WHICH degraded
    path, so "no credible echo estimates" never reads the same as "the cloud
    combined and its nulls move".
    """
    if combined is None:
        return {
            "locked": False,
            "reason": "combine_failed",
            "n_positions": n_positions,
        }
    geometry = combined.geometry
    return {
        "locked": bool(geometry.locked),
        "reason": str(geometry.reason),
        "n_confident": int(geometry.n_confident),
        "n_positions": int(geometry.n_positions),
        "median_tau_us": float(geometry.median_tau_us),
        "clustered_fraction": float(geometry.clustered_fraction),
        "thin_evidence": bool(geometry.thin_evidence),
    }


def cloud_geometry_verdict(positions: Sequence[_CloudPosition]) -> dict[str, Any]:
    """PR-3b's one use of the combiner: combine, then read ``.geometry``.

    A convenience wrapper around :func:`combine_cloud_positions` +
    :func:`_geometry_verdict_from_combined` for callers that only have
    ``positions`` (the corpus acceptance test; any future direct caller) —
    the conductor itself does NOT call this (see
    :meth:`CrossoverV2Conductor._close_cloud_group`'s own single combine).

    **Reason-string divergence, documented not silently left (N4 review
    finding, 2026-07-27).** An empty ``positions`` short-circuits HERE with
    ``reason="no_positions"`` before ever reaching the combiner, while
    :func:`_geometry_verdict_from_combined` called directly with a
    ``combined=None`` and ``n_positions=0`` (e.g. because
    ``combine_cloud_positions([])`` was called some other way) reports
    ``reason="combine_failed"`` for the exact same "there were zero
    positions" fact. Unreachable through the conductor today (a group only
    closes with at least its just-captured position already retained), but
    the two functions disagree on naming WHICH degraded path a caller hit —
    the entire point of a ``reason`` field — so this wrapper owns disclosing
    the split rather than leaving a future reader to discover it by diffing
    the two bodies.
    """
    if not positions:
        return {"locked": False, "reason": "no_positions", "n_positions": 0}
    combined = combine_cloud_positions(positions)
    return _geometry_verdict_from_combined(combined, len(positions))


# --------------------------------------------------------------------------- #
# PR-4: contract-derived analysis bands + the live-flow honesty pipeline
# --------------------------------------------------------------------------- #
#
# docs/flat-linearization-productization-plan.md, PR-4: "The echo/detector
# band and PR-2's signal_band_hz derive from the declared contract: the
# summed system's swept band (RoleBand.band as composed) for the passband;
# the tweeter's usable_frequency_range_hz / measurement_band_hz for the upper
# echo band -- replacing DEFAULT_ECHO_BAND_HZ's flat constant at the call
# site." This section is that derivation, plus the single result-assembly
# function issue #1742 item 4 asks for.

# The contract-derived echo/null analysis band's LOWER edge must not drift
# below this floor without disclosure. Provenance, not a new calibration:
# spatial_combine.py's BAND_BELOW_PASSBAND_MARGIN_DB comment (PR-2, N-3) pins
# a six-band sweep of the SAME JTS3 cdhorn corpus this program's corpus tests
# already use --
#
#   band            residue deficit    screen catches it?
#   (5000, 19000)   40.43-41.98 dB     yes  (the module default)
#   (4000, 20000)   35.46-35.58 dB     yes, by 10.46 dB -- comfortable
#   (3000, 19000)   26.53-27.05 dB     yes, by only 1.53 dB -- "already thin
#                                      one octave up"
#   (2000, 19000)   18.21-18.23 dB     NO -- a false negative, not a
#                                      narrowed gap (this speaker's crossover
#                                      sits at 2 kHz; the woofer's own
#                                      passband is inside the analysed band)
#
# re-derived by test_band_deficit_separation_depends_on_the_analysis_band.
# 4000 Hz is the lowest edge in that pinned table with COMFORTABLE headroom
# (10.46 dB, vs the 3 kHz row's thin 1.53 dB) -- the row printed above is
# the one that actually justifies this constant's value.
#
# **A declared contract whose derived echo band dips below this floor is
# CLAMPED up to it, and the clamp is disclosed** (event + payload). PR-4
# shipped the reviewed disclose-don't-override design -- warn, then run the
# detector on the declared band anyway -- and the first real cloud session
# falsified it (2026-07-27, session cap_4NUGqx3yIzSuv4ta2ozfKw; issue
# #1763): the JTS3 tweeter's CORRECTLY declared measurement_band_hz
# [2000, 18000] produced a (2000, 18000) analysis band, fired the designed
# WARNING, and proceeded -- so that session's tau/r/registry outputs carry
# an uncalibrated-regime asterisk on the one measurement that mattered (the
# 2 kHz row above is a false NEGATIVE, not a narrowed gap: this speaker
# crosses over at 2 kHz, so the woofer's own passband sits inside the
# analysed band). Disclosure alone does not keep a session inside a
# calibrated regime; the clamp does, and the disclosure keeps the declared
# value visible so nobody has to read the clamped band as a declaration.
# The two quantities the derivation had been conflating are the driver's
# declared operating/measurement WINDOW (excitation + SNR scoring, which
# measurement_band_hz owns) and the echo/null ANALYSIS band (a
# detector-calibration concern, which this floor owns).
#
# **Clamping costs no cross-session comparability**, which is why it is
# cheap: the detector's quefrency step is 1e6 / BANDWIDTH, so the clamped
# JTS3 band (4000, 18000) resolves at 1e6 / 14000 = 71.4 us -- identical to
# the module default (5000, 19000), also 14 kHz wide, the band S0 was
# measured at. A clamped session's tau ladder is directly comparable to
# S0's rather than merely adjacent to it.
#
# See _derive_cloud_echo_band_hz.
ECHO_BAND_HF_REGIME_FLOOR_HZ = 4000.0

# Cloud curves decimated for persistence (bundle cloud.json + the durable v2
# state's compact cloud block) -- mirrors
# jasper.web.correction_crossover_v2.MAX_PERSISTED_SUM_POINTS (512), which
# this module cannot import without a circular dependency (that module
# imports THIS one). Kept as an independent constant rather than a shared one
# for that reason; if the two ever need to diverge, they now can.
CLOUD_CURVE_MAX_JSON_POINTS = 512


def _composed_swept_band_hz(roles: Sequence[RoleBand]) -> tuple[float, float]:
    """The summed system's swept band -- the union of every declared
    ``RoleBand.band`` -- PR-4's contract-derived ``signal_band_hz``.

    No existing function composes across roles (each ``RoleBand.band`` is one
    driver's own excitation-ceiling band, from
    ``excitation_safety_plan.resolve_driver_excitation_ceilings``); this is
    that composition, added here because it is conductor-owned wiring policy
    (which roles participate in the passband), not a pure-DSP concern that
    belongs in ``spatial_combine`` or ``program.py``.
    """
    lo = min(float(r.band.lower_hz) for r in roles)
    hi = max(float(r.band.upper_hz) for r in roles)
    return (lo, hi)


@dataclass(frozen=True)
class _CloudEchoBand:
    """The echo/null analysis band the pipeline will APPLY, plus how it was
    derived -- one value, so the band and its provenance cannot be carried
    (or persisted) apart from each other.

    ``band_hz`` is what the detector actually runs on. ``derived_lo_hz`` is
    the lower edge the declared contract produced BEFORE the HF-regime clamp
    (equal to ``band_hz[0]`` whenever no clamp happened), so a reader can
    always tell a contract-derived band from a clamped one **without** the
    journal -- the honesty rule issue #1763 turned into a requirement.
    ``source`` names WHICH derivation path produced the band, because
    "the module default" means something different when nothing was declared
    than when a clamp could not produce a usable band:

    * ``declared`` -- the tweeter's declared ``measurement_band_hz``,
      possibly narrowed by the passband containment clamp, possibly raised
      by the HF-regime clamp (``hf_regime_clamped`` tells which).
    * ``undeclared_default`` -- no measurement band was threaded through, so
      ``DEFAULT_ECHO_BAND_HZ`` stands in (pre-PR-4 behaviour, unchanged).
    * ``clamp_degenerate_default`` -- the HF clamp would have left a band too
      narrow for the detector to resolve anything in (see
      :func:`_min_clamped_echo_band_width_hz`), so ``DEFAULT_ECHO_BAND_HZ``
      stands in instead.
    * ``passband_fallback`` -- the declared band sits entirely outside the
      composed passband, so the passband itself stands in.
    """

    band_hz: tuple[float, float]
    source: str
    hf_regime_clamped: bool
    derived_lo_hz: float

    def disclosure(self) -> dict[str, Any]:
        """The JSON-native provenance block the pipeline payload carries.

        Deliberately does NOT repeat ``band_hz``: the payload already
        publishes the applied band as ``echo_band_hz``, and two copies of one
        pair is how they come to disagree.
        """
        return {
            "source": self.source,
            "hf_regime_clamped": self.hf_regime_clamped,
            "derived_lo_hz": float(self.derived_lo_hz),
            "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
        }


def _min_clamped_echo_band_width_hz() -> float:
    """The narrowest band the HF-regime clamp may hand the detector, derived
    from the DETECTOR's own constants rather than picked.

    ``detect_echo``'s quefrency step is ``resolution_us = 1e6 / bandwidth``,
    and two of its gates are multiples of that step: the searched window's
    edge margin (``WINDOW_EDGE_MARGIN_STEPS``, one step above
    ``search_us[0]``) and -- independently of the window --
    ``assess_geometry``'s refusal to cluster any estimate whose ``tau_us``
    is below ``GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``. The geometry
    floor is the binding one, and once it reaches the TOP of the searched
    window no delay the detector is allowed to look for can be clustered at
    all, so the band cannot produce a geometry lock however good the room is:

        GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / DEFAULT_ECHO_SEARCH_US[1]
          = 3.0 * 1e6 / 800 us
          = 3750 Hz

    (The edge margin's own bound is 1.0 * 1e6 / (800 - 120) us => 1470 Hz,
    i.e. slacker, which is why the geometry floor is the one to read.
    ``DEFAULT_ECHO_SEARCH_US`` is the right window to read because this
    program's ``combine_positions`` call passes no ``echo_search_us``, so the
    default window is the one actually searched.)

    This dominates the detector's other width constraint,
    ``MIN_ECHO_BAND_BINS`` (16 bins of ``detect_echo``'s own FFT): that FFT
    is floored at 4096 points, so at this program's 48 kHz the coarsest bin
    spacing is 11.72 Hz and 16 bins need only 15 * 11.72 = 175.8 Hz -- 21x
    narrower than the bound above. One rule is therefore enough: a band that
    clears this floor clears the bin-count refusal too.

    Derived rather than hard-coded so a change to either detector constant
    moves this bound with it instead of leaving a stale literal behind.
    """
    from jasper.audio_measurement.spatial_combine import (
        DEFAULT_ECHO_SEARCH_US,
        GEOMETRY_MIN_RESOLUTION_STEPS,
    )

    return GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / float(DEFAULT_ECHO_SEARCH_US[1])


def _derive_cloud_echo_band_hz(
    signal_band_hz: tuple[float, float],
    tweeter_measurement_band_hz: tuple[float, float] | None,
) -> _CloudEchoBand:
    """The contract-derived echo/null analysis band (PR-4): the tweeter's
    declared ``measurement_band_hz``, replacing ``DEFAULT_ECHO_BAND_HZ``'s
    flat constant at this call site -- returned WITH its provenance (see
    :class:`_CloudEchoBand`).

    Falls back to ``DEFAULT_ECHO_BAND_HZ`` when the tweeter's measurement
    band was not threaded through (an older/incomplete confirmed profile) --
    that constant is the module's own long-standing default, not a new
    invention, and every existing corpus test that validated
    ``identify_interference_nulls`` against the S0 corpus did so at exactly
    this band (``S0_BAND_HZ`` in ``tests/test_interference_nulls.py``).

    **Containment (inherited PR-2/PR-6a constraint):** clamped to sit INSIDE
    ``signal_band_hz`` (the derived passband), never wider. A band that
    neither contains nor sits clear of the analysis band leaves
    ``detect_echo``'s signal-presence screen uncalibrated
    (``spatial_combine.BAND_BELOW_PASSBAND_MARGIN_DB``'s docstring: "What is
    NOT calibrated: a passband narrower than the analysis band, or
    overlapping it"). Since ``signal_band_hz`` is the union of BOTH roles'
    excitation bands (always at least as wide as one driver's own
    measurement window in the ordinary 2-way case -- the woofer's lower edge
    sits well below the tweeter's, and the tweeter's own excitation ceiling
    upper edge is never narrower than its measurement band, per
    ``resolve_driver_excitation_ceilings``'s "Band-edge asymmetry" rule),
    this clamp is a no-op for every declared contract exercised by this
    program's tests and only bites a genuinely malformed one.

    **HF regime (issue #1763):** when the contained lower edge sits below
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ`, it is RAISED to that floor and the
    clamp is disclosed -- a WARNING event (slug suffix
    ``cloud_echo_band_clamped_to_hf_regime``) plus the provenance this
    returns, so neither a journal reader nor a payload reader has to infer
    it from the band alone. The contract's
    upper edge is kept: the floor is a statement about where the detector's
    calibrations hold, not about how wide the driver's window is. See
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ`'s own comment for the six-band
    deficit table behind the number, and for why PR-4's disclose-and-proceed
    design was replaced.

    **When the clamp cannot produce a usable band** -- the surviving width
    ``upper - floor`` is below :func:`_min_clamped_echo_band_width_hz` -- the
    band falls back to ``DEFAULT_ECHO_BAND_HZ`` with its own disclosure
    rather than to a stub the detector would refuse everything in. That trade
    is stated rather than glossed: the default is NOT re-clamped into the
    passband, so in this corner the band can sit outside a pathologically low
    passband and leave the signal-presence screen's deficit statistic
    uncalibrated. That is the lesser loss -- an uncontained band still runs
    both estimators, whereas a band too narrow to resolve any delay in the
    searched window makes every number downstream meaningless. It is also
    unreachable from any plausible contract: it needs
    ``min(declared_upper, passband_upper)`` below 7750 Hz, i.e. a "tweeter"
    (or a whole 2-way system) that is not swept into the top three octaves --
    the same malformed-contract family as the passband fallback below.
    """
    from jasper.audio_measurement.spatial_combine import DEFAULT_ECHO_BAND_HZ

    declared = tweeter_measurement_band_hz is not None
    band = tweeter_measurement_band_hz or DEFAULT_ECHO_BAND_HZ
    lo = max(float(band[0]), float(signal_band_hz[0]))
    hi = min(float(band[1]), float(signal_band_hz[1]))
    if lo >= hi:
        # A genuinely malformed declared contract -- the tweeter's own
        # measurement band sits entirely outside the composed passband.
        # Fall back to the passband itself rather than hand a caller an
        # inverted/degenerate pair that would raise deep inside
        # combine_positions with no context about why.
        log_event(
            logger, "correction.crossover_v2_cloud_echo_band_degenerate",
            level=logging.WARNING,
            declared_measurement_band_hz=list(band),
            signal_band_hz=list(signal_band_hz),
        )
        return _CloudEchoBand(
            band_hz=(float(signal_band_hz[0]), float(signal_band_hz[1])),
            source="passband_fallback",
            hf_regime_clamped=False,
            derived_lo_hz=lo,
        )
    if lo < ECHO_BAND_HF_REGIME_FLOOR_HZ:
        min_width_hz = _min_clamped_echo_band_width_hz()
        if hi - ECHO_BAND_HF_REGIME_FLOOR_HZ < min_width_hz:
            log_event(
                logger, "correction.crossover_v2_cloud_echo_band_clamp_degenerate",
                level=logging.WARNING,
                derived_lo_hz=lo, upper_hz=hi,
                floor_hz=ECHO_BAND_HF_REGIME_FLOOR_HZ,
                min_width_hz=min_width_hz,
                fallback_band_hz=list(DEFAULT_ECHO_BAND_HZ),
            )
            return _CloudEchoBand(
                band_hz=(float(DEFAULT_ECHO_BAND_HZ[0]), float(DEFAULT_ECHO_BAND_HZ[1])),
                source="clamp_degenerate_default",
                hf_regime_clamped=False,
                derived_lo_hz=lo,
            )
        # ``clamped_lo_hz`` equals ``floor_hz`` by construction; both are
        # logged so a journal reader does not have to know that to read the
        # line.
        log_event(
            logger, "correction.crossover_v2_cloud_echo_band_clamped_to_hf_regime",
            level=logging.WARNING,
            derived_lo_hz=lo, clamped_lo_hz=ECHO_BAND_HF_REGIME_FLOOR_HZ,
            floor_hz=ECHO_BAND_HF_REGIME_FLOOR_HZ, upper_hz=hi,
        )
        return _CloudEchoBand(
            band_hz=(ECHO_BAND_HF_REGIME_FLOOR_HZ, hi),
            source="declared" if declared else "undeclared_default",
            hf_regime_clamped=True,
            derived_lo_hz=lo,
        )
    return _CloudEchoBand(
        band_hz=(lo, hi),
        source="declared" if declared else "undeclared_default",
        hf_regime_clamped=False,
        derived_lo_hz=lo,
    )


def _decimate_curve_for_json(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray,
) -> dict[str, list[float]]:
    """Stride-decimate one combined curve to at most
    :data:`CLOUD_CURVE_MAX_JSON_POINTS`, for disclosure only.

    **No longer the same shape as ``_decimate_sum`` (issue #1858).** Before
    that fix this mirrored ``jasper.web.correction_crossover_v2._decimate_sum``
    exactly (floor-division stride, identity when already short enough) so
    the two persisted curve payloads read the same way to a consumer.
    ``_decimate_sum`` now block-averages instead, because its input
    (``conductor.measure_predicted_sum``) is the RAW, unsmoothed prediction
    and a stride over that aliases below ~500 Hz. This function's input,
    ``combined.power_mean_spec_db``, has already been through
    ``smooth_fractional_octave`` inside :func:`combine_positions` before it
    ever reaches here, so a plain stride over an already-smoothed curve does
    not reintroduce that failure mode -- the two callers start from
    differently-prepared curves, which is why one still strides and the
    other no longer does. ``freqs_hz`` and ``magnitude_db`` remain
    identity-shaped (floor-division stride) either way.
    """
    n = len(freqs_hz)
    step = max(1, n // CLOUD_CURVE_MAX_JSON_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs_hz[::step]],
        "magnitude_db": [float(m) for m in magnitude_db[::step]],
    }


def _null_registry_to_dict(report: Any) -> dict[str, Any]:
    """``InterferenceNullReport`` -> a plain JSON dict.

    PR-1 shipped no ``to_dict`` (the module docstring's own words: "zero
    production callers by design until the plan's PR-4 wires it into the
    conductor's cloud-group analysis") -- this is that wiring layer's owned
    serialization, mirroring ``FlatSpecReport.to_dict``'s shape so the two
    persisted reports read consistently.
    """
    return {
        "nulls": [
            {
                "f_lo_hz": n.f_lo_hz, "f_hi_hz": n.f_hi_hz,
                "f_center_hz": n.f_center_hz, "n": n.n, "tau_us": n.tau_us,
                "r_time": n.r_time, "r_freq": n.r_freq,
                "agreement": n.agreement, "depth_db": n.depth_db,
                "classification": n.classification,
                "evidence": dict(n.evidence),
            }
            for n in report.nulls
        ],
        "excluded_bands_hz": [list(b) for b in report.excluded_bands_hz],
        "excluded_fraction": float(report.excluded_fraction),
        "refusals": [
            {
                "f_center_hz": r.f_center_hz, "depth_db": r.depth_db,
                "reason": r.reason, "evidence": dict(r.evidence),
            }
            for r in report.refusals
        ],
        "reason": report.reason,
        "classification": report.classification,
        "band_hz": list(report.band_hz),
        "tau_ladder_us": float(report.tau_ladder_us),
        "arrival_tau_us": float(report.arrival_tau_us),
        "arrival_r_time": float(report.arrival_r_time),
        "arrival_r_max": float(report.arrival_r_max),
        "n_corroborating": int(report.n_corroborating),
        "r_freq": float(report.r_freq),
        "agreement": float(report.agreement),
        "ladder_arrival_gap": float(report.ladder_arrival_gap),
        "capped": bool(report.capped),
        "min_depth_db": float(report.min_depth_db),
        "n_candidates": int(report.n_candidates),
    }


def _geometry_guidance_copy(geometry: Mapping[str, Any]) -> str:
    """Plain-language "spread the mic further" guidance from a geometry
    verdict dict (:func:`cloud_geometry_verdict`'s own shape) -- the
    household-facing surface issue #1742 item 2 asked for. Recorded since
    PR-3b (the durable v2 state's ``cloud`` block, ``GEOMETRY_RETRY_POSITIONS``'s
    own comment). PR-4 carries this copy onto the envelope and `/state`
    (`crossover_v2_status_block`'s compact projection); no household-facing
    surface renders it yet (zero JS/asset changes in PR-4) -- PR-7 renders
    it.

    Softened, never suppressed, when ``thin_evidence`` -- and the softened
    copy names the qualitative floor ("the bare minimum of positions"),
    never a discrete number or a percentage, because thin_evidence is a
    cliff at an exact confident-estimate count, not a gradient
    (spatial_combine.GeometryLock's own docstring) -- naming the actual
    count would read as a gradient the instrument does not claim. Empty
    string when not locked -- nothing to say.
    """
    if not geometry.get("locked"):
        return ""
    if geometry.get("thin_evidence"):
        return (
            "The measured echo pattern looks the same at every microphone "
            "position, but only the bare minimum of positions gave a "
            "confident enough reading to tell. Spreading the microphone "
            "further apart next time would make this more certain."
        )
    return (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )


# --------------------------------------------------------------------------- #
# Carve-out disclosure (owner decision 1, 2026-07-25; plan PR-6b)
#
# The owner's decision of record: identified interference nulls are excluded
# from spec evaluation AND from correction, the band's tolerance applies to the
# SURVIVING envelope, and "the report discloses 'EQ cannot fill these' with the
# numbers." ``evaluate_flat_spec`` already does the excluding -- the masked bins
# leave both the reference level and every band's deviation. What it does not
# do, and must not, is say WHY: it is a pure evaluator that takes a bool mask
# and holds no product policy (its own module docstring). So the "why" is
# assembled here, in the wiring layer that already holds the registry and the
# spec report side by side, next to ``_geometry_guidance_copy`` -- the other
# household-facing copy derived from a pipeline verdict.
#
# **This module owns the carve-out copy strings; PR-7 renders them.** One
# owner, so a chart callout and the envelope's expert disclosure cannot say
# different things about the same carved range.
# --------------------------------------------------------------------------- #

# Which honesty instrument carved a range. Snake_case and self-identifying,
# mirroring the vocabulary rule interference_nulls.py states for its own slugs.
CARVE_OUT_SOURCE_IDENTIFIED_NULL = "identified_null"
CARVE_OUT_SOURCE_POSITION_SCREEN = "position_screen"


def _format_carve_out_hz(hz: float) -> str:
    """One frequency as household copy — kHz at and above 1 kHz, Hz below.

    Deliberately NOT the ``f"{hz:.0f} Hz"`` form the envelope's flatness lines
    use: those quote a single worst bin, while these copy strings list several
    frequencies in one sentence, where five-digit Hz figures read as noise.
    """
    return f"{hz / 1000.0:.1f} kHz" if hz >= 1000.0 else f"{hz:.0f} Hz"


def _join_carve_out_phrases(parts: Sequence[str]) -> str:
    """``["a", "b", "c"]`` -> ``"a, b and c"``. No serial comma, matching the
    house copy elsewhere in this flow."""
    parts = tuple(parts)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _null_classification_copy(classification: str) -> str:
    """The classification's own household sentence, or ``""`` for a
    classification this copy does not cover.

    **The ``position_invariant`` wording is load-bearing and pre-registered**
    (plan PR-1's classification vocabulary, PR-7's callout copy): a single
    session cannot separate "travels with the speaker" from "a path in the room
    that did not change while measuring", and the output must not claim it can
    — S0 separated them only by MOVING the speaker. So the copy names both and
    names the experiment that would tell them apart.

    No hardware noun appears here, in either branch. The classification is
    evidence about how a null behaved across a mic cloud; it is not evidence
    about what part of a speaker or room produced it, and naming one would be
    the device-taxonomy guess this program forbids in shipped copy (the JTS3
    rim-wave attribution is session knowledge, not measured general truth).
    """
    from jasper.audio_measurement.interference_nulls import (
        CLASSIFICATION_POSITION_DEPENDENT,
        CLASSIFICATION_POSITION_INVARIANT,
    )

    if classification == CLASSIFICATION_POSITION_INVARIANT:
        return (
            " It sat at the same frequencies at every microphone position — "
            "consistent with something that travels with the speaker, or with "
            "a path that did not change while measuring; moving the speaker "
            "and measuring again would tell those apart."
        )
    if classification == CLASSIFICATION_POSITION_DEPENDENT:
        return (
            " It appeared at some microphone positions and not others, so "
            "whatever causes it does not travel with the speaker."
        )
    return ""


def _carve_out_records(
    null_report: Any, screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Every carved range, tagged with the instrument that carved it.

    The two honesty instruments are listed SEPARATELY rather than merged: a
    merged interval loses which instrument found it, and the registry's rows
    are the only ones carrying τ/r — the exclusion *reason of record*. Ranges
    from the two sources may overlap each other; that is reported as two rows
    (one per instrument's own evidence), not silently collapsed, because "both
    instruments flagged this" is a stronger statement than either alone.
    ``merged_excluded_bands_hz`` remains the merged view for anyone counting.

    A registry row's interval is the null's OWN ``f_lo_hz``/``f_hi_hz`` (its
    half-depth width), unclipped to any spec band — τ and r describe the whole
    null, so clipping the interval to a band edge would attach the numbers to a
    fragment of what was measured.

    Ordered by lower edge, then by source, so two rows starting at the same
    frequency come out in a stable order rather than an input-order one.
    """
    records: list[dict[str, Any]] = []
    for null in null_report.nulls:
        records.append(
            {
                "f_lo_hz": float(null.f_lo_hz),
                "f_hi_hz": float(null.f_hi_hz),
                "source": CARVE_OUT_SOURCE_IDENTIFIED_NULL,
                "f_center_hz": float(null.f_center_hz),
                "n": int(null.n),
                "tau_us": float(null.tau_us),
                "r_time": float(null.r_time),
                "r_freq": float(null.r_freq),
                "depth_db": float(null.depth_db),
                "classification": str(null.classification),
                "reason": (
                    "A delayed copy of the sound cancels this range, and EQ "
                    "cannot fill a cancellation, so it is left out of "
                    "correction and out of grading."
                    + _null_classification_copy(str(null.classification))
                ),
            }
        )
    for band in screen_bands_hz:
        records.append(
            {
                "f_lo_hz": float(band[0]),
                "f_hi_hz": float(band[1]),
                "source": CARVE_OUT_SOURCE_POSITION_SCREEN,
                "reason": (
                    "The microphone positions disagreed about this range much "
                    "more than about the rest of the spectrum, so it reads as "
                    "interference rather than the speaker's own response and "
                    "is left out of correction and out of grading."
                ),
            }
        )
    records.sort(key=lambda record: (record["f_lo_hz"], record["source"]))
    return records


def _carve_out_disclosure_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The band's household-facing headline — plain language, no τ/r.

    ``""`` when nothing was carved in the band, mirroring
    :func:`_geometry_guidance_copy`'s "empty string when not locked — nothing
    to say" rule rather than rendering a "no interference found" sentence a
    reader could mistake for a measurement.

    The delay is quoted in **milliseconds** here because it is the one number
    that makes the sentence mean something to a household ("a delayed copy
    arrives 0.32 ms later"); τ stays in microseconds in the structured record,
    which is the registry's own unit and the one owner of it.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    screened = [r for r in records if r["source"] == CARVE_OUT_SOURCE_POSITION_SCREEN]
    sentences: list[str] = []
    if nulls:
        where = _join_carve_out_phrases(
            [_format_carve_out_hz(float(r["f_center_hz"])) for r in nulls]
        )
        # One ladder, one τ (IdentifiedNull.tau_us is "the same value on every
        # rung of one report" — its own docstring), so the first row's delay
        # describes them all.
        delay_ms = float(nulls[0]["tau_us"]) / 1000.0
        plural = len(nulls) > 1
        sentences.append(
            f"{'Interference nulls at' if plural else 'An interference null at'} "
            f"{where} — a delayed copy of the sound arrives {delay_ms:.2f} ms "
            f"later. EQ cannot fill {'these' if plural else 'this'}, so "
            f"{'they are' if plural else 'it is'} left out of correction and "
            "out of this band's grading."
        )
    if screened:
        plural = len(screened) > 1
        # "One range" rather than "1 range": this is prose, and the frequency
        # figures are the numerals a reader should be counting in it.
        count = f"{len(screened)}" if plural else "One"
        subject = f"{count} {'further ' if nulls else ''}"
        subject += "ranges are" if plural else "range is"
        tail = (
            "left out because the microphone positions disagreed about "
            if nulls
            else (
                "left out of correction and out of this band's grading "
                "because the microphone positions disagreed about "
            )
        )
        sentences.append(
            f"{subject} {tail}{'them' if plural else 'it'} too much to grade."
        )
    return " ".join(sentences)


def _carve_out_expert_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The expert-layer line — the same carve-outs WITH τ and r.

    Separated from :func:`_carve_out_disclosure_copy` rather than folded into
    it because the two registers have different readers and the plan puts τ/r
    behind a disclosure ("τ/r vocabulary lives in an expert disclosure, not the
    headline"). Both are produced here so a chart callout and the envelope's
    ``<details>`` cannot drift into saying different things.

    ``r`` is reported as the pair the registry actually holds — the
    time-domain and frequency-domain estimates — rather than one averaged
    figure, because their AGREEMENT is what admitted the null in the first
    place, and an average would hide it.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    if not nulls:
        return ""
    where = _join_carve_out_phrases(
        [
            f"{_format_carve_out_hz(float(r['f_center_hz']))} (rung {int(r['n'])}, "
            f"{float(r['depth_db']):.1f} dB deep)"
            for r in nulls
        ]
    )
    first = nulls[0]
    return (
        f"carved out of grading: {where}; delay τ {float(first['tau_us']):.0f} µs, "
        f"reflection ratio r {float(first['r_time']):.3f} measured in time / "
        f"{float(first['r_freq']):.3f} implied by null depth"
    )


def carve_outs_by_band(
    spec_report: Any,
    null_report: Any,
    screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Per spec band: which ranges were carved out, why, and with what numbers.

    Owner decision 1 (2026-07-25) in payload form. One entry per band of
    ``spec_report``, **always all of them, in the report's own order**, so a
    consumer can join to ``spec["bands"]`` by index or by ``band_hz`` and can
    render "nothing carved here" without having to infer it from an absence.

    A record is included in a band when its interval OVERLAPS the band's
    ``[f_lo_hz, f_hi_hz)`` span, so a null straddling a band edge appears under
    both bands it actually carves — it removes bins from both.

    **What this does NOT include: the gate-validity clamp.** Bins below the
    group's ``validity_floor_hz`` also leave the spec evaluation (plan PR-5),
    but they are not an interference verdict and PR-5 deliberately keeps them
    out of the honesty instruments' own accounting, disclosed separately as
    ``validity_floor_hz``. So a band's ``n_excluded`` on the spec report can
    exceed what these records cover, and the floor is the difference — the same
    separation ``_compact_cloud_status`` carries for exactly this reason.
    """
    records = _carve_out_records(null_report, screen_bands_hz)
    out: list[dict[str, Any]] = []
    for band in spec_report.bands:
        f_lo, f_hi = float(band.f_lo_hz), float(band.f_hi_hz)
        in_band = [
            record
            for record in records
            if record["f_lo_hz"] < f_hi and record["f_hi_hz"] > f_lo
        ]
        out.append(
            {
                "band_hz": [f_lo, f_hi],
                "intervals": [dict(record) for record in in_band],
                "disclosure": _carve_out_disclosure_copy(in_band),
                "expert": _carve_out_expert_copy(in_band),
            }
        )
    return out


@dataclass(frozen=True)
class _CloudFitEvidence:
    """What a closed spatial cloud contributes to the correction envelope.

    The three optional arguments of
    :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`,
    travelling together as one value so the fit cannot be handed a half-supplied
    pair (``compose_envelope`` raises on ``band_spread`` without
    ``n_positions``, and this makes that unreachable from this module).

    ``excluded_bands_hz`` is the MERGED honesty mask — the power-vs-median
    screen union the identified-null registry, as
    :func:`assemble_cloud_group_result` merged it. Not the screen's intervals
    and not the registry's: the wiring contract (issue #1742 item 4) is that
    the instruments are consumed together.

    ``boost_excluded_bands_hz`` does NOT go to the envelope. It is the
    boost-only bound (#1967) the fit vocabulary takes, and it rides here
    because it is derived from the same closed cloud at the same moment —
    see :meth:`CrossoverV2Conductor._boost_excluded_bands_hz`. Empty is the
    ordinary case and means "nothing contradicted a boost", never "no
    evidence".
    """

    excluded_bands_hz: tuple[tuple[float, float], ...]
    band_spread: tuple[Any, ...]
    n_positions: int
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()


def cloud_validity_floor_hz(positions: Sequence[_CloudPosition]) -> float | None:
    """The group's own gated validity floor — the WORST (highest) of its
    positions' floors, or ``None`` when no position reported a usable one.

    Why the worst rather than a mean or the anchor's: the combined curve is a
    power mean ACROSS these positions, so a bin below any one position's
    reflection-gate floor is contaminated in the average by that position's
    truncated-window artifact (``gating.f_valid_floor_hz`` — the same
    quantity ``_analyze_verify``'s tracking band already clamps up to, W6.9
    forensics). Taking the highest floor is the only choice under which every
    graded bin is inside every contributing capture's validity.

    ``None`` (no position carried a finite, positive floor) means the lower
    edge could not be verified — NOT that it is zero. Callers disclose it as
    unknown and clamp nothing; see :func:`assemble_cloud_group_result`.
    """
    floors = [
        float(getattr(p.response, "validity_floor_hz", None) or 0.0)
        for p in positions
    ]
    usable = [f for f in floors if math.isfinite(f) and f > 0.0]
    return max(usable) if usable else None


def _crossover_region_null_registry(
    combined: Any,
    *,
    echo_band_hz: tuple[float, float],
    crossover_region_hz: tuple[float, float] | None,
    identify: Any,
) -> dict[str, Any] | None:
    """Ask the null registry about the CROSSOVER REGION — and never let the
    answer gate anything (#1967, #1867).

    The defect, in the panel's own words: the registry "did not return
    'unknown,' it was **never asked** — its band excludes the region." The
    gating band's lower edge is floored at
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ` (4 kHz), so on a 2 kHz crossover the
    one region that dominates the residual is structurally unreachable by the
    one instrument built to explain it, while the cloud screen separately
    carves it out of grading. #1867 adds the mechanism that makes this
    concrete: the τ ≈ 303 µs comb's own model puts rungs at 1649 Hz and
    4948 Hz, and neither is visible from above 4 kHz.

    **What the 4 kHz floor protects, stated before it is touched.** It is not
    a round number: ``ECHO_BAND_HF_REGIME_FLOOR_HZ``'s comment pins a six-band
    sweep of this same corpus in which the detector's signal-presence screen
    catches the band-below-passband condition by 10.46 dB at a 4 kHz edge, by
    only 1.53 dB at 3 kHz, and **fails outright at 2 kHz — a false NEGATIVE,
    not a narrowed gap**, precisely because at that edge the woofer's own
    passband sits inside the analysed band. #1763 then falsified the original
    disclose-and-proceed design in the field: a correctly declared
    [2000, 18000] window fired the designed warning, proceeded, and left that
    session's τ/r/registry outputs carrying an uncalibrated-regime asterisk on
    the one measurement that mattered. Disclosure did not keep the session
    inside a calibrated regime; the clamp did.

    **So the floor does not move.** ``echo_band_hz`` is unchanged, the gating
    registry still runs on it, and this function's output is unioned into
    NOTHING — not ``merged_mask``, not ``spec_mask``, not the trusted floor,
    not a verdict. What changes is only that the question gets asked and the
    answer gets published.

    **Why that is sound, and it is the same argument R9 already ships.** The
    failure the clamp prevents is a screen whose deficit statistic is
    uncalibrated in this regime — i.e. the band's outputs are not trustworthy
    enough to *decide* on. It is not that the detector produces nothing there.
    Classification that can never reach a decision cannot be corrupted by a
    mis-calibrated screen; the worst case is a finding a reader discounts.
    ``gating.SEARCH_T_MIN_MS`` made exactly this trade for exactly this reason
    — a candidate below it "is recorded in the ``internal_reflection_ledger``,
    and it NEVER gates" — after the R9 certification found a challenger that
    fired 13/13 on the horn's own internal feature. Asymmetric cost: a false
    *detection* that gates is catastrophic, a false detection that only
    classifies is noise.

    **And this is where R10a's objective is the enabling context, not
    decoration.** A finding here used to be uninterpretable: nothing in the
    flow could say whether energy at 1649 Hz was a room null, a driver
    feature, or the two branches summing. The committed crossover now answers
    that — ``crossover_region_hz`` comes from the shipped graph's own
    committed regions, and the per-branch objective knows what each branch is
    *supposed* to be doing across that span. The finding is published WITH the
    band that produced it, so a reader gets "a null inside the committed
    handoff" rather than an unattributed anomaly. That is why this ships in
    the objective round and not before it.

    Returns ``None`` — never an empty dict — when there is no committed
    crossover to name a region with, or when the gating band already reaches
    the region (nothing was hidden, so there is nothing to disclose), or when
    the extension would be degenerate.
    """
    if crossover_region_hz is None:
        return None
    region_lo_hz = float(crossover_region_hz[0])
    gating_lo_hz, gating_hi_hz = float(echo_band_hz[0]), float(echo_band_hz[1])
    if region_lo_hz >= gating_lo_hz:
        return None
    if region_lo_hz <= 0.0 or region_lo_hz >= gating_hi_hz:
        return None

    # The SAME upper edge as the gating band, lowered to reach the region.
    # Extending rather than carving a narrow window keeps the detector's own
    # width constraints satisfied (its quefrency step is 1e6 / bandwidth), so
    # the extension is not a differently-resolved instrument reporting in the
    # same units as the gating one.
    band_hz = (region_lo_hz, gating_hi_hz)
    try:
        report = identify(combined, band_hz=band_hz)
    except Exception:  # noqa: BLE001 - a classify-only surface may never
        # break a session. The gating registry above has already run and is
        # unaffected; an extension that cannot be computed is simply absent.
        log_event(
            logger, "correction.crossover_v2_crossover_region_registry_failed",
            level=logging.WARNING, band_hz=list(band_hz),
        )
        return None

    block = _null_registry_to_dict(report)
    block.update({
        "band_hz": list(band_hz),
        # The two load-bearing flags, spelled out rather than implied by
        # absence from a mask a reader cannot see from here.
        "gating": False,
        "regime": "uncalibrated_below_hf_floor",
        "hf_regime_floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
        "crossover_region_hz": [
            float(crossover_region_hz[0]), float(crossover_region_hz[1]),
        ],
        "why": (
            "Classification only. Below "
            f"{ECHO_BAND_HF_REGIME_FLOOR_HZ:.0f} Hz the detector's "
            "signal-presence screen is uncalibrated for a band that spans the "
            "committed crossover, so a finding here is evidence to read, "
            "never a reason to exclude a band or move a verdict."
        ),
    })
    log_event(
        logger, "correction.crossover_v2_crossover_region_registry",
        band_hz=list(band_hz),
        crossover_region_hz=list(crossover_region_hz),
        classification=str(block.get("classification", "")),
        n_candidates=int(block.get("n_candidates", 0) or 0),
        gating=False,
    )
    return block


def committed_crossover_region_hz(
    regions: Iterable[Any], *, octaves: float = 1.0,
) -> tuple[float, float] | None:
    """The band the COMMITTED crossover hands off in — ``Fc ± octaves`` across
    every committed region, or ``None`` when nothing is committed.

    Derived from the preset's own ``crossover_regions`` (the same objects
    :func:`~jasper.active_speaker.branch_chain.sections_by_role` walks), never
    from the session's working Fc, because this band's whole purpose is to say
    where the SHIPPED graph divides the spectrum. A speaker with no committed
    region has no handoff and gets ``None`` — the same "invent nothing" rule
    ``sections_by_role`` follows.

    One octave because that is the span the crossover report (#1968 Q4) uses
    for correction-authority tapering and the span R10a's own bench scores the
    crossover-region residual over; keeping one number for "the crossover
    region" is why it is a default here rather than three literals.
    """
    fcs = [
        float(getattr(r, "fc_hz", 0.0)) for r in regions
        if float(getattr(r, "fc_hz", 0.0)) > 0.0
    ]
    if not fcs:
        return None
    span = 2.0 ** octaves
    return (min(fcs) / span, max(fcs) * span)


def assemble_cloud_group_result(
    combined: Any,
    *,
    echo_band_hz: tuple[float, float],
    echo_band_provenance: Mapping[str, Any] | None = None,
    validity_floor_hz: float | None = None,
    tier: str = "",
    position_records: Sequence[Mapping[str, Any]] = (),
    crossover_region_hz: tuple[float, float] | None = None,
    spec_report_sink: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """The wiring contract (issue #1742 item 4) -- THE single function that
    consumes the exclusion mask, ``geometry.locked``, and the null registry
    TOGETHER. No other code in this program may read
    ``combined.excluded``/``combined.geometry.locked`` and treat that as the
    honesty verdict on its own; doing so is reading the mask alone, the hole
    this item exists to close (see the plan doc's "Architecture" table: "the
    mask alone is a hole").

    Runs :func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    on ``combined`` at ``echo_band_hz``, unions its excluded bins with the
    combiner's own power-vs-median screen (``combined.excluded``), and
    evaluates :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`
    against the merged mask -- the plan's "merged honesty mask = screen ∪
    identified nulls" line, made executable.

    ``combined`` may be ``None`` (the group could not be combined at all --
    :func:`combine_cloud_positions`'s own honest "unknown") or a
    :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`.

    **The spec-curve SSOT (plan PR-5).** The ``spec`` report this builds is
    the ONE construction every spec-facing surface reads -- the flatness
    gauge, the observe ledger's spec-facing summary, `/state`, and the
    envelope all render ``flatness`` (:func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge`
    of that same report) rather than deriving a number of their own. Nothing
    downstream re-evaluates the curve.

    **The carve-out disclosure (plan PR-6b, owner decision 1).** ``carve_outs``
    is :func:`carve_outs_by_band` of the SAME registry and the SAME spec report
    — per band, which ranges left this band's grading, in plain language, with
    τ/r behind an expert string. It is a third reading of one evaluation, never
    a second one: the bins are already gone from ``spec`` by the time this runs,
    and no verdict here can move. The tolerance table is untouched — the 8-16 kHz
    row still reads ±2.5 dB, applied to whatever survives the carve-out (the
    owner's decision was to disclose the carve-out, not to re-spec the band).

    **``echo_band_provenance`` (issue #1763) is how a payload reader tells a
    contract-derived band from a clamped one.** ``echo_band_hz`` publishes the
    band the detector actually ran on, which is necessary but not sufficient:
    a reader seeing ``[4000, 18000]`` cannot tell whether the driver declared
    that window or whether the HF-regime clamp raised a declared 2 kHz edge
    into it, and the difference is exactly the asterisk issue #1763 exists to
    make visible. :meth:`_CloudEchoBand.disclosure` supplies the block (its
    ``source`` / ``hf_regime_clamped`` / ``derived_lo_hz`` / ``floor_hz``);
    the conductor passes it alongside the band it came from. ``None`` when a
    caller did not state one — "not stated", never "not clamped", the same
    unknown-vs-zero rule ``validity_floor_hz`` follows below.

    **``validity_floor_hz`` clamps the spec band's lower edge.** Bins below
    the group's gated validity floor (:func:`cloud_validity_floor_hz`) are
    "not a measurement, they're an artifact of a truncated gate window"
    (``_analyze_verify``'s own W6.9 comment about the tracking band), so they
    are excluded from the spec evaluation -- from the reference level as well
    as from every band's deviation, since a contaminated bin must not be able
    to re-center the target either. Two properties this deliberately keeps:

    * The clamp rides the evaluation's exclusion mask but **not**
      ``merged_excluded_bands_hz``, which stays the honesty instruments'
      own count (screen union identified nulls). ``excluded_interval_count``
      on `/state` is the "how much interference did we find" number and must
      not silently absorb a gate artifact. ``validity_floor_hz`` is reported
      alongside so a reader can tell the two apart in ``spec.n_excluded`` --
      and it is carried all the way to the LIVE surfaces, not just the
      durable state and the bundle: ``_compact_cloud_status`` projects it
      onto `/state`, the envelope, and the doctor's read. Without that a page
      seeing a large ``n_excluded`` could not distinguish a combed room from
      one capture's collapsed gate.
    * A ``None`` floor clamps NOTHING and is reported as ``None``. The
      alternative -- withholding the whole gauge, which is what the retired
      per-capture ``_flatness_tracking`` did when a capture had no floor --
      would throw away the 2-16 kHz evidence over an unverified lower edge.

    Regime, measured on the S0 main leg 2026-07-27, RE-DERIVED 2026-08-02
    (#2045): the spec table's lower edge is 250 Hz and **all ten** of that
    session's positions gate to 142.857 Hz, where the clamp changes no graded
    number at all -- every band figure, the reference level, the verdict, and
    the whole gauge are byte-identical (only the report-wide
    ``excluded_intervals`` gains the sub-250 Hz region it removed, which is
    why the gauge quotes spec-band BIN counts and not an interval count). **So
    the group floor on this corpus is 142.857 Hz and the clamp is a no-op**,
    which ``test_flat_spec_ssot.test_the_real_s0_positions_no_longer_collapse_a_gate``
    pins.

    It was not always. Until PR #1991, ``cloud_04`` reported a measured
    reflection at **1777.8 Hz** and the group floor was 1777.8 Hz -- but that
    reading was the first-reflection detector firing early, the #1790 field
    instance the prominence vote was written to reject. The COST of clamping
    is still worth stating, because it is the mechanism's own behaviour and it
    moves the headline in the flattering direction; measured at that same
    floor supplied explicitly (``test_flat_spec_ssot.CLAMP_FLOOR_HZ``, pinned
    by ``test_the_validity_floor_clamp_costs_the_low_band``), clamping:

    * moves **987 bins** out of the 250 Hz-2 kHz band;
    * **re-centres the reference** -27.2386 -> -28.3062 dB (-1.0676 dB),
      because the reference is a power mean over non-excluded 250 Hz-8 kHz
      bins and the clamp removed the loud low end of it;
    * moves the HEADLINE ``max_db`` -8.9389 -> -7.8713 dB, i.e. **+1.0676 dB
      in the FLATTERING direction** -- exactly the reference shift, because
      the worst bin (15999.7 Hz) survives the clamp, so its deviation moves
      one-for-one with the reference. This is the first number the ledger
      line prints and it moves FURTHER than the RMS does;
    * takes the pooled RMS 3.8031 -> 3.1740 dB (-0.6291 dB);
    * **flips the 250 Hz-2 kHz band verdict**, +4.2458 dB (fail) ->
      -1.2146 dB (pass), since ``BandResult.passed`` is
      ``abs(max_deviation_db) <= tolerance_db``. Overall stays False here
      only because the other two bands still fail on their own.

    Direction is **response-shape dependent, not a property of the clamp**:
    on THIS corpus the removed region sat above the surviving reference, so
    dropping it lowered the reference and flattered every surviving
    deviation. A speaker whose sub-floor region is quiet would move the other
    way. Do not generalize the sign.

    None of that is the speaker improving -- it is the same speaker graded on
    fewer bins, which is exactly what ``n_bins``/``n_excluded`` on the gauge
    exist to keep visible (``ConvergenceResidual``'s own rule). One collapsed
    gate in a group is therefore expensive by design.

    **Deferred alternative, recorded rather than dismissed:** the honest
    third option is per-position, per-bin validity masking INSIDE
    ``combine_positions`` -- mask each position's contribution below that
    position's OWN floor and combine the survivors, so nine good captures
    keep contributing at 500 Hz instead of one bad one costing the band. It
    is strictly better than a group-wide clamp and is out of scope here only
    because it is a ``spatial_combine`` signature and estimator change (the
    power mean would need per-bin weights), not a wiring one. Revisit
    trigger: a real session where one collapsed gate meaningfully shrinks the
    graded band -- the S0 ``cloud_04`` case above is that evidence already,
    so this is queued on measured grounds, not speculation.

    **Fail-soft, named, not absolute** (S4 review finding, 2026-07-26 --
    corrected from an earlier "any exception is caught" overclaim). Catches
    exactly ``(ValueError, TypeError, IndexError, AttributeError)`` --
    the documented raise surface of every function this calls
    (:func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    and :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` both
    raise only ``ValueError`` on malformed input;
    :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`
    raises ``ValueError`` via ``zip(strict=True)`` on a length mismatch or
    ``IndexError`` on an out-of-bounds index; a malformed/incomplete
    ``combined``-like object raises ``TypeError``/``AttributeError`` reading
    its fields; :func:`carve_outs_by_band` adds no new family -- it reads
    already-built records by keys it set itself, and its only external reads
    are attribute lookups on the two reports and indexing/``float()`` on the
    screen intervals, i.e. ``AttributeError``/``IndexError``/``TypeError``/
    ``ValueError``). ``_run_cloud_pipeline`` relies on exactly this bounded set --
    a downstream DSP failure inside it is diagnostic/disclosure machinery,
    never a capture-accept gate, so this bounded family is caught and
    reported as ``available: False`` rather than surfacing to the caller.
    Any OTHER exception -- ``KeyError``/``RuntimeError``/``OSError`` (none
    observed on this call surface today; would indicate a genuine bug in a
    callee) or ``MemoryError``/``KeyboardInterrupt`` -- propagates
    uncaught, by design: it should reach the caller rather than silently
    become an honest-looking "unavailable".
    """
    if combined is None:
        return {"available": False, "reason": "combine_failed"}
    try:
        from jasper.active_speaker.flat_spec import (
            evaluate_flat_spec,
            spec_flatness_gauge,
        )
        from jasper.audio_measurement.interference_nulls import (
            identify_interference_nulls,
        )
        from jasper.attribution.position_evidence import position_evidence_block
        from jasper.audio_measurement.spatial_combine import merged_true_intervals

        null_report = identify_interference_nulls(combined, band_hz=echo_band_hz)
        crossover_registry = _crossover_region_null_registry(
            combined, echo_band_hz=echo_band_hz,
            crossover_region_hz=crossover_region_hz,
            identify=identify_interference_nulls,
        )
        merged_mask = np.asarray(combined.excluded, dtype=bool) | np.asarray(
            null_report.excluded, dtype=bool
        )
        # NOTE: ``crossover_registry`` is deliberately absent from this union
        # and from ``spec_mask`` below. See its builder for why classification
        # there may never become gating.
        # The honesty mask is what the instruments found; the spec mask adds
        # the gate-validity clamp on top (see this function's docstring for
        # why the two stay distinguishable).
        spec_mask = merged_mask
        if validity_floor_hz is not None and math.isfinite(validity_floor_hz):
            spec_mask = merged_mask | (
                np.asarray(combined.freqs_hz, dtype=float) < float(validity_floor_hz)
            )
        spec_report = evaluate_flat_spec(
            combined.freqs_hz, combined.power_mean_spec_db, spec_mask,
        )
        # #2291/#2160: hand the LIVE report to a caller that needs the object
        # rather than the serialized copy below. ``evaluate_spec`` reads
        # ``overall_passed`` and each band's ``evaluable``/``passed``, which
        # ``to_dict`` flattens away, and the round's spec verdict must be the
        # SAME report this function already built — re-evaluating it from
        # ``combined`` in the conductor would be a second owner of the merged
        # honesty mask, which is exactly what this function exists to prevent.
        # A sink rather than a second return value because every other caller
        # (and every test) reads the dict, and widening the return type would
        # change all of them to serve one consumer.
        if spec_report_sink is not None:
            spec_report_sink(spec_report)
        geometry_dict = {
            "locked": bool(combined.geometry.locked),
            "reason": str(combined.geometry.reason),
            "n_confident": int(combined.geometry.n_confident),
            "n_positions": int(combined.geometry.n_positions),
            "median_tau_us": float(combined.geometry.median_tau_us),
            "clustered_fraction": float(combined.geometry.clustered_fraction),
            "thin_evidence": bool(combined.geometry.thin_evidence),
        }
        return {
            "available": True,
            "geometry": geometry_dict,
            "geometry_guidance": _geometry_guidance_copy(geometry_dict),
            "screen_excluded_bands_hz": [
                list(b) for b in combined.excluded_bands_hz
            ],
            "merged_excluded_bands_hz": [
                list(b) for b in merged_true_intervals(combined.freqs_hz, merged_mask)
            ],
            "null_registry": _null_registry_to_dict(null_report),
            # #1967/#1867: the crossover region, ASKED. Classification only —
            # never unioned into any mask above. ``None`` when there is no
            # committed crossover to name a region with, or when the gating
            # band already reached it. See
            # :func:`_crossover_region_null_registry`.
            "null_registry_crossover_region": crossover_registry,
            "spec": spec_report.to_dict(),
            # PR-6b: owner decision 1's disclosure half — the SAME registry
            # and the SAME spec report above, re-read per band as "what was
            # carved out of this band's grading, and why". Not a second
            # evaluation: `evaluate_flat_spec` already removed these bins, and
            # nothing here can change a verdict.
            "carve_outs": carve_outs_by_band(
                spec_report, null_report, combined.excluded_bands_hz,
            ),
            # PR-5: the spec-facing gauge — a pure reduction of the SAME
            # ``spec`` report above, carried here so no downstream surface
            # has to (or may) derive its own. Byte-identical wherever it is
            # rendered, because there is one number, copied.
            "flatness": spec_flatness_gauge(spec_report).to_dict(),
            "validity_floor_hz": (
                float(validity_floor_hz)
                if validity_floor_hz is not None and math.isfinite(validity_floor_hz)
                else None
            ),
            "echo_band_hz": list(echo_band_hz),
            "echo_band_provenance": (
                dict(echo_band_provenance)
                if isinstance(echo_band_provenance, Mapping)
                else None
            ),
            # WHICH INSTRUMENT measured this group (flow-simplification §1.2).
            # ``None`` means unknown, never a guessed default — same
            # discipline as ``echo_band_provenance`` directly above, and for
            # the same reason: the two tiers make materially different claims,
            # so a reader that cannot tell them apart must say so.
            "tier": str(tier) or None,
            "curve": _decimate_curve_for_json(
                combined.freqs_hz, combined.power_mean_spec_db,
            ),
            # WO-1 (attribution plan §6, §11.1 A7): the MEMBERS behind every
            # aggregate above. The combiner has computed each position's
            # curve and echo diagnostic all along and this function used to
            # drop them, which is why P2 — the position-variance classifier
            # §5 calls a free probe — was not actually free, and why
            # ``clustered_fraction`` was the summary of a distribution nobody
            # could inspect. Serialization only: no new signal, no threshold,
            # no verdict. Never raises (see ``position_evidence_block``), so
            # it cannot turn a good group into a failed one.
            "positions": position_evidence_block(
                combined,
                position_records=position_records,
                validity_floor_hz=validity_floor_hz,
            ),
        }
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log_event(
            logger, "correction.crossover_v2_cloud_pipeline_failed",
            level=logging.WARNING, error=str(exc),
        )
        return {"available": False, "reason": "pipeline_failed"}


def spec_report_for_predicted_sum(predicted_sum: Any) -> Any:
    """Grade the PREDICTED post-apply response against the flat spec.

    ``predicted_sum`` is the ``(freqs_hz, magnitude_db)`` pair
    :func:`~jasper.audio_measurement.program_analysis.predicted_branch_sum`
    produces — on the v2 path, rebuilt from the LINEARIZED branches at the
    committed trim AND the committed delay (rung P3 / R10b), i.e. a model of
    exactly what the emitted graph will do.
    Returns a :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`, or
    ``None`` when there is no usable prediction to grade (``None`` input, a
    malformed pair, or a curve the evaluator refuses). **``None`` means
    "unknown", never "passed"** — the caller must not read it as permission.

    The two preparation steps are the caller-side half of
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`'s input
    contract, which takes an already-combined, already-1/3-octave-smoothed
    curve and deliberately owns neither operation. Both are done with the SAME
    owners the measured cloud curve went through, so the two reports differ by
    as little as the two curves' provenance allows:

    * block-average onto the shared analysis grid
      (:func:`~jasper.audio_measurement.spatial_combine.decimate_curve_to_analysis_grid`).
      Not an optimization detail — ``smooth_fractional_octave`` is an
      O(bins x window) Python loop whose cost is effectively quadratic in bin
      count, and a raw 512k-point prediction grid takes ~11 s to smooth on a
      laptop, worse on a Pi 5. The confirm seam is a household waiting on an
      apply; ``MAX_ANALYSIS_BINS`` is the bound the combiner already adopted
      for exactly this reason, with its own "why this is not a loss of
      information" argument (the narrowest window here, 1/3-octave at 250 Hz,
      is ~60 Hz wide against ~1.46 Hz spacing).
    * 1/3-octave smooth at the spec fraction, matching the combiner's
      ``power_mean_spec_db``.

    **The frames are still not identical, and that is stated rather than
    hidden.** The measured curve is a spatial power mean over eight in-room
    positions; this one is a two-branch anechoic-ish model at the mark. Both
    are graded by the same evaluator against the same absolute tolerances and
    both are normalized to their OWN 250 Hz-8 kHz reference, so what survives
    the comparison is SHAPE — which is what the spec grades. It is a coarse
    direction check, and the threshold its caller applies is sized to that.
    """
    if predicted_sum is None:
        return None
    from jasper.active_speaker.flat_spec import evaluate_flat_spec
    from jasper.audio_measurement.analysis import smooth_fractional_octave
    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    try:
        freqs_hz, magnitude_db = predicted_sum
        grid, curve_db = decimate_curve_to_analysis_grid(
            np.asarray(freqs_hz, dtype=float), np.asarray(magnitude_db, dtype=float),
        )
        return evaluate_flat_spec(
            grid, smooth_fractional_octave(grid, curve_db, fraction=3),
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        # The same bounded family, for the same reason, as
        # ``assemble_cloud_group_result``: a malformed or degenerate prediction
        # is a diagnostic gap, not a crash. It becomes an honest "no report",
        # and the caller's own gate decides what an absent report permits.
        log_event(
            logger, "correction.crossover_v2_predicted_spec_failed",
            level=logging.WARNING, error=str(exc),
        )
        return None


def _commanded_delta(raw_predicted_sum: Any, predicted_sum: Any) -> Any:
    """``(freqs_hz, delta_db)`` — what the applied correction COMMANDS on the
    summed response, or ``None`` (PR-L5's delta probe, the commanded half).

    The linearized-branch prediction minus the raw-branch one, both built from
    the SAME measured branches with the SAME summation model
    (``program_analysis.predicted_branch_sum``) at the SAME committed residual
    delay, so the branch measurements, the summation model, and the alignment
    all divide out and what is left is the shape the emitted filters and trims
    ask the speaker for.

    ``None`` — the probe reports ``unavailable``, which is not a pass — when
    either curve is missing, when they are the same object (a trims-only
    candidate: it emits no filters, so relative to the raw crossover it
    commands nothing this probe could grade, and the VERIFY tracking check
    remains its comparator), or when the two curves cannot be put on one grid.
    """
    if raw_predicted_sum is None or predicted_sum is None:
        return None
    if raw_predicted_sum is predicted_sum:
        return None
    try:
        raw_freqs, raw_db = raw_predicted_sum
        freqs, db = predicted_sum
        grid = np.asarray(freqs, dtype=float)
        delta = np.asarray(db, dtype=float) - np.interp(
            grid, np.asarray(raw_freqs, dtype=float), np.asarray(raw_db, dtype=float),
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log_event(
            logger, "correction.crossover_v2_commanded_delta_failed",
            level=logging.WARNING, error=str(exc),
        )
        return None
    return grid, delta


@dataclass(frozen=True)
class _LinearizationState:
    """What ONE candidate build's linearization produced, as a value (#2291).

    **This class is the scratch channel's replacement.** Until Phase 2b the
    same seven facts lived on the conductor as ``self._last_*`` fields written
    as a side effect of the fit. That made them a *return channel with no
    caller*: the Fc sweep had to snapshot and restore all seven around every
    candidate (``_FC_SWEEP_CONDUCTOR_FIELDS``) precisely because a value
    belonging to candidate N would otherwise be read as candidate N+1's — or as
    the anchor's. One save/restore bug away from publishing a prescription
    computed for a different crossover, which is the family the 2026-08-10
    incident belongs to.

    Held per build and passed by hand, the question cannot arise: a state
    describes exactly the candidate whose build returned it, and a build a
    retake moots is dropped whole — the same reason
    :class:`_SpeculativeClose` exists, applied one layer down.

    ``outcome`` is the union of the planner's own verdict
    (``"fitted"``/``"trim_rejected"``) and the two the conductor decides
    without planning at all: an eligibility refusal
    (``"ineligible_mic_tier"``/``"ineligible_repeats"``) and the SF2 degrade
    (``"fit_failed"``). Empty means no build ran. It is stamped verbatim onto
    the candidate, which is then the single reader every other surface quotes.

    Every other field is ``None``/empty on all three non-planning outcomes,
    including ``linearized_predicted_sum`` — so a candidate that degraded to
    trims-only publishes the RAW two-branch prediction as its VERIFY prior,
    which is what the trims-only lane means. Legacy left that one field
    un-cleared on the SF2 path (its own comment named the gap); a fit that
    raised part-way has no linearized model, so carrying one forward was the
    fail-open direction.
    """

    outcome: str = ""
    level_frame: Any = None
    level_frame_disagreement_db: float = 0.0
    level_frame_cores: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    level_frame_trims: Mapping[str, float] = field(default_factory=dict)
    linearized_predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    realized_level_match: RealizedLevelMatch | None = None

    @classmethod
    def from_plan(cls, plan: LinearizationPlan) -> "_LinearizationState":
        """Everything a planned candidate leaves behind, read off the plan.

        A straight projection — no policy, no re-derivation. The plan is the
        single owner of each of these values; this is the conductor naming the
        subset it consumes downstream.
        """
        return cls(
            outcome=plan.outcome,
            level_frame=plan.level_frame,
            level_frame_disagreement_db=plan.level_frame_disagreement_db,
            level_frame_cores=plan.level_frame_cores,
            level_frame_trims=plan.level_frame_trims,
            linearized_predicted_sum=plan.linearized_predicted_sum,
            realized_level_match=plan.realized_level_match,
        )

    @property
    def realized_branch_level(self) -> dict[str, Any] | None:
        """The realized-level verdict serialized, or ``None`` when none ran."""
        return (
            None if self.realized_level_match is None
            else self.realized_level_match.to_dict()
        )


@dataclass(frozen=True)
class _SpeculativeClose:
    """A group close that already RAN, waiting for the household to want it.

    The eager-fit rider's one carried value (owner UX direction, 2026-07-30).
    The household's last stage-1 position lands, the phone shows the confirm,
    and the several seconds the fit costs used to start only when they walked
    back to a browser and tapped Continue — dead air that read as a stalled
    screen. The fit now starts on the ACCEPT and parks its product here.

    **Why the built candidate cannot simply be stashed on the conductor.**
    ``_candidate`` is :meth:`CrossoverV2Conductor.confirm_cloud_measure_group`'s
    fire-once guard AND, until this rider, the held-set predicate; writing a
    speculative build into it would have closed the retake window in the same
    instant it opened (both seams carried a comment saying exactly that). So a
    speculative build lands HERE, where nothing else reads it, and reaches
    ``_candidate`` only through the household's own confirmation.

    Everything :meth:`CrossoverV2Conductor._commit_measure_candidate` needs and
    nothing else: ``analysis`` rides along for the raw ``predicted_sum`` the
    commanded-delta diff needs, and ``cloud`` for the build's log line.

    ``level_frame_finding`` is the #1866 record — present only when THIS
    build's frame gate took the finding+proceed path. It rides the built
    close rather than the conductor for the reason the whole class exists: a
    speculative build a retake moots is dropped whole, and a record left on
    ``self`` would survive that drop and be published against the next
    candidate, which measured something else.
    """

    candidate: Any
    predicted_sum: Any
    analysis: Any
    cloud: "_CloudFitEvidence | None"
    level_frame_finding: Mapping[str, Any] | None = None
    linearization: _LinearizationState = field(default_factory=_LinearizationState)
    """What THIS build's linearization produced — see :class:`_LinearizationState`.

    Rides the close for the same reason ``level_frame_finding`` does, and now
    for the whole of the fit's output rather than one record of it: a
    speculative build a retake moots is dropped whole, and anything left on
    ``self`` would survive that drop and be read against the next candidate.
    """


# --------------------------------------------------------------------------- #
# the conductor
# --------------------------------------------------------------------------- #


class CrossoverV2Conductor:
    """The v2 phase state machine driving one relay capture session.

    Construct with the session identity, the declared drivers, the crossover Fc,
    the safety caps + session volume, and the injected :class:`V2FlowSeams`.
    Hand :meth:`authorize_begin`, :meth:`on_armed`, and :meth:`consume_capture`
    to :func:`jasper.capture_relay.session.run_capture_plan`; call
    :meth:`note_apply_complete` once an apply lands (the deferred VERIFY then
    arms) — an optional synchronous shortcut for a caller that already holds
    this conductor; the seam-based ``apply_complete``/``apply_failed`` checks
    in :meth:`authorize_begin` are the durable path and work even without this
    call. Since the two-stage split (D10) no shipped session reaches that hold,
    so neither is on the critical path. :meth:`snapshot` / :meth:`hydrate`
    carry phase persistence.
    """

    def __init__(
        self,
        *,
        session_id: str,
        source_preset: Any,
        roles_bands: Sequence[RoleBand],
        fc_hz: float,
        driver_caps_dbfs: Mapping[str, float],
        session_volume_db: float,
        seams: V2FlowSeams,
        tier: str = "",
        driver_spacing_m: float = 0.0,
        accepted_phases: Sequence[str] = (),
        applied: bool = False,
        gain_plan_db: Mapping[str, float] | None = None,
        index_phase_map: Mapping[int, str] | None = None,
        post_apply_verifies: bool | None = None,
        measure_predicted_sum: Any = None,
        measure_predicted_spec_report: Mapping[str, Any] | None = None,
        measure_commanded_delta: Any = None,
        measure_entry_baseline: "EntryBaseline | None" = None,
        measure_gate_window_ms: float | None = None,
        verify_pilot_transfer_prior: Mapping[str, Any] | None = None,
        driver_class_by_role: Mapping[str, str] | None = None,
        radiating_diameter_mm_by_role: Mapping[str, float] | None = None,
        crossover_search_band_hz_by_role: Mapping[
            str, tuple[float, float] | None
        ] | None = None,
        measurement_protection_sections_by_role: Mapping[
            str, Sequence[CrossoverSection]
        ] | None = None,
        tweeter_measurement_band_hz: tuple[float, float] | None = None,
        attempt_history: Sequence[AttemptRecord] = (),
        attempt_floor: FloorStats | None = None,
        last_attempt_decision: Mapping[str, Any] | None = None,
        speaker_id: str = "",
        tuning_attempt_id: str = "",
        sound_design_revision: int | None = None,
    ) -> None:
        roles = tuple(roles_bands)
        if len(roles) != 2:
            raise CrossoverV2FlowError("the v2 conductor is a 2-way flow")
        self.session_id = str(session_id)
        self.sound_design_revision = sound_design_revision
        # Which INSTRUMENT this session is running. Empty = unknown (a caller
        # that never declared one), never silently ``TIER_FULL`` — see
        # ``V2ConductorSnapshot.tier`` for why guessing is the dishonest
        # option. Validated so an unknown id fails at construction rather than
        # riding into the durable state and out to `/state`.
        self._tier = normalize_tier(tier) if tier else ""
        self._preset = source_preset
        self._roles = roles
        self._woofer, self._tweeter = roles[0], roles[1]
        self._fc_hz = float(fc_hz)
        # PR-4: the contract-derived analysis bands for the cloud-group
        # honesty pipeline (combine's echo/signal bands, the null gate's
        # search band) -- computed once here so every group-close event uses
        # the SAME derived values. See _composed_swept_band_hz /
        # _derive_cloud_echo_band_hz for the derivation and their citations.
        self._cloud_signal_band_hz = _composed_swept_band_hz(roles)
        # The band AND its provenance travel as one value (issue #1763), so
        # the pipeline payload can never publish an applied band without the
        # disclosure of how it was derived.
        self._cloud_echo_band = _derive_cloud_echo_band_hz(
            self._cloud_signal_band_hz, tweeter_measurement_band_hz,
        )
        self._caps = dict(driver_caps_dbfs)
        self._session_volume_db = float(session_volume_db)
        self._seams = seams
        # True once ``authorize_begin`` has refused: the relay posts its OWN
        # capture_refused for those (what the phone's authorize loop consumes)
        # into a last-write-wins slot, so the terminal rider must not overwrite
        # it (panel SF1). consume_capture refusals leave it False — nothing
        # precedes them there, which is what makes the rider load-bearing.
        self.relay_published_refusal = False
        self._measurement_protection_sections_by_role = None
        if measurement_protection_sections_by_role is not None:
            self._measurement_protection_sections_by_role = {
                str(role): tuple(sections)
                for role, sections in measurement_protection_sections_by_role.items()
            }
        # S3's lifecycle state. Attempts belong to the commissioning journey,
        # not to this relay session (stage 2 always mints a new one). The
        # conductor owns the bounded history; the injected floor/store writer
        # keep persistence outside it and the decision kernel pure.
        self._attempt_history = list(attempt_history)[
            -AttemptBudget().hard_cap_attempts:
        ]
        self._attempt_floor = attempt_floor
        self._last_attempt_decision = (
            dict(last_attempt_decision)
            if isinstance(last_attempt_decision, Mapping) else None
        )
        self._speaker_id = str(speaker_id or "unknown")
        self._tuning_attempt_id = str(tuning_attempt_id or "")
        # Layer-1a linearization (#1668 PR-C): per-role driver class, used by
        # class_prior_limit(). #1665's component-entry declarations HAVE landed
        # and both production construction sites populate this
        # (``correction_crossover_v2.prepare_v2_session`` /
        # ``prepare_v2_verify``, from ``_resolve_driver_class_by_role``); the
        # empty default remains for callers with no declaration, matching
        # linearization_envelope.compose_envelope's own "unknown".
        self._driver_class_by_role = (
            dict(driver_class_by_role) if driver_class_by_role else {}
        )
        # #1675 owner ruling: the declared effective radiating diameter per
        # role, the ka/beaming prior the Fc selector reads. Collected since
        # #1665 and consumed by nothing in Python until R17 — it reaches here
        # by the SAME draft path ``driver_class_by_role`` takes. Empty means
        # undeclared, which a consumer must DISCLOSE rather than fill in: there
        # is no conservative default diameter.
        self._radiating_diameter_mm_by_role = (
            dict(radiating_diameter_mm_by_role) if radiating_diameter_mm_by_role else {}
        )
        # R17: each participating role's declared crossover search band. An
        # EMPTY map is not "no constraint" — ``_fc_candidate_set`` reads a role
        # that is absent here exactly as a role that declared nothing, so a
        # caller which passes none gets no proposal at all. That is the
        # fail-closed direction ``resolve_fc_search_band`` documents.
        self._crossover_search_band_hz_by_role: dict[str, tuple[float, float] | None] = (
            dict(crossover_search_band_hz_by_role)
            if crossover_search_band_hz_by_role else {}
        )
        self._fc_evaluations: tuple[Any, ...] = ()
        self._fc_selection: Any = None
        self._fc_selected_evaluation: Any = None
        self._geometry = MeasurementGeometry(
            driver_spacing_m=float(driver_spacing_m),
            mic_distance_m=MEASUREMENT_DISTANCE_M,
        )
        # Where this round is, and the walk it is somewhere in (#2291 Phase 4).
        # ONE aggregate: the index map, the ordered phases, the group index
        # spans, the accepted phases, the accepted indexes inside an open group,
        # and the applied flag were six correlated fields here and could
        # disagree. Every read below goes to ``self._journey`` — its frozen
        # ``plan`` for the walk, its own methods for progress — and every write
        # is one of its two transitions.
        #
        # The standard 3-entry session uses the default map; a verify-only
        # re-arm session (§5.2 "Re-verify") maps its single entry
        # {1: PHASE_VERIFY}.
        self._journey = CommissionJourney(
            JourneyPlan.from_index_map(
                index_phase_map if index_phase_map is not None else _INDEX_PHASE,
                post_apply_verifies=post_apply_verifies,
            ),
            accepted_phases=accepted_phases,
            applied=applied,
        )
        self._gain_plan_db = dict(gain_plan_db) if gain_plan_db else None
        # CHECK's measured room floor, held from CHECK's accept until MEASURE
        # reads it in ``_measure_priors`` (issue #1830).
        #
        # **In-memory only — deliberately neither a constructor argument nor
        # part of the snapshot**, unlike ``_gain_plan_db`` beside it. The
        # ordinary path is one conductor per run consuming CHECK then
        # MEASURE, so the field is simply there. A conductor REHYDRATED past
        # an accepted CHECK (same session id, gain plan restored — the shape
        # that can compose a MEASURE program without re-running CHECK) has no
        # ambient of its own, and MEASURE's SNR verdict is then honestly
        # ABSENT rather than graded against a floor this conductor never
        # measured. That is the deliberate trade: an ambient report is a claim
        # about this room at this mic position, and §5.6's binding rule
        # invalidates CHECK/MEASURE evidence across sessions for exactly that
        # reason — so a persisted floor could outlive the position it
        # describes. A missing verdict says "not measured"; a stale one would
        # say something false. Pinned by
        # ``test_measure_priors_carry_no_ambient_when_check_never_ran``.
        self._check_ambient_report: dict[str, Any] | None = None
        # Retained per-position evidence, in capture order, keyed by group
        # phase. The ASSEMBLY SEAM for PR-4: this list is the input
        # ``combine_positions`` consumes, and PR-4 extends the pipeline that
        # reads it (nulls → spec → persistence) without changing what PR-3b
        # puts in it. Bounded by the plan's own entry count.
        self._group_positions: dict[str, list[_CloudPosition]] = {
            phase: [] for phase in self._journey.plan.group_indexes
        }
        # R16's lateral walk keeps its OWN retention rather than joining the
        # dict above, because a pose is per-driver evidence and a cloud position
        # is one summed curve — putting the two in one list would give the
        # combiner an input it cannot combine. Idempotent per index exactly like
        # ``_group_positions``: a retake replaces its pose. Bounded by
        # ``LATERAL_POSE_PROMPTS``; each pose holds two curves on the fixed
        # ~120-point basis, so the whole walk is a few thousand complex values.
        self._lateral_poses: list[LateralPose] = []
        # WO-1: the per-position evidence metadata handed to the retention
        # seam, kept by position id so the group close can serialize the
        # members alongside the aggregate (attribution plan §6 / §11.1 A7).
        # Small and bounded — one flat dict of scalars per accepted position.
        #
        # It tracks ``_group_positions`` on REPLACEMENT (a retake overwrites
        # its position id, so this never holds two records for one prompted
        # spot) but NOT on REMOVAL: the geometry-retry branch drops a take
        # from ``_group_positions`` and leaves its record here. That is
        # harmless rather than merely tolerated — the serializer joins on
        # ``combined.position_ids``, which is built from the retained
        # positions, so an orphaned record is never read. Do not "fix" it by
        # pruning in the retry branch without checking that: the retake
        # normally arrives and overwrites the record anyway.
        self._group_position_meta: dict[str, dict[str, dict[str, Any]]] = {
            phase: {} for phase in self._journey.plan.group_indexes
        }
        # Geometry-locked retakes already spent, per group — the bound behind
        # "up to GEOMETRY_RETRY_POSITIONS extra positions, ONCE".
        self._geometry_retries_used: dict[str, int] = {
            phase: 0 for phase in self._journey.plan.group_indexes
        }
        # The group's closing geometry verdict, as a plain dict for the host to
        # persist/disclose. ``None`` until the group closes.
        self._group_geometry: dict[str, dict[str, Any]] = {}
        # PR-4: the group's closing honest-instrument pipeline result (mask ∪
        # null registry, evaluated spec, geometry guidance copy) -- see
        # assemble_cloud_group_result. Populated the SAME moment as
        # ``_group_geometry`` above, in ``_close_cloud_group``. ``None`` until
        # the group closes, mirroring that dict's own "never confuse
        # not-yet-run with a clean verdict" rule.
        self._group_cloud_result: dict[str, dict[str, Any]] = {}
        # #2291: the LIVE ``FlatSpecReport`` behind ``_group_cloud_result``'s
        # serialized ``spec`` key, per phase. Kept beside it, and populated in
        # the same statement, because the round's spec verdict
        # (``verification.evaluate_spec``) reads ``overall_passed`` and each
        # band's ``evaluable``/``passed`` — structure ``to_dict`` flattens.
        # Not persisted: it is a live object, and the dict is the durable copy.
        self._group_spec_report: dict[str, Any] = {}
        # #1872: which phases' evidence artifact has already been PUBLISHED —
        # the one part of a group close that is a genuine per-phase
        # singleton (the evidence store is write-once; see
        # :meth:`_run_cloud_pipeline`'s own ``publish_cloud`` guard). A
        # RE-close (a voluntary retake, or a geometry-locked retry's own
        # retake landing after the group already accepted via budget
        # exhaustion) still recomputes ``_group_cloud_result`` and re-logs
        # ``cloud_spec`` every time — only the durable write skips a repeat
        # attempt once this set already has the phase. Marked on a
        # SUCCESSFUL publish only, so a transient failure on one close still
        # gets a chance on the next.
        self._group_cloud_published: set[str] = set()
        # The group's most recent COMBINE, held from its geometry close until
        # the household confirms past it (flow-simplification §2.6 —
        # ``confirm_cloud_measure_group``). Only CLOUD_MEASURE ever populates
        # it, because only that group's close fits a correction. Held rather
        # than recomputed because a combine is 2.7-6 s of real operator time
        # (see ``_close_cloud_group``); overwritten if a voluntary retake
        # re-closes the group, so the confirm always fits the newest evidence.
        self._group_combined: dict[str, Any] = {}

        # What this session may play, and how loud — the four declarations the
        # composers read, frozen together so a subset cannot drift (#2291 Phase
        # 5a-ii; ``crossover_v2.programs`` owns the level policy).
        self._excitation = _programs.SessionExcitation(
            roles=self._roles,
            caps_dbfs=self._caps,
            session_volume_db=self._session_volume_db,
            fc_hz=self._fc_hz,
        )
        # Programs — CHECK is composable now; MEASURE waits on the gain solve,
        # VERIFY on Fc (composable now, played only after apply). Composed ONCE
        # and held, because ``program_for_phase`` answers by OBJECT IDENTITY
        # and #2291's before→after comparability depends on it.
        self._check_program = self._excitation.check_program()
        self._measure_program: ExcitationProgram | None = (
            self._excitation.measure_program(self._gain_plan_db)
            if self._gain_plan_db is not None
            else None
        )
        self._verify_program = self._excitation.verify_program()

        # Per-SLOT attempt bookkeeping + the last failure reason. A slot is the
        # phase for a single-capture phase and the ``phase:index`` pair inside a
        # position group (``_slot_of_index``), so a rejected position spends its
        # own retry budget instead of the whole group's.
        #
        # ONE meter per slot (:class:`SlotAttempts`, owner ruling #2086). It
        # replaced a pair — a cumulative attempt count measured against
        # ``ReasonSpec.retry_budget``, plus a per-slot geometry-rejection
        # DISCOUNT that existed only to keep the conductor's own retakes from
        # eating the household's budget. Under one pooled bound the discount has
        # nothing to discount from: a geometry rung spends an extra like any
        # other, it is just booked to the speaker rather than the household.
        self._slot_attempts: dict[str, SlotAttempts] = {}
        self._last_reason: dict[str, str] = {}
        # The capture evidence paired with each slot's last rejection. The
        # global ``_last_failure_*`` pair serves persistence; it cannot answer
        # a replayed begin for an older slot without risking evidence from a
        # different position. Exhaustion reads this slot-owned pair instead.
        self._last_pilot_evidence: dict[
            str, tuple[str, bool | None, bool | None]
        ] = {}
        # Positions the flow GAVE UP on: ``{phase: {index: observed_code}}``.
        # Written when a slot's extras are spent and the group can still
        # proceed without it (``_resolve_spent_slot``), so the group closes with
        # the positions it has instead of the session dying at the mic.
        self._group_unresolved: dict[str, dict[int, str]] = {
            phase: {} for phase in self._journey.plan.group_indexes
        }
        self._armed_index: int | None = None
        # The most recent authorized (index, attempt) — the host reads it to
        # address the terminal ``capture_result`` host event at a play-seam
        # failure (§5.10 / W6.1), so the phone stops waiting instead of
        # recording into silence forever.
        self._armed_capture: tuple[int, int] | None = None
        # MEASURE→VERIFY handoff evidence. A verify-only re-arm session
        # rehydrates both from the persisted state (§5.2 re-verify).
        self._measure_predicted_sum: Any = measure_predicted_sum
        # Two-stage commission D4: the spec verdict for the curve above, graded
        # ONCE by ``_assert_accountable`` against the FULL-RESOLUTION in-memory
        # tuple and held here in its serialized (``FlatSpecReport.to_dict``)
        # form. Carried alongside ``_measure_predicted_sum`` by the same route
        # and for the same reason a verify-only re-arm rehydrates that curve:
        # the re-arm builds a fresh conductor which never runs a fit, so
        # without this the first "Try again" would persist the curve with its
        # verdict silently dropped (the ``cloud`` B1 bug shape).
        #
        # Serialized rather than the live dataclass on purpose: the rehydration
        # route can only ever hand back JSON, so holding one type here keeps
        # this field's readers from having to accept two.
        self._measure_predicted_spec_report: dict[str, Any] | None = (
            dict(measure_predicted_spec_report)
            if isinstance(measure_predicted_spec_report, Mapping)
            else None
        )
        # PR-L5: what the applied correction COMMANDS on the summed response
        # (``_commanded_delta``). Carried alongside ``_measure_predicted_sum``
        # for the same reason and by the same route — the delta probe runs at
        # VERIFY, which a re-arm session reaches with a fresh conductor.
        self._measure_commanded_delta: Any = measure_commanded_delta
        # #2291's "before" measurement. WRITTEN by stage 1, whose
        # ``PHASE_ENTRY_BASELINE`` capture reduces it (``_consume_entry_baseline``);
        # PASSED IN on stage 2, which never captures one and rehydrates it from
        # durable state so the post-apply verdict has something to compare
        # against. Same field, same two routes, same reason as
        # ``_measure_commanded_delta`` directly above.
        self._measure_entry_baseline: "EntryBaseline | None" = measure_entry_baseline
        # #2291's round grading, and its fire-once guard. The round is graded
        # at the point stage 2's post-apply evidence is COMPLETE, which is two
        # different moments on the two tiers (see :meth:`_grade_round_once`),
        # so the guard is what keeps one session to one grading rather than one
        # per trigger that happens to fire.
        self._round_evaluated = False
        self._round_evaluation: Any = None
        # Where this round's receipt landed: round id + the bundle artifact's
        # fingerprint. Persisted, so the NEXT round can find the previous one
        # without scanning bundles. ``None`` until a receipt is written, and it
        # stays ``None`` when writing failed — an identity for a receipt that
        # does not exist would be worse than no identity.
        self._round_receipt_identity: dict[str, Any] | None = None
        # Which arm of ``correction_rollback_failed`` this session's refusal is
        # (#2291): ``True`` a restore attempted against a real anchor and
        # failed, ``False`` there was never an anchor, ``None`` not established
        # — the copy owner treats the last as the Undo arm rather than inventing
        # the more alarming claim about a speaker that may have an anchor.
        self._last_failure_rollback_anchor: bool | None = None
        # The post-apply VERIFY analysis, retained for the Full tier: its round
        # is graded when the post-apply CLOUD closes, which is a later call
        # with no access to the capture the benefit verdict differences.
        self._verify_analysis: ProgramAnalysis | None = None
        # This session's delta-probe verdict, refined once more if a post-apply
        # position group closes (which adds the spatial arm). ``None`` until
        # VERIFY is consumed.
        self._delta_probe: DeltaProbeMap | None = None
        # VERIFY's own measured-vs-predicted curve pair and gated validity
        # floor, held so the post-apply group's close can re-run the probe with
        # the spatial arm without re-analyzing a capture.
        self._verify_tracking_curve: Any = None
        self._verify_validity_floor_hz: float | None = None
        # Each cloud group's across-position level spread, the spatial arm's
        # two inputs. Keyed by phase; absent for a group that never closed and
        # empty for one with fewer than two positions (the express tier's
        # post-apply group is the mark alone by design).
        self._group_band_spread: dict[str, tuple[Any, ...]] = {}
        self._measure_gate_window_ms: float | None = measure_gate_window_ms
        # The accepted MEASURE capture's analysis, held ONLY while something
        # still needs it: from MEASURE's accept until the CLOUD_MEASURE group
        # closes and the fit consumes it (timing move, 2026-07-27 — see
        # ``_measure_verdict``), then released. A session with no cloud group
        # never sets it at all, because that shape fits at MEASURE and consumes
        # the analysis in the same call.
        #
        # **The lifetime is deliberately tight, because the object is not
        # small.** It is dominated by per-occurrence float64/complex128 arrays
        # on the analysis FFT grid, so its size scales with capture length via
        # ``program_analysis._n_fft_for``. Measured 2026-07-27 on the S0
        # corpus's own grid (524,289 bins — a long summed capture): ONE
        # two-occurrence ``DriverResponse`` is 33.6 MB of ndarray payload
        # (4.19 freqs + 4.19 magnitude + 8.39 complex_tf per occurrence). A
        # MEASURE analysis holds one per role with its in-capture repeats
        # attached. That regime is the S0 corpus's, not a production MEASURE's
        # (different program, different grid), and it is quoted to establish
        # the ORDER — tens of megabytes, not kilobytes — on a 1 GB Pi that also
        # retains every cloud position's response for the combine.
        #
        # ``None`` therefore means one of three things, all fine: no MEASURE
        # accepted yet, a session shape that never retains, or an analysis
        # already consumed. Only the FIRST can reach
        # ``_close_measure_cloud_candidate``, and only via a same-session
        # ``hydrate`` — see that method for why production cannot.
        self._measure_analysis: Any = None
        self._candidate: Any = None
        # The #2291 Phase 1 proposal contract for whatever ``_candidate`` holds.
        # Written only by ``commit_intervention_proposal``, so it can never
        # describe a candidate that was planned but not committed.
        self._intervention_proposal: InterventionProposal | PlanRefusal | None = None
        # HAS THE HOUSEHOLD CONFIRMED? — the held-set predicate, decoupled from
        # the fire-once guard above by the eager-fit rider (owner UX direction,
        # 2026-07-30). ``cloud_measure_group_awaiting_confirm`` used to answer
        # it with ``_candidate is None``, which conflated two different
        # questions and made a candidate built EARLY indistinguishable from a
        # household that had moved on — see that method's docstring for the
        # window that conflation would have silently shut.
        self._group_confirmed = False
        # A group close that already ran speculatively, parked until the
        # household confirms (see :class:`_SpeculativeClose`). ``None`` means
        # no eager fit is banked: none was started, one was discarded by a
        # retake, or one already committed.
        self._speculative_close: _SpeculativeClose | None = None
        # Serializes the group-close critical section against the eager fit,
        # which is the ONE piece of this conductor that runs off the relay
        # thread. Three entry points take it — ``_cloud_verdict``'s retain +
        # close, ``run_speculative_group_close``, and
        # ``confirm_cloud_measure_group`` — and none nests inside another, so a
        # plain ``Lock`` is correct AND enforces that: the confirm path reaches
        # ``_close_measure_cloud_candidate``, never the lock-taking
        # ``_close_cloud_group``. A future edit that makes one call another
        # deadlocks loudly here rather than quietly growing a second locking
        # discipline.
        #
        # **What it actually covers**, stated honestly: the two fit inputs the
        # eager path can race — ``_group_combined`` and the
        # ``_group_cloud_result`` its ``_cloud_fit_evidence`` reads, both
        # written by the cloud pipeline inside the same locked region. It does
        # NOT cover ``_measure_analysis``, the fit's first argument, which is
        # written at MEASURE's accept and released by the close. That one is
        # safe by PHASE ORDERING rather than by this lock: it is written on the
        # relay thread eight captures before any cloud group can close, and the
        # only writer after that is the close itself, which holds this lock.
        #
        # **The invariant it buys, stated once.** The combine and the
        # speculative stash are written TOGETHER under this lock, so a banked
        # fit can never outlive the cloud it was fitted from: the only thing
        # that re-stashes the combine is a retake, and it drops the stash in
        # the same locked region. That is why no generation counter is needed
        # to tell a stale bank from a current one — there is no window in
        # which the two can disagree.
        self._close_lock = threading.Lock()
        # Set the instant the household's set-completion signal is admitted,
        # so the seconds the combine + fit spend are a NAMED state rather than
        # indistinguishable from "still waiting for the tap". Never cleared: a
        # close that raises leaves its own failure state, which renders ahead
        # of any phase, and a close that succeeds sets ``_candidate``, which
        # takes precedence in ``cloud_close_state``.
        self._group_close_running = False
        self._verify_outcome: str | None = None  # pass | fail | inconclusive
        # WHICH VERDICT produced that outcome (issue #1974) — written with it,
        # never apart, by ``_set_verify_outcome``. "inconclusive" is reached by
        # two verdicts with no shared mechanism (a too-short gate, and the
        # recording chain moving), and the done screen has to tell a household
        # WHY the check could not settle long after the terminal failure screen
        # has aged out. ``failure.code`` cannot answer that: it is the most
        # recent rejection of ANY phase, and a later persist with no failure
        # nulls it while this outcome stands.
        self._verify_code: str | None = None
        # VERIFY's own gate, reduced to what the screens need (issue #1974 /
        # #1966) — see ``_gate_record``. THE THIRD MEMBER OF THE TRIPLE above:
        # written only by ``_set_verify_outcome``, so it always describes the
        # same capture as the outcome and code beside it. An attempt that
        # early-returns never reaches that method and therefore leaves all
        # three standing together, rather than replacing one of them.
        self._verify_gate: dict[str, Any] | None = None
        # The VERIFY tracking numbers behind the verify_fail screen's collapsed
        # expert disclosure (#1605). Set only once the tolerance comparison is
        # actually reached (the tracking numbers exist); the early-return
        # verdicts (locate/agc/gate/level-shift) leave it None so no half-empty
        # disclosure renders.
        self._verify_evidence: dict[str, Any] | None = None
        # The span that comparison actually graded (issue #1868). Same
        # lifecycle as the evidence beside it — set only when a tracking
        # comparison was reached — but unlike the evidence it is surfaced on
        # EVERY outcome, because a pass is exactly when an unstated band
        # overclaims. See ``_verify_graded_band_from_tracking``.
        self._verify_graded_band_hz: list[float] | None = None
        # The FRAME that comparison spanned — one offset, one tilt (rung P1).
        # Same lifecycle and same every-outcome surfacing rule as the graded
        # band above, and for the same class of reason: the band bounds how
        # WIDE the claim is, this bounds how much of it was the instrument.
        self._verify_frame: dict[str, Any] | None = None
        # The plan §7 claim record — all four entries, on EVERY outcome that
        # reached a grade (R18, #1868). Same lifecycle and surfaced-on-a-pass
        # rule as the two above: "Verified." over no claim list reads as
        # "everything was checked", and two of the four never are.
        self._verify_claims: dict[str, Any] | None = None
        # (``_flatness_evidence`` lived here until PR-5. The flatness a
        # household sees is now the cloud-verify group's spec verdict, read
        # off ``group_cloud_result(PHASE_CLOUD_VERIFY)["flatness"]`` — no
        # per-attempt stash, because it is not a per-attempt claim.)
        self._last_failure_code: str | None = None
        # The pilot evidence belonging to ``_last_failure_code`` (#2085).
        # ALWAYS written together with it — see ``_pilot_heard_for``, which is
        # the only reader and re-checks the pairing rather than trusting it.
        # ``None`` is "no pilot evidence for this failure", which is also the
        # honest value for the failures that never ran a capture at all (an
        # apply-seam refusal, a delta-probe rollback).
        self._last_failure_pilot_heard: bool | None = None
        # G3 (measurement-honesty gate, 2026-07-22) — SESSION-SCOPED since
        # #1927. The FIRST usable VERIFY attempt of THIS conductor's own
        # lifetime records its per-role pilot transfer here, and every LATER
        # attempt of the same session is compared against it. Nothing else
        # ever writes this field: a prior session's numbers have no path into
        # it, by construction rather than by a check.
        #
        # Why (owner ruling, 2026-07-31, option (b) on #1927). Until then a
        # verify-only re-arm (``prepare_v2_verify``) REHYDRATED this from the
        # previous session's persisted ``verify_priors``, so the reference
        # never expired. The gate then conflated two different quantities:
        # within-session chain consistency (its stated purpose — VERIFY
        # replays the identical program through the identical graph, so 0.35
        # dB is a sane ceiling there) and cross-day setup identity, where an
        # ordinary mic re-placement alone plausibly exceeds it. The 2026-07-30
        # bench (#1870 finding 1) measured exactly that: a day-later owed
        # verify stepped 0.775 / 0.777 dB against a rehydrated baseline —
        # deterministic to 0.002 dB, and unescapable, because every retry
        # re-compared against the same frozen number. Re-baselining per
        # session keeps the within-session protection and surrenders the
        # cross-session comparability the gate could never honestly claim.
        self._verify_pilot_baseline: dict[str, float] | None = None
        # WHEN this session set the reference above (epoch float, the same
        # clock and type as the host's persisted ``failure.at``). Stamped in
        # the same statement that sets the baseline, so the two cannot
        # disagree about which attempt the reference came from.
        self._verify_pilot_baseline_at: float | None = None
        # The PREVIOUS session's reference, as dated HISTORY — never a
        # comparator. ``prepare_v2_verify`` threads it so this session can
        # DISCLOSE that it reset the reference and by how much; it is read by
        # ``_verify_verdict`` only to compute that disclosure, and by nothing
        # else. Shape: ``{"values": {role: dB}, "at": epoch}``; absent, empty,
        # or undated leaves the disclosure silent, because an undated record
        # cannot be presented as history without inventing a date (#1942's
        # rule, one field over).
        self._verify_pilot_prior: dict[str, float] | None = None
        self._verify_pilot_prior_at: float | None = None
        if isinstance(verify_pilot_transfer_prior, Mapping):
            prior_values = verify_pilot_transfer_prior.get("values")
            prior_at = verify_pilot_transfer_prior.get("at")
            if isinstance(prior_values, Mapping) and isinstance(prior_at, (int, float)):
                values = {
                    str(role): float(value)
                    for role, value in prior_values.items()
                    if isinstance(value, (int, float))
                }
                # Values AND a date, together or not at all — a half-record
                # would render a disclosure that cannot say when.
                if values:
                    self._verify_pilot_prior = values
                    self._verify_pilot_prior_at = float(prior_at)
        # The disclosure itself, set once by the attempt that establishes this
        # session's reference and only when the prior one differed by more
        # than the gate's own ceiling — one threshold, not a second definition
        # of "materially". ``None`` means there is nothing to say: no prior, no
        # reference yet, or a prior this session's own chain agrees with.
        self._verify_level_reference_reset: dict[str, float] | None = None
        # Transient, recomputed on every VERIFY attempt (never carried
        # forward itself) — this attempt's step vs the baseline above, or
        # ``None`` when there is nothing to compare (no usable pilots this
        # attempt, no shared role with the baseline, or this very attempt is
        # the one that just established the baseline). ``_log_verify_diag``
        # reads it for the ``pilot_transfer_step_db`` diagnostic field.
        self._verify_pilot_transfer_step_db: float | None = None
        # Which (if any) measurement-honesty check produced the LAST MEASURE
        # verdict — reset at the top of every ``_measure_verdict`` call so a
        # stale value from a PRIOR attempt can never leak into this attempt's
        # diagnostic. G2 and the sweep-locate floor reuse an existing reason
        # code shared with a pre-existing check
        # (REASON_DRIFT_BASELINES_DISAGREE / REASON_LOCATE_FAILED), so the
        # reason code alone cannot tell telemetry which check actually fired —
        # this side channel can. Read by ``_log_measure_diag``; never consulted
        # by ``_measure_verdict`` itself, so a bug here cannot change a verdict.
        #
        # NOT every value here is a refusal any more. Since the #2087 ruling G1
        # writes ``ripple_disclosure`` on a capture it ACCEPTS, so a reader
        # must pair this field with ``accepted=`` on the same diag line rather
        # than treating a non-empty value as a rejection. The value was renamed
        # from ``ripple_ceiling`` at that point precisely so the vocabulary
        # cannot be mistaken for its refusing siblings.
        self._last_measure_guard: str = ""
        # G1's banked reservation (owner ruling 2026-08-03, issue #2087), or
        # ``None`` when the accepted MEASURE had nothing to reserve about.
        # ``{"predicted_ripple_db": float, "threshold_db": float}`` — see
        # ``_note_ripple_reservation``.
        #
        # Reset at the top of every ``_measure_verdict`` call, exactly like the
        # two diagnostics above, and that lifecycle is the contract rather than
        # bookkeeping: it makes the reservation describe THE ACCEPTED CAPTURE
        # and no other. A household who re-measures and lands a clean capture
        # has the reservation cleared by that accept — which is correct, and is
        # why this is one record rather than an accumulating list. MEASURE
        # accepts at most once per session (the flow moves on to the cloud
        # group), so the surviving value always belongs to the capture the
        # candidate was built from.
        self._measure_ripple_reservation: dict[str, Any] | None = None

    # --- program composition -------------------------------------------------

    def _compose_measure_program(
        self, gain_plan_db: Mapping[str, float], *, extra_backoff_db: float = 0.0,
    ) -> ExcitationProgram:
        """MEASURE's program at the solved gains.

        A delegate, kept because MEASURE is the one program with a LIFECYCLE:
        it cannot be composed until the CHECK solve produces a plan and it is
        recomposed on the clip-retry rearm, so three call sites here name it.
        CHECK's and VERIFY's composers had one call site each (the constructor)
        and were inlined onto :attr:`_excitation` rather than kept as one-line
        wrappers.
        """
        return self._excitation.measure_program(
            gain_plan_db, extra_backoff_db=extra_backoff_db,
        )

    # --- priors per phase ----------------------------------------------------
    #
    # Thin argument lists over :mod:`jasper.active_speaker.crossover_v2.priors`,
    # which owns every one of the WITHHOLDING decisions their docstrings used to
    # carry. What is left here is this conductor's own reading of its session
    # state — which is exactly the part that could not move.

    def _check_priors(self) -> MeasurementPriors:
        return _priors.check_priors(fc_hz=self._fc_hz)

    def _configured_crossover_transfers(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, int]]:
        return _priors.configured_crossover_transfers(self._preset)

    def _measure_priors(self) -> MeasurementPriors:
        return _priors.measure_priors(
            fc_hz=self._fc_hz,
            source_preset=self._preset,
            protection_sections_by_role=self._measurement_protection_sections_by_role,
            ambient_report=self._check_ambient_report,
            # Derived here rather than there: its producer shares
            # ``_declared_alignment_delay_range_ms`` with the plausibility gate,
            # which is not a priors concern.
            alignment_delay_bounds_us=alignment_delay_search_bounds_us(self._preset),
        )

    def _lateral_priors(self) -> MeasurementPriors:
        return _priors.lateral_priors(
            fc_hz=self._fc_hz, ambient_report=self._check_ambient_report,
        )

    def _measure_sweep_bounds(self) -> tuple[float | None, float | None]:
        return _priors.measure_sweep_bounds(self._measure_program)

    def _verify_priors(self) -> MeasurementPriors:
        return _priors.verify_priors(
            fc_hz=self._fc_hz,
            source_preset=self._preset,
            predicted_sum=self._measure_predicted_sum,
            sweep_bounds=self._measure_sweep_bounds(),
        )

    def _cloud_priors(self) -> MeasurementPriors:
        return _priors.cloud_priors(fc_hz=self._fc_hz)

    def _entry_baseline_priors(self) -> MeasurementPriors:
        return _priors.entry_baseline_priors(fc_hz=self._fc_hz)

    def _fc_candidate_set(self) -> FcCandidateSet:
        """This session's proposable Fc set, from DECLARATIONS only (R17).

        Four bounds, all already owned elsewhere and merely gathered here: the
        HF role's declared hard floor and the lower role's declared ceiling
        (``roles_bands``, which is what ``resolve_driver_excitation_ceilings``
        confirmed), the intersected declared search band, and the beaming
        ceiling from the lower driver's declared diameter.

        The configured Fc is always in the returned set even when every bound
        would exclude it (§9.8, and on live jts3 the ka ceiling genuinely sits
        below it) — otherwise this speaker would have no golden candidate to
        prove equivalence against.

        **``FcSearchBand.band_hz is None`` means NO PROPOSAL, and this is the
        one place that has to know it.** The same ``None`` reaching
        :func:`fc_candidate_set` as ``search_band_hz`` would mean the opposite
        — "no declared band constrains this" — and the set would then be bounded
        only by the excitation bands, proposing frequencies below the tweeter's
        own declaration. Translated here to ``count=0``, so the refusal rides
        the ordinary machinery and the returned ``limits`` still explain the
        bounds rather than vanishing with the proposals.
        """
        search = resolve_fc_search_band(self._crossover_search_band_hz_by_role)
        return fc_candidate_set(
            configured_hz=self._fc_hz,
            hf_hard_floor_hz=self._tweeter.band.lower_hz,
            lower_driver_hard_ceiling_hz=self._woofer.band.upper_hz,
            search_band_hz=search.band_hz,
            lower_driver_diameter_mm=self._radiating_diameter_mm_by_role.get(
                self._woofer.role
            ),
            count=0 if search.band_hz is None else MAX_PROPOSED_FC_CANDIDATES,
        )

    # --- journey delegation --------------------------------------------------

    @property
    def post_apply_verifies(self) -> bool:
        """Will this session's correction be MEASURED after it is applied?

        The boost-permission evidence gate (see the ``FitVocabulary``
        construction in ``_build_candidate``): a round nobody will verify may
        not put energy in. Public because it is a fact ABOUT the journey that
        callers legitimately ask — tests reached the private field for it before
        this property existed.
        """
        return self._journey.plan.post_apply_verifies

    # --- read surfaces -------------------------------------------------------

    @property
    def accepted_phases(self) -> frozenset[str]:
        return self._journey.accepted_phases

    @property
    def attempt_history(self) -> tuple[AttemptRecord, ...]:
        """Accepted applied-candidate attempts, oldest first and bounded."""
        return tuple(self._attempt_history)

    @property
    def last_attempt_decision(self) -> dict[str, Any] | None:
        """The kernel's last decision, or the explicit no-floor status."""
        return (
            dict(self._last_attempt_decision)
            if self._last_attempt_decision is not None else None
        )

    def phase_status(self, phase: str) -> str:
        return self._journey.phase_status(phase)

    @property
    def session_phases(self) -> tuple[str, ...]:
        """The ordered phases this session runs (its ``index_phase_map``'s)."""
        return self._journey.plan.phases

    def pending_phases(self) -> tuple[str, ...]:
        return self._journey.pending_phases()

    def group_geometry(self, phase: str) -> dict[str, Any] | None:
        """The closing geometry verdict for one position group, or ``None``.

        ``None`` means the group has not closed yet (or this session has no
        such group) — never "the geometry was fine", which is
        ``{"locked": False, ...}``.
        """
        verdict = self._group_geometry.get(phase)
        return dict(verdict) if verdict is not None else None

    def group_cloud_result(self, phase: str) -> dict[str, Any] | None:
        """PR-4's honest-instrument pipeline result for one closed group, or
        ``None`` when the group has not closed yet (mirrors
        :meth:`group_geometry`'s own "never confuse not-yet-run with a clean
        verdict" rule).
        """
        result = self._group_cloud_result.get(phase)
        return dict(result) if result is not None else None

    def group_positions(self, phase: str) -> tuple[str, ...]:
        """Accepted position ids in one group, in capture order."""
        return tuple(p.position_id for p in self._group_positions.get(phase, ()))

    def group_position_takes(self, phase: str) -> tuple[dict[str, Any], ...]:
        """The SURVIVING take per position — ``{position_id, index, attempt}``.

        A position id alone is ambiguous once a geometry retake has happened:
        two takes share it, and only one is in the cloud. The attempt
        disambiguates, and it is what joins these entries to the per-take
        evidence artifacts (which are path-qualified by attempt for exactly
        this reason).
        """
        return tuple(
            {"position_id": p.position_id, "index": p.index, "attempt": p.attempt}
            for p in self._group_positions.get(phase, ())
        )

    @property
    def lateral_poses(self) -> tuple[LateralPose, ...]:
        """The accepted lateral walk, in capture order (plan §4.4).

        Empty when the session ran no lateral group, or when every pose was
        dropped. A consumer must treat those two the same way — fewer sampled
        positions than planned — and DISCLOSE it rather than infer robustness
        from evidence it does not have.
        """
        return tuple(self._lateral_poses)

    def lateral_mark_return_drift_db(self) -> dict[str, float] | None:
        """Per-role worst |Δ dB| between the walk's two AT-MARK poses.

        The walk opens and closes at the same spot with the same program, so
        this is one number for "did anything move while the household walked" —
        a nudged stand, a shifted body, a room that changed. Both poses are
        neutral and sampled onto the same basis, so the difference is exactly a
        repeat-measurement difference and nothing else.

        **Reported, never gated.** No evidence in this campaign fixes a
        threshold for it, and inventing one would be a refusal the plan did not
        authorize; the consumer widens its own uncertainty with this number
        instead. ``None`` when either bracket pose is missing — never ``0.0``,
        which would read as "nothing moved".
        """
        opening = next((p for p in self._lateral_poses if p.at_mark), None)
        closing = next(
            (p for p in reversed(self._lateral_poses) if p.at_mark), None
        )
        if opening is None or closing is None or opening.index == closing.index:
            return None
        drift: dict[str, float] = {}
        for first in opening.curves:
            last = closing.curve(first.role)
            if last is None or first.freqs_hz.size != last.freqs_hz.size:
                continue
            lo, hi = first.band_hz
            # Only where BOTH poses were actually driven; outside the sweep band
            # the samples are noise and their difference is noise squared.
            inside = (first.freqs_hz >= lo) & (first.freqs_hz <= hi)
            if not np.any(inside):
                continue
            delta = 20.0 * np.log10(
                np.abs(last.complex_tf[inside]) / np.abs(first.complex_tf[inside])
            )
            finite = delta[np.isfinite(delta)]
            if finite.size:
                drift[first.role] = float(np.max(np.abs(finite)))
        return drift or None

    @property
    def tier(self) -> str:
        """The commission tier this session runs, or ``""`` when undeclared."""
        return self._tier

    @property
    def current_phase(self) -> str:
        return self._journey.current_phase

    @property
    def candidate(self) -> Any:
        return self._candidate

    @property
    def verify_outcome(self) -> str | None:
        return self._verify_outcome

    @property
    def verify_code(self) -> str | None:
        """The reason code behind :attr:`verify_outcome`, or ``None`` on a pass.

        Written with the outcome, never apart — see ``_set_verify_outcome``.
        The host persists the pair so the done screen can name WHY a check came
        back inconclusive (issue #1974) once the terminal failure screen has
        aged out.
        """
        return self._verify_code

    @property
    def verify_gate(self) -> dict[str, Any] | None:
        """VERIFY's gate as the screens need it, or ``None`` (issue #1974).

        ``{"disclosure": str, "reflection_measured": bool}`` — see
        ``_gate_record``. Surfaced on EVERY outcome, for the reason the graded
        band and the frame are: a pass is exactly when nobody asks how much of
        the response the comparison could actually see.

        It describes the capture that produced :attr:`verify_outcome` and
        :attr:`verify_code`, never a later attempt's — the three are one write
        (``_set_verify_outcome``). A screen may therefore pair them freely,
        which is exactly what the done screen's cause copy does.
        """
        return dict(self._verify_gate) if self._verify_gate else None

    @property
    def verify_evidence(self) -> dict[str, Any] | None:
        """The verify_fail expert-disclosure numbers (#1605), or None."""
        return dict(self._verify_evidence) if self._verify_evidence else None

    @property
    def verify_graded_band_hz(self) -> list[float] | None:
        """``[lo, hi]`` VERIFY's tracking comparison graded, or None (#1868).

        Surfaced on every outcome, including a pass — see
        :func:`_verify_graded_band_from_tracking`.
        """
        return list(self._verify_graded_band_hz) if self._verify_graded_band_hz else None

    @property
    def verify_frame(self) -> dict[str, Any] | None:
        """The frame VERIFY's comparison spanned, or None (rung P1).

        Surfaced on every outcome for the same reason the graded band is (see
        :func:`_verify_frame_from_tracking`): a PASS is precisely when an
        undisclosed tilt would let a household — or the next iterate loop —
        read model agreement into what was partly instrument.
        """
        return dict(self._verify_frame) if self._verify_frame else None

    @property
    def verify_claims(self) -> dict[str, Any] | None:
        """The plan §7 claim record, or ``None`` (R18, #1868) — surfaced on
        every outcome including a pass, because two of its four entries are
        structurally not-evaluated and a household reading "Verified." has no
        other way to learn that. See :func:`_verify_claims`.
        """
        return dict(self._verify_claims) if self._verify_claims else None

    @property
    def applied(self) -> bool:
        return self._journey.applied

    @property
    def measure_predicted_sum(self) -> Any:
        return self._measure_predicted_sum

    @property
    def measure_predicted_spec_report(self) -> dict[str, Any] | None:
        """The spec verdict for :attr:`measure_predicted_sum`, or ``None``.

        Two-stage commission D4's "grade once" half. ``None`` means the
        prediction could not be graded — **never that it passed**; the host
        persists it verbatim and every surface that renders it must treat
        absence as absence (see :func:`spec_report_for_predicted_sum`'s own
        unknown-is-not-permission rule).

        The report is graded against the full-resolution tuple, which is the
        whole point of stashing it: what SURVIVES to the durable state is
        ``_decimate_sum``'s 512-point block average (issue #1858 — a raw
        stride before that fix), and re-grading that would be a different
        instrument from the one the accountability veto refused on.
        """
        return (
            dict(self._measure_predicted_spec_report)
            if self._measure_predicted_spec_report is not None
            else None
        )

    @property
    def measure_commanded_delta(self) -> Any:
        return self._measure_commanded_delta

    @property
    def round_receipt_identity(self) -> dict[str, Any] | None:
        """Where this session's round receipt landed, or ``None`` (#2291).

        ``{round_id, artifact_fingerprint, receipt_fingerprint}``. Persisted by
        the host into the durable state so the NEXT round can resolve the
        previous one by identity instead of scanning bundles. ``None`` means no
        receipt was written — an unwired seam, or a write that failed — and is
        never a claim that one exists.
        """
        record = self._round_receipt_identity
        return dict(record) if isinstance(record, Mapping) else None

    @property
    def round_evaluation(self) -> Any:
        """This session's graded round, or ``None`` before it is graded."""
        return self._round_evaluation

    @property
    def measure_entry_baseline(self) -> "EntryBaseline | None":
        """#2291's pre-apply side of this round, or ``None``.

        ``None`` is the honest "this round has no comparable before" —
        :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_BASELINE_UNAVAILABLE`,
        which is INDETERMINATE and never a pass. Reached three ways: a stage-1
        session whose entry-baseline capture has not landed (or was rejected),
        a stage 2 the host rehydrated nothing into, and a state file written by
        a build before the key shipped.

        The record is frozen and its fields are immutable, so this returns it
        directly rather than copying like ``measure_predicted_spec_report``.
        """
        return self._measure_entry_baseline

    @property
    def last_intervention_proposal(self) -> "InterventionProposal | PlanRefusal | None":
        """The #2291 contract for the committed candidate, or why there is none.

        ``None`` before any candidate is committed; an
        :class:`~jasper.active_speaker.crossover_v2.contracts.InterventionProposal`
        after; a
        :class:`~jasper.active_speaker.crossover_v2.contracts.PlanRefusal` when
        the committed candidate could not satisfy the contract.  Both the
        configured-Fc walk and the alternative-Fc selection produce it through
        the same :meth:`commit_intervention_proposal` seam.

        Read-only and immutable, so unlike the ``_last_*`` scratch fields this
        cannot be a caller's return channel.
        """
        return self._intervention_proposal

    @property
    def delta_probe(self) -> DeltaProbeMap | None:
        """This session's realized-vs-commanded verdict (PR-L5), or ``None``
        when no post-apply capture has been consumed yet."""
        return self._delta_probe

    @property
    def measure_gate_window_ms(self) -> float | None:
        return self._measure_gate_window_ms

    @property
    def measure_ripple_reservation(self) -> dict[str, Any] | None:
        """G1's banked reservation about the accepted MEASURE, or ``None``.

        ``{"predicted_ripple_db": float, "threshold_db": float}`` — the host
        persists it, ``crossover_v2_status_block`` projects it, and the
        envelope turns it into one household sentence plus one expert line.
        ``None`` means the accepted capture summed as coherently as the
        calibration corpus did, and the screens then say nothing at all: a
        clean measurement has no reservation, and inventing a "no concerns"
        line would spend a household's attention on a non-event.

        Copied on the way out, like every other dict-valued property on this
        class, so a caller cannot reach back into the conductor's state.
        """
        reservation = self._measure_ripple_reservation
        return dict(reservation) if reservation else None

    @property
    def verify_pilot_transfer_reference(self) -> Mapping[str, Any] | None:
        """This session's own G3 reference, DATED, for the host to persist.

        ``{"values": {role: dB}, "at": epoch}``, or ``None`` when no usable
        VERIFY attempt established one. The date is what lets the NEXT
        session's disclosure name when this reference was taken; a record that
        travels without it cannot be shown as history (#1942), so the two are
        one value rather than two keys that could be written apart.

        The next session receives this as ``verify_pilot_transfer_prior`` and
        may only DISCLOSE it. There is no longer a constructor argument that
        can make a previous session's numbers this session's comparator.
        """
        if self._verify_pilot_baseline is None or self._verify_pilot_baseline_at is None:
            return None
        return {
            "values": dict(self._verify_pilot_baseline),
            "at": self._verify_pilot_baseline_at,
        }

    @property
    def verify_level_reference_reset(self) -> Mapping[str, float] | None:
        """This session's level-reference reset, when it is worth disclosing.

        ``{"prior_at": epoch, "step_db": float}`` when this session set its own
        reference and the previous one differed by more than the gate ceiling;
        ``None`` otherwise. Report-only — see ``_note_level_reference_reset``.
        """
        return (
            dict(self._verify_level_reference_reset)
            if self._verify_level_reference_reset is not None
            else None
        )

    @property
    def last_failure_code(self) -> str | None:
        """The most recent rejection's reason code (host persistence reads it)."""
        return self._last_failure_code

    @property
    def last_failure_pilot_heard(self) -> bool | None:
        """Pilot evidence for :attr:`last_failure_code` — the host persists it.

        The envelope re-renders the failure's sentence from the pair (#2085),
        so both halves have to survive the session that produced them.

        **This getter checks nothing**, and an earlier docstring here claiming
        it "re-checks the pairing" was wrong: the two attributes are written
        together, so validating one against the other compares a value with
        itself. The pairing that CAN diverge is between this evidence and the
        code a caller chooses to persist — several terminal arms pass a
        ``failure_code`` the capture loop never produced — and that check
        belongs to the caller, which has the differing value.
        ``persist_conductor_state`` makes it.
        """
        return self._last_failure_pilot_heard if self._last_failure_code else None

    @property
    def last_failure_rollback_anchor(self) -> bool | None:
        """Which ``correction_rollback_failed`` arm this failure is (#2291).

        ``True`` a restore ran against a real anchor and did not complete;
        ``False`` there was never an anchor to restore to; ``None`` the
        question does not apply to this code, or predates the record.

        Paired with :attr:`last_failure_code` on exactly
        :attr:`last_failure_pilot_heard`'s terms — written together, gated on
        the code being present, and persisted so the envelope can re-render
        the right sentence in a later process.
        """
        return self._last_failure_rollback_anchor if self._last_failure_code else None

    def _pilot_heard_for(
        self, code: str | None, *, slot: str | None = None,
    ) -> bool | None:
        """The pilot evidence recorded WITH ``code``, else ``None`` (#2085).

        With ``slot``, reads the pair owned by that capture position; without
        it, reads the global pair used by persisted terminal state. Both forms
        re-check the code because the failure being described is not always
        the failure last consumed — :meth:`_refuse` can name a code the capture
        loop never produced, and a replayed begin can address an older slot.
        Attaching one capture's evidence to another's code would put a
        confident, wrong sentence in front of a household. An unknown pairing
        degrades to the registry copy, which claims nothing unmeasured.
        """
        if slot is not None:
            paired = self._last_pilot_evidence.get(slot)
            if code is None or paired is None or paired[0] != code:
                return None
            return paired[1]
        if code is None or code != self._last_failure_code:
            return None
        return self._last_failure_pilot_heard

    def _reflection_measured_for(
        self, code: str | None, *, slot: str,
    ) -> bool | None:
        """The gate discriminator recorded with ``code`` at ``slot``."""
        paired = self._last_pilot_evidence.get(slot)
        if code is None or paired is None or paired[0] != code:
            return None
        return paired[2]

    @property
    def armed_capture(self) -> tuple[int, int] | None:
        """The last authorized ``(index, attempt)`` — the host addresses the
        terminal ``capture_result`` host event at a play-seam failure to it."""
        return self._armed_capture

    def _phase_of_index(self, index: int) -> str:
        phase = self._journey.plan.phase_for_index(index)
        if phase is None:
            raise CrossoverV2FlowError(f"no v2 phase for capture index {index}")
        return phase

    def _slot_of_index(self, index: int) -> str:
        """The retry-budget key for one capture index.

        For every single-capture phase this is the phase name itself, so the
        CHECK/MEASURE/VERIFY bookkeeping is byte-identical to the pre-cloud
        flow. Inside a position group it is ``phase:index``: eight prompted
        positions are eight independent captures, and collapsing them onto one
        cumulative counter would let a retake at position 2 refuse position 7.
        """
        phase = self._phase_of_index(index)
        return f"{phase}:{index}" if phase in GROUP_PHASES else phase

    def _cloud_prompt(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt for one group index — the SAME table the plan emitted.

        A group's first PROMPTED index is its anchor's first move, so the
        group's indexes map onto :data:`CLOUD_POSITION_PROMPTS` from the front:
        the group's ``i``-th index (0-based) takes ``CLOUD_POSITION_PROMPTS[i]``,
        exactly as ``build_v2_capture_plan`` enumerates them. Running off the
        end cannot happen (``_validated_cloud_counts`` refuses a group longer
        than the table), but a defensive fallback keeps a prompt-less capture
        from being a crash rather than a retake.
        """
        offsets = self._journey.plan.group_offsets(phase)
        try:
            position = offsets.index(index)
        except ValueError:
            position = 0
        # R16: the lateral walk has its own (derived) table and its own length.
        # Same front-loading rule, same builder enumeration order.
        table = (
            LATERAL_POSE_PROMPTS if phase == PHASE_LATERAL
            else CLOUD_POSITION_PROMPTS
        )
        if position < len(table):
            return table[position]
        # A DISTINCT defensive spot, not a repeat of a table row: this fallback
        # only fires for a group longer than the table (which
        # ``_validated_cloud_counts`` refuses, so it is unreachable today), and
        # a group that long has already walked every row. Naming one of them
        # again would send the operator back to a spot the cloud has, which is
        # the one thing an extra position must not do. 45 cm right is past the
        # table's widest RIGHT offset (40 cm) and inside the geometry rung's.
        return _pose(_LATERAL_POSE, 45.0, POSITION_ROLE_OFFAX, side="RIGHT")

    def _prompt_shown_for(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt the operator ACTUALLY followed for the take in hand.

        Not always the table entry: after a geometry-locked rejection the phone
        showed a wider-spot retry rung instead, so a retake's evidence must
        record THAT instruction — the sidecar's prompt is the only durable
        statement of where a curve was measured, and one that names a spot the
        operator was told to abandon is worse than none.

        ``_last_reason`` still holds the rejection that produced this retake
        (``consume_capture`` clears it only on acceptance), and
        ``_geometry_retries_used`` counts the rung that was shown, so the pair
        identifies the instruction exactly. A wider-spread rung is ``wide`` by
        construction — it asks for :data:`GEOMETRY_RETRY_OFFSET_CM`, past the
        wide class by design, and ``wide`` is computed from that distance.
        """
        slot = self._slot_of_index(index)
        if self._last_reason.get(slot) == REASON_CLOUD_GEOMETRY_LOCKED:
            used = max(self._geometry_retries_used.get(phase, 1), 1)
            rung = CLOUD_GEOMETRY_RETRY_PROMPTS[
                min(used - 1, len(CLOUD_GEOMETRY_RETRY_PROMPTS) - 1)
            ]
            return CloudPositionPrompt(
                rung,
                offset_cm=GEOMETRY_RETRY_OFFSET_CM,
                role=POSITION_ROLE_OFFAX,
            )
        return self._cloud_prompt(phase, index)

    # --- lifecycle -----------------------------------------------------------

    def note_apply_complete(self) -> None:
        """The apply-complete host event — arms the soft-held VERIFY (§5.2)."""
        self._journey.mark_applied()
        log_event(
            logger, "correction.crossover_v2_apply_complete",
            session_id=self.session_id,
        )

    def _apply_observed(self) -> bool:
        if self._journey.applied:
            return True
        try:
            observed = bool(self._seams.apply_complete())
        except (OSError, RuntimeError, ValueError):
            observed = False
        if observed:
            self._journey.mark_applied()
        return observed

    def snapshot(self) -> V2ConductorSnapshot:
        return V2ConductorSnapshot(
            session_id=self.session_id,
            accepted_phases=self._journey.accepted_capture_phases(),
            session_phases=self._journey.plan.phases,
            applied=self._journey.applied,
            gain_plan_db=dict(self._gain_plan_db) if self._gain_plan_db else None,
            candidate_fingerprint=(
                getattr(self._candidate, "fingerprint", None)
                if self._candidate is not None else None
            ),
            tier=self._tier,
            cloud_close=self.cloud_close_state,
            attempt_history=tuple(self._attempt_history),
            last_attempt_decision=self._last_attempt_decision,
        )

    @classmethod
    def hydrate(
        cls,
        snapshot: V2ConductorSnapshot | None,
        *,
        session_id: str,
        **kwargs: Any,
    ) -> "CrossoverV2Conductor":
        """Rebuild a conductor, applying the §5.6 session-binding rule.

        Same session ⇒ resume, keeping the accepted phases + gain plan (skips
        accepted phases). A different or absent session ⇒ fresh start at CHECK
        (CHECK/MEASURE evidence invalidated — mic position is unverifiable
        across sessions).
        """
        journey: dict[str, Any] = {}
        if snapshot is not None:
            journey = {
                "attempt_history": snapshot.attempt_history,
                "last_attempt_decision": snapshot.last_attempt_decision,
            }
        # Explicit caller values win for migrations/tests that deliberately
        # replace one journey fact; ordinary production hydration supplies
        # none and receives the durable snapshot wholesale.
        journey.update({
            key: kwargs.pop(key)
            for key in tuple(journey)
            if key in kwargs
        })
        if snapshot is not None and snapshot.session_id == session_id:
            return cls(
                session_id=session_id,
                accepted_phases=snapshot.accepted_phases,
                applied=snapshot.applied,
                gain_plan_db=snapshot.gain_plan_db,
                **journey,
                **kwargs,
            )
        if snapshot is not None:
            log_event(
                logger, "correction.crossover_v2_session_rebound",
                level=logging.INFO,
                prior_session=snapshot.session_id,
                session_id=session_id,
            )
        return cls(session_id=session_id, **journey, **kwargs)

    # --- relay callbacks -----------------------------------------------------

    def authorize_begin(self, index: int, attempt: int, entry: Any = None) -> None:
        """Admit (or defer / refuse) one phone ``begin_capture`` (§5.7).

        VERIFY is soft-held (:class:`CaptureBeginDeferred`) until an apply is
        observed. **Since the two-stage split (work order D10) no shipped
        session reaches that hold**: stage 1 has no VERIFY index at all, and
        stage 2's conductor is constructed ``applied=True``, so
        ``_apply_observed`` short-circuits before either the deferral or the
        ``apply_failed`` refusal below. The machinery is retained rather than
        deleted — no new design may depend on it, and a conductor built without
        a prior apply still gets the honest hold. If the auto-apply hit a
        TERMINAL failure (``seams.apply_failed()`` names a reason), the hold is
        refused outright rather than held toward a dishonest relay_timeout — the
        household sees the real reason, not a manufactured "link timed out."
        Every other begin is admitted.

        **Retry exhaustion does NOT normally arrive here** (owner ruling
        #2086). A slot that has spent its extras is settled at the verdict that
        spent the last one — ``_resolve_spent_slot`` either drops the position
        and advances the group, or names the honest end — so the household is
        never handed a "try again" screen whose button is about to end the
        session. The refusal below is the backstop for a begin that reaches a
        settled slot anyway (a page that ignored the verdict, a replayed
        event), and its copy says the tries are gone rather than inviting one
        more.
        """
        phase = self._phase_of_index(index)
        if phase == PHASE_VERIFY and not self._apply_observed():
            failure_code = ""
            try:
                failure_code = str(self._seams.apply_failed() or "")
            except (OSError, RuntimeError, ValueError):
                failure_code = ""
            if failure_code:
                self._last_failure_code = failure_code
                # The apply seam's own verdict — no capture ran, so there is
                # no pilot evidence to pair with it (#2085). Written rather
                # than left alone so a previous capture's evidence cannot
                # trail into this failure's copy.
                self._last_failure_pilot_heard = None
                spec = REASON_REGISTRY.get(failure_code)
                message = reason_message(failure_code, spec) if spec else failure_code
                self.relay_published_refusal = True
                raise CaptureBeginRefused(failure_code, message)
            raise CaptureBeginDeferred("awaiting_apply", VERIFY_ANCHOR_HOLD_MESSAGE)
        # ONE pooled meter per slot: the planned capture, then at most
        # MAX_EXTRA_ATTEMPTS_PER_POSITION extras, whoever asks for them. The
        # first attempt of any slot is always admitted and always free.
        slot = self._slot_of_index(index)
        ledger = self._slot_attempts.setdefault(slot, SlotAttempts())
        if ledger.admitted:
            last = self._last_reason.get(slot)
            if last in NON_RETRIABLE_CODES:
                # Not exhaustion — a condition another take cannot clear. Its
                # own copy already names the one action that helps, so it is
                # published unchanged (and it never promised "measure again").
                spec = REASON_REGISTRY[last]
                self.relay_published_refusal = True
                raise CaptureBeginRefused(
                    spec.code,
                    reason_message(
                        spec.code,
                        spec,
                        pilot_heard=self._pilot_heard_for(last, slot=slot),
                    ),
                )
            if ledger.extras_left <= 0:
                code = last or REASON_LOCATE_FAILED
                spec = REASON_REGISTRY[code]
                diagnosis = reason_diagnosis(
                    code,
                    spec,
                    pilot_heard=self._pilot_heard_for(code, slot=slot),
                    reflection_measured=self._reflection_measured_for(
                        code, slot=slot,
                    ),
                )
                self.relay_published_refusal = True
                raise CaptureBeginRefused(
                    # ATTRIBUTE: the code the household is told about, and the
                    # one ``_persist_terminal_failure`` records, is the
                    # condition actually observed here — never a generic
                    # exhaustion code that would erase what went wrong.
                    code,
                    self._extras_spent_message(
                        ledger,
                        diagnosis=diagnosis,
                        outcome=self._spent_slot_outcome(phase, index),
                    ),
                )
            ledger.spend(self._extra_initiator(slot))
        ledger.admitted += 1
        self._armed_index = index
        self._armed_capture = (index, attempt)
        log_event(
            logger, "correction.crossover_v2_authorized",
            session_id=self.session_id, phase=phase, index=index, attempt=attempt,
            # The same numbers the household reads, in the journal (ruling item
            # 2). ``attempt`` alone is the PLAN's running counter — it was 12
            # while the screen said "step 6" on 2026-08-03 — so it cannot tell a
            # support read how many tries this POSITION has had.
            extra_used=ledger.extras_used,
            extra_allowed=MAX_EXTRA_ATTEMPTS_PER_POSITION,
            extra_by_speaker=ledger.by_speaker,
        )

    def _extra_initiator(self, slot: str) -> str:
        """Who is asking for the extra attempt about to be admitted.

        Read off the rejection that kept the plan alive at this slot, because
        that is the only place the distinction is visible: a geometry rung is
        the conductor demanding a wider take of a capture that was otherwise
        fine, and it travels the ordinary begin path with ``retake=false``
        (see :data:`GEOMETRY_RETRY_POSITIONS` — rejecting is the only lever
        that keeps a fixed-length plan on the same index). Everything else —
        a "Try again" after a quality rejection, a voluntary retake — is the
        household choosing to spend one.
        """
        return (
            ATTEMPT_INITIATOR_SPEAKER
            if self._last_reason.get(slot) == REASON_CLOUD_GEOMETRY_LOCKED
            else ATTEMPT_INITIATOR_HOUSEHOLD
        )

    @staticmethod
    def _extras_spent_message(
        ledger: SlotAttempts, *, diagnosis: str, outcome: str,
    ) -> str:
        """The household sentence for a position whose extras are gone.

        Keeps an evidence-derived diagnosis when one exists, then names the
        count and terminal outcome. It deliberately does NOT reuse the full
        registry ``message``: retriable rows end by inviting an action the
        flow will no longer grant.
        """
        used = ledger.extras_used
        tries = "try" if used == 1 else "tries"
        count = (
            f"JTS measured this spot {ledger.admitted} times — the planned one "
            f"plus {used} extra {tries} — and still could not get a clean read."
        )
        return " ".join(part for part in (diagnosis, count, outcome) if part)

    def _spent_slot_outcome(self, phase: str, index: int) -> str:
        """The state after an exhausted slot, derived from conductor state."""
        if self._journey.plan.is_group(phase):
            if index in self._group_unresolved[phase]:
                return "This position was left out and the group continued."
            if index in self._retained_group_indexes(phase):
                return (
                    "JTS kept the earlier measurement for this position and "
                    "the group continued."
                )
            return (
                "The measurement cannot continue because too few positions "
                "produced a clean read."
            )
        return "The measurement cannot continue because this step needs a clean read."

    def on_armed(self, state: Any = None) -> None:
        """Play the armed phase's excitation program (the host stimulus)."""
        index = self._armed_index
        if index is None:
            raise CrossoverV2FlowError("on_armed with no authorized capture")
        phase = self._phase_of_index(index)
        program = self.program_for_phase(phase)
        log_event(
            logger, "correction.crossover_v2_play",
            session_id=self.session_id, phase=phase, program_id=program.program_id,
        )
        self._seams.play(phase, program)

    def program_for_phase(self, phase: str) -> ExcitationProgram:
        """The composed program this session plays for ``phase``.

        Public because it is the only honest way to ask "what will this
        conductor play", which the host's duration budgeting, the identity
        invariant, and every test that synthesises a capture all need. The
        answer is BY IDENTITY — see
        :func:`jasper.active_speaker.crossover_v2.programs.program_for_phase`,
        which owns the mapping and the reason the summed-sweep phases must all
        receive one object rather than three equal ones.
        """
        try:
            return _programs.program_for_phase(
                phase,
                check=self._check_program,
                measure=self._measure_program,
                verify=self._verify_program,
            )
        except _programs.NoProgramForPhaseError as exc:
            # The flow's own error type is what every caller (and the relay
            # runner above them) already handles; the selector is pure and has
            # no business knowing it.
            raise CrossoverV2FlowError(str(exc)) from exc

    def consume_capture(
        self, index: int, attempt: int, result: Any, entry: Any = None,
    ) -> dict[str, Any]:
        """Analyze one uploaded capture and advance (or reject) the phase."""
        phase = self._phase_of_index(index)
        slot = self._slot_of_index(index)
        program = self.program_for_phase(phase)
        priors = (
            self._measure_priors() if phase == PHASE_MEASURE
            else self._verify_priors() if phase == PHASE_VERIFY
            else self._lateral_priors() if phase == PHASE_LATERAL
            else self._cloud_priors() if phase in GROUP_PHASES
            else self._entry_baseline_priors() if phase == PHASE_ENTRY_BASELINE
            else self._check_priors() if phase == PHASE_CHECK
            else MeasurementPriors()
        )
        # The whole CaptureResult crosses the seam (not just wav bytes): the
        # production analyze binding resolves the mic calibration from the
        # phone-reported setup/device, and the conductor's declared geometry
        # rides along so the parallax correction reaches the analysis.
        # ``phase=phase`` (issue #1855): the flow's OWN phase, threaded
        # explicitly because ``program.phase`` is not a reliable stand-in —
        # every cloud position plays ``self._verify_program`` and so always
        # carries ``program.phase == "verify"`` (see ``program_for_phase``).
        analysis = self._seams.analyze(
            program, result, priors, self._geometry, phase=phase,
        )
        if phase == PHASE_CHECK:
            verdict = self._consume_check(analysis)
        elif phase == PHASE_MEASURE:
            verdict = self._consume_measure(analysis)
            # R17's candidate sweep, HERE and nowhere later: ``result`` is the
            # raw capture, and it is alive only inside this call. What the
            # conductor retains past it are derived ``DriverResponse``s, which
            # §4.2's conditioning policy refuses to un-compose. Only on an
            # accepted MEASURE that a walk will follow — a rejected capture has
            # no evidence to adjudicate from, and a session with no walk has no
            # lateral robustness term and no close to adjudicate at.
            if verdict.accepted and PHASE_LATERAL in self._journey.plan.phases:
                self._sweep_fc_candidates(program, result, analysis)
        elif phase == PHASE_LATERAL:
            verdict = self._consume_lateral_pose(index, attempt, analysis)
        elif phase in GROUP_PHASES:
            verdict = self._consume_cloud_position(
                phase, index, attempt, analysis, result
            )
        elif phase == PHASE_ENTRY_BASELINE:
            # Explicit, ahead of the catch-all below, because that ``else``
            # routes anything unrecognised into ``_consume_verify`` — which
            # would grade #2291's pre-apply capture as a post-apply tracking
            # result, bank it as a tuning attempt, and do it silently.
            verdict = self._consume_entry_baseline(index, attempt, analysis, result)
        else:
            verdict = self._consume_verify(analysis, attempt=attempt)
        # THIS capture's pilot evidence, attached to whatever verdict came back
        # — at ONE point, deliberately, rather than at each gate that can
        # produce ``locate_failed``. Three separate gates already refuse on a
        # locate-confidence floor (``_stimulus_locate_ok``,
        # ``_sweep_locate_confidence_ok``, VERIFY's ``summed_sweep_heard``
        # integrity check), they sit in three different verdict methods, and a
        # fourth is a plausible addition; per-gate assignment would make
        # "remembered to carry it" a condition of the copy being honest. Here
        # it cannot be forgotten, and the fact is the same one regardless of
        # which floor fired: did the pilot pair clear the room in this
        # recording. See ``locate_failed_message``.
        reflection_measured: bool | None = None
        if verdict.code == REASON_VERIFY_INCONCLUSIVE:
            gate_record = _gate_record(analysis.summed_response)
            if gate_record is not None:
                reflection_measured = bool(gate_record["reflection_measured"])
        verdict = replace(
            verdict,
            pilot_heard=analysis.pilot_snr_ok,
            reflection_measured=reflection_measured,
        )
        if not verdict.accepted and verdict.code is not None:
            # Recorded BEFORE the settle so both readers see it: the settle's
            # own attribution fallback, and ``_extra_initiator`` at the next
            # begin (a geometry rung is only identifiable from the rejection
            # that produced it).
            self._last_reason[slot] = verdict.code
            self._last_pilot_evidence[slot] = (
                verdict.code,
                verdict.pilot_heard,
                verdict.reflection_measured,
            )
            # SETTLE HERE, not at the next begin (owner ruling #2086 item 3).
            # If this rejection spent the slot's last extra, the position is
            # decided now — dropped and the group advanced, or the honest end
            # named — so the household is never shown a retry screen whose
            # button only leads to a pre-play refusal.
            verdict = self._resolve_spent_slot(phase, index, slot, verdict)
        if verdict.accepted:
            # A position group's PHASE is accepted only when its last index is
            # in; a single-capture phase closes on its own acceptance. Both
            # cases route through ``_note_accepted`` so there is one place that
            # decides "this phase is done."
            self._note_accepted(phase, index)
            # A clean acceptance supersedes the slot's rejection. A settled
            # exhaustion remains paired for the defensive replay/backstop;
            # the group has advanced, so retaining it cannot spend a new slot.
            if not (
                "unresolved" in verdict.payload
                or verdict.payload.get("kept_earlier_take") is True
            ):
                self._last_reason.pop(slot, None)
                self._last_pilot_evidence.pop(slot, None)
            self._last_failure_code = None
            self._last_failure_pilot_heard = None
        elif verdict.code is not None:
            # Re-read off the FINAL verdict: a settled close can substitute a
            # product refusal (the CLOUD_VERIFY delta probe) for the quality
            # rejection that got here, and the attributed code must be the one
            # actually returned.
            self._last_reason[slot] = verdict.code
            self._last_failure_code = verdict.code
            # Cleared together with the code above and set together with it
            # here: the envelope renders the persisted failure's sentence from
            # this pair, and a discriminator outliving its code would describe
            # one capture with another's evidence.
            self._last_failure_pilot_heard = verdict.pilot_heard
        # Every verdict carries the position's honest count (ruling item 2), so
        # the phone can say "extra try 2 of 3" instead of repeating "one more
        # time" while a hidden meter runs. Stamped once, here, rather than in
        # each ``_consume_*``: the ledger is this method's bookkeeping, and one
        # writer is what keeps the number the phone renders and the number the
        # journal logs from drifting.
        verdict = self._with_attempt_payload(slot, verdict)
        log_event(
            logger, "correction.crossover_v2_result",
            session_id=self.session_id, phase=phase,
            accepted=verdict.accepted, code=verdict.code or "",
            # The discriminator behind the sentence the household just read
            # (#2085). Without it the journal shows four identical
            # ``code=locate_failed`` lines for what were, on the JTS3 session
            # that filed this, one genuinely-quiet capture and three whose
            # pilot pair was heard fine — indistinguishable without opening
            # the WAVs. ``pilot_heard=`` is emitted on accepted captures too,
            # so a reader can see the fact was established rather than
            # guessing whether an absent field means unheard or unlogged.
            pilot_heard=verdict.pilot_heard,
        )
        return verdict.to_relay_dict()

    def _with_attempt_payload(
        self, slot: str, verdict: PhaseVerdict
    ) -> PhaseVerdict:
        ledger = self._slot_attempts.get(slot)
        if ledger is None:
            return verdict
        return replace(
            verdict, payload={**verdict.payload, "attempts": ledger.to_payload()}
        )

    def _resolve_spent_slot(
        self, phase: str, index: int, slot: str, verdict: PhaseVerdict
    ) -> PhaseVerdict:
        """Decide a position whose extras are gone — attribute, then degrade.

        Returns ``verdict`` unchanged while the slot still has extras left; the
        household keeps retrying exactly as before. Once they are spent there
        are three honest outcomes, in this order:

        1. **An earlier take is still standing** (the household retook a
           position that had already been accepted, and the retakes failed).
           Nothing was lost — keep the earlier curve and move on. This is the
           `Keep the earlier measurement` escape, applied automatically once
           there is no try left to offer.
        2. **The group can still reach a usable cloud** — drop this position,
           record the observed condition against it, and advance. The cloud
           tolerates variable position counts down to
           :data:`MIN_RESOLVED_CLOUD_POSITIONS`; below the declared plan length
           the claim is degraded and disclosed, not refused.
        3. **It cannot** (a single-capture phase, or a group with too few
           curves in hand and too few positions left to reach the floor) —
           return a terminal result on THIS final capture. The runner ends the
           set immediately and the phone renders diagnosis + count + exact
           outcome with no retry affordance. :meth:`authorize_begin` retains a
           defensive backstop for a replayed/older page only.

        A group phase never reaches (3) with anything left to measure, which is
        why the 2026-08-03 shape — a pre-play refusal at a cloud position while
        the screen read "step 6, one last time" — is now unreachable from
        ordinary retry exhaustion.
        """
        ledger = self._slot_attempts.get(slot)
        if ledger is None or ledger.extras_left > 0:
            return verdict
        observed = verdict.code or self._last_reason.get(slot) or ""
        diagnosis = ""
        if observed in REASON_REGISTRY:
            diagnosis = reason_diagnosis(
                observed,
                REASON_REGISTRY[observed],
                pilot_heard=verdict.pilot_heard,
                reflection_measured=verdict.reflection_measured,
            )
        if not self._journey.plan.is_group(phase):
            outcome = "phase_cannot_proceed"
            self._log_slot_spent(
                phase, index, observed, outcome,
                diagnosis=diagnosis,
                pilot_heard=verdict.pilot_heard,
                reflection_measured=verdict.reflection_measured,
            )
            return self._terminal_spent_verdict(
                phase, index, slot, verdict,
                diagnosis=diagnosis,
                outcome=outcome,
            )
        with self._close_lock:
            retained = self._retained_group_indexes(phase)
            if index in retained:
                # (1) The earlier take survives — a rejection never replaces a
                # retained curve — so this position is not unresolved at all.
                self._log_slot_spent(
                    phase, index, observed, "kept_earlier_take",
                    diagnosis=diagnosis,
                    pilot_heard=verdict.pilot_heard,
                    reflection_measured=verdict.reflection_measured,
                )
                return self._settled_group_verdict(
                    phase, index, {"kept_earlier_take": True}
                )
            # (3) Can this group still reach the floor? Curves in hand PLUS the
            # positions the household has not walked yet — never the count so
            # far, which would make the answer depend on walk order and end the
            # session at position 1 of 8 with seven good spots still ahead.
            unwalked = self._journey.unresolved_in_group(phase, excluding=index)
            if len(retained) + len(unwalked) < self._group_position_floor(phase):
                outcome = "below_position_floor"
                self._log_slot_spent(
                    phase, index, observed, outcome,
                    diagnosis=diagnosis,
                    pilot_heard=verdict.pilot_heard,
                    reflection_measured=verdict.reflection_measured,
                )
                return self._terminal_spent_verdict(
                    phase, index, slot, verdict,
                    diagnosis=diagnosis,
                    outcome=outcome,
                )
            # (2) Attribute and continue.
            self._group_unresolved[phase][index] = observed
            self._log_slot_spent(
                phase, index, observed, "position_unresolved",
                diagnosis=diagnosis,
                pilot_heard=verdict.pilot_heard,
                reflection_measured=verdict.reflection_measured,
            )
            return self._settled_group_verdict(
                phase,
                index,
                {
                    "unresolved": {
                        "index": index,
                        "code": observed,
                        "diagnosis": diagnosis,
                    }
                },
            )

    def _terminal_spent_verdict(
        self,
        phase: str,
        index: int,
        slot: str,
        verdict: PhaseVerdict,
        *,
        diagnosis: str,
        outcome: str,
    ) -> PhaseVerdict:
        """Return the last capture's terminal, no-more-attempts verdict."""
        ledger = self._slot_attempts[slot]
        return replace(
            verdict,
            payload={
                **verdict.payload,
                # Overrides ``to_relay_dict``'s retryable reason. The same
                # observed code/evidence still selects the diagnosis; only the
                # unavailable retry action is replaced.
                "reason": self._extras_spent_message(
                    ledger,
                    diagnosis=diagnosis,
                    outcome=self._spent_slot_outcome(phase, index),
                ),
                # Generic runner/page contract: publish this capture_result,
                # then finish without waiting for a dead next begin.
                "terminal": True,
                "terminal_outcome": outcome,
            },
        )

    def _settled_group_verdict(
        self, phase: str, index: int, payload: dict[str, Any]
    ) -> PhaseVerdict:
        """Advance the group past a settled position.

        ``accepted=True`` is the relay's only "this slot is done, move on"
        signal — the runner completes a fixed-length set at exactly
        ``capture_target`` accepted captures with ``index == accepted_count +
        1`` (see :data:`GEOMETRY_RETRY_POSITIONS`' note), so a settled position
        has to look accepted on the wire or the phone re-prompts the same spot
        forever. The payload says what actually happened, and
        ``_group_unresolved`` is what the group close and the journal read.

        Caller holds ``_close_lock``.
        """
        if self._journey.plan.is_last_index_of_group(phase, index):
            # R16: a dropped LAST pose must still close the walk, or a session
            # whose final capture could not be measured would end with no
            # candidate at all — the anchor's coefficients were never the poses'
            # to withhold. Same "settled looks accepted on the wire" contract.
            if phase == PHASE_LATERAL:
                return PhaseVerdict(
                    True, payload={**self._close_lateral_walk(), **payload}
                )
            closing = self._close_cloud_group(phase, None)
            if not closing.accepted:
                # This slot is already spent: a close-time product gate (for
                # example the delta probe) has replaced the position's
                # retryable rejection with its OWN hard-stop finding. Publish
                # that exact closing code/copy as terminal on this capture;
                # otherwise the page offers a retry the ledger cannot admit.
                # Do not route through ``_terminal_spent_verdict`` — its
                # diagnosis belongs to the earlier position rejection, while
                # ``closing`` is now the reason the phase cannot proceed.
                closing_payload = {
                    **closing.payload,
                    "terminal": True,
                    "terminal_outcome": "phase_cannot_proceed",
                }
            else:
                # Only a successful close continues the group, so only that
                # path carries the settled position's left-out/kept payload.
                closing_payload = {**closing.payload, **payload}
            return replace(closing, payload=closing_payload)
        return PhaseVerdict(True, payload=payload)

    def _log_slot_spent(
        self,
        phase: str,
        index: int,
        observed: str,
        outcome: str,
        *,
        diagnosis: str,
        pilot_heard: bool | None,
        reflection_measured: bool | None,
    ) -> None:
        log_event(
            logger, "correction.crossover_v2_position_attempts_spent",
            level=logging.WARNING,
            session_id=self.session_id, phase=phase, index=index,
            observed=observed, outcome=outcome,
            # A settled accepted result has ``code=None`` by protocol. Preserve
            # the final rejected capture's exact observation/evidence pairing
            # here so support does not have to infer it from earlier logs.
            diagnosis=diagnosis,
            pilot_heard=pilot_heard,
            reflection_measured=reflection_measured,
            extra_allowed=MAX_EXTRA_ATTEMPTS_PER_POSITION,
        )

    def _note_accepted(self, phase: str, index: int) -> None:
        # The journey's group-close rule: a position the flow gave up on
        # (``_group_unresolved``) counts as resolved too, because the relay
        # advanced past it and the phase would otherwise never close.
        # ``_group_positions`` remains the sole record of what was MEASURED.
        self._journey.accept(phase, index)

    # --- per-phase verdicts --------------------------------------------------
    #
    # Each ``_consume_<phase>`` is a thin wrapper: compute the verdict via the
    # UNCHANGED ``_<phase>_verdict`` logic, log that capture's full numeric
    # diagnostics (Part 1 — on the accepted path AND every rejection) through
    # ``_safe_log_diag`` — never the raw ``_log_*_diag`` call directly, so a
    # bug in the logging path can never crash or flip the verdict already
    # decided above it — then return the verdict. Splitting it this way means
    # the diagnostic log call is the ONLY new control flow here — none of the
    # accept/reject branching below moved or changed.

    def _consume_check(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        verdict = self._check_verdict(analysis)
        self._safe_log_diag(self._log_check_diag, analysis, verdict)
        return verdict

    def _check_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        if not _stimulus_locate_ok(analysis):
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        if analysis.channel_map_ok is False:
            return PhaseVerdict(False, REASON_CHANNEL_MAP_MISMATCH)
        if analysis.pilot_snr_ok is False:
            # Band-relative ambient-compensated linearity fix (2026-07-20):
            # the quiet pilot's own in-band SNR was too low to trust the
            # ambient-subtracted delta either way — ``analysis.linearity_ok``
            # is already None (unknown) in this case since issue #1838 (see
            # ``program_analysis._pilot_observations``'s docstring), so this
            # branch is the ONLY path that can fail on it. Route to the
            # honest room/positioning reason, never AGC — the phone's mic
            # didn't misbehave, there just wasn't enough signal above the
            # room to measure.
            return PhaseVerdict(False, REASON_SNR_FLOOR)
        if analysis.linearity_ok is False:
            # W6.12: don't blame the phone's mic when the room was the actual
            # cause. The CHECK gain solve ALREADY computes an SNR-floor
            # verdict against THIS capture's own ambient bands (``_analyze_check``
            # runs ``_solve_gain_plan`` unconditionally, before this branch),
            # independent of whether linearity itself passed — reuse that
            # existing evidence rather than re-deriving a second ambient
            # judgment. The other phases reach the same honest destination by
            # a different route: since issue #1810 their own pre-pilot ambient
            # window makes ``pilot_snr_ok`` a real verdict, and they branch on
            # it to ``REASON_PILOT_LEVEL_COLLAPSE`` above their own linearity
            # check. CHECK keeps the gain-solve route because it already has
            # a stronger, band-resolved ambient judgment in hand.
            if analysis.gain_plan is not None and not analysis.gain_plan.snr_floor_ok:
                return PhaseVerdict(False, REASON_NOISY_ROOM_LINEARITY)
            return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)
        gain_plan = analysis.gain_plan
        if gain_plan is None or not gain_plan.snr_floor_ok:
            return PhaseVerdict(False, REASON_SNR_FLOOR)
        # Accept: keep the solved gains + ambient, compose the MEASURE program,
        # publish CHECK evidence.
        self._gain_plan_db = dict(gain_plan.gain_db)
        # HOLD the ambient report, don't just publish it (issue #1830). Until
        # now the only thing done with it was the publish one line below —
        # it crossed the seam to check.json and was dropped, so MEASURE's
        # per-driver SNR verdict had no noise floor to grade against. See
        # ``_measure_priors``.
        self._check_ambient_report = (
            dict(analysis.ambient_report) if analysis.ambient_report else None
        )
        self._measure_program = self._compose_measure_program(self._gain_plan_db)
        self._seams.publish_check(gain_plan, analysis.ambient_report or {})
        return PhaseVerdict(True, payload={"measurement_phase": PHASE_CHECK})

    def _consume_measure(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        verdict = self._measure_verdict(analysis)
        self._safe_log_diag(self._log_measure_diag, analysis, verdict)
        return verdict

    def _note_ripple_reservation(self, predicted_ripple_db: float) -> None:
        """Bank G1's reservation about the capture being accepted (#2087).

        Records the fact and says so in the journal. It decides nothing —
        the caller has already decided to proceed, and this method must never
        acquire a branch that could change that, or the ruling would quietly
        grow a gate back.

        ``guard`` keeps carrying this in ``correction.crossover_v2_measure_diag``
        so the existing per-capture telemetry can still find these captures,
        but the VALUE is ``ripple_disclosure`` rather than the old
        ``ripple_ceiling``: the field's siblings name checks that REFUSED, and
        leaving a refusal's vocabulary on a path that now accepts would mislead
        exactly the reader that field exists for. The dedicated event below is
        the stable line to alert or count on — ``guard`` is one field on a
        diagnostic that fires on every capture, accepted or not.

        WARNING level, deliberately. The session proceeds, so this is not an
        error; but the household is being handed a tuning built on a capture
        the calibration corpus says is unusually incoherent, and an operator
        reading the journal at INFO would have to know to look for it.
        """
        self._last_measure_guard = "ripple_disclosure"
        self._measure_ripple_reservation = {
            "predicted_ripple_db": float(predicted_ripple_db),
            # The threshold rides WITH the value rather than being re-read at
            # render time. A screen showing "12.4 dB, above 15.0" would be a
            # lie the moment the constant moves, and the disclosure is a
            # statement about what was true when the capture was judged.
            "threshold_db": float(MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB),
        }
        log_event(
            logger, "correction.crossover_v2_ripple_disclosed",
            level=logging.WARNING,
            session_id=self.session_id,
            predicted_ripple_db=round(float(predicted_ripple_db), 3),
            threshold_db=float(MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB),
        )

    def _measure_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        # Reset every call — a stale value from a PRIOR attempt must never
        # leak into THIS attempt's diagnostic (see __init__'s comment).
        self._last_measure_guard = ""
        self._measure_ripple_reservation = None
        # The seven linearization fields this used to reset with them are gone
        # (#2291 Phase 2b): a build returns its own :class:`_LinearizationState`
        # and nothing outlives the build, so there is no prior attempt's value
        # left to leak into this one.
        if not _stimulus_locate_ok(analysis):
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        # --- "too quiet" runs BEFORE "glitched" (D3, issue #1838) ---
        #
        # A capture nobody could hear produces the same symptoms as a spliced
        # one: the locator lands the sweeps in the wrong place, the residual
        # blows past its ceiling, and `glitch_detected` fires on noise. Until
        # #1838 the glitch branch sat second and swallowed both level
        # verdicts below it, so session cap_-Us10xORVNlFa_dgi-sP7g — whose
        # MEASURE played 33 dB below flat — told the household its capture had
        # glitched and silently re-armed the SAME unwinnable level, twice,
        # until the session timed out. Low SNR CAUSES the glitch signal, so
        # the level verdicts have to be asked first or the reported cause is
        # never the real one. (This very likely also explains a share of the
        # historical "capture glitched" reports.)
        #
        # Neither branch re-arms: re-running an inaudible measurement at the
        # same level cannot succeed, and both reason codes already carry a
        # household action that can. ``pilot_level_collapse`` names the room
        # and the level ("quiet the room / move the microphone closer");
        # ``locate_failed`` picks its own sentence from the pilot evidence
        # since #2085, because the ORDER here means the locate branch below
        # is reached only once the pilot has been asked — see
        # ``locate_failed_message``.
        if analysis.pilot_snr_ok is False:
            # Issue #1810. Also ahead of the linearity branch: below the SNR
            # floor the two-pilot delta is not evidence about anything
            # (``_pilot_observations`` reports ``linearity_ok`` as None), so
            # the honest verdict is about the room and the level, never the
            # phone's microphone.
            return PhaseVerdict(False, REASON_PILOT_LEVEL_COLLAPSE)
        if not _sweep_locate_confidence_ok(analysis):
            self._last_measure_guard = "sweep_locate_confidence"
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        if analysis.glitch_detected:
            # Repeat-level disagreement reuses this same code (§5.2) — the
            # analysis already folded it into glitch_detected.
            self._rearm_measure_after_transient()
            return PhaseVerdict(False, REASON_DRIFT_BASELINES_DISAGREE)
        # Measurement-honesty gate G2 (2026-07-22 — the xrun detector): a
        # uniform whole-capture schedule shift the repeat-pair drift check
        # above is structurally blind to (see SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
        # for the evidence). Routed identically to the glitch branch above —
        # same silent auto-retry, same reused reason code (§5.2's "never a
        # new user-facing code for a capture-glitch class" convention) — the
        # ``guard`` diag field (below) is what tells telemetry the two apart.
        # ``program_for_phase`` (not the bare ``self._measure_program``,
        # which mypy types ``ExcitationProgram | None``) is the ALREADY
        # type-narrowed accessor — it raises if MEASURE were somehow armed
        # before CHECK produced a program, which can't happen on this path
        # (we are actively processing a MEASURE analysis).
        if not _sweep_schedule_ok(
            analysis, self.program_for_phase(PHASE_MEASURE).sample_rate_hz
        ):
            self._last_measure_guard = "sweep_schedule"
            self._rearm_measure_after_transient()
            return PhaseVerdict(False, REASON_DRIFT_BASELINES_DISAGREE)
        if _any_sweep_clipped(analysis):
            self._rearm_measure_after_transient(extra_backoff_db=CLIP_RETRY_BACKOFF_DB)
            return PhaseVerdict(False, REASON_CLIPPED)
        if analysis.linearity_ok is False:
            return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)
        if analysis.alignment is not None and analysis.alignment.status != ALIGNMENT_OK:
            return PhaseVerdict(False, REASON_DELAY_EXCEEDS_SEARCH_WINDOW)
        # Trust gate (owner ruling, 2026-07-20): this is GCC's capture/seed
        # confidence, not confidence in T2's refined delay (the alignment and
        # candidate retain both facts separately). Below the floor the
        # candidate is never built or published — a household has no basis to
        # judge a confidence number, so this is guidance ("move the mic"), not
        # a question ("apply anyway?"). Skipped entirely when there is no
        # alignment estimate at all (a trims-only candidate) — same condition
        # the former review-screen nudge used.
        if (
            analysis.alignment is not None
            and analysis.alignment.confidence < ALIGNMENT_CONFIDENCE_TRUST_FLOOR
        ):
            return PhaseVerdict(False, REASON_LOW_ALIGNMENT_CONFIDENCE)
        # Physical-plausibility backstop (Fix 3): a confidently-WRONG delay
        # (high GCC correlation confidence at the wrong lag) clears the trust
        # gate above but is still physically implausible against the
        # preset's declared search bound — reuses the SAME re-measure
        # guidance rather than a new reason code, since the household action
        # is identical ("move the mic, measure again").
        if (
            analysis.alignment is not None
            and analysis.alignment.status == ALIGNMENT_OK
            and not alignment_delay_plausible(analysis.alignment.delay_us, self._preset)
        ):
            return PhaseVerdict(False, REASON_LOW_ALIGNMENT_CONFIDENCE)
        # Measurement-honesty DISCLOSURE G1 (2026-07-22; owner ruling
        # 2026-08-03, issue #2087). **This branch does not refuse.** A
        # predicted ripple above the threshold says the two branches sum less
        # coherently in this room, on this rig, than the calibration corpus
        # did — see MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB for the evidence and
        # for why the owner converted it. So the capture is ACCEPTED and the
        # measurement carries an honest reservation to the household instead of
        # sending them to move a microphone that was never the problem.
        #
        # Deliberately NOT a `return` — control falls through to the same
        # candidate build every accepted MEASURE runs, so the reservation
        # changes what the household is TOLD and nothing about what is built,
        # fitted, gated, or applied. Every accountability gate below
        # (``_assert_accountable``'s level-frame, realized-level and
        # predicted-improvement refusals) still runs unchanged on this
        # candidate, which is what keeps "proceed" from meaning "unchecked".
        #
        # Skipped when there is no candidate or no alignment estimate (a
        # trims-only path) — the same skip condition the gate carried, kept
        # because a reservation about a candidate that does not exist would
        # describe nothing.
        if (
            analysis.candidate is not None
            and analysis.alignment is not None
            and analysis.candidate.predicted_ripple_db
            > MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB
        ):
            self._note_ripple_reservation(analysis.candidate.predicted_ripple_db)
        if analysis.candidate is None:
            # Fail FAST, at the capture that produced the unusable analysis.
            # Until the 2026-07-27 timing move this raise happened one call
            # deeper and one line later (``_build_candidate``'s own identical
            # check, still there as the residual) — same exception, same
            # message, same phase, so the host's ``internal_error`` mapping is
            # unchanged. Hoisting it is what keeps that behaviour at MEASURE:
            # the candidate build now happens eight captures later, and a
            # household must not walk the whole cloud for a session that was
            # already unable to produce a candidate at sweep two.
            raise CrossoverV2FlowError("MEASURE analysis produced no candidate")
        self._measure_gate_window_ms = self._measure_gate(analysis)
        # **The fit runs at the last capture before the apply.** Which capture
        # that is depends on the session's own phase list, and there are
        # exactly two shapes:
        #
        # * a session that runs a CLOUD_MEASURE group (every production
        #   measurement session since PR-3b — ``prepare_v2_session`` passes
        #   ``build_v2_cloud_index_phase_map()``) defers the fit, the candidate
        #   build, and the auto-apply trigger to that group's close, so the fit
        #   consumes the cloud's honesty verdict instead of preceding it by
        #   eight captures. Owner decision, 2026-07-27 — the work order's own
        #   pre-registered phase order. See ``_close_measure_cloud_candidate``.
        # * a session with no such group (the pre-cloud 3-entry shape this
        #   class still defaults to) has nothing to wait for, so it builds here
        #   and behaves as it did before the move: same accept, same payload
        #   keys, same auto-apply timing. Scoped precisely, because the
        #   unqualified phrase would be wrong — the candidate it publishes
        #   DOES gain an always-empty ``exclusion_evidence`` key, which is
        #   omitted from ``_core()`` when empty and so leaves the fingerprint
        #   byte-identical. The FLOW is unchanged; the artifact gains one
        #   empty, non-fingerprinted field.
        #
        # Neither branch moves anything but the CANDIDATE BUILD's timing; only
        # its trigger point moves, and only for a session that has a cloud.
        #
        # This paragraph used to read "the 2026-07-20 'no human Apply gate'
        # ruling is untouched by either branch: apply is still automatic and
        # still needs no tap", which has been false since the two-stage split
        # (PR-T3) superseded that ruling. Nothing auto-applies: the candidate
        # is a PROPOSAL the review screen shows, and the apply is a separate,
        # explicit household POST to ``/correction/crossover/v2/apply`` — as
        # ``confirm_cloud_measure_group``'s own docstring already says. Fixed
        # here rather than left as a follow-up because it sits ~40 lines below
        # G1's disclosure branch and materially OVERSTATES that change's blast
        # radius: a reader going top-down would conclude a capture accepted
        # with a ripple reservation auto-applies without anyone deciding.
        #
        # On the deferring branch ONLY, the analysis is retained rather than
        # consumed — it is the fit's input and must outlive the prompted cloud
        # walk. The non-deferring branch consumes it in this same call and
        # never stores it: keeping a tens-of-megabytes reference that nothing
        # will ever read is not free on a 1 GB Pi (see the field's own comment
        # in ``__init__`` for the measurement). Exactly one is ever held; a
        # MEASURE re-arm overwrites it, and the group close releases it.
        # R16 adds a THIRD deferring shape to the two below. The rule in bold
        # above is the invariant, not the cloud's name in it: once a lateral
        # walk follows the anchor, MEASURE is no longer the last capture before
        # the apply, and fitting here would put a proposal on the review screen
        # that predates five minutes of evidence the household was just asked to
        # produce — the exact defect the 2026-07-27 decision removed for the
        # cloud. The lateral group's last accepted pose closes it instead.
        if PHASE_CLOUD_MEASURE in self._journey.plan.phases or PHASE_LATERAL in self._journey.plan.phases:
            self._measure_analysis = analysis
            return PhaseVerdict(True, payload={"measurement_phase": PHASE_MEASURE})
        # The pre-cloud 3-entry shape, which NO production caller constructs
        # any more (``prepare_v2_session`` always builds a cloud map,
        # ``prepare_v2_verify`` maps VERIFY alone). It keeps folding the
        # candidate payload into this verdict, but note that since
        # flow-simplification §2.6 moved the trigger onto the confirm seam,
        # the host no longer reads ``auto_apply`` off a capture verdict — so a
        # future caller reviving this shape has to wire its own apply trigger
        # rather than inherit one. Kept honest here rather than discovered
        # later by a session that measures and never applies.
        return PhaseVerdict(
            True,
            payload={
                "measurement_phase": PHASE_MEASURE,
                **self._publish_measure_candidate(analysis, None),
            },
        )

    def _retained_group_indexes(self, phase: str) -> set[int]:
        """Which indexes of one group already hold evidence — one accessor over
        the two retentions (cloud curves, R16 poses), so the retry/settle
        bookkeeping never branches on which list a phase happens to use."""
        if phase == PHASE_LATERAL:
            return {pose.index for pose in self._lateral_poses}
        return {p.index for p in self._group_positions.get(phase, ())}

    def _group_position_floor(self, phase: str) -> int:
        """How few resolved positions still lets a group stand — see
        :func:`~jasper.active_speaker.crossover_v2.spatial.group_position_floor`.
        """
        return _spatial.group_position_floor(
            phase, min_resolved_cloud_positions=MIN_RESOLVED_CLOUD_POSITIONS,
        )

    def _consume_lateral_pose(
        self, index: int, attempt: int, analysis: ProgramAnalysis,
    ) -> PhaseVerdict:
        """One pose of the R16 lateral walk (plan §4.4).

        The screens — MEASURE's own capture-integrity gates in MEASURE's order,
        minus the three that judge the alignment solve §4.4 forbids re-running —
        belong to
        :func:`~jasper.active_speaker.crossover_v2.spatial.lateral_pose_screens`,
        and the two-curve floor to its
        :func:`~jasper.active_speaker.crossover_v2.spatial.lateral_curves_sufficient`
        (which runs after the build, for the reason stated there).

        What stays here is the retain-and-close half, and one rule that is this
        method's rather than the ladder's: a rejected pose does NOT re-arm
        MEASURE with a level backoff. The pose must be measured at the ANCHOR'S
        level or its curve is not comparable to the anchor's, and a quieter
        retake would answer a different question.
        """
        program = self.program_for_phase(PHASE_LATERAL)
        kind = _spatial.lateral_pose_screens(
            _spatial.CaptureScreens(
                stimulus_located=_stimulus_locate_ok(analysis),
                pilot_snr_ok=analysis.pilot_snr_ok,
                linearity_ok=analysis.linearity_ok,
                glitch_detected=bool(analysis.glitch_detected),
                sweep_locate_confidence_ok=_sweep_locate_confidence_ok(analysis),
                sweep_schedule_ok=_sweep_schedule_ok(
                    analysis, program.sample_rate_hz
                ),
                any_sweep_clipped=_any_sweep_clipped(analysis),
            )
        )
        if kind is not None:
            return PhaseVerdict(False, _screen_refusal_code(kind))
        bands = _primary_sweep_bands(program)
        curves = [
            lateral_pose_curve(response, bands[response.role])
            for response in analysis.driver_responses
            if response.repeat_index is None and response.role in bands
        ]
        kind = _spatial.lateral_curves_sufficient(len(curves))
        if kind is not None:
            return PhaseVerdict(False, _screen_refusal_code(kind))
        prompt = self._prompt_shown_for(PHASE_LATERAL, index)
        pose = LateralPose(
            pose_id=f"{PHASE_LATERAL}_{index:02d}",
            index=index,
            attempt=attempt,
            prompt=prompt.text,
            role=prompt.role,
            offset_cm=float(prompt.offset_cm),
            at_mark=float(prompt.offset_cm) == 0.0,
            captured_at=time.time(),
            curves=tuple(curves),
        )
        log_event(
            logger, "correction.crossover_v2_lateral_pose",
            session_id=self.session_id, pose_id=pose.pose_id, index=index,
            attempt=attempt, offset_cm=pose.offset_cm, position_role=pose.role,
            at_mark=pose.at_mark, curves=len(pose.curves),
        )
        # ONE critical section for retain + close, exactly as the cloud's
        # position verdict takes: the candidate build reads the whole walk, and
        # a retain that landed half-way through it would fit a session that
        # never existed.
        with self._close_lock:
            self._lateral_poses = sorted(
                [p for p in self._lateral_poses if p.index != index] + [pose],
                key=lambda p: p.index,
            )
            payload: dict[str, Any] = {"position_id": pose.pose_id}
            if self._journey.plan.is_last_index_of_group(PHASE_LATERAL, index):
                payload.update(self._close_lateral_walk())
            return PhaseVerdict(True, payload=payload)

    def _close_lateral_walk(self) -> dict[str, Any]:
        """Build the candidate the household reviews, once the walk is done.

        The lateral shape's counterpart to
        :meth:`_close_measure_cloud_candidate`, and deliberately the smaller of
        the two: no combine, no geometry retake, no confirm screen to wait for,
        so the walk's last accepted pose is simply the last capture before the
        apply and this folds the candidate into its verdict exactly as the
        pre-cloud 3-entry shape folds it into MEASURE's.

        ``cloud=None`` is the same honest ``None`` the cloud path passes when
        its pipeline did not become available: this session ran no pre-apply
        cloud, so the envelope's spatial terms have no evidence — §4.2's
        recorded, accepted risk for the driver-only path, not something the
        lateral walk quietly substitutes for. §4.4 is explicit that side
        evidence may not become the fit target.

        A walk where nothing was captured still closes: the anchor already owns
        the coefficients (see :meth:`_group_position_floor`).
        """
        if self._measure_analysis is None:
            raise CrossoverV2FlowError(
                "lateral walk closed with no retained MEASURE analysis"
            )
        analysis, self._measure_analysis = self._measure_analysis, None
        # R17 adjudicates HERE — §4.4's rule that anything reading the whole
        # walk waits for the whole walk. Before the candidate build below, so
        # a selector bug cannot be blamed on the published candidate; it
        # cannot raise (``_adjudicate_fc`` is total over its inputs) and it
        # writes nothing the build reads.
        self._adjudicate_fc()
        log_event(
            logger, "correction.crossover_v2_lateral_walk_closed",
            session_id=self.session_id,
            planned=len(self._journey.plan.group_offsets(PHASE_LATERAL)),
            captured=len(self._lateral_poses),
            mark_return_drift_db=self.lateral_mark_return_drift_db(),
        )
        selected = self._fc_selected_evaluation
        self._fc_selected_evaluation = None
        if selected is not None:
            return self._commit_fc_candidate(selected)
        # Preserve the configured winner's established publication path.
        return self._publish_measure_candidate(analysis, None)

    # --- R17: the Fc candidate sweep -----------------------------------------

    def _fc_candidate_sections(
        self, fc_hz: float,
    ) -> dict[str, tuple[CrossoverSection, ...]]:
        """The configured branch sections, re-cornered at ``fc_hz``.

        Order, direction and role assignment are the preset's — only the corner
        moves, because R17 adjudicates WHERE to cross, never what shape to
        cross with (topology search is deferred, #1894).
        """
        return {
            role: tuple(replace(section, fc_hz=float(fc_hz)) for section in sections)
            for role, sections in sections_by_role(
                getattr(self._preset, "crossover_regions", ()) or ()
            ).items()
        }

    def _fc_candidate_priors(
        self, fc_hz: float, sections: Mapping[str, tuple[CrossoverSection, ...]],
    ) -> MeasurementPriors:
        """MEASURE's priors re-pointed at one candidate — THREE fields move.

        ``configured_polarity_sign_by_role`` and
        ``measurement_protection_response_by_role`` are carried UNCHANGED:
        polarity is how the drivers are wired and protection is the filter the
        graph actually emitted, and neither moves when the crossover corner
        does. They are also load-bearing here rather than merely harmless —
        ``_compose_configured_path_ir`` raises on a PARTIAL prior set, so
        dropping either would refuse the composition outright instead of
        producing a candidate.
        """
        overlap = overlap_band_hz(float(fc_hz))
        return replace(
            self._measure_priors(),
            crossover_fc_hz=float(fc_hz),
            configured_crossover_response_by_role=_role_transfers(sections),
            # Same union as ``_measure_priors``, at THIS candidate's corner:
            # the radiating span the fit masks to, widened by the unclamped
            # overlap band. A superset is the safe side for a required mask.
            #
            # **A TWIN, and now a cross-module one** (#2336 gate, N2): the same
            # formula lives in ``crossover_v2.priors.measure_priors``. It was one
            # module's two call sites before 5a-iii and is two modules' now, so
            # the pair can drift without either side looking wrong. Resolving it
            # belongs to 5a-v, which moves this method: give the union ONE owner
            # in ``priors`` and have both callers ask for it, rather than
            # copying the expression a third time.
            candidate_required_band_hz_by_role={
                role: (min(radiating_band_hz(sec)[0], overlap[0]),
                       max(radiating_band_hz(sec)[1], overlap[1]))
                for role, sec in sections.items()
            },
        )

    def _fc_branch_operators(
        self,
        freqs: np.ndarray,
        analysis: Any,
        sections: Mapping[str, tuple[CrossoverSection, ...]],
        linearization: Mapping[str, Any],
        trims: Mapping[str, float],
    ) -> dict[str, np.ndarray]:
        """What this candidate does to one driver's NEUTRAL pose measurement.

        ``sign * (C_c / P) * K * 10**(trim/20)``, plus the alignment's polarity
        and residual delay on the tweeter — §4.2's re-composition cascaded with
        everything :func:`predicted_branch_sum` applies to the linearized pair.
        So ``sum_role M_pose,role * operator_role`` IS this candidate's model at
        that pose, and the kernel's pose sum is one multiply-add.

        Every factor is evaluated by the SAME function the emitted graph is
        built from — :func:`crossover_response_complex` for the crossover,
        :func:`chain_response` for the correction biquads — so this model and
        the speaker can never disagree about what a filter does.
        """
        protection = _role_transfers(self._measurement_protection_sections_by_role) or {}
        polarity = {
            role: -1 if inverted else 1
            for role, inverted in role_polarity(self._preset).items()
        }
        filters = linearization_filters_by_role(linearization)
        residual_us = summed_model_residual_delay_us(
            analysis.alignment.anchor_delay_us
            if analysis.alignment.status == ALIGNMENT_OK else None,
            analysis.alignment.delay_us,
        )
        operators: dict[str, np.ndarray] = {}
        for role, section in sections.items():
            emitted = np.asarray(protection[role](freqs), dtype=np.complex128)
            configured = np.asarray(
                crossover_response_complex(freqs, section), dtype=np.complex128
            )
            # ``where=`` guards an exact zero only. The CONDITIONING of this
            # ratio is the analysis's gate, not this function's: a candidate
            # whose C/P left the composition policy's window never reached here
            # (it refused as unfittable), and any non-finite bin that survives
            # is dropped by ``band_flatness``'s own finite mask rather than
            # scored. Re-stating the policy's ceiling here would put a second
            # writer on a number that has one.
            operator = (
                polarity.get(role, 1)
                * np.divide(
                    configured, emitted,
                    out=np.zeros_like(configured), where=emitted != 0,
                )
                * chain_response(filters.get(role, ()), freqs)
                * 10.0 ** (float(trims.get(role, 0.0)) / 20.0)
            )
            if role == self._tweeter.role:
                operator = (
                    operator
                    * analysis.alignment.polarity_sign
                    * np.exp(-1j * 2.0 * np.pi * freqs * residual_us * 1e-6)
                )
            operators[role] = operator
        return operators

    def _evaluate_fc_candidate(
        self, fc_hz: float, anchor: Any, program: Any, result: Any,
    ) -> FcCandidateEvaluation:
        """One candidate, fitted and reduced to its retained record.

        **This method is the release point.** The per-candidate analysis and
        fit — hundreds of megabytes between them — are locals here, so they are
        unreachable the moment it returns and the next candidate's allocation
        never overlaps them. The configured candidate reuses the anchor's own
        analysis rather than re-running it: it is the same priors, so a second
        run would buy an identical result for 7 s and 400 MB.
        """
        sections = self._fc_candidate_sections(fc_hz)
        analysis = (
            anchor if fc_hz == self._fc_hz
            else self._seams.analyze(
                program, result, self._fc_candidate_priors(fc_hz, sections),
                self._geometry, phase=PHASE_MEASURE,
            )
        )
        cand = analysis.candidate
        # The SAME hard gate ``_build_candidate`` applies, and applied for the
        # same reason: ``plan_linearization`` states in its own docstring that
        # it assumes eligibility and does not re-check, so calling it on a
        # phone-tier or under-repeated session raises inside the fit engine
        # rather than declining. An ineligible session simply has no linearized
        # model to compare candidates with — an honest refusal, never a
        # fallback to the raw prediction, which would put candidates fitted
        # differently side by side and call the difference a crossover.
        if cand is None or self._linearization_ineligible_reason(analysis) is not None:
            return _fc_refusal(fc_hz, EVAL_REFUSED_UNFITTABLE)
        preset = replace(self._preset, crossover_regions=tuple(
            replace(region,
                    id=f"{region.lower_driver}_{region.upper_driver}_{round(fc_hz):.0f}hz",
                    fc_hz=float(fc_hz))
            for region in self._preset.crossover_regions))
        built = self._build_measure_candidate(
            analysis, None, candidate_sections=sections, source_preset=preset,
        )
        candidate = built.candidate
        if candidate.linearization_outcome == "fit_failed":
            return _fc_refusal(fc_hz, EVAL_REFUSED_UNFITTABLE)
        trims, linearization = candidate.role_attenuations_db, candidate.linearization
        grid = lateral_evidence_grid_hz()
        tweeter_lo, woofer_hi = self._measure_sweep_bounds()
        # THIS candidate's own linearized prediction, off the state its own
        # build returned. Until #2291 Phase 2b it was read back off a conductor
        # field that had to be nulled beforehand and restored afterwards; a
        # value cannot be another candidate's.
        anchor_sum = built.linearization.linearized_predicted_sum
        if anchor_sum is None:
            # Eligible but the fit still produced no linearized prediction.
            return _fc_refusal(fc_hz, EVAL_REFUSED_UNFITTABLE)
        return FcCandidateEvaluation(
            fc_hz=float(fc_hz),
            freqs_hz=grid,
            branch_operator_by_role=self._fc_branch_operators(
                grid, analysis, sections, linearization, trims,
            ),
            anchor_sum_db=np.interp(grid, anchor_sum[0], anchor_sum[1]),
            # ``overlap_band_hz`` with the REAL sweep bounds, never
            # ``crossover_region_band_hz``: that one is built for summed
            # CAPTURES and takes a gate-derived floor, while this scores a
            # per-branch MODEL on the grid the poses share.
            scoring_band_hz=overlap_band_hz(
                float(fc_hz),
                tweeter_sweep_lo_hz=tweeter_lo,
                woofer_sweep_hi_hz=woofer_hi,
            ),
            headroom_cost_db=max(
                (float(fit.get("headroom_cost_db") or 0.0)
                 for fit in linearization.values() if isinstance(fit, Mapping)),
                default=0.0,
            ),
            candidate=candidate.to_dict(),
            predicted_sum=(np.asarray(built.predicted_sum[0]).copy(),
                           np.asarray(built.predicted_sum[1]).copy()),
            predicted_spec_report=(dict(self._measure_predicted_spec_report)
                if self._measure_predicted_spec_report is not None else None),
            commanded_delta=_commanded_delta(analysis.predicted_sum,
                                             built.predicted_sum),
            level_frame_finding=built.level_frame_finding,
            # THIS candidate's realized inter-driver level, which the sweep can
            # carry since the cutover (#2307 gate note N6): the verdict is a
            # value on the build's own state rather than a conductor field the
            # sweep restores, so a selected alternative corner's proposal now
            # records its OWN evidence instead of an absence.
            realized_branch_level=built.linearization.realized_branch_level,
        )

    def _fc_evaluation_budget_s(self) -> float:
        """The explicit wall budget for this one-time serial computation."""
        return FC_SWEEP_COMPUTE_BUDGET_S

    def _sweep_fc_candidates(self, program: Any, result: Any, anchor: Any) -> None:
        """Evaluate the proposable Fc set against THIS capture, then release.

        Runs at MEASURE-consume because the raw capture is alive only here: the
        retained anchor holds derived ``DriverResponse``s, and §4.2's own
        conditioning policy refuses to un-compose them. Adjudication still
        happens at the walk's close, so nothing publishes early (§4.4).

        **Never raises.** A sweep that cannot run leaves the disclosure short
        and says so; no household loses a measured capture because an advisory
        could not be computed. Nothing here writes an emitted filter; a selected
        executable candidate waits for Sound-owned acceptance at Review.
        """
        if self._measurement_protection_sections_by_role is None:
            # No protection map means no §4.2 composition at all, so a
            # candidate's crossover cannot be substituted for the emitted one.
            return
        # No save/restore (#2291 Phase 2b). The fit used to write seven
        # conductor fields the walk's own candidate build then read, so the
        # sweep had to snapshot them and put them back — the restore was what
        # made the published candidate byte-identical to a no-selector run.
        # Each build now returns its own :class:`_LinearizationState`, so a
        # swept candidate's values never reach the anchor's and there is
        # nothing left to restore.
        started = time.monotonic()
        slowest_s = 0.0
        evaluations: list[FcCandidateEvaluation] = []
        try:
            # INSIDE the try, with the disclosure log, so "never raises" is
            # structural rather than a claim about which of these happens to be
            # total today. Deriving the candidate set reads household
            # declarations and the budget reads the MEASURE program; both are
            # ordinary sources of a malformed-input raise, and both used to sit
            # outside this block — where an advisory could have cost the
            # household an ACCEPTED MEASURE (resilience lens).
            candidates = self._fc_candidate_set()
            budget_s = self._fc_evaluation_budget_s()
            for fc_hz in candidates.candidates:
                elapsed = time.monotonic() - started
                # Forecast, not a bare deadline check: a candidate costs about
                # what the slowest one so far did, and STARTING one that cannot
                # finish inside the phone's window is how an advisory becomes a
                # terminal failure. The first candidate always runs — there is
                # nothing to forecast from, and a sweep that scores nothing has
                # no comparison to offer.
                if evaluations and elapsed + slowest_s > budget_s:
                    evaluations.extend(
                        _fc_refusal(rest, EVAL_REFUSED_BUDGET)
                        for rest in candidates.candidates[len(evaluations):]
                    )
                    break
                try:
                    evaluations.append(
                        self._evaluate_fc_candidate(fc_hz, anchor, program, result)
                    )
                except (
                    ArithmeticError, AttributeError, RuntimeError, TypeError,
                    ValueError, KeyError, IndexError,
                ) as exc:
                    log_event(
                        logger, "correction.crossover_v2_fc_candidate_refused",
                        level=logging.WARNING, session_id=self.session_id,
                        fc_hz=round(float(fc_hz), 1), reason=type(exc).__name__,
                    )
                    evaluations.append(_fc_refusal(fc_hz, EVAL_REFUSED_UNFITTABLE))
                slowest_s = max(slowest_s, time.monotonic() - started - elapsed)
            attempted = [
                round(e.fc_hz, 1)
                for e in evaluations if e.refusal != EVAL_REFUSED_BUDGET
            ]
            skipped = [
                {"fc_hz": round(e.fc_hz, 1), "reason": e.refusal}
                for e in evaluations if e.refusal == EVAL_REFUSED_BUDGET
            ]
            comparison_complete = fc_comparison_complete(
                evaluations, len(candidates.candidates)
            )
            log_event(
                logger, "correction.crossover_v2_fc_sweep",
                session_id=self.session_id, configured_hz=round(self._fc_hz, 1),
                planned=len(candidates.candidates),
                evaluated=sum(1 for e in evaluations if e.refusal is None),
                candidate_order=[round(fc, 1) for fc in candidates.candidates],
                attempted=attempted, skipped=skipped,
                comparison_complete=comparison_complete,
                elapsed_s=round(time.monotonic() - started, 2),
                budget_s=round(budget_s, 2),
                limits={k: round(v, 1) for k, v in candidates.limits.items()},
                rejected=[
                    [round(fc, 1), reason] for fc, reason in candidates.rejected
                ],
            )
        except (
            ArithmeticError, AttributeError, RuntimeError, TypeError,
            ValueError, KeyError, IndexError,
        ) as exc:
            # The whole advisory declined, loudly. Same caught set as the
            # per-candidate handler above; ``CrossoverV2FlowError`` is a
            # ``RuntimeError``, so a refused candidate-set derivation lands here
            # rather than escaping into the capture's accept path.
            log_event(
                logger, "correction.crossover_v2_fc_sweep_refused",
                level=logging.WARNING, session_id=self.session_id,
                reason=type(exc).__name__,
            )
        finally:
            self._fc_evaluations = tuple(evaluations)

    def _adjudicate_fc(self) -> None:
        """Turn the retained per-candidate evidence into ONE recommendation.

        At the walk's close, where §4.4 puts every judgement that reads the
        whole walk. Releases the evaluations after: the selection is what the
        review screen renders, and the evidence behind it has done its job.
        """
        evaluations, self._fc_evaluations = self._fc_evaluations, ()
        if not evaluations:
            return
        candidates = self._fc_candidate_set()
        self._fc_selection = select_fc(
            evaluations,
            [pose.curves for pose in self._lateral_poses],
            configured_hz=self._fc_hz,
            limits=candidates.limits,
            planned=len(candidates.candidates),
        )
        recommended = self._fc_selection.recommended_hz
        if recommended is not None:
            self._fc_selected_evaluation = next(
                (e for e in evaluations
                 if math.isclose(e.fc_hz, recommended, abs_tol=0.05)
                 and e.candidate is not None), None)
        log_event(
            logger, "correction.crossover_v2_fc_selection",
            session_id=self.session_id, verdict=self._fc_selection.verdict,
            configured_hz=round(self._fc_hz, 1),
            recommended_hz=(
                round(self._fc_selection.recommended_hz, 1)
                if self._fc_selection.recommended_hz is not None else None
            ),
            margin_db=(
                round(self._fc_selection.margin_db, 3)
                if self._fc_selection.margin_db is not None else None
            ),
            evaluated=self._fc_selection.evaluated,
            planned=self._fc_selection.planned,
            candidate_order=[round(fc, 1) for fc in self._fc_selection.candidate_order],
            attempted=[round(fc, 1) for fc in self._fc_selection.attempted],
            skipped=[
                {"fc_hz": round(fc, 1), "reason": reason}
                for fc, reason in self._fc_selection.skipped
            ],
            comparison_complete=self._fc_selection.comparison_complete,
            poses=len(self._lateral_poses),
        )

    @property
    def fc_selection(self) -> FcSelection | None:
        """This session's Fc RECOMMENDATION, or ``None`` if no sweep ran.

        Review accepts through Sound, then applies this exact candidate.
        """
        return self._fc_selection

    def _consume_cloud_position(
        self,
        phase: str,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> PhaseVerdict:
        verdict = self._cloud_position_verdict(
            phase, index, attempt, analysis, result
        )
        self._safe_log_diag(
            lambda a, v: self._log_cloud_diag(phase, index, a, v), analysis, verdict
        )
        return verdict

    def _cloud_position_verdict(
        self,
        phase: str,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> PhaseVerdict:
        """One prompted position: light per-capture QC, then the group check.

        The QC ladder — which screens run, in which order, and the two VERIFY
        gates a moved microphone makes inapplicable — belongs to
        :func:`~jasper.active_speaker.crossover_v2.spatial.cloud_position_screens`.
        What stays here is the retain-and-close half: minting the position from
        the prompt the operator was actually given, and taking the ONE critical
        section that the group close and the eager fit share.
        """
        response = analysis.summed_response
        # All SEVEN screens stated, though this ladder reads three. A cloud
        # position plays the summed VERIFY program, whose sweep is
        # ``KIND_SUMMED_SWEEP``, so the three sweep-domain predicates are
        # vacuously true here — but that is a fact about the capture, and it is
        # the caller's to state rather than the record's to assume. See
        # :class:`~jasper.active_speaker.crossover_v2.spatial.CaptureScreens`
        # for what a permissive default would cost the day a rung reads one.
        kind = _spatial.cloud_position_screens(
            _spatial.CaptureScreens(
                stimulus_located=_stimulus_locate_ok(analysis),
                pilot_snr_ok=analysis.pilot_snr_ok,
                linearity_ok=analysis.linearity_ok,
                glitch_detected=bool(analysis.glitch_detected),
                sweep_locate_confidence_ok=_sweep_locate_confidence_ok(analysis),
                sweep_schedule_ok=_sweep_schedule_ok(
                    analysis, self._verify_program.sample_rate_hz
                ),
                any_sweep_clipped=_any_sweep_clipped(analysis),
            ),
            has_summed_response=response is not None,
        )
        if kind is not None:
            return PhaseVerdict(False, _screen_refusal_code(kind))
        prompt = self._prompt_shown_for(phase, index)
        position = _CloudPosition(
            position_id=f"{phase}_{index:02d}",
            index=index,
            attempt=attempt,
            prompt=prompt.text,
            wide=prompt.wide,
            role=prompt.role,
            captured_at=time.time(),
            response=response,
            sample_rate_hz=self._verify_program.sample_rate_hz,
            echo_band_hz=self._cloud_echo_band.band_hz,
            signal_band_hz=self._cloud_signal_band_hz,
        )
        # ONE critical section for retain + close (eager-fit rider,
        # 2026-07-30). Everything the eager fit reads is written in here — the
        # retained positions, the combine they produce, and the cloud pipeline
        # result — and ``run_speculative_group_close`` takes the same lock, so
        # a fit running off the relay thread cannot observe this half-done. On
        # a VOLUNTARY retake this is also what makes the discard below atomic
        # with the re-stash.
        with self._close_lock:
            self._retain_cloud_position(phase, position, analysis, result)
            if not self._journey.plan.is_last_index_of_group(phase, index):
                return PhaseVerdict(
                    True, payload={"position_id": position.position_id}
                )
            return self._close_cloud_group(phase, position)

    def _retain_cloud_position(
        self,
        phase: str,
        position: _CloudPosition,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> None:
        """Record one position in the group and hand it to the evidence seam.

        Idempotent per index: a retaken position REPLACES the earlier take, so
        a group can never carry two curves for one prompted spot.

        **WO-1 moved the ``retain_position is None`` early return BELOW the
        metadata build**, so the record is assembled whether or not a retention
        seam is bound — see
        :func:`~jasper.active_speaker.crossover_v2.spatial.cloud_position_record`
        for the two consumers that ordering serves, and for what each field of
        the record is.

        The added cost when no seam is bound is one small dict plus one
        SHA-256 of the capture's WAV bytes (:func:`_capture_wav_sha256`) — a
        few milliseconds per accepted position, ~10 times per session.

        **That hash stays inside ``_close_lock`` on purpose.** ``_group_position_meta``
        is written here and read by :meth:`_run_cloud_pipeline` at the group
        close, and since the eager-fit rider that close can run on a
        background thread. Both sides are already under ``_close_lock`` —
        exactly the protection the rider's own comment claims for the
        retained positions and the cloud pipeline result — so the members and
        the aggregate a fit reads are always the same group's. Hoisting the
        hash out of the lock to shave milliseconds would buy a torn read.
        """
        retained = self._group_positions[phase]
        retained[:] = [p for p in retained if p.index != position.index]
        retained.append(position)
        retained.sort(key=lambda p: p.index)
        gating = getattr(position.response, "gating", None) or {}
        metadata = _spatial.cloud_position_record(
            position_id=position.position_id,
            phase=phase,
            index=position.index,
            attempt=position.attempt,
            prompt=position.prompt,
            wide=position.wide,
            role=position.role,
            captured_at=position.captured_at,
            session_id=self.session_id,
            gate_window_ms=_gate_window_ms(position.response),
            gate_floor_source=_gate_floor_source(position.response),
            gate_disclosure=_gate_disclosure(position.response),
            validity_floor_hz=getattr(
                position.response, "validity_floor_hz", None
            ),
            gating_applied=bool(gating.get("applied")),
            summed_ripple_db=analysis.summed_ripple_db,
            glitch_detected=bool(analysis.glitch_detected),
            wav_sha256=_capture_wav_sha256(result),
        )
        self._group_position_meta.setdefault(phase, {})[
            position.position_id
        ] = metadata
        if self._seams.retain_position is None:
            return
        try:
            self._seams.retain_position(position.position_id, result, metadata)
        except (OSError, RuntimeError, TypeError, ValueError):
            # Evidence retention is forensics, never a gate: a full disk must
            # not turn an acoustically-good position into a retake.
            log_event(
                logger, "correction.crossover_v2_position_retain_failed",
                level=logging.WARNING,
                session_id=self.session_id, phase=phase,
                position_id=position.position_id, exc_info=True,
            )

    def _close_cloud_group(
        self, phase: str, position: _CloudPosition | None
    ) -> PhaseVerdict:
        """The group-end combine, and the one bounded retake it can ask for.

        ``position`` is the take that just landed at the group's last index, or
        ``None`` when the group is closing because that position was SETTLED
        without a curve (:meth:`_resolve_spent_slot`). A settled close never
        asks for a geometry retake: the retake lever works by rejecting the
        take at this index, and there is no take — asking would re-open the
        slot whose tries are exactly what just ran out.

        Combines the group's retained positions exactly ONCE (S3 review
        finding, 2026-07-26: an earlier revision called
        ``combine_cloud_positions`` a second time from the pipeline step
        below — measured seconds-per-combine, 3-6 s across runs/hosts on the
        S0 ten-position corpus, worse on a Pi 5 (N2 review finding,
        2026-07-27: restated from an earlier "5.6-6.2 s" point figure that
        did not reproduce across hosts). With ``GEOMETRY_RETRY_POSITIONS = 2``
        allowing up to 3 close attempts per group, the pre-fix worst case was
        3 × 2 = 6 combines, not the earlier "4x" claim — real operator
        seconds this wiring does not need to spend). Both the retry-gating
        verdict AND the honest-instrument pipeline read the SAME ``combined``
        object.
        """
        positions = self._group_positions[phase]
        combined = combine_cloud_positions(positions)
        # PR-L5's spatial arm reads the across-position level spread of BOTH
        # groups. Stashed off the one combine this method already paid for
        # rather than added to the published group result, because it is
        # comparison input, not a disclosure the household reads.
        self._group_band_spread[phase] = tuple(
            getattr(combined, "band_spread", None) or ()
        )
        verdict = _geometry_verdict_from_combined(combined, len(positions))
        retries = self._geometry_retries_used[phase]
        # Four conjuncts and a narrowing, all of them subtle — see
        # :func:`~jasper.active_speaker.crossover_v2.spatial.geometry_retake`,
        # which owns why a THIN lock is accepted rather than retried and why a
        # close that has already recorded a verdict never asks again.
        retake = _spatial.geometry_retake(
            locked=verdict.get("locked"),
            thin_evidence=verdict.get("thin_evidence"),
            retries_used=retries,
            budget=GEOMETRY_RETRY_POSITIONS,
            group_already_closed=phase in self._group_geometry,
            have_take_to_replace=position is not None,
        )
        if retake is not None:
            # Narrowed by ``have_take_to_replace`` above: a retake is returned
            # only when there is a take at this index to drop.
            assert position is not None
            replacing = position
            self._geometry_retries_used[phase] = retake.retries_after
            # Drop the take being replaced FROM THE CLOUD. This is what the
            # protocol's retake lever means — the same index is measured again
            # — not a claim that dropping beats appending (see
            # GEOMETRY_RETRY_POSITIONS, where that claim was withdrawn). Its
            # evidence artifact stays on disk under its own attempt-qualified
            # path: the capture was fine, and a forensic record of what the
            # operator actually walked is worth more than a tidy bundle.
            retained = self._group_positions[phase]
            retained[:] = [p for p in retained if p.index != replacing.index]
            log_event(
                logger, "correction.crossover_v2_cloud_geometry_retry",
                session_id=self.session_id, phase=phase,
                retry=retake.retries_after, of=GEOMETRY_RETRY_POSITIONS,
                median_tau_us=verdict.get("median_tau_us"),
                clustered_fraction=verdict.get("clustered_fraction"),
            )
            prompt = CLOUD_GEOMETRY_RETRY_PROMPTS[
                min(retake.rung, len(CLOUD_GEOMETRY_RETRY_PROMPTS) - 1)
            ]
            return PhaseVerdict(
                False, REASON_CLOUD_GEOMETRY_LOCKED,
                payload={"prompt": prompt, "geometry": dict(verdict)},
            )
        # #1872: a retake of the group's LAST position can land AFTER the
        # group already closed once — a genuine voluntary retake (the retry
        # guard above requires exactly that: "never AFTER the group has
        # already recorded a verdict"), or a geometry-locked retry's own
        # retake arriving late because an EARLIER attempt at this same index
        # already exhausted the retry budget and was silently accepted
        # (session.py's confirm-hold widens the admission window this can
        # land in ON PURPOSE — see its own comment on ``completion_pending``
        # — because closing that window would also close the legitimate
        # voluntary-retake case). Either way this is a REAL close of the
        # group as it stands NOW, and everything below — the verdict, the
        # ``cloud_group_complete`` log, and ``_run_cloud_pipeline``'s
        # recompute (``_group_cloud_result``, the ``cloud_spec`` log) — runs
        # every time so nothing downstream (the FIT's exclusion evidence,
        # the disclosure screen, the journal) can describe a cloud the
        # household no longer walked. The one thing that is a per-phase
        # SINGLETON is the durable EVIDENCE ARTIFACT write — see
        # :meth:`_run_cloud_pipeline`'s own ``publish_cloud`` guard, which is
        # where the write-once evidence store's contract actually lives.
        self._group_geometry[phase] = verdict
        log_event(
            logger, "correction.crossover_v2_cloud_group_complete",
            session_id=self.session_id, phase=phase,
            positions=len(self._group_positions[phase]),
            geometry_locked=bool(verdict.get("locked")),
            geometry_reason=verdict.get("reason") or "",
            thin_evidence=bool(verdict.get("thin_evidence")),
            geometry_retries=retries,
            # Positions the flow gave up on (ruling #2086 item 3). A group that
            # closed short says so here, so a support read can tell a degraded
            # cloud from a walk the household completed.
            unresolved=len(self._group_unresolved.get(phase, {})),
        )
        # S4 review finding (2026-07-26): the group's accept is decided above
        # (the log line just fired) — the honesty pipeline below is
        # diagnostic/disclosure machinery layered on TOP of that decision, and
        # must never be able to cost the group its accept.
        #
        # **Scope, corrected 2026-07-27 (N1):** "decided" is not "recorded".
        # ``_note_accepted`` runs in ``consume_capture`` AFTER this method
        # returns, so a raise anywhere below — including the candidate build,
        # which is deliberately NOT wrapped — unwinds before the phase is
        # marked accepted. The resulting state is honest but worth naming:
        # ``event=correction.crossover_v2_cloud_group_complete`` is in the
        # journal, the group's geometry verdict is on the conductor, and the
        # phase is NOT in ``accepted_phases`` — so nothing durable claims a
        # completed group, and the host maps the raise to a terminal
        # ``internal_error`` screen. The claim this wrap makes is therefore
        # about the PIPELINE only: a named-family pipeline exception cannot
        # cost the accept. It says nothing about the candidate build below,
        # which is allowed to fail the capture, because it is the session's
        # product rather than its disclosure.
        # assemble_cloud_group_result's own try/except (ValueError, TypeError,
        # IndexError, AttributeError -- the documented raise surface of
        # everything it calls) and _run_cloud_pipeline's own try/except around
        # the publish_cloud seam (OSError, RuntimeError, TypeError, ValueError
        # -- the same family every other evidence-publish boundary in this
        # file uses) each guard their own step; this wrap is the outer
        # backstop for that SAME six-member named family (N1 review finding,
        # 2026-07-27: the prior wording claimed this was unconditional --
        # "structurally true rather than merely usually true" -- which
        # overclaimed past what the code does. A KeyError, or anything else
        # outside these six names, is NOT caught here either and propagates
        # uncaught exactly as assemble_cloud_group_result's own docstring
        # discloses -- pinned by
        # test_an_unnamed_exception_family_still_propagates_through_the_outer_wrap).
        # Scoped claim: a NAMED-family exception cannot cost the accept; the
        # residual propagates by design.
        try:
            self._run_cloud_pipeline(phase, combined, positions)
        except (OSError, RuntimeError, TypeError, ValueError, IndexError, AttributeError):
            log_event(
                logger, "correction.crossover_v2_cloud_pipeline_call_failed",
                level=logging.WARNING,
                session_id=self.session_id, phase=phase, exc_info=True,
            )
        payload: dict[str, Any] = {
            "group_complete": phase,
            "geometry": dict(verdict),
        }
        if position is not None:
            payload["position_id"] = position.position_id
        if phase == PHASE_CLOUD_MEASURE:
            # The pre-apply cloud's geometry verdict and disclosure pipeline are
            # in hand — but the FIT no longer runs here. Flow-simplification
            # §2.6: firing fit + auto-apply on this acceptance made the final
            # prompted position the one spot in the whole session a household
            # could not choose to redo, because the speaker was already being
            # retuned by the time the "Retake" control could have been tapped.
            # The fit moves to :meth:`confirm_cloud_measure_group`, which the
            # host calls when the household confirms PAST the final position.
            # No trust gate moved: the fit still runs only after the full
            # cloud, under the same gates — one user tap now sits in front of
            # it. Stash the combine so the confirm does not pay for a second
            # one (measured 2.7-6 s, see this method's own docstring).
            self._group_combined[phase] = combined
            # …and DROP any eagerly-fitted candidate in the same breath
            # (eager-fit rider, 2026-07-30). Reaching here a second time is a
            # VOLUNTARY retake (§2.6): the household redid the final position,
            # so the cloud just changed and anything fitted from the old one is
            # answering a question nobody asked any more. Dropping it here —
            # inside the same locked region that re-stashes the combine, and
            # BEFORE the accept that lets the host start the next eager fit —
            # is what makes "a bank always matches the current combine" hold
            # without a generation counter to check it against. Freeing the
            # reference also matters on its own: a candidate carries the fit's
            # arrays, and this is a 1 GB Pi.
            self._speculative_close = None
            payload["awaiting_confirm"] = True
        if phase == PHASE_CLOUD_VERIFY:
            # The delta probe's spatial arm, and the only point in the session
            # where it can run: both clouds are walked, so "did the correction
            # make the room less even" is finally a measured question rather
            # than a modelled one. Deliberately OUTSIDE the disclosure wrap
            # above — this is a product gate, like the candidate build, and a
            # gate that cannot fail the capture is not a gate.
            refusal = self._delta_probe_refusal(self._run_delta_probe())
            if refusal is not None:
                # No round grading on a refusal. The probe already rolled back
                # and named itself with the more specific code, and a group can
                # be retaken — so grading here would burn the fire-once guard
                # on evidence that may yet be replaced, exactly as it would on
                # a rejected VERIFY (see ``_consume_verify``). The receipt is
                # write-once, so "grade the first ending" is not a shape this
                # can safely take twice.
                return PhaseVerdict(
                    False, refusal,
                    payload={"delta_probe": self._delta_probe.to_dict()}
                    if self._delta_probe is not None else {},
                )
            # #2291: the Full tier's post-apply evidence is complete here — the
            # spatial arm has landed, so the spec verdict has a report and the
            # benefit verdict has everything it will get.
            return self._grade_round_once(PhaseVerdict(True, payload=payload))
        return PhaseVerdict(True, payload=payload)

    def cloud_measure_group_awaiting_confirm(self) -> bool:
        """Whether the pre-apply cloud is walked but not yet confirmed.

        True exactly between the final prompted position's acceptance and the
        household's confirmation past it — the window in which a voluntary
        retake of that position is still meaningful (§2.6), and the predicate
        the host wires as the runner's held-set gate (work order D1).

        **This asks about the HOUSEHOLD, not about the candidate** — the
        decoupling the eager-fit rider had to land before it could fit
        anything early (owner UX direction, 2026-07-30). Until then this read
        ``self._candidate is None``, which is ALSO
        :meth:`confirm_cloud_measure_group`'s fire-once guard, and both this
        seam and the host's ``completion_signal_required`` carried a comment
        warning what that conflation would cost: a candidate built BEFORE the
        confirm would have flipped this to False, un-held the runner's set and
        shut the retake window in the same instant, silently, at the exact
        moment the design exists to keep it open.

        So the two questions are now answered by two different fields.
        ``_group_confirmed`` records the household's own act and nothing else;
        ``_candidate`` stays the fire-once guard. An eagerly-built candidate
        parks in ``_speculative_close`` and is invisible here BY CONSTRUCTION —
        pinned by
        ``test_a_speculative_candidate_does_not_release_the_held_set``.
        """
        return (
            PHASE_CLOUD_MEASURE in self._group_combined
            and not self._group_confirmed
        )

    @property
    def cloud_close_state(self) -> str:
        """Where the pre-apply cloud's close has got to — the household-facing
        distinction the wizard renders while no candidate has been PROPOSED yet.

        ``awaiting_confirm`` (walked, the phone is showing the confirm screen),
        ``running`` (the household confirmed; the close is in flight), or ``""``
        (nothing pending — no cloud group, or the candidate is published, or the
        close already failed and its own failure state renders).

        "Proposed", not "exists", since the eager-fit rider (2026-07-30): a
        candidate may be BUILT and banked during ``awaiting_confirm`` and this
        deliberately keeps saying ``awaiting_confirm``, because what the screen
        reports is whose move it is, not whether a computation has finished.
        The household's move is on their phone until they make it, and an eager
        fit is invisible by design — see
        ``test_the_eager_fit_is_invisible_to_the_speaker_page``.
        """
        if self._candidate is not None:
            return CLOUD_CLOSE_NONE
        if self._group_close_running:
            return CLOUD_CLOSE_RUNNING
        if self.cloud_measure_group_awaiting_confirm():
            return CLOUD_CLOSE_AWAITING_CONFIRM
        return CLOUD_CLOSE_NONE

    def run_speculative_group_close(self) -> bool:
        """Fit the pre-apply cloud NOW, before the household confirms.

        The eager-fit rider's entry point (owner UX direction, 2026-07-30).
        The household walks the last stage-1 position, accepts it, and then
        carries a phone back to a browser — tens of seconds in which the
        speaker used to do nothing, so the several seconds of combine + fit
        were spent AFTER they arrived, as dead air on a screen that looked
        stalled. This runs that fit on the accept instead and banks it; the
        household's Continue then commits a finished candidate.

        Returns True when a build was banked. Safe to call at any time and
        from anywhere — every reason not to run is checked here rather than at
        the call site, so the host's trigger stays one line.

        **Runs OFF the relay thread** (the host starts it on a background
        thread) and is the only part of this conductor that does. It takes
        ``_close_lock`` for the whole fit, which is what keeps that honest: a
        retake's ``_close_cloud_group`` and the household's
        ``confirm_cloud_measure_group`` both take the same lock, so the three
        can interleave only at their boundaries, never inside the fit. The
        price is that a retake landing mid-fit waits for it — bounded by one
        fit, and paid while the household is walking to a spot, since the
        capture that follows a retake tap is itself far longer than a fit.

        **It never closes the retake window.** Nothing here writes
        ``_group_confirmed`` or ``_candidate``, so the runner's held set stays
        held and the phone keeps offering Retake exactly as long as it would
        have. A retake then DISCARDS what this banked
        (:meth:`_close_cloud_group`) and the next accept re-runs it.

        **A failure here is dropped, not remembered.** The bank stays empty and
        the confirm refits from scratch, so a household that hits the
        accountability veto or a fit bug sees the identical failure, raised
        from the identical place, at the identical moment they would have seen
        it before this rider existed. The cost is one wasted fit on a session
        that is already ending; the alternative — re-raising a stored
        exception across a thread boundary — buys seconds on a terminal path
        in exchange for a second, subtly different failure route to reason
        about. Rendering it EARLY was never an option: the household may still
        retake, which moots it entirely.
        """
        with self._close_lock:
            if not self.cloud_measure_group_awaiting_confirm():
                return False
            if self._speculative_close is not None or self._candidate is not None:
                return False
            if self._measure_analysis is None:
                return False
            combined = self._group_combined[PHASE_CLOUD_MEASURE]
            started = time.monotonic()
            try:
                built = self._build_measure_candidate(
                    self._measure_analysis, self._cloud_fit_evidence(combined),
                )
            except Exception as exc:  # noqa: BLE001 - see docstring
                # Deliberately open, and one of the few places in this file
                # that earns it: this is speculative work whose failure the
                # household has not asked about yet, and the confirm path is
                # about to run the same fit and raise the same thing where it
                # CAN be handled. Swallowing it here must therefore not depend
                # on guessing the fit's raise surface — the accountability veto
                # (``CaptureBeginRefused``) alone raises outside the named
                # families this file's other boundaries use. ``Exception``, not
                # ``BaseException``: a Stop or an interpreter teardown must
                # still tear this thread down rather than be logged as a fit
                # that merely did not bank.
                log_event(
                    logger, "correction.crossover_v2_speculative_close_failed",
                    level=logging.WARNING, session_id=self.session_id,
                    error=type(exc).__name__, exc_info=True,
                )
                return False
            self._speculative_close = built
            log_event(
                logger, "correction.crossover_v2_speculative_close_banked",
                session_id=self.session_id,
                candidate_fingerprint=built.candidate.fingerprint,
                elapsed_s=round(time.monotonic() - started, 3),
            )
            return True

    def note_group_close_started(self) -> None:
        """The household's set-completion signal arrived; the fit is next.

        The host calls this and persists BEFORE running the close, because the
        close is the session's slowest step (the combine plus the fit) and the
        wizard renders from durable state: without this write the speaker page
        would keep telling a household to confirm on their phone for the
        several seconds after they already did.

        **The corner that ordering buys, named rather than left to be found.**
        A crash in the window between that persist and the close leaves
        ``running`` on disk with nothing running — the speaker page would show
        "JTS is working out your correction" indefinitely. It is bounded (the
        wizard's Start over is present on every screen and clears the durable
        state) and it is the right trade: the alternative ordering lies to
        every household on every successful close, this one only after a
        crash. The cheap mitigation if it ever bites: ``running`` beside a
        relay that is no longer in flight is DETECTABLY stale, and the
        envelope already has ``status["relay"]`` to see that with.
        """
        self._group_close_running = True

    def confirm_cloud_measure_group(self) -> dict[str, Any] | None:
        """Close out the pre-apply cloud on the household's EXPLICIT confirmation.

        **This is the group-close seam** (§2.6), and since the two-stage split
        (work order D1) it is an explicit confirmation entry point rather than
        an inference. It used to take the 1-based wire ``index`` of a begin and
        gate on that index being strictly past the cloud group — in practice
        VERIFY's begin, which stage 1 no longer has, so a session that ends at
        the cloud would never have fitted anything at all. The host now calls
        this directly when the phone's set-completion signal arrives (the
        household tapping "Continue" on the "all spots measured" screen), and
        there is no index to reason about: a begin *inside* the group is not a
        confirmation because it does not come through this call at all.

        Returns the ``{candidate_fingerprint, headroom_cost_db}`` payload
        :meth:`_publish_measure_candidate` builds, or ``None`` when there is
        nothing to confirm. **It no longer carries an ``auto_apply`` flag and
        nothing downstream applies anything** — the candidate is a PROPOSAL the
        review screen shows, and the apply is a separate, explicit household
        POST to ``/correction/crossover/v2/apply``.

        Why a separate method the HOST calls, rather than folding it into
        :meth:`authorize_begin`: admission stays bookkeeping — budget, defer,
        refuse — and the one call that fits a correction stays visible at the
        host boundary, next to the ``persist_conductor_state`` that must
        precede anything reading the durable candidate.

        Fires at most once per session: the guard is ``self._candidate``, which
        :meth:`_publish_measure_candidate` sets — that guard, not the retired
        index gate, is what makes this idempotent against a repeated signal. A
        raise leaves it unset, so a genuinely retryable failure can be retried;
        a session with no cloud group (the 3-entry shape, the post-apply
        session) never has anything stashed and always returns ``None``.

        **Two guards now, not one** (eager-fit rider, 2026-07-30). The
        fire-once guard above is unchanged, but "is there anything to confirm"
        is asked directly — ``_group_combined`` — rather than borrowed from
        :meth:`cloud_measure_group_awaiting_confirm`, which since the rider
        answers the household's question instead. Recording the confirmation
        is this method's own first act, so the retake window shuts on the
        household's TAP and not on whether the fit that follows succeeds.

        **The fit may already be done.** When the eager close banked one
        (:meth:`run_speculative_group_close`), this commits it and returns in
        milliseconds; otherwise it fits here exactly as it did before the
        rider. The lock is what makes "may already be done" safe to ask: an
        eager fit still in flight holds it, so this simply waits for it rather
        than racing it.
        """
        with self._close_lock:
            if PHASE_CLOUD_MEASURE not in self._group_combined:
                return None
            if self._candidate is not None:
                return None
            self._group_confirmed = True
            log_event(
                logger, "correction.crossover_v2_cloud_group_confirmed",
                session_id=self.session_id, phase=PHASE_CLOUD_MEASURE,
                positions=len(self._group_positions[PHASE_CLOUD_MEASURE]),
                # Did the household's wait get to skip the fit entirely?
                banked=self._speculative_close is not None,
            )
            return self._close_measure_cloud_candidate(
                self._group_combined[PHASE_CLOUD_MEASURE]
            )

    def _close_measure_cloud_candidate(self, combined: Any) -> dict[str, Any]:
        """Fit, build, and publish the candidate the household will review.

        The relocated tail of :meth:`_measure_verdict` (owner decision,
        2026-07-27). It runs once per session, driven by
        :meth:`confirm_cloud_measure_group` (flow-simplification §2.6 moved
        the trigger from the final position's ACCEPTANCE to the household's
        confirmation past it; the two-stage split, work order D1, made that
        confirmation an explicit signal and moved the APPLY out of the session
        entirely). What it produces is a PROPOSAL, not an action.

        **The fit now consumes the cloud** (plan interpretation call (A), the
        wiring half of PR-6): :func:`_cloud_fit_evidence` turns this group's
        closed pipeline result into the merged honesty intervals and the
        cross-position spread that :func:`compose_envelope`'s
        ``spatial_exclusion_limit`` / ``position_stability_limit`` terms
        consume. A group whose pipeline did not become available yields
        ``None`` and the fit runs exactly as it did before this move —
        disclosed, not silent (see :func:`_cloud_fit_evidence`).

        Reaching this with ``_measure_analysis`` already ``None`` means MEASURE
        was accepted by a DIFFERENT conductor instance — the same-session
        ``hydrate`` branch, which carries ``accepted_phases`` but no analysis.
        (The tail of this method releases the analysis, but that release
        happens strictly AFTER this check and only once per group, so it can
        never be what this branch is seeing — see the release comment for why
        a second close of one group is structurally impossible.)
        **Production cannot reach it**: ``prepare_v2_session`` hydrates against
        a freshly MINTED relay session id, so the id never matches and hydrate
        always takes the fresh-start-at-CHECK branch (§5.6's own rule). If it
        is ever reached, this raises rather than returning a payload with no
        candidate behind it: a confirmation that silently produced nothing
        would leave the household on a review screen with nothing to review,
        and an honest ``internal_error`` screen beats that.
        """
        if self._measure_analysis is None:
            raise CrossoverV2FlowError(
                "cloud-measure group closed with no retained MEASURE analysis"
            )
        # The eager fit's payoff, and the whole point of the rider: when a
        # build is banked, the household's confirmation costs a commit rather
        # than a fit, and the review screen is up by the time they look at it.
        # A bank is only ever present for the CURRENT combine — a retake drops
        # it in the same locked region that re-stashes the combine — so
        # consuming it here cannot smuggle a stale cloud past the confirm.
        banked = self._speculative_close
        if banked is not None:
            self._speculative_close = None
            payload = self._commit_measure_candidate(banked)
        else:
            payload = self._publish_measure_candidate(
                self._measure_analysis, self._cloud_fit_evidence(combined)
            )
        # Released on success: the fit has consumed it and nothing reads it
        # again, so a tens-of-megabytes reference should not survive to the end
        # of a session that still has six captures to go (see the field's
        # comment in ``__init__``).
        #
        # **Why releasing cannot strand a re-delivered capture.** Releasing
        # makes a SECOND call raise instead of rebuilding, so it is only safe
        # if a second call cannot happen — and it cannot: the sole caller
        # (``confirm_cloud_measure_group``) refuses once ``self._candidate`` is
        # set, which the line above does. Neither retake shape is a
        # counter-example: a GEOMETRY retake returns REJECTED from
        # ``_close_cloud_group`` well before any confirm, and a VOLUNTARY
        # retake (§2.6) is only admitted while the confirm has not happened,
        # so it re-closes the group and re-stashes the combine without ever
        # reaching here twice. Left in place on a raise — that session is
        # already failing, and the conductor is about to be discarded.
        self._measure_analysis = None
        return payload

    def _publish_measure_candidate(
        self, analysis: ProgramAnalysis, cloud: "_CloudFitEvidence | None",
    ) -> dict[str, Any]:
        """Build and publish one candidate for the household to review.

        The single build/publish path, called from whichever capture is the
        last of the measuring session for this shape — the CLOUD_MEASURE group
        close when the session runs one, MEASURE's own accept when it does not
        (see :meth:`_measure_verdict`). Returns the candidate's identity and
        its disclosed level cost; nothing it returns triggers an apply.

        **The accountability seam (linearization-integrity PR-L4).** This is the
        last moment before the speaker is touched, and it is where the two
        load-bearing assertions live — the realized inter-driver level (item 1)
        and the spec-graded prediction (item 2). Both run AFTER the build and
        BEFORE ``self._candidate`` is set and ``publish_candidate`` fires, so a
        refusal leaves no candidate for anything downstream to apply, and the
        confirm seam's ``CaptureBeginRefused`` arm persists a named reason with
        its own household copy.

        They live here and not inside :meth:`_build_candidate` on purpose: that
        method's SF2 arm catches a fit-engine failure and degrades to the
        trims-only path, which is the right answer for a BUG in the fit and
        exactly the wrong answer for an accountability refusal — quietly
        shipping an unlinearized candidate is the silent-failure shape this PR
        exists to remove.

        On the pre-cloud 3-entry shape — which no production caller constructs
        (see :meth:`_measure_verdict`'s own note) — this method is reached from
        ``consume_capture`` instead, so a refusal propagates out of THAT seam
        rather than the confirm one and lands in the host's catch-all as
        ``internal_error``. Still loud, still leaves the speaker untouched, just
        without the named screen; a caller reviving that shape has to wire its
        own refusal handling, exactly as it has to wire its own apply trigger.

        **Split into build + commit** by the eager-fit rider (2026-07-30), so
        the expensive half can run before the household confirms while the
        half that MUTATES this conductor waits for them. This method is the
        two called back to back and is what every pre-rider caller still gets.
        """
        return self._commit_measure_candidate(
            self._build_measure_candidate(analysis, cloud)
        )

    def _build_measure_candidate(
        self, analysis: ProgramAnalysis, cloud: "_CloudFitEvidence | None",
        *,
        candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
        source_preset: Any = None,
    ) -> _SpeculativeClose:
        """Fit and accountability-gate one candidate. Commits NOTHING.

        The expensive half of :meth:`_publish_measure_candidate` — seconds of
        fit — and the half the eager-fit rider runs off the relay thread before
        the household has confirmed anything.

        **What "commits nothing" has to mean for that to be safe.** Three
        things make a candidate REAL, and none of them happen here: it is not
        written to ``self._candidate`` (the fire-once guard), the
        ``publish_candidate`` seam does not fire (no evidence is written), and
        the retained MEASURE analysis is not released. So a build that a retake
        moots can simply be dropped, leaving the conductor exactly as it was.

        The accountability gate DOES run here, and deliberately: it is part of
        producing a candidate, not part of proposing one. It raises
        ``CaptureBeginRefused`` before anything is banked, which on the eager
        path means the bank stays empty and the confirm refits — see
        :meth:`run_speculative_group_close` for why that costs a failing
        session one extra fit and buys an unchanged failure path.

        **It writes no conductor state at all** since #2291 Phase 2b. The fit's
        by-products — the outcome string, the linearized VERIFY prior, the
        level-frame evidence and the realized-level verdict — used to land on
        ``self`` as seven ``_last_*`` fields; they now ride the returned
        :class:`_SpeculativeClose` with everything else this build produced.
        The eager path still holds ``_close_lock``, for the ordering of the
        commit that follows rather than for state this method leaves behind.
        """
        if candidate_sections is None and source_preset is None:
            candidate, linearization = self._build_candidate(analysis, cloud)
        else:
            candidate, linearization = self._build_candidate(
                analysis, cloud, candidate_sections=candidate_sections,
                source_preset=source_preset,
            )
        # VERIFY-prediction coherence fix (hardware-validation-caught, #1668
        # PR-D): when this attempt fitted Layer-1a linearization (fitted OR
        # trim_rejected — both emit the correction filters, see
        # ``plan_linearization``'s tail), the persisted prediction VERIFY
        # compares against must be the LINEARIZED model, the exact thing the
        # emitted graph now carries — never the raw-branch one. The
        # ineligible/fit_failed path is untouched: the state's
        # ``linearized_predicted_sum`` is ``None`` there, so this stays
        # byte-identical to ``analysis.predicted_sum``, as before. It is
        # computed here rather than at MEASURE because the fit is here; nothing
        # reads it in between (``_cloud_priors`` deliberately carries no
        # ``predicted_sum``, and VERIFY is the next capture after this close).
        predicted_sum = (
            linearization.linearized_predicted_sum
            if linearization.linearized_predicted_sum is not None
            else analysis.predicted_sum
        )
        # PR-L4: the last gate before a candidate can be proposed at all.
        # Raises CaptureBeginRefused, so nothing below runs — no candidate is
        # stashed, none is published, and the review screen has nothing to
        # offer rather than an unaccountable proposal.
        level_frame_finding = self._assert_accountable(
            predicted_sum, analysis.predicted_sum, linearization=linearization,
        )
        return _SpeculativeClose(
            candidate=candidate,
            predicted_sum=predicted_sum,
            analysis=analysis,
            cloud=cloud,
            level_frame_finding=level_frame_finding,
            linearization=linearization,
        )

    def commit_intervention_proposal(
        self,
        candidate: Any,
        *,
        predicted_sum: Any,
        commanded_delta: Any,
        level_frame_finding: Mapping[str, Any] | None,
        realized_branch_level: Mapping[str, Any] | None = None,
    ) -> None:
        """The ONE seam through which a planned candidate becomes real (#2291).

        Both commit sites — the configured-Fc walk
        (:meth:`_commit_measure_candidate`) and the alternative-Fc selection
        (:meth:`_commit_fc_candidate`) — install a candidate through here, so
        Phase 2 has a single place to hollow rather than two near-duplicate
        inline blocks that had already drifted.

        **What this seam covers, exactly:** the three conductor state writes
        that were byte-identical at both sites (``_candidate``,
        ``_measure_predicted_sum``, ``_measure_commanded_delta``), the two
        irreversible seam fires (``publish_candidate`` then
        ``_publish_level_frame_finding``), and — new, and consuming nothing —
        the #2291 proposal contract.

        **What it deliberately does NOT cover:** ``_measure_predicted_spec_
        report``, and the ``correction.crossover_v2_candidate_built``
        disclosure.  Both differ between the two sites today — the walk installs
        the spec report out-of-band from inside ``_assert_accountable``
        (``_stash_predicted_spec_report``) while the selection installs it here,
        and the two log lines carry different fields.  Folding either in would
        be a behavior change, which Phase 1 is not; they stay at their call
        sites until Phase 2 makes the planner return them as data.

        ``realized_branch_level`` arrives ALREADY SERIALIZED (#2291 Phase 2b).
        Both sites now hold their candidate's own verdict, but by different
        routes — the walk off its build's :class:`_LinearizationState`, the
        selection off the retained
        :class:`~jasper.active_speaker.fc_selector.FcCandidateEvaluation`,
        which may only carry plain data across the walk — so the mapping is
        what they have in common. This seam does no conversion; the owner of
        each verdict serializes it.

        Ordering is preserved rather than merely similar: every conductor
        attribute write still completes before ``publish_candidate``, the first
        observable side effect, so a re-entrant reader sees exactly what it saw
        before.  The proposal is assembled last, after every pre-existing side
        effect, and cannot raise — see :func:`plan_intervention_proposal`.
        """
        self._candidate = candidate
        self._measure_predicted_sum = predicted_sum
        self._measure_commanded_delta = commanded_delta
        self._seams.publish_candidate(candidate)
        self._publish_level_frame_finding(level_frame_finding)
        self._intervention_proposal = plan_intervention_proposal(
            candidate,
            session_id=self.session_id,
            predicted_response_after=predicted_sum,
            predicted_spec_after=self._measure_predicted_spec_report,
            commanded_delta=commanded_delta,
            accountability=level_frame_finding,
            realized_branch_level=realized_branch_level,
            evidence_identities={
                "session_id": self.session_id,
                "program_id": str(getattr(candidate, "program_id", "") or ""),
            },
        )

    def _commit_fc_candidate(self, evaluation: FcCandidateEvaluation) -> dict[str, Any]:
        from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate

        if evaluation.candidate is None or evaluation.predicted_sum is None:
            raise CrossoverV2FlowError("selected Fc has no executable candidate")
        candidate = MeasuredCrossoverCandidate.from_mapping(evaluation.candidate)
        self._measure_predicted_spec_report = dict(
            evaluation.predicted_spec_report or {}) or None
        # THIS candidate's own realized-level verdict, retained on its own
        # evaluation (#2291 Phase 2b, closing #2307 gate note N6). Until the
        # cutover the only verdict reachable here belonged to the ANCHOR — the
        # sweep restored the conductor's scratch fields when it ended — so
        # passing anything would have been the cross-context leak #2291 exists
        # to close, and the proposal recorded the absence instead. The sweep
        # now carries each candidate's own, so the selected corner's proposal
        # describes the corner that was selected.
        self.commit_intervention_proposal(
            candidate,
            predicted_sum=evaluation.predicted_sum,
            commanded_delta=evaluation.commanded_delta,
            level_frame_finding=evaluation.level_frame_finding,
            realized_branch_level=evaluation.realized_branch_level,
        )
        log_event(
            logger, "correction.crossover_v2_candidate_built",
            session_id=self.session_id, candidate_fingerprint=candidate.fingerprint,
            linearization=candidate.linearization_outcome,
            selected_fc_hz=round(evaluation.fc_hz, 1), cloud_evidence=False,
            excluded_bands=0, cloud_positions=0)
        return {"candidate_fingerprint": candidate.fingerprint,
                "headroom_cost_db": worst_headroom_cost_db(candidate.linearization)}

    def _commit_measure_candidate(self, built: _SpeculativeClose) -> dict[str, Any]:
        """Make a built candidate REAL: stash it, publish it, disclose it.

        The cheap half of :meth:`_publish_measure_candidate`, and the ONLY
        place a candidate becomes visible to anything else. Reached identically
        whether the fit ran moments ago on the household's confirmation or
        seconds earlier on the eager path — the eager fit buys latency, never a
        different product, which is why the two halves are split here rather
        than duplicated.

        Runs on the relay thread under ``_close_lock``, so the ``_candidate``
        write and the ``publish_candidate`` seam — the two irreversible acts —
        happen exactly once, in confirmation order.
        """
        candidate = built.candidate
        predicted_sum = built.predicted_sum
        analysis = built.analysis
        cloud = built.cloud
        # This build's own realized-level verdict, off the state it returned —
        # the same route the selected-Fc path takes, differing only in where
        # the state was held between planning and commit.
        self.commit_intervention_proposal(
            candidate,
            predicted_sum=predicted_sum,
            commanded_delta=_commanded_delta(analysis.predicted_sum, predicted_sum),
            level_frame_finding=built.level_frame_finding,
            realized_branch_level=built.linearization.realized_branch_level,
        )
        log_event(
            logger, "correction.crossover_v2_candidate_built",
            session_id=self.session_id,
            candidate_fingerprint=candidate.fingerprint,
            # Which linearization path this candidate's build took. This field
            # lived on ``correction.crossover_v2_measure_diag`` until the
            # timing move; it could not stay there, because that line is
            # emitted eight captures before the fit now runs and would report
            # "" forever (the retired-field treatment PR-5 gave the per-capture
            # ``flatness_*`` fields, for the same reason). Read off the
            # candidate rather than a conductor field since #2291 Phase 2b:
            # the build stamped it there, so the disclosure and the artifact
            # quote one value instead of two that agreed by timing.
            linearization=candidate.linearization_outcome,
            # Did the cloud's honesty verdict actually reach the envelope?
            cloud_evidence=cloud is not None,
            excluded_bands=len(cloud.excluded_bands_hz) if cloud else 0,
            cloud_positions=cloud.n_positions if cloud else 0,
        )
        return {
            "candidate_fingerprint": candidate.fingerprint,
            # (An ``auto_apply: True`` literal lived here until the two-stage
            # split, PR-T3. It told the host to fire the apply the moment this
            # returned — unconditionally, three seconds before VERIFY, with the
            # household holding a phone. Apply is now the household's own POST
            # from the review screen, so a key named for an automatic trigger
            # would name a path that no longer exists; it is DELETED rather
            # than left inert, because "nothing applies without an explicit
            # household action" is this flow's invariant and a vestigial flag
            # is how an invariant quietly comes back undone.)
            #
            # "This correction costs N dB of maximum level" (PR-L5). The
            # worst branch's charge — the quantity the graph actually gives up,
            # and the same number the emitter absorbs.
            #
            # This is the CONFIRM payload, which the host reads for
            # ``auto_apply``; it is not by itself the household disclosure the
            # owner's ruling asks for. That one is persisted by
            # ``correction_crossover_v2._candidate_summary`` (same reducer) and
            # reaches the envelope's screens only through
            # ``crossover_envelope_v2._candidate_review_payload``, which
            # projects it as ``headroom_cost`` — the screens read that payload,
            # never the summary directly. Both numbers are here because they
            # answer to different readers, and both come from
            # ``worst_headroom_cost_db`` so they cannot drift.
            "headroom_cost_db": self._candidate_headroom_cost_db(),
        }

    def _publish_level_frame_finding(
        self, record: Mapping[str, Any] | None,
    ) -> None:
        """Persist the banked frame disagreement, or say why it was not.

        **Called here — after ``publish_candidate``, inside
        :meth:`_commit_measure_candidate` — and that placement is the design.**
        Three properties fall out of it and none of them survive moving the
        call to the gate:

        * **Once per session.** ``_commit_measure_candidate`` runs under
          ``_close_lock`` behind ``confirm_cloud_measure_group``'s fire-once
          ``_candidate`` guard, whereas the gate itself runs on the
          speculative build AND again on the confirm when a retake mooted it.
          The finding store is write-once, so a gate-site publish would ask it
          twice for one path and hit a PATH_CONFLICT on the second.
        * **Never for a candidate that does not exist.** A build the household
          retakes past, or one item 1 or item 2 refuses after the frame gate
          banked, leaves no finding — which is correct: the record describes
          the frame behind a specific proposal, and there is no proposal.
        * **The citation resolves.** The finding cites the candidate artifact,
          which the line above just published.

        Fail-soft, and deliberately louder about it than the seam beneath it.
        Plan §3.4 makes findings *optional evidence artifacts* — "a session
        with no findings behaves exactly as it does today" — so a store
        failure must not undo a candidate that is already durable and already
        published. The gate has decided; this is disclosure.
        """

        if record is None or self._seams.publish_findings is None:
            return
        try:
            self._seams.publish_findings(record)
        except (OSError, RuntimeError, TypeError, ValueError):
            log_event(
                logger, "correction.crossover_v2_level_frame_finding_failed",
                level=logging.WARNING, session_id=self.session_id, exc_info=True,
            )

    def _candidate_headroom_cost_db(self) -> float:
        """The applied correction's disclosed max-level cost, dB (PR-L5).

        Delegates to the fit module's own reducer so this payload and the web
        layer's browser-visible one cannot disagree about a household-facing
        number.
        """
        linearization = getattr(self._candidate, "linearization", None)
        if not isinstance(linearization, Mapping):
            return 0.0
        return worst_headroom_cost_db(linearization)

    def _refuse(self, code: str) -> "CaptureBeginRefused":
        """Build the refusal for ``code``, with that code's household copy, and
        record it as this conductor's failure code.

        One construction point so a refusal can never ship a bare code where a
        household expects a sentence (:data:`REASON_REGISTRY` is the §5.10 SSOT
        for the code, its template, and its budget; since #2085 the sentence
        itself comes from :func:`reason_message` — see below).

        **Stamping ``_last_failure_code`` is the load-bearing half**, not
        bookkeeping. The host's ``CaptureBeginRefused`` arm persists
        ``conductor.last_failure_code`` and falls back to
        :data:`REASON_RELAY_TIMEOUT` when it is unset — so a refusal that
        raised without stamping would reach the household as "The measurement
        link timed out", a false statement about a session that was refused on
        purpose. Raising through this one constructor is what makes the
        registry copy above actually the copy that renders.

        Copy comes from :func:`reason_message`, not from ``spec`` directly
        (#2085), so a refusal built here renders the same sentence the
        capture's own relay verdict did. No code routed through this method is
        evidence-keyed TODAY — the accountability refusals hold literals — but
        every other render path now asks the selector, and leaving one that
        does not is how the two accounts diverge again the first time a
        refusal code grows a fact to branch on.

        The evidence is read BEFORE the stamp below, because
        ``_pilot_heard_for`` answers "does the evidence I hold belong to this
        code", and stamping first would make that question answer itself.
        """
        spec = REASON_REGISTRY[code]
        pilot_heard = self._pilot_heard_for(code)
        self._last_failure_code = code
        self._last_failure_pilot_heard = pilot_heard
        return CaptureBeginRefused(
            code, reason_message(code, spec, pilot_heard=pilot_heard),
        )

    def _assert_accountable(
        self, predicted_sum: Any, raw_predicted_sum: Any = None,
        *, linearization: _LinearizationState | None = None,
    ) -> Mapping[str, Any] | None:
        """The three accountability assertions, run before the PROPOSAL exists:
        PR-L5's shared-level-frame agreement, then PR-L4's items 1 and 2.

        ``linearization`` is the candidate's own planner output — the
        decision-as-data half of #2291. The gate reads its level-frame and
        realized-level verdicts and owns only the *decision*: which refusal
        fires, what the household is told, and what is banked. ``None`` means
        no build produced one, which is the same evidence state as an
        ineligible session and takes the same path: no frame to disagree, no
        realized verdict to fail, and item 2's abstain below.

        "Pre-apply" until PR-T3, when the apply moved out of the session
        entirely; the gate did not move with it, and did not need to. Refusing
        here means no candidate is ever stashed or published, so the review
        screen has nothing to offer and the household is never asked to decide
        about a correction JTS cannot stand behind.

        Raises :class:`CaptureBeginRefused` with a named
        :data:`REASON_REGISTRY` code — the host's own refusal arm then persists
        it and the envelope renders its copy, so a refusal here reaches the
        household as a sentence rather than a stall.

        Returns the caller's **banked finding record** when the frame gate took
        the #1866 finding+proceed path, and ``None`` otherwise; either way the
        caller proceeds to publish. It is returned rather than stashed on the
        conductor on purpose: this method runs on the SPECULATIVE build too
        (:meth:`run_speculative_group_close`), and a build a retake moots is
        simply dropped — a record on ``self`` would outlive the candidate it
        describes and be published against the next one.

        Order is most-specific-first, and each step is a narrower diagnosis of
        the one after it. The FRAME gate (PR-L5) leads: it asks whether the two
        instruments a trim is derived from still agree about where the drivers
        sit, which is upstream of any trim. Item 1 follows, grading the level
        the committed trim actually REALIZES — the backstop for a frame that
        agreed but a trim that still landed wrong. Item 2 is last and most
        general: this correction does not measure better. When more than one is
        true, naming the earliest cause is more useful to whoever reads the
        journal, and the household copy is more actionable.
        """
        # --- PR-L5: the two level FRAMES agree ---------------------------
        #
        # Runs before item 1 because it is the more specific diagnosis of the
        # same disease: item 1 grades the level the committed trim REALIZES,
        # this grades whether the two instruments that trim was derived from
        # still agree about where the drivers sit. On the 2026-07-27 captures
        # the disagreement was 10.9-13.1 dB; PR-L3 fixed its cause, and this
        # is what stops the next cause from shipping silently.
        #
        # It refuses under PR-L4's own ``driver_levels_disagree`` code, not a
        # new one: the household's remedy is identical (re-check sensitivity
        # and the pad in speaker setup) and one consistent sentence beats two
        # near-duplicates. The journal separates them by ``event=``.
        #
        # **The refusal is no longer unconditional (owner ruling, #1866,
        # 2026-07-30).** A disagreement over tolerance now asks ONE more
        # question before it stops the session: does the realized-level check
        # pass on the pair this session is about to ship? If it does, the
        # session banks the disagreement as a finding and PROCEEDS; the hard
        # refusal remains only when the realized check ALSO fails. Why the
        # ruling went that way, in one line: #1929 removed a structural bias
        # from one estimator, it did not make the two agree, and what is left
        # refuses healthy speakers — a pair identical by construction reads
        # 0.910 dB apart and ordinary woofer passband tilt adds ~1.33 dB per
        # dB/octave, so a −2 dB/oct woofer refuses at 3.574 while the realized
        # instrument reads 1.41 and passes. The field case is the 2026-07-30
        # session: 3.2307 dB under the banded estimator, realized −0.247,
        # predicted on-axis residual 3.106 → 1.333 dB (all recorded on #1870).
        # Refusing that is a false negative on a good tune, and the diagnosis
        # the gate already computed reached no artifact at all.
        #
        # **What "proceeds" commits, stated precisely — because the obvious
        # reading is wrong.** The ruling's own wording is "proceeds on the
        # near-Fc anchor (the trim solve)", and that describes an outcome the
        # code does not produce. Proceeding changes NOTHING about the trims:
        # the fit commits the anchor it always computed, and in
        # ``anchor_base + giveback + level_frame_offset`` the trim term
        # CANCELS — ``offset = system − trim − core``, leaving
        # ``giveback + system − core`` (the cancellation is derived in
        # ``anchor_base_db``'s own comment). So the committed inter-driver
        # placement is set by the CORE-MEDIAN frame — the disputed estimator —
        # not by the trim solve. On the conductor fixture: committed −0.674,
        # which is the core-median value to 4 dp; anchoring on the trim solve's
        # placement instead would give +2.535; the two differ by 3.209, exactly
        # the banked disagreement, which is not a coincidence but the identity
        # ``placement_trim − placement_core = −offset``.
        #
        # The honest description of this branch is therefore: **the pipeline
        # commits the anchor it always computed (which embeds the disputed
        # estimator); proceeding is the same tune, not refused; the realized
        # check gates the OUTCOME rather than selecting an estimator.** That is
        # a weaker claim than "we proceed on the corroborated estimator" and it
        # is the true one — the realized check's pass is evidence that the
        # shipped pair is level, not evidence about which estimator was right.
        #
        # **RATIFIED.** This description differs from the ruling's original
        # wording, so it was put to the owner rather than merged under the
        # inverted account; the owner confirmed it on 2026-07-30 (#1866 comment
        # 5137494519) as "the ruling's operative form". The two phrases above
        # are retired: anything still asserting them is describing a mechanism
        # this code does not implement.
        #
        # **What the realized check is, and is not.** It is a CLOSED-LOOP
        # check, not cross-band arbitration: its own docstring says "One
        # estimator, not a second opinion" — the levels come from
        # ``solve_branch_trims`` on the TRIMMED pair, the same power-band
        # average over the same ``branch_level_bands_hz`` halves that set the
        # trim. So it cannot referee the two frames against each other, and
        # nothing here should read as if it did. What it IS: independent of the
        # fit's core median (different inputs — the post-fit linearized
        # branches — different band, different statistic), and non-vacuous —
        # it fails on a −6 dB/oct woofer where the frame gate also fails. It
        # answers one question, the useful one: did the pair we are about to
        # ship end up level?
        #
        # **Ordering: nothing moved, and nothing needed to.** The realized
        # verdict this branch consults is item 1's own
        # ``linearization.realized_level_match``, which reads later in this
        # method but was computed earlier in the build — the planner returns
        # the frame and the realized match on one plan, complete before
        # ``_build_measure_candidate`` calls this. There is no reordering here
        # and no second computation: item 1 keeps its own gate, its own event,
        # and its own refusal below, and every OTHER gate's semantics are
        # byte-identical to before this change.
        #
        # ``match is None`` (no fit ran) falls to the refusal, and that is the
        # fail-closed direction rather than an oversight: with no realized
        # verdict there is no outcome check to gate on, so the ruling's
        # precondition is unmet. In practice it is unreachable from here — the
        # frame is only non-zero when a fit completed, and a fit that raised
        # part-way yields a state carrying neither — but a future path that
        # separates them must refuse, not proceed.
        state = linearization if linearization is not None else _LinearizationState()
        if (
            state.level_frame_disagreement_db
            > LEVEL_FRAME_AGREEMENT_TOLERANCE_DB
        ):
            frame = state.level_frame
            realized = state.realized_level_match
            banked = realized is not None and realized.matched
            log_event(
                logger,
                (
                    "correction.crossover_v2_level_frame_finding" if banked
                    else "correction.crossover_v2_level_frame_refused"
                ),
                level=logging.WARNING if banked else logging.ERROR,
                session_id=self.session_id,
                reason="" if banked else REASON_DRIVER_LEVELS_DISAGREE,
                disagreement_db=round(
                    float(state.level_frame_disagreement_db), 3
                ),
                tolerance_db=LEVEL_FRAME_AGREEMENT_TOLERANCE_DB,
                system_level_db=(
                    round(float(frame.system_level_db), 3)
                    if frame is not None else None
                ),
                reference_role=frame.reference_role if frame is not None else "",
                offset_db=(
                    {k: round(float(v), 3) for k, v in frame.offset_db.items()}
                    if frame is not None else {}
                ),
                core_level_db=dict(state.level_frame_cores),
                # The two fields the finding path adds, and only it: the OTHER
                # estimator's per-role level-match term, and the realized
                # verdict that decided which way this went. Both are ``None``/
                # ``{}`` on the refusal arm so that line stays what #1934
                # shipped.
                trim_band_average_db=(
                    {k: round(float(v), 3)
                     for k, v in state.level_frame_trims.items()}
                    if banked else {}
                ),
                realized_difference_db=(
                    round(float(realized.difference_db), 3)
                    if banked and realized is not None else None
                ),
            )
            if not banked:
                raise self._refuse(REASON_DRIVER_LEVELS_DISAGREE)
            finding = self._level_frame_finding_record(state)
        else:
            finding = None

        # --- item 1: the inter-driver realized level ---------------------
        match = state.realized_level_match
        if match is not None and not match.matched:
            log_event(
                logger, "correction.crossover_v2_level_match_refused",
                level=logging.ERROR, session_id=self.session_id,
                reason=REASON_DRIVER_LEVELS_DISAGREE,
                difference_db=round(float(match.difference_db), 3),
                tolerance_db=match.tolerance_db,
                level_w_db=round(float(match.level_w_db), 3),
                level_t_db=round(float(match.level_t_db), 3),
            )
            raise self._refuse(REASON_DRIVER_LEVELS_DISAGREE)

        # --- item 2: spec-grade the prediction ---------------------------
        #
        # PR-6b made auto-apply unconditional at this seam ("this is
        # unconditionally True here, not a second decision"). This deliberately
        # AMENDS that, under the linearization-integrity work order's PR-L4
        # item 2 (docs/linearization-integrity-plan.md), which is the sanction
        # for the change: on 2026-07-27 the honest flatness instrument failed
        # all three bands two seconds before an unconditional auto-apply, and
        # its verdict reached zero surfaces. PR-6b's claim — that MEASURE's
        # trust gates already decided — was true about the CAPTURE and silent
        # about the CORRECTION. This adds the missing half: the capture is
        # trusted, and now the thing built from it has to show its work.
        #
        # **BEFORE and AFTER, on the same instrument** (PR-L4 review B1). The
        # first cut of this gate compared the model's residual against the
        # MEASURED pre-apply cloud's, which is not a comparison: an
        # eight-position in-room spatial mean and a gated two-branch model at
        # the mark are different instruments in different frames, so the margin
        # between them is room-sized. Held to a constant, excellent correction
        # (predicted pooled 0.858 dB) the reviewer varied only the ROOM and
        # watched the verdict flip — the shipped fixture applied at +0.333, and
        # every BETTER room refused. That is exactly backwards: it punished
        # good rooms, and it tightened as the correction improved. Worse, it was
        # a live trap — an owner who undoes first re-measures a decent speaker
        # and re-runs into a stricter bar.
        #
        # The fix is to ask the question the gate was always trying to ask, of
        # one instrument: grade the RAW pre-fit two-branch prediction and the
        # LINEARIZED one through the IDENTICAL `spec_report_for_predicted_sum`,
        # and require the correction to move ITS OWN model materially. Same
        # branches, same grid, same evaluator, same position — the room cancels
        # because it is not in either term.
        #
        # **Graded ONCE, here** (two-stage commission D4). This is the last
        # place the FULL-RESOLUTION `(freqs, magnitudes)` tuple exists: what
        # survives to the durable state is `_decimate_sum`'s 512-point block
        # average (issue #1858 — a raw stride before that fix), and re-grading
        # that later would be a DIFFERENT instrument from the one this veto
        # refuses on — the two can disagree on a narrow band,
        # on the one screen whose entire purpose is the honest spec verdict. So
        # the report this gate computes is the report the host persists, and
        # the persisted curve stays what it is: a drawing, not the instrument.
        #
        # It is hoisted ABOVE the trims-only abstain below (it used to sit
        # underneath) for a reason the gate itself does not care about but the
        # review screen does: the trims-only lane still commits trims and still
        # predicts a response, so it HAS a gradeable prediction. Leaving it
        # ungraded would put "we could not predict this" in front of a
        # household about a prediction we can in fact grade. **The gate's own
        # decisions are untouched** — every `return` and `raise` below is
        # exactly where it was, reached on exactly the same condition.
        after = spec_report_for_predicted_sum(predicted_sum)
        self._stash_predicted_spec_report(after, predicted_sum)
        if raw_predicted_sum is None or state.linearized_predicted_sum is None:
            # No fit ran this attempt (ineligible mic tier, or the fit failed
            # into SF2's trims-only fallback), so `predicted_sum` IS
            # `raw_predicted_sum` — the same object. Grading a thing against
            # itself always returns "no improvement", which would refuse every
            # trims-only candidate on the strength of arithmetic rather than
            # evidence. Abstain, loudly — carrying the after-report the hoist
            # above just produced, so the ledger and the wire cannot state
            # different verdicts about one session's one prediction.
            self._log_prediction_ledger(reason="no_linearization", after=after)
            return finding
        if after is None:
            self._log_prediction_ledger(reason="prediction_ungradeable")
            return finding
        if after.overall_passed:
            # A prediction that meets the spec on its own needs no improvement
            # argument, and gating an in-spec result on "how much did it
            # improve" would refuse the flattest speakers hardest.
            self._log_prediction_ledger(reason="predicted_in_spec", after=after)
            return finding
        before = spec_report_for_predicted_sum(raw_predicted_sum)
        if before is None:
            self._log_prediction_ledger(reason="baseline_ungradeable", after=after)
            return finding
        from jasper.active_speaker.flat_spec import spec_convergence_residual

        after_rms_db = spec_convergence_residual(after).rms_db
        before_rms_db = spec_convergence_residual(before).rms_db
        if after_rms_db is None or before_rms_db is None:
            self._log_prediction_ledger(
                reason="residual_unevaluable", after=after, before=before,
            )
            return finding
        improvement_db = float(before_rms_db) - float(after_rms_db)
        if improvement_db >= PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB:
            self._log_prediction_ledger(
                reason="improved", after=after, before=before,
                improvement_db=improvement_db,
            )
            return finding
        self._log_prediction_ledger(
            reason=REASON_CORRECTION_NOT_AN_IMPROVEMENT, after=after, before=before,
            improvement_db=improvement_db, level=logging.ERROR,
        )
        raise self._refuse(REASON_CORRECTION_NOT_AN_IMPROVEMENT)

    def _level_frame_finding_record(
        self, state: _LinearizationState,
    ) -> Mapping[str, Any] | None:
        """This session's banked frame disagreement, as flat evidence (#1866).

        Built ONLY on the finding+proceed path, from the plan this candidate's
        own build returned — no measurement, no re-derivation, no second
        verdict. Taking the state as an argument rather than reading it off
        ``self`` is what makes "this session's" true of one candidate rather
        than of whichever build ran last (#2291 Phase 2b). The
        attribution package turns it into an M7 finding
        (:func:`~jasper.attribution.promotion.promote_level_frame_disagreement`);
        this method owns *what the evidence is*, that one owns *what it means*.
        Nothing here imports attribution, so the flow keeps no dependency on
        the diagnosis layer.

        **Flat, and every value a finite scalar or a string**, because that is
        what :class:`~jasper.attribution.findings.Finding` accepts — nesting
        would be rejected at construction, and rejection is a lost diagnosis.
        Per-role numbers are therefore suffixed with the role, which is also
        what makes the record self-describing to a reader who has never seen
        this schema.

        **All THREE instruments ride, not just the two that disagreed.** A
        reader of this finding is being asked to believe that a session
        proceeded past a gate that would have stopped it, so the record has to
        carry the whole basis for that: the fit's per-driver median
        (``core_level_db_*``), the trim solve's per-driver level-match term
        (``trim_band_average_db_*``), the reconciled per-role offset that IS
        their disagreement, and the realized-level check whose PASS is what
        let the session proceed. Banking only the first two would record the
        argument and drop the reason it was allowed to stand.

        Returns ``None`` when the frame produced no per-role bands to describe
        — unreachable on this path (the gate fired on a frame that had roles),
        but a record with no band cannot become a finding, and returning
        ``None`` here says so at the producer instead of failing validation
        two layers away.
        """

        frame = state.level_frame
        cores = state.level_frame_cores
        realized = state.realized_level_match
        # The band this finding is ABOUT: the span the two level reads were
        # actually taken over, unioned across roles. Deliberately the CORE
        # bands and not the radiating ones — a high-pass branch radiates to
        # infinity, so a radiating union has no upper edge, while the core
        # band is exactly the finite span each median was computed on.
        #
        # **The union is an OUTER hull, and it spans a gap neither median
        # read** — on the conductor fixture the woofer's core stops at 1255.8
        # Hz and the tweeter's starts at 2020.0, so 1255.8-2020.0 Hz is inside
        # the finding's band and inside no measurement. That is the right shape
        # rather than a rounding of it: this finding is about the RELATIONSHIP
        # between two drivers, which lives in the handoff sitting in that gap,
        # and a band stated as two disjoint intervals would say the finding is
        # about two places when it is about one. It is not, and must not be
        # read as, a claim that anything was measured in the gap — the
        # per-role ``core_band_*`` keys below are what say where each number
        # actually came from.
        edges = [
            band for role in cores
            if (band := cores[role].get("band_hz")) is not None
        ]
        lo_edges = [float(band[0]) for band in edges]
        hi_edges = [float(band[1]) for band in edges if band[1] is not None]
        if not lo_edges or not hi_edges:
            return None
        record: dict[str, Any] = {
            "f_lo_hz": min(lo_edges),
            "f_hi_hz": max(hi_edges),
            "disagreement_db": round(
                float(state.level_frame_disagreement_db), 3
            ),
            "tolerance_db": float(LEVEL_FRAME_AGREEMENT_TOLERANCE_DB),
            "reference_role": frame.reference_role if frame is not None else "",
            "system_level_db": (
                round(float(frame.system_level_db), 3)
                if frame is not None else None
            ),
        }
        if realized is not None:
            record.update(
                realized_difference_db=round(float(realized.difference_db), 3),
                realized_tolerance_db=float(realized.tolerance_db),
                realized_level_w_db=round(float(realized.level_w_db), 3),
                realized_level_t_db=round(float(realized.level_t_db), 3),
            )
        for role, core in cores.items():
            band = core.get("band_hz") or (None, None)
            radiating = core.get("radiating_band_hz") or (None, None)
            record[f"core_level_db_{role}"] = core.get("level_db")
            record[f"core_band_lo_hz_{role}"] = band[0]
            record[f"core_band_hi_hz_{role}"] = band[1]
            record[f"radiating_band_lo_hz_{role}"] = radiating[0]
            record[f"radiating_band_hi_hz_{role}"] = radiating[1]
            if role in state.level_frame_trims:
                record[f"trim_band_average_db_{role}"] = round(
                    float(state.level_frame_trims[role]), 3
                )
            if frame is not None and role in frame.offset_db:
                record[f"frame_offset_db_{role}"] = round(
                    float(frame.offset_db[role]), 3
                )
        return record

    def _stash_predicted_spec_report(
        self, report: Any, predicted_sum: Any,
    ) -> None:
        """Hold the graded prediction for the host to persist, and say so when
        there is nothing to hold (two-stage commission D4).

        The stash is the SERIALIZED report, taken from the one live
        :class:`~jasper.active_speaker.flat_spec.FlatSpecReport` this session
        ever built for this curve — never a second evaluation.

        **An absent report becomes a user-visible dead end, so it gets its own
        named line.** The two-stage flow's review screen (PR-T2) will render
        "we could not predict this" and refuse the Apply control on it; the
        line lands with the ``None`` rather than with the screen, because per
        AGENTS.md's no-silent-failure rule a disclosure nobody can grep for is
        not a disclosure — and the ``None`` is already reachable. The gate's
        own ``correction.crossover_v2_prediction_gate`` ledger cannot serve:
        it carries this state as one ``reason=`` value among seven, and it does
        not fire at all on the trims-only lane's behalf. ``why`` separates the
        two causes, which have different remedies — a prediction that was never
        built (no summed model to grade) from one the evaluator refused (a
        malformed or degenerate curve, already logged in detail by
        :func:`spec_report_for_predicted_sum` itself).
        """
        self._measure_predicted_spec_report = (
            report.to_dict() if report is not None else None
        )
        if report is not None:
            return
        log_event(
            logger, "correction.crossover_v2_prediction_ungradeable",
            level=logging.WARNING, session_id=self.session_id,
            why="no_prediction" if predicted_sum is None else "evaluator_refused",
        )

    def _log_prediction_ledger(
        self,
        *,
        reason: str,
        after: Any = None,
        before: Any = None,
        improvement_db: float | None = None,
        level: int = logging.INFO,
    ) -> None:
        """One ledger line per session for item 2's gate, on EVERY path.

        Mirrors item 1's ``correction.crossover_v2_realized_level_match``, which
        logs whether or not it refuses (PR-L4 review S4). A gate that only
        speaks when it fires leaves "it passed" and "it never ran" looking
        identical in the journal — the exact ambiguity this PR exists to remove,
        and the one a field diagnosis of a dark speaker would need first.
        """
        from jasper.active_speaker.flat_spec import spec_convergence_residual

        def _rms(report: Any) -> float | None:
            if report is None:
                return None
            value = spec_convergence_residual(report).rms_db
            return round(float(value), 3) if value is not None else None

        if self._measure_predicted_spec_report is not None:
            self._measure_predicted_spec_report["comparison"] = {
                "reason": reason,
                "baseline_rms_db": _rms(before),
                "selected_rms_db": _rms(after),
                "improvement_db": (
                    round(float(improvement_db), 3)
                    if improvement_db is not None else None
                ),
                "required_db": PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
            }

        log_event(
            logger, "correction.crossover_v2_prediction_gate",
            level=level, session_id=self.session_id, reason=reason,
            before_rms_db=_rms(before),
            after_rms_db=_rms(after),
            after_passed=(after.overall_passed if after is not None else None),
            improvement_db=(
                round(float(improvement_db), 3) if improvement_db is not None else None
            ),
            required_db=PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
        )

    def _cloud_fit_evidence(self, combined: Any) -> "_CloudFitEvidence | None":
        """This group's honesty verdict, in the shape the fit envelope takes.

        ``None`` — the fit runs with no cloud terms, byte-identical to every
        candidate built before the timing move — in exactly two cases, and both
        are disclosed rather than silent:

        * the positions could not be combined at all (``combined is None``);
        * the combine succeeded but the honesty pipeline did not become
          available (a null-gate or spec-evaluator failure, already logged as
          ``correction.crossover_v2_cloud_pipeline_failed``).

        The second is **all-or-nothing on purpose.** A failed pipeline still
        leaves ``combined.excluded_bands_hz`` — the power-vs-median screen's
        own intervals — and it would be easy to hand the fit those. That is
        precisely the mask-alone read issue #1742 item 4 forbids: the screen
        structurally cannot see a position-invariant null (plan "S0 executed"
        § e.1 — 0 of 5462 bins in 8-16 kHz on the S0 corpus), so a
        screen-only mask would exclude the interference the cloud CAN see while
        silently correcting the interference it cannot, which is worse than
        excluding nothing and being honest about it. One verdict, or none.
        """
        if combined is None:
            return None
        result = self._group_cloud_result.get(PHASE_CLOUD_MEASURE) or {}
        if result.get("available") is not True:
            log_event(
                logger, "correction.crossover_v2_fit_without_cloud",
                level=logging.WARNING, session_id=self.session_id,
                reason=str(result.get("reason") or "no_pipeline_result"),
            )
            return None
        intervals = tuple(
            (float(band[0]), float(band[1]))
            for band in result.get("merged_excluded_bands_hz") or ()
        )
        return _CloudFitEvidence(
            excluded_bands_hz=intervals,
            band_spread=tuple(combined.band_spread),
            n_positions=int(combined.n_positions),
            boost_excluded_bands_hz=self._boost_excluded_bands_hz(combined, result),
        )

    def _boost_excluded_bands_hz(
        self, combined: Any, result: Mapping[str, Any],
    ) -> tuple[tuple[float, float], ...]:
        """Bands BELOW the null registry's own floor where this cloud's
        positions disagree about a dip (#1967) — the derivation, and what it
        deliberately cannot do, belong to
        :func:`~jasper.active_speaker.crossover_v2.spatial.boost_excluded_bands_hz`.

        What stays here is the journal. The derivation is side-effect-free and
        hands its fields back as data, so this owns the two event NAMES and the
        ``session_id`` that identifies them — and the level rule, which is the
        one place the answer changes how loudly it is reported: an empty result
        is the ordinary case (INFO), a non-empty one narrows a shipped gate and
        is worth seeing (WARNING).
        """
        exclusion = _spatial.boost_excluded_bands_hz(
            combined, result, echo_band_hz=self._cloud_echo_band.band_hz,
        )
        diagnostics = dict(exclusion.diagnostics)
        if diagnostics.pop("variance_check_failed", False):
            log_event(
                logger, "correction.crossover_v2_boost_variance_failed",
                level=logging.WARNING, session_id=self.session_id,
                band_hz=diagnostics["unadjudicated_span_hz"],
            )
        log_event(
            logger, "correction.crossover_v2_boost_evidence",
            level=logging.WARNING if exclusion.bands else logging.INFO,
            session_id=self.session_id, **diagnostics,
        )
        return exclusion.bands

    def _run_cloud_pipeline(
        self, phase: str, combined: Any, positions: Sequence[_CloudPosition],
    ) -> None:
        """PR-4: the honest-instrument pipeline.

        ``combined`` is the SAME object ``_close_cloud_group`` just derived
        its retry-gating verdict from — ONE combine per group close (S3
        review finding, 2026-07-26), never a second call to
        :func:`combine_cloud_positions`. ``positions`` is that same group's
        retained list, read for exactly one thing: its gated validity floor
        (:func:`cloud_validity_floor_hz`), which clamps the spec band's lower
        edge (plan PR-5).

        WO-1 adds a second read of the same group — ``_group_position_meta``,
        the per-position records the retention seam was already handed. They
        carry what the combiner structurally cannot know because they are
        properties of the CAPTURE, not of the combine: the gate actually
        applied, the summed ripple, which attempt survived, and the WAV's
        content digest.

        **Runs on EVERY close of the group, including a re-close from a
        retake (#1872).** ``_group_cloud_result`` and the ``cloud_spec`` log
        below always describe the cloud that is CURRENTLY retained — the fit
        (``_cloud_fit_evidence``), the candidate's fingerprinted
        ``exclusion_evidence``, and the room layer's disclosure
        (``jasper.web.correction_crossover_v2``'s ``_cloud_summary``) all
        read ``_group_cloud_result`` (via :meth:`group_cloud_result`), so a
        stale copy here would silently feed them the pre-retake cloud. Only
        the durable evidence-artifact PUBLISH below is a per-phase
        singleton — see its own comment for why.

        Never raises and never affects the accepted verdict already decided
        above — this is diagnostic/disclosure machinery, not a capture gate.
        """
        result = assemble_cloud_group_result(
            combined,
            echo_band_hz=self._cloud_echo_band.band_hz,
            echo_band_provenance=self._cloud_echo_band.disclosure(),
            position_records=tuple(
                self._group_position_meta.get(phase, {}).values()
            ),
            validity_floor_hz=cloud_validity_floor_hz(positions),
            tier=self._tier,
            # #1967: where the SHIPPED graph divides the spectrum, from the
            # preset's committed regions — the context that makes a
            # crossover-region finding interpretable rather than an
            # unattributed anomaly.
            crossover_region_hz=committed_crossover_region_hz(
                getattr(self._preset, "crossover_regions", ()) or ()
            ),
            # #2291: retain the live report for this phase, beside the live
            # ``combined`` this method already stashes. The round's SPEC
            # verdict needs the object; the dict below keeps the serialized
            # copy every other surface reads.
            spec_report_sink=lambda report: self._group_spec_report.__setitem__(
                phase, report
            ),
        )
        self._group_cloud_result[phase] = result
        # PR-5: the spec verdict a session's journal carries. It replaces the
        # per-VERIFY-capture ``flatness_*`` fields ``_log_verify_diag`` used
        # to log from the retired capture-grid construction — same operator
        # question, answered by the instrument that can actually answer it,
        # logged once per group instead of once per capture.
        flatness = result.get("flatness") if result.get("available") else None
        flatness = flatness if isinstance(flatness, Mapping) else {}
        spec = result.get("spec") if result.get("available") else None
        spec = spec if isinstance(spec, Mapping) else {}
        log_event(
            logger, "correction.crossover_v2_cloud_spec",
            session_id=self.session_id, phase=phase,
            available=bool(result.get("available")),
            reason=str(result.get("reason") or ""),
            spec_passed=flatness.get("passed"),
            spec_evaluable=flatness.get("evaluable"),
            flatness_max_db=flatness.get("max_db"),
            flatness_max_hz=flatness.get("max_hz"),
            # WHICH FRAME the deviation above is stated against (issue
            # #1857). ``flatness_max_db``/``_max_hz`` name a worst point
            # without naming its zero, and the pointer moves — sign and
            # frequency — under a different reference band. Two scalars, the
            # same shape ``_log_verify_diag`` uses for its tracking band.
            flatness_reference_band_lo_hz=_band_edge(
                flatness.get("reference_band_hz"), 0
            ),
            flatness_reference_band_hi_hz=_band_edge(
                flatness.get("reference_band_hz"), 1
            ),
            # EVERY band's own deviation from that same reference (issue
            # #1857) -- not just the one ``flatness_max_db`` picked. See
            # ``_per_band_flatness_log_field`` for why: a band that is
            # uniformly off can drag the shared reference toward itself and
            # make an unrelated band's ordinary ripple read as the larger
            # deviation, and this is the log line the #1857 corpus session's
            # own forensics started from.
            flatness_bands=_per_band_flatness_log_field(spec.get("bands")),
            flatness_rms_db=flatness.get("rms_db"),
            spec_n_excluded=flatness.get("n_excluded"),
            validity_floor_hz=result.get("validity_floor_hz"),
        )
        # #1872: the PUBLISH is the one per-phase SINGLETON in this pipeline
        # (everything above re-runs on every close). The evidence store is
        # write-once, but that means "refuses a write whose bytes differ
        # from what is already there" — an identical retry is accepted
        # idempotently, so this guard is not standing in for a check the
        # store already makes; it exists so a re-close (a genuine retake, or
        # a geometry-locked retry's own retake landing after the group
        # already accepted) does not spend an attempt that is guaranteed to
        # be REFUSED once the retake's recomputed bytes differ from the
        # first close's, which they normally will. Marked in
        # ``_group_cloud_published`` only on success, so a transient failure
        # (a full disk, not a write-once conflict) still gets a chance on
        # the group's next close rather than being locked out for the rest
        # of the session.
        if phase in self._group_cloud_published:
            # The skip itself is the one fact nothing else states: everything
            # ABOVE this line just recomputed and re-logged fresh
            # (``_group_cloud_result``, ``cloud_spec``), but the durable
            # artifact this phase already published now LAGS that fresh
            # result — a reader would otherwise have to infer the gap from
            # counting ``cloud_spec`` lines. INFO, not WARNING: this is the
            # retake contract working as designed, not a failure.
            log_event(
                logger, "correction.crossover_v2_cloud_publish_skipped",
                session_id=self.session_id, phase=phase,
            )
        elif self._seams.publish_cloud is not None:
            try:
                self._seams.publish_cloud(
                    phase, self._group_cloud_result[phase]
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # Mirrors _retain_cloud_position's fail-soft boundary: evidence
                # publication is forensics, never a gate, so a full disk or a
                # write-once conflict must not undo the group's own accept.
                log_event(
                    logger, "correction.crossover_v2_cloud_publish_failed",
                    level=logging.WARNING,
                    session_id=self.session_id, phase=phase, exc_info=True,
                )
            else:
                self._group_cloud_published.add(phase)

    def _log_cloud_diag(
        self,
        phase: str,
        index: int,
        analysis: ProgramAnalysis,
        verdict: PhaseVerdict,
    ) -> None:
        response = analysis.summed_response
        log_event(
            logger, "correction.crossover_v2_cloud_diag",
            session_id=self.session_id, phase=phase, index=index,
            accepted=verdict.accepted, code=verdict.code or "",
            positions_in=len(self._group_positions.get(phase, ())),
            gate_window_ms=_gate_window_ms(response),
            gate_floor_source=_gate_floor_source(response),
            validity_floor_hz=getattr(response, "validity_floor_hz", None),
            summed_ripple_db=analysis.summed_ripple_db,
            linearity_ok=analysis.linearity_ok,
            # Issue #1810 — see ``_log_measure_diag``'s note.
            pilot_snr_ok=analysis.pilot_snr_ok,
            pilot_snr_db=_worst_pilot_snr_db(analysis),
            glitch=analysis.glitch_detected,
        )

    def _consume_entry_baseline(
        self,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> PhaseVerdict:
        """#2291's "before" capture: screen it, reduce it, retain it.

        The accept rule reuses shipped gates and invents none; which of
        VERIFY's it mirrors, which it drops, and why each drop is safe belong
        to
        :func:`~jasper.active_speaker.crossover_v2.spatial.entry_baseline_screens`.
        Every refusal it returns becomes an ordinary rejected
        :class:`PhaseVerdict` with a shipped ``REASON_*``, so the slot's normal
        retry budget and household copy apply with no new machinery.

        What stays here is the order of the three steps — screen, retain only
        on an accept, and journal either way.
        """
        verdict, measured = self._entry_baseline_verdict(analysis)
        if verdict.accepted and measured is not None:
            self._retain_entry_baseline(index, attempt, measured, analysis, result)
        self._safe_log_diag(
            lambda a, v: self._log_entry_baseline_diag(index, a, v), analysis, verdict
        )
        return verdict

    def _entry_baseline_verdict(
        self, analysis: ProgramAnalysis,
    ) -> tuple[PhaseVerdict, "MeasuredResponse | None"]:
        """The screens above, and the reduced side when they all pass.

        The ladder itself — which of VERIFY's gates it reuses, which it drops,
        and why — belongs to
        :func:`~jasper.active_speaker.crossover_v2.spatial.entry_baseline_screens`.
        This renders its answer as the household's :class:`PhaseVerdict`.
        """
        screen = _spatial.entry_baseline_screens(
            analysis,
            stimulus_located=_stimulus_locate_ok(analysis),
            reference_mark=REFERENCE_MARK_DESIGN_AXIS,
        )
        if screen.kind is not None:
            return (
                PhaseVerdict(
                    False,
                    _screen_refusal_code(screen.kind),
                    payload=dict(screen.integrity_payload or {}),
                ),
                None,
            )
        measured = screen.measured
        assert measured is not None  # an accepted screen carries its side
        return PhaseVerdict(True, payload={"program_id": measured.program_id}), measured

    def _retain_entry_baseline(
        self,
        index: int,
        attempt: int,
        measured: "MeasuredResponse",
        analysis: ProgramAnalysis,
        result: Any,
    ) -> None:
        """Bank the accepted baseline, and hand its bytes to the evidence seam.

        ``retain_position`` is reused rather than duplicated even though this
        is not a :data:`GROUP_PHASES` member, so an entry baseline lands in
        ``refs["position_artifacts"]`` beside every other retained take and one
        replay path covers both. Being outside the group bookkeeping is exactly
        why the call is explicit here: nothing else would make it.

        Fail-soft on the same terms as :meth:`_retain_cloud_position` — same
        caught family, same WARN. Evidence retention is forensics, never a
        gate: a full disk must not turn an acoustically-good baseline into a
        retake, and the reduced record (which is what the round actually
        grades) is banked whether or not any bytes were stored.
        """
        fingerprint = self._entry_graph_fingerprint()
        metadata = _spatial.entry_baseline_record(
            index=index,
            attempt=attempt,
            session_id=self.session_id,
            program_id=measured.program_id,
            reference_mark=measured.reference_mark,
            graph_fingerprint=fingerprint,
            captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            validity_floor_hz=getattr(
                analysis.summed_response, "validity_floor_hz", None
            ),
            gate_window_ms=_gate_window_ms(analysis.summed_response),
            summed_ripple_db=analysis.summed_ripple_db,
            glitch_detected=bool(analysis.glitch_detected),
            wav_sha256=_capture_wav_sha256(result),
        )
        take_id = str(metadata["take_id"])
        artifact_ref = ""
        if self._seams.retain_position is not None:
            try:
                self._seams.retain_position(take_id, result, metadata)
            except (OSError, RuntimeError, TypeError, ValueError):
                log_event(
                    logger, "correction.crossover_v2_position_retain_failed",
                    level=logging.WARNING,
                    session_id=self.session_id, phase=PHASE_ENTRY_BASELINE,
                    position_id=take_id, exc_info=True,
                )
            else:
                artifact_ref = take_id
        from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

        self._measure_entry_baseline = EntryBaseline.from_measurement(
            measured,
            graph_fingerprint=fingerprint,
            captured_at=str(metadata["captured_at"]),
            artifact_ref=artifact_ref,
        )

    def _entry_graph_fingerprint(self) -> str:
        """Which graph this capture was measured through, or the unknown word.

        One line over the coordinator's reader so the ENTRY BASELINE's own
        retention keeps its call site; the round's receipt asks the same
        function directly. Never raises and never gates — see
        :data:`ENTRY_GRAPH_FINGERPRINT_UNKNOWN` for the three honest ways the
        host cannot answer, and why the answer is a word rather than an empty
        string.
        """
        from jasper.active_speaker.crossover_v2 import coordinator

        return coordinator.entry_graph_fingerprint(
            self._round_ports(), session_id=self.session_id,
        )

    # --- #2291: the round, graded and acted on -------------------------------

    def _round_ports(self) -> "RoundPorts":
        """Narrow this conductor's seams down to the five a round may call.

        The coordinator is handed capabilities rather than the conductor, so
        what it can reach is its argument type: it cannot play a program or
        publish a candidate, because :class:`RoundPorts` has no name for those.
        """
        from jasper.active_speaker.crossover_v2.coordinator import RoundPorts

        return RoundPorts(
            rollback=self._seams.rollback,
            rollback_available=self._seams.rollback_available,
            applied_boosts=self._seams.applied_boosts,
            entry_graph_fingerprint=self._seams.entry_graph_fingerprint,
            publish_round_receipt=self._seams.publish_round_receipt,
        )

    def _grade_round_once(self, verdict: PhaseVerdict) -> PhaseVerdict:
        """Grade this round and act on the adoption table. Once per session.

        **One owner, two triggers**, because "stage 2's post-apply evidence is
        complete" happens at two different moments:

        * Express — the end of :meth:`_consume_verify`, when this session plans
          no post-apply cloud. VERIFY is all the evidence there will be.
        * Full — the end of :meth:`_close_cloud_group` for
          ``PHASE_CLOUD_VERIFY``, when the spatial arm has landed too.

        **Both triggers require an ACCEPTED capture.** VERIFY and a position
        group each carry a retry budget, so a rejected one does not end the
        session; grading it would burn this guard on evidence the household
        then replaced, and the receipt — which is write-once — would describe a
        capture the round did not end on. A session that ends on a terminal
        rejection therefore writes no round receipt, which is the honest
        record: its post-apply evidence never completed.

        The fire-once guard is here rather than in the coordinator because it
        is a fact about THIS SESSION rather than about the round: only the
        conductor knows a second trigger is the same session's.

        Everything after it belongs to
        :func:`~jasper.active_speaker.crossover_v2.coordinator.run_round`,
        which grades, acts, and banks; this stamps the results onto the
        conductor's own state and maps the coordinator's refusal KIND to the
        :data:`REASON_REGISTRY` code whose copy the household reads. The
        vocabulary stays here because the registry does.
        """
        from jasper.active_speaker.crossover_v2 import coordinator

        if self._round_evaluated:
            return verdict
        self._round_evaluated = True
        decision = coordinator.run_round(
            coordinator.RoundEvidence(
                session_id=self.session_id,
                tier=self._tier,
                post_analysis=self._verify_analysis,
                entry_baseline=self._measure_entry_baseline,
                # The post-apply CLOUD's report — ``None`` on a tier that walks
                # no cloud, which the evaluator reads as "no report" rather than
                # as a pass (#2160's honest wire).
                spec_report=self._group_spec_report.get(PHASE_CLOUD_VERIFY),
                # The APPLIED candidate's identity. ``_tuning_attempt_id``
                # first, for :meth:`_grade_verify_attempt`'s reason and by the
                # same chain: on the stage that grades a round it is the only
                # one populated, read from the durable state's candidate
                # fingerprint at prepare time. Reading ``self._candidate`` alone
                # was the dead stage-2 read that emptied the receipt's
                # ``proposal_fingerprint``, which the contract refuses, so every
                # production round lost its receipt to the fail-soft handler.
                proposal_fingerprint=(
                    self._tuning_attempt_id
                    or str(getattr(self._candidate, "fingerprint", "") or "")
                ),
                commanded_delta_present=self._measure_commanded_delta is not None,
                realization_tolerance_db=VERIFY_TOLERANCE_DB,
                reference_mark=REFERENCE_MARK_DESIGN_AXIS,
            ),
            self._round_ports(),
        )
        self._round_evaluation = decision.evaluation
        self._round_receipt_identity = decision.receipt_identity
        refusal = decision.refusal
        if refusal is None:
            return verdict
        return self._round_refusal_for(refusal)

    def _round_refusal_for(self, refusal: Any) -> PhaseVerdict:
        """Map a coordinator refusal KIND to the code the household reads.

        One explicit arm per :data:`coordinator.REFUSAL_KINDS` member, and the
        fallback is LOUD rather than a catch-all. An `else` that silently
        answered for an unrecognised kind is how a future kind ships wearing
        another kind's sentence: every kind the coordinator can return is a
        deliberate household outcome, so one arriving here unrecognised is a
        wiring defect and must say so.

        It still refuses rather than raising, and under the most conservative
        code available — the round already decided something is wrong with this
        graph, and losing that verdict to a mapping gap would be worse than
        naming it imprecisely for one release. ``rollback_anchor_available``
        rides through as-is: ``None`` is the copy owner's "not established" arm.
        """
        from jasper.active_speaker.crossover_v2 import coordinator

        if refusal.kind == coordinator.REFUSAL_RESTORED:
            return self._round_refusal(round_restore_reason(refusal.cause))
        if refusal.kind != coordinator.REFUSAL_ROLLBACK_FAILED:
            log_event(
                logger, "correction.crossover_v2_round_refusal_kind_unmapped",
                level=logging.ERROR, session_id=self.session_id,
                kind=str(refusal.kind),
            )
        return self._round_refusal(
            REASON_CORRECTION_ROLLBACK_FAILED,
            rollback_anchor_available=refusal.rollback_anchor_available,
        )

    def _round_refusal(
        self, code: str, *, rollback_anchor_available: bool | None = None,
    ) -> PhaseVerdict:
        """Stamp a round-driven refusal the way the delta probe already does.

        ``rollback_anchor_available`` travels with the code because
        :data:`REASON_CORRECTION_ROLLBACK_FAILED` covers two situations whose
        household sentences differ — a restore that failed (Undo can still
        help) and a restore that was never possible (Undo refuses on the very
        predicate that routed here). The fact is recorded, never re-derived at
        render time: the anchor can change between the round and the screen,
        and the screen must describe the round.
        """
        self._last_failure_code = code
        # A round verdict, not a capture — no pilot evidence belongs to it, and
        # the prior capture's must not trail in (#2085).
        self._last_failure_pilot_heard = None
        self._last_failure_rollback_anchor = rollback_anchor_available
        return PhaseVerdict(False, code)

    def _log_entry_baseline_diag(
        self, index: int, analysis: ProgramAnalysis, verdict: PhaseVerdict,
    ) -> None:
        response = analysis.summed_response
        baseline = self._measure_entry_baseline
        log_event(
            logger, "correction.crossover_v2_entry_baseline_diag",
            session_id=self.session_id, index=index,
            accepted=verdict.accepted, code=verdict.code or "",
            program_id=(baseline.program_id if baseline is not None else ""),
            reference_mark=REFERENCE_MARK_DESIGN_AXIS,
            graph_fingerprint=(
                baseline.graph_fingerprint if baseline is not None else ""
            ),
            artifact_ref=(baseline.artifact_ref if baseline is not None else ""),
            gate_window_ms=_gate_window_ms(response),
            validity_floor_hz=getattr(response, "validity_floor_hz", None),
            summed_ripple_db=analysis.summed_ripple_db,
            linearity_ok=analysis.linearity_ok,
            pilot_snr_ok=analysis.pilot_snr_ok,
            glitch=analysis.glitch_detected,
        )

    def _consume_verify(
        self, analysis: ProgramAnalysis, *, attempt: int,
    ) -> PhaseVerdict:
        verdict = self._verify_verdict(analysis)
        self._safe_log_diag(self._log_verify_diag, analysis, verdict)
        # #2291: the round's post-apply side. Retained BEFORE grading, because
        # the Full tier grades the round later (when the post-apply cloud
        # closes) from a call that cannot see this capture.
        self._verify_analysis = analysis
        self._grade_verify_attempt(analysis, verdict, capture_attempt=attempt)
        # Grade the round HERE when this ACCEPTED capture is the last
        # post-apply evidence there will be — i.e. the session plans no
        # post-apply cloud. On a Full session the cloud close grades it.
        #
        # **Only on an accepted verdict**, and that is load-bearing rather than
        # tidy. VERIFY has a retry budget, so a rejected capture does NOT end
        # the session: grading one would burn the fire-once guard on evidence
        # the household then replaced, and the session would finish carrying a
        # receipt describing a capture it did not end on — demanding operator
        # recovery for a round that went on to succeed. A session that ends on
        # a terminal rejection writes no round receipt, which is the honest
        # record: its post-apply evidence never completed.
        if verdict.accepted and PHASE_CLOUD_VERIFY not in self._journey.plan.phases:
            return self._grade_round_once(verdict)
        return verdict

    def _grade_verify_attempt(
        self,
        analysis: ProgramAnalysis,
        verdict: PhaseVerdict,
        *,
        capture_attempt: int,
    ) -> None:
        """Hand a VERIFY record to S3 and bank an accepted new attempt once.

        A rejected capture is still mapped and judged so capture-integrity
        failures reach STOP_EVIDENCE at the loop boundary (#2033), but it is a
        retry of the same applied candidate and is not appended to accepted
        history. A repeated successful re-verify of a candidate already in
        history is likewise not a new tuning attempt and cannot double-write
        model error.
        """

        candidate_id = self._tuning_attempt_id
        if not candidate_id and self._candidate is not None:
            candidate_id = str(getattr(self._candidate, "fingerprint", "") or "")
        attempt_id = candidate_id or f"{self.session_id}:{capture_attempt}"
        already_recorded = any(
            item.attempt_id == attempt_id for item in self._attempt_history
        )
        if already_recorded:
            return

        record = attempt_record_from_verify(analysis, attempt_id=attempt_id)
        writer = self._seams.record_model_error
        # The model-error store banks PREDICTION error, and its number is the
        # tracking deviation — read straight off the analysis rather than off
        # ``record.grade_db``. The two happen to be equal today, and that
        # coincidence is exactly the hazard: ``model_error_store`` owns
        # prediction/realization error while the attempts ledger owns the
        # acoustic grade, so a future change to what the LEDGER grades must not
        # silently change what the STORE banks (#2291). One quantity, one
        # source, named at the read.
        tracking_deviation_db = _attempt_optional_float(
            (analysis.verify_tracking or {}).get(
                ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED
            )
        )
        if verdict.accepted and writer is not None and tracking_deviation_db is not None:
            try:
                # ``max_db_notch_excluded`` is measured deviation from the
                # fitted prediction, whose predicted error is exactly zero.
                # Claim the durable observation identity before banking the
                # journey projection: a crash-recovery capture can measure a
                # slightly different grade for the same applied candidate.
                identity_accepted = writer(
                    speaker_id=self._speaker_id,
                    attempt_id=record.attempt_id,
                    metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
                    predicted_db=0.0,
                    realized_db=tracking_deviation_db,
                    context={
                        "session_id": self.session_id,
                        "provenance": record.provenance,
                    },
                )
            except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
                # Ordinary persistence outages are forensics failures: they do
                # not reverse a VERIFY the measurement gate already accepted.
                log_event(
                    logger,
                    "correction.crossover_v2_model_error_write_failed",
                    level=logging.WARNING,
                    session_id=self.session_id,
                    speaker_id=self._speaker_id,
                    attempt_id=record.attempt_id,
                    exc_info=True,
                )
            else:
                if not identity_accepted:
                    # The store already owns this candidate identity with
                    # different numbers. Do not let a fresh recovery capture
                    # create a second truth in journey history or its decision.
                    # A verify re-arm hydrates the previous candidate's last
                    # decision; clear that projection too, or the done screen
                    # will call the prior basis "the latest applied result".
                    self._last_attempt_decision = None
                    log_event(
                        logger,
                        "correction.crossover_v2_model_error_identity_conflict",
                        level=logging.WARNING,
                        session_id=self.session_id,
                        speaker_id=self._speaker_id,
                        attempt_id=record.attempt_id,
                    )
                    return

        prospective = [*self._attempt_history, record]
        # Evidence refusal outranks grading preconditions. ``decide_next``
        # requires a real floor, but #2033's integrity result is meaningful
        # even on a speaker that has not adopted one. Construct the kernel's
        # typed evidence verdict from its own vocabulary; do not let the
        # flow-owned no-floor status mask a rejected capture.
        if not record.integrity.comparable:
            decision = LoopDecision(
                decision=STOP_EVIDENCE,
                reason=REASON_ATTEMPT_NOT_COMPARABLE,
                attempts_used=len(prospective),
                budget=AttemptBudget(),
                floor=self._attempt_floor,
                basis_attempt_ids=(record.attempt_id,),
                provenance=record.provenance,
                notes=record.integrity.reasons,
            ).to_dict()
        elif self._attempt_floor is None:
            decision = {
                "decision": None,
                "reason": ATTEMPT_REASON_NO_FLOOR,
                "attempts_used": len(prospective),
                "budget": AttemptBudget().to_dict(),
                "improved": None,
                "magnitude_db": None,
                "improvement_db": None,
                "floor": None,
                "basis_attempt_ids": [attempt_id],
                "provenance": record.provenance,
                "repeats_over_cap": False,
                "notes": [],
            }
        else:
            decision = decide_next(prospective, self._attempt_floor).to_dict()
        self._last_attempt_decision = decision
        floor = decision.get("floor")
        log_event(
            logger,
            "correction.crossover_v2_attempt_decision",
            session_id=self.session_id,
            speaker_id=self._speaker_id,
            decision=str(decision.get("decision") or "ungraded"),
            reason=str(decision.get("reason") or ""),
            basis=",".join(
                str(item) for item in decision.get("basis_attempt_ids", ())
            ),
            floor_db=(floor.get("claim_floor_db") if isinstance(floor, Mapping) else None),
            floor_basis=(floor.get("basis") if isinstance(floor, Mapping) else None),
            provenance=str(decision.get("provenance") or ""),
        )
        if not verdict.accepted:
            return

        self._attempt_history = prospective[-AttemptBudget().hard_cap_attempts:]

    def _set_verify_outcome(
        self, outcome: str, code: str | None, gate: dict[str, Any] | None,
    ) -> None:
        """Record the verify outcome, its verdict, and its gate — as ONE write.

        One call, not three assignments (issue #1974). "inconclusive" is
        reached by two verdicts that share no mechanism, and the done screen
        names the cause by reading the code and the gate TOGETHER — so any two
        of these three drawn from different attempts is a screen telling a
        household the wrong reason. ``code=None`` is the pass: nothing
        rejected it. ``gate=None`` is an ungateable capture.

        **The gate is a parameter, not a field this method reads.** An earlier
        revision recomputed ``_verify_gate`` at the top of ``_verify_verdict``,
        before the early returns — which meant an attempt that early-returned
        (``locate_failed`` / ``pilot_level_collapse`` / ``agc_behavioral_fail``,
        none of which reach here) overwrote the gate while leaving the PREVIOUS
        attempt's outcome and code standing. The adversarial gate on PR #1994
        reproduced the consequence on the real conductor: an attempt-1
        "inconclusive" whose window was capped at the ceiling, followed by an
        attempt-2 locate failure whose capture DID find a reflection, made the
        done screen say "a reflection reached the microphone sooner…" about a
        verdict whose own capture had found none — #1974 re-created in a new
        place. Requiring the gate at the call site is what makes that
        unavailable rather than merely intended.
        """
        self._verify_outcome = outcome
        self._verify_code = code
        self._verify_gate = gate

    def _verify_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        # Reset every call — a stale value from a PRIOR attempt must never
        # leak into THIS attempt's diagnostic (mirrors ``_last_measure_guard``'s
        # method-top reset in ``_measure_verdict``, see its own comment).
        # Every early return below (locate_failed, pilot_level_collapse,
        # agc_behavioral_fail, gate-comparability) runs BEFORE the G3 block
        # gets a chance to recompute this, so it must not still hold a REAL
        # step number from an earlier attempt that reached that block —
        # ``_log_verify_diag`` runs unconditionally after this method
        # returns and would otherwise misreport it as fresh.
        self._verify_pilot_transfer_step_db = None
        # Same reset discipline: only a verdict that reaches the tracking
        # comparison below carries expert-disclosure evidence (#1605) or a
        # graded band (#1868); the early returns must not surface a prior
        # attempt's numbers — or a prior attempt's band, which would claim
        # this capture graded a span it never reached.
        self._verify_evidence = None
        self._verify_graded_band_hz = None
        self._verify_frame = None
        self._verify_claims = None
        # THIS attempt's gate, as a LOCAL — deliberately not written to
        # ``self`` here. It is computed before the early returns because the
        # gate-comparability refusal below needs it (that is the verdict whose
        # copy used to assert a reflection nobody had looked for, issue
        # #1974), but it only becomes conductor state through
        # ``_set_verify_outcome``, alongside the outcome and code it belongs
        # to. See that method for the desync this ordering prevents.
        #
        # Named for what it is, not "gate": ``verify_gate`` below is this same
        # capture's window in MILLISECONDS, and in the one method where
        # confusing the two produced a household-visible bug they should not
        # share a name.
        gate_record = _gate_record(analysis.summed_response)
        if not _stimulus_locate_ok(analysis):
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        if analysis.pilot_snr_ok is False:
            # Issue #1810 — the verdict the JTS3 session of 2026-07-28 should
            # have got. It runs ahead of BOTH the linearity branch and the G3
            # transfer gate below: a pilot pair that never cleared the room
            # floor cannot establish or move a transfer baseline either, so
            # letting it reach G3 would seed that gate with noise.
            return PhaseVerdict(False, REASON_PILOT_LEVEL_COLLAPSE)
        # Capture-integrity gate (issue #1971). Ahead of EVERY grade below it,
        # for the reason ``_measure_verdict`` puts the same class of check
        # ahead of its own: a spliced or clipped recording is not evidence
        # about the speaker, so no verdict drawn from it — linearity, the
        # gate-window comparison, G3's transfer step, or the tracking max — is
        # worth reporting. Until this existed nothing on the VERIFY path ever
        # looked: ``glitch_detected`` came from ``_estimate_drift``, which is
        # structurally MEASURE-only, and both ``_sweep_schedule_ok`` and
        # ``_sweep_locate_confidence_ok`` filter ``KIND_SWEEP`` while VERIFY's
        # sweep is ``KIND_SUMMED_SWEEP``.
        #
        # ``None`` is the pre-#1971 analysis shape and means NO EVIDENCE —
        # the same convention ``linearity_ok`` / ``pilot_snr_ok`` use two
        # lines up, where only an explicit ``False`` refuses. It is not a
        # silent pass: ``_log_verify_diag`` prints ``integrity=unavailable``
        # for it, distinct from ``integrity=ok``, and the production analyze
        # seam always populates the record (pinned by test).
        #
        # Two reason codes, because the two failures need different household
        # actions and #1838's D3 is explicit that they must not share one:
        # a sweep nobody could hear is a level/mic problem that re-running at
        # the same level cannot fix (``locate_failed``), while a spliced or
        # clipped timeline is the transient capture-glitch class §5.2 says
        # reuses ``drift_baselines_disagree`` rather than minting a new code.
        # The diag's ``integrity=`` field names which check fired, the way
        # ``guard=`` disambiguates MEASURE's own shared codes.
        integrity = analysis.capture_integrity
        if integrity is not None and integrity.failed:
            payload = {"capture_integrity": integrity.to_dict()}
            if INTEGRITY_CHECK_SWEEP_HEARD in integrity.failed:
                return PhaseVerdict(False, REASON_LOCATE_FAILED, payload=payload)
            return PhaseVerdict(
                False, REASON_DRIFT_BASELINES_DISAGREE, payload=payload,
            )
        if analysis.linearity_ok is False:
            return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)
        # Gate-comparability rule (§5.2): a shorter VERIFY gate manufactures
        # overlay differences that aren't driver alignment ⇒ inconclusive.
        verify_gate = _gate_window_ms(analysis.summed_response)
        if (
            self._measure_gate_window_ms is not None
            and verify_gate is not None
            and verify_gate + 1e-6 < self._measure_gate_window_ms
        ):
            self._set_verify_outcome(
                "inconclusive", REASON_VERIFY_INCONCLUSIVE, gate_record,
            )
            return PhaseVerdict(False, REASON_VERIFY_INCONCLUSIVE)
        # Measurement-honesty gate G3 (2026-07-22): the tracking-max
        # comparison below is exactly the thing a shifted recording chain
        # invalidates, so check the chain's OWN consistency first — this
        # gate is level-independent (unlike gate-comparability above, which
        # must stay first regardless). VERIFY replays the identical program
        # through the identical applied graph on every attempt, so its own
        # leading pilot pair's transfer (captured level minus programmed
        # gain) should not move between attempts either — see
        # VERIFY_PILOT_TRANSFER_STEP_CEILING_DB for the evidence. The FIRST
        # usable attempt of this conductor's own lifetime (never pilots
        # absent, never a legacy program missing ``programmed_hi_gain_db``)
        # only records the reference; it never rejects on this attempt.
        #
        # Since #1927 that is the ONLY attempt that can record it — no prior
        # session's number can arrive here (``__init__``), so a first attempt
        # is structurally unable to fire this gate, and what the gate means is
        # exactly what it measures: the chain moved DURING this sitting.
        transfer = _pilot_transfer_by_role(analysis)
        if transfer:
            if self._verify_pilot_baseline is None:
                self._verify_pilot_baseline = dict(transfer)
                self._verify_pilot_baseline_at = time.time()
                self._note_level_reference_reset(transfer)
            else:
                shared = [r for r in transfer if r in self._verify_pilot_baseline]
                if shared:
                    self._verify_pilot_transfer_step_db = max(
                        abs(transfer[r] - self._verify_pilot_baseline[r])
                        for r in shared
                    )
        if (
            self._verify_pilot_transfer_step_db is not None
            and self._verify_pilot_transfer_step_db > VERIFY_PILOT_TRANSFER_STEP_CEILING_DB
        ):
            self._set_verify_outcome(
                "inconclusive", REASON_VERIFY_LEVEL_SHIFT, gate_record,
            )
            return PhaseVerdict(False, REASON_VERIFY_LEVEL_SHIFT)
        tracking = analysis.verify_tracking or {}
        self._verify_evidence = _verify_evidence_from_tracking(tracking)
        self._verify_graded_band_hz = _verify_graded_band_from_tracking(tracking)
        self._verify_frame = _verify_frame_from_tracking(tracking)
        # Every §7 claim, graded here BEFORE any of them gates — so a capture
        # that fails one still discloses the others rather than reporting only
        # the first thing that went wrong.
        self._verify_claims = _verify_claims(tracking, analysis.verify_absolute)
        # Notch-aware, validity-floor-clamped comparator (W6.7 ruling 1 + W6.9
        # forensics): gate on the NOTCH-EXCLUDED max, not the raw full-band
        # max — and both are now computed over `tracking["tracking_band_hz"]`,
        # this capture's own gate-derived validity floor clamped up from the
        # nominal band (`program_analysis._analyze_verify`), not the nominal
        # [Fc/2, 2·Fc] band alone. Inside a predicted interference notch, or
        # below measurement validity, depth/level agreement is hypersensitive
        # to sub-dB/sub-degree branch differences (or outright unmeasurable)
        # and is not a meaningful tracking signal — the run-7 hardware failure
        # (27.83 dB raw max, against a predicted sum whose OWN ripple was
        # ~30 dB) was entirely that; the run-7/8 sequel traced the SAME class
        # of false divergence to a fixed-window prediction baking a room
        # reflection into a sub-floor region the notch rule alone didn't
        # always catch. ``max_db``/``rms_db`` (still clamped, just not
        # notch-excluded) and the pre-clamp ``*_full_band`` numbers still
        # travel in the persisted evidence as diagnostic fields only.
        max_db = tracking.get("max_db_notch_excluded")
        if not isinstance(max_db, (int, float)) or max_db > VERIFY_TOLERANCE_DB:
            self._set_verify_outcome(
                "fail", REASON_VERIFY_OUT_OF_TOLERANCE, gate_record,
            )
            return PhaseVerdict(
                False, REASON_VERIFY_OUT_OF_TOLERANCE,
                payload={"tracking": dict(tracking)},
            )
        # PR-L5's delta probe. Runs only once tracking has PASSED — a session
        # that already failed at the handoff band does not need a second
        # verdict about the same capture, and its retry budget (2) still means
        # something. What this adds on top is the band tracking cannot see: the
        # whole span the correction commands, which is where a realization
        # defect like the 2026-07-27 shelf lives.
        self._verify_tracking_curve = analysis.verify_tracking_curve
        summed = analysis.summed_response
        if summed is not None:
            self._verify_validity_floor_hz = summed.validity_floor_hz
        refusal = self._delta_probe_refusal(self._run_delta_probe())
        if refusal is not None:
            self._set_verify_outcome("fail", refusal, gate_record)
            return PhaseVerdict(
                False, refusal,
                payload={
                    "tracking": dict(tracking),
                    "delta_probe": (
                        self._delta_probe.to_dict()
                        if self._delta_probe is not None else {}
                    ),
                },
            )
        # Absolute remains independent; the terminal owner classifies its miss.
        self._set_verify_outcome("pass", None, gate_record)
        return PhaseVerdict(
            True, payload={
                "measurement_phase": PHASE_VERIFY,
                "tracking": dict(tracking),
                **(
                    {"delta_probe": self._delta_probe.to_dict()}
                    if self._delta_probe is not None else {}
                ),
            }
        )

    def _note_level_reference_reset(self, transfer: Mapping[str, float]) -> None:
        """Record that this session set its own G3 reference, if that is news.

        Runs once, in the same statement block that establishes the baseline
        (#1927). The comparison it makes is with the PREVIOUS session's dated
        record and is **reported, never enforced** — no verdict reads it. That
        separation is the whole point of the per-session lifetime: the number
        it computes is exactly the one that used to refuse a day-later verify.

        Silent unless the step clears :data:`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB`
        — the gate's own definition of a level move, reused rather than
        restated, so "materially different" can never drift from "would have
        fired".
        """
        if self._verify_pilot_prior is None or self._verify_pilot_prior_at is None:
            return
        shared = [r for r in transfer if r in self._verify_pilot_prior]
        if not shared:
            return
        step = max(
            abs(transfer[r] - self._verify_pilot_prior[r]) for r in shared
        )
        if step <= VERIFY_PILOT_TRANSFER_STEP_CEILING_DB:
            return
        self._verify_level_reference_reset = {
            "prior_at": self._verify_pilot_prior_at,
            "step_db": step,
        }
        # The bench's grep target. ``_log_verify_diag`` carries the
        # WITHIN-session ``pilot_transfer_step_db``; this is the number that
        # number can no longer be — the step across the session boundary, the
        # one the #1870 field finding was actually measuring. Its own event so
        # a corpus sweep can count resets without parsing every verify diag,
        # and INFO because a reset is ordinary, not a fault.
        log_event(
            logger, "correction.crossover_v2_level_reference_reset",
            level=logging.INFO,
            session_id=self.session_id,
            step_db=round(step, 3),
            prior_age_s=round(time.time() - self._verify_pilot_prior_at, 1),
            ceiling_db=VERIFY_PILOT_TRANSFER_STEP_CEILING_DB,
        )

    # --- delta probe (linearization-integrity PR-L5) --------------------------

    def _applied_offset_db(self) -> float:
        """The apply's own declared whole-band level move, dB (#1811).

        Read through the optional seam, fail-soft to ``0.0``: an unbound seam,
        an unreadable durable state, or a non-finite value all mean "nothing
        known", and ``classify_delta_probe`` then leaves the entire shift
        visible as ``residual_offset_db`` rather than absorbing it. Claiming an
        offset we cannot read would be the one dishonest option here.
        """
        seam = self._seams.applied_offset_db
        if seam is None:
            return 0.0
        try:
            value = float(seam())
        except (OSError, RuntimeError, TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    def _run_delta_probe(self) -> DeltaProbeMap | None:
        """Classify what the speaker actually did against what was commanded.

        Runs twice per full session and once per express one: at VERIFY, on
        the at-the-mark map alone, and again at the post-apply group's close,
        where the spatial arm becomes measurable. The second call can only
        ever ADD evidence — VERIFY has already refused the session if the mark
        arm did not match — so the later verdict supersedes the earlier.

        The arithmetic, stated plainly because it matters (see
        :mod:`jasper.active_speaker.delta_probe`): the ERROR this classifies is
        ``measured − predicted``, exactly the residual VERIFY's tracking check
        already computes — and it is read off
        ``ProgramAnalysis.verify_tracking_curve``, the very smoothed pair the
        tracking scalars were reduced from, rather than re-derived here. One
        comparison, two consumers. What the delta framing adds is the commanded
        curve — the axis the shortfall-vs-model-error discriminator needs — and
        a band. The band is the one the correction actually commands something
        in, which on this speaker reaches an octave and a half above the
        ``[Fc/2, 2·Fc]`` window tracking looks at, and is where the 2026-07-27
        shelf-realization defect lived.

        Returns ``None`` when the tracking curve or the commanded delta is
        missing. ``None`` is the same thing :data:`~jasper.active_speaker.
        delta_probe.VERDICT_UNAVAILABLE` is: no evidence to refuse on, and no
        permission granted either.
        """
        tracked = self._verify_tracking_curve
        commanded = self._measure_commanded_delta
        if tracked is None or commanded is None:
            return None
        try:
            freqs, measured_s, predicted_s = tracked
            freqs = np.asarray(freqs, dtype=float)
            measured_s = np.asarray(measured_s, dtype=float)
            predicted_s = np.asarray(predicted_s, dtype=float)
            commanded_db = np.interp(
                freqs,
                np.asarray(commanded[0], dtype=float),
                np.asarray(commanded[1], dtype=float),
            )
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            log_event(
                logger, "correction.crossover_v2_delta_probe_failed",
                level=logging.WARNING, session_id=self.session_id, error=str(exc),
            )
            return None

        # realized − commanded == measured − predicted (the raw-branch
        # prediction cancels), so the realized curve is reconstructed from the
        # three quantities this conductor actually holds.
        realized_db = (measured_s - predicted_s) + commanded_db
        floor_hz = self._verify_validity_floor_hz
        band_hz = (
            float(floor_hz) if floor_hz is not None and math.isfinite(floor_hz)
            else float(freqs[0]),
            float(freqs[-1]),
        )
        probe = classify_delta_probe(
            freqs, realized_db, commanded_db, band_hz=band_hz,
            spatial=spatial_cost_from_group_spreads(
                {"band_spread": self._group_band_spread.get(PHASE_CLOUD_MEASURE, ())},
                {"band_spread": self._group_band_spread.get(PHASE_CLOUD_VERIFY, ())},
            ),
            expected_offset_db=self._applied_offset_db(),
        )
        self._delta_probe = probe
        log_event(
            logger, "correction.crossover_v2_delta_probe",
            # ``level_mismatch`` produces no refusal by design, so WARNING is
            # the only thing that puts it in front of anyone reading the
            # journal for a session that otherwise "passed" (#1811).
            level=(
                logging.WARNING
                if probe.rollback or probe.verdict == VERDICT_LEVEL_MISMATCH
                else logging.INFO
            ),
            session_id=self.session_id,
            verdict=probe.verdict,
            reason=probe.reason,
            rollback=probe.rollback,
            probe_band_hz=tuple(round(v, 1) for v in probe.probe_band_hz),
            n_bins=probe.n_bins,
            max_error_db=round(probe.max_error_db, 3),
            rms_error_db=round(probe.rms_error_db, 3),
            worst_hz=round(probe.worst_hz, 1),
            exceedance_octaves=round(probe.exceedance_octaves, 3),
            gain_factor=(
                round(probe.gain_factor, 4)
                if probe.gain_factor is not None else None
            ),
            expected_offset_db=round(probe.expected_offset_db, 3),
            residual_offset_db=(
                None if probe.residual_offset_db is None
                else round(probe.residual_offset_db, 3)
            ),
            spatial_available=probe.spatial.available,
            spatial_widened=probe.spatial.widened,
            spatial_worst_center_hz=round(probe.spatial.worst_center_hz, 1),
            spatial_worst_widening_db=round(probe.spatial.worst_widening_db, 3),
        )
        return probe

    def _delta_probe_refusal(self, probe: DeltaProbeMap | None) -> str | None:
        """Roll the correction back and return the reason code, or ``None``.

        The automatic half of PR-L5's "rollback is automatic on the
        non-matched classes". Rollback runs BEFORE the refusal is returned, so
        by the time the household reads the copy ("the previous sound has been
        put back") it is already true.

        A conductor with no ``rollback`` seam still refuses — the verdict is
        real whether or not this process can act on it, and the failure screen
        already offers Undo — but it refuses under
        :data:`REASON_CORRECTION_ROLLBACK_FAILED`, whose copy says the
        correction is STILL APPLIED. The three verdict-specific codes all
        promise "the previous sound has been put back", and that promise is
        only theirs to make when the restore actually happened; a household
        listening to a correction while being told it was reverted is a false
        statement about their speaker, not a rounding of one.
        """
        if probe is None or probe.verdict not in DELTA_PROBE_ROLLBACK_VERDICTS:
            return None
        verdict_code = DELTA_PROBE_REASON_BY_VERDICT[probe.verdict]
        restored = False
        error = ""
        if self._seams.rollback is not None:
            try:
                restored = bool(self._seams.rollback(verdict_code))
            except (OSError, RuntimeError, TypeError, ValueError, AttributeError,
                    KeyError) as exc:
                # A rollback that could not run must not swallow the verdict
                # that asked for it: the refusal still fires, and the
                # household's Undo button is still on the screen. The family is
                # wider than this file's usual four because this call sits
                # OUTSIDE the cloud pipeline's own wrap — nothing downstream
                # would catch an AttributeError/KeyError from a host binding,
                # and losing the verdict is strictly worse than reporting it
                # with the restore marked failed.
                error = str(exc)
        code = verdict_code if restored else REASON_CORRECTION_ROLLBACK_FAILED
        log_event(
            logger, "correction.crossover_v2_delta_probe_rollback",
            level=logging.ERROR, session_id=self.session_id,
            reason=code, verdict=probe.verdict, restored=restored,
            seam_bound=self._seams.rollback is not None, error=error,
        )
        self._last_failure_code = code
        # A delta-probe verdict, not a capture — no pilot evidence belongs to
        # it, and the prior capture's must not trail in (#2085).
        self._last_failure_pilot_heard = None
        return code

    # --- diagnostic logging (Part 1) ------------------------------------------
    #
    # One ``log_event`` per consumed capture, on the accepted path AND every
    # rejection — pure observability, read-only against ``analysis``/the
    # conductor's own state. None of these calls choose a verdict or a retry;
    # they run AFTER the verdict already exists.

    def _safe_log_diag(
        self,
        log_fn: Callable[[ProgramAnalysis, PhaseVerdict], None],
        analysis: ProgramAnalysis,
        verdict: PhaseVerdict,
    ) -> None:
        """Best-effort wrapper around one ``_log_*_diag`` call.

        Symmetric with the capture-retention path's own best-effort
        guarantee (Part 2): a bug in diagnostic-field extraction (a malformed
        ``analysis``, an unexpected ``None``) must never crash the capture or
        change the verdict already decided by ``_<phase>_verdict`` above —
        it degrades to a WARN instead. The caught set matches the realistic
        failure modes of these read-only field-extraction calls (attribute/
        key/index access and numeric conversion on ``analysis``'s own
        fields) — never a bare ``except Exception``.
        """
        try:
            log_fn(analysis, verdict)
        except (AttributeError, TypeError, ValueError, KeyError, IndexError):
            log_event(
                logger, "correction.crossover_v2_diag_log_failed",
                level=logging.WARNING, session_id=self.session_id,
                phase=analysis.phase, exc_info=True,
            )

    def _log_check_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        woofer = _pilot_diag_fields(_pilot_by_role(analysis, self._woofer.role))
        tweeter = _pilot_diag_fields(_pilot_by_role(analysis, self._tweeter.role))
        log_event(
            logger, "correction.crossover_v2_check_diag",
            session_id=self.session_id, accepted=verdict.accepted, code=verdict.code or "",
            pilot_snr_ok=analysis.pilot_snr_ok,
            woofer_snr_db=woofer["snr_db"],
            woofer_captured_delta_db=woofer["captured_delta_db"],
            woofer_programmed_delta_db=woofer["programmed_delta_db"],
            woofer_channel_map_target_rise_db=woofer["channel_map_target_rise_db"],
            woofer_channel_map_cross_rise_db=woofer["channel_map_cross_rise_db"],
            tweeter_snr_db=tweeter["snr_db"],
            tweeter_captured_delta_db=tweeter["captured_delta_db"],
            tweeter_programmed_delta_db=tweeter["programmed_delta_db"],
            tweeter_channel_map_target_rise_db=tweeter["channel_map_target_rise_db"],
            tweeter_channel_map_cross_rise_db=tweeter["channel_map_cross_rise_db"],
        )
        self._log_measure_level_solve(analysis)

    def _log_measure_level_solve(self, analysis: ProgramAnalysis) -> None:
        """One event per driver disclosing its solved MEASURE level (#1825).

        A separate event rather than more fields on the CHECK diag above:
        this is a per-ROLE record with its own evidence (the ambient band it
        was solved against and the SNR it demanded there), and flattening two
        roles × six fields into the already-wide diag line would bury it.
        Emitted from the diagnostic path, so it lands on a REJECTED check too
        — knowing what level the solve WOULD have chosen is exactly what a
        `snr_floor` refusal needs read beside it.
        """
        gain_plan = analysis.gain_plan
        if gain_plan is None:
            return
        for role, solve in (gain_plan.role_solves or {}).items():
            band = solve.band_hz
            log_event(
                logger, "correction.crossover_v2_measure_level_solve",
                session_id=self.session_id,
                role=role,
                solved_gain_db=round(float(solve.gain_db), 3),
                flat_target_gain_db=round(float(solve.flat_target_gain_db), 3),
                reduction_db=round(float(solve.reduction_db), 3),
                bound_by=solve.bound_by,
                band_lo_hz=round(band[0], 1) if band else None,
                band_hi_hz=round(band[1], 1) if band else None,
                ambient_dbfs=(
                    round(float(solve.ambient_dbfs), 2)
                    if solve.ambient_dbfs is not None else None
                ),
                required_snr_db=(
                    round(float(solve.required_snr_db), 2)
                    if solve.required_snr_db is not None else None
                ),
                required_capture_dbfs=(
                    round(float(solve.required_capture_dbfs), 2)
                    if solve.required_capture_dbfs is not None else None
                ),
                # #1838: without this the disclosed triple no longer adds up —
                # `required_capture_dbfs` is `ambient + required_snr + crest`.
                crest_factor_db=(
                    round(float(solve.crest_factor_db), 2)
                    if solve.crest_factor_db is not None else None
                ),
            )

    def _log_measure_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        drift = analysis.drift
        align = analysis.alignment
        cand = analysis.candidate
        delay_us, delay_role, polarity = alignment_to_candidate_fields(
            analysis, woofer_role=self._woofer.role, tweeter_role=self._tweeter.role,
        )
        woofer_snr_db, woofer_snr_verdict = _driver_snr_fields(
            _driver_response_by_role(analysis, self._woofer.role)
        )
        tweeter_snr_db, tweeter_snr_verdict = _driver_snr_fields(
            _driver_response_by_role(analysis, self._tweeter.role)
        )
        sweep_residual_ms_worst, sweep_locate_confidence_min = _sweep_schedule_diag_fields(
            analysis, self.program_for_phase(PHASE_MEASURE).sample_rate_hz
        )
        # First-vs-last per-role epsilon (sweep-composition PR-A, #1668) —
        # diagnostic only, never gated (DriftEstimate.per_role_epsilon_ppm's
        # own docstring). None-safe for a legacy construction site that
        # predates the field (empty mapping) or a role absent from it (<2
        # located occurrences that role).
        woofer_repeat_epsilon_ppm = (
            drift.per_role_epsilon_ppm.get(self._woofer.role) if drift else None
        )
        tweeter_repeat_epsilon_ppm = (
            drift.per_role_epsilon_ppm.get(self._tweeter.role) if drift else None
        )
        log_event(
            logger, "correction.crossover_v2_measure_diag",
            session_id=self.session_id, accepted=verdict.accepted, code=verdict.code or "",
            alignment_confidence=round(float(align.confidence), 4) if align else None,
            alignment_confidence_source=(align.confidence_source if align else None),
            alignment_seed_delay_us=(
                round(float(align.seed_delay_us), 3)
                if align and align.seed_delay_us is not None else None
            ),
            alignment_refinement_delta_us=(
                round(float(align.delay_us - align.seed_delay_us), 3)
                if align and align.seed_delay_us is not None else None
            ),
            gate_window_ms=self._measure_gate(analysis),
            gate_floor_source=self._measure_gate_floor_source(analysis),
            validity_floor_hz=_measure_validity_floor_hz(analysis),
            epsilon_ppm=round(float(drift.epsilon_ppm), 3) if drift else None,
            max_residual_samples=round(float(drift.max_residual_samples), 3) if drift else None,
            repeat_level_delta_db=(
                round(float(drift.repeat_level_delta_db), 3) if drift else None
            ),
            woofer_repeat_epsilon_ppm=(
                round(float(woofer_repeat_epsilon_ppm), 3)
                if woofer_repeat_epsilon_ppm is not None else None
            ),
            tweeter_repeat_epsilon_ppm=(
                round(float(tweeter_repeat_epsilon_ppm), 3)
                if tweeter_repeat_epsilon_ppm is not None else None
            ),
            delay_us=round(delay_us, 3) if delay_us is not None else None,
            delay_role=delay_role,
            polarity=polarity,
            predicted_ripple_db=(
                round(float(cand.predicted_ripple_db), 4) if cand else None
            ),
            # #1667: how far the RAW candidate's (ripple-optimal-where-
            # trusted) tweeter trim moved from solve_branch_trims's
            # band-average seed — this always reports the RAW candidate's
            # own recovery, even on a linearization-eligible attempt (the
            # linearized path's own recovery travels separately in the
            # evidence JSON). The sanity-guard fallback path reads as
            # exactly 0.0 (raw == seed); ``None`` only when this candidate
            # predates trim_band_average_db.
            trim_ripple_gain_db=(
                round(
                    float(
                        cand.trim_db[self._tweeter.role]
                        - cand.trim_band_average_db[self._tweeter.role]
                    ),
                    4,
                )
                if cand and cand.trim_band_average_db is not None else None
            ),
            alignment_seed_ripple_db=(
                round(float(cand.alignment_seed_ripple_db), 4)
                if cand and cand.alignment_seed_ripple_db is not None else None
            ),
            flatness_improvement_db=(
                round(float(cand.flatness_improvement_db), 4)
                if cand and cand.flatness_improvement_db is not None else None
            ),
            anchor_delay_us=(
                round(float(cand.anchor_delay_us), 3)
                if cand and cand.anchor_delay_us is not None else None
            ),
            snap_delta_us=(
                round(float(cand.snap_delta_us), 3)
                if cand and cand.snap_delta_us is not None else None
            ),
            snap_found=(bool(cand.snap_found) if cand else None),
            woofer_snr_db=woofer_snr_db,
            woofer_snr_verdict=woofer_snr_verdict,
            tweeter_snr_db=tweeter_snr_db,
            tweeter_snr_verdict=tweeter_snr_verdict,
            sweep_residual_ms_worst=(
                round(sweep_residual_ms_worst, 3)
                if sweep_residual_ms_worst is not None else None
            ),
            sweep_locate_confidence_min=(
                round(sweep_locate_confidence_min, 4)
                if sweep_locate_confidence_min is not None else None
            ),
            # Which (if any) measurement-honesty gate fired this verdict —
            # disambiguates a G1/G2 fire from the pre-existing check that
            # shares its reused reason code (see __init__'s comment on
            # ``_last_measure_guard``).
            guard=self._last_measure_guard,
            # The pilot SNR guard's own evidence (issue #1810). Live on this
            # phase only since the pre-pilot ambient window shipped; before
            # that ``pilot_snr_ok`` was True and ``pilot_snr_db`` +inf (logged
            # as None) by construction, so a REASON_PILOT_LEVEL_COLLAPSE line
            # with numbers here is what distinguishes a real low-SNR capture
            # from the structurally-dead guard it replaced.
            pilot_snr_ok=analysis.pilot_snr_ok,
            pilot_snr_db=_worst_pilot_snr_db(analysis),
            # (A ``linearization`` field lived here until the 2026-07-27
            # timing move. It reported which path the candidate build took,
            # and the candidate build now happens eight captures later, at the
            # cloud-measure group close — so this line could only ever have
            # reported "". It moved to ``correction.crossover_v2_candidate_built``
            # rather than being kept as a permanently-empty field, the same
            # treatment PR-5 gave the per-capture ``flatness_*`` fields when
            # their subject moved to the cloud.)
        )

    def _log_verify_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        integrity = analysis.capture_integrity
        tracking = analysis.verify_tracking or {}
        band = tracking.get("tracking_band_hz")
        tracking_band_lo_hz: float | None = None
        tracking_band_hi_hz: float | None = None
        if isinstance(band, (list, tuple)) and len(band) == 2:
            tracking_band_lo_hz, tracking_band_hi_hz = band[0], band[1]
        validity_floor_hz = (
            analysis.summed_response.validity_floor_hz
            if analysis.summed_response is not None else None
        )
        # (The ``flatness_*`` fields this line carried until PR-5 came from
        # the retired per-capture construction. The spec verdict is logged
        # on every close of the group instead — ``correction.
        # crossover_v2_cloud_spec`` in ``_run_cloud_pipeline``, once per
        # close rather than once per capture.)
        # Measurement-honesty gate G3's own diagnostics: the current
        # attempt's raw pilot transfer (re-derived fresh, read-only — never
        # the mutated conductor state) and the step vs baseline
        # ``_verify_verdict`` already computed and stashed transiently.
        pilot_transfer_db = _pilot_transfer_by_role(analysis).get(VERIFY_PILOT_ROLE)
        # Frame discipline (rung P1): the journal line an operator greps for
        # "did apply do what we predicted" is also where the answer "84 % of
        # that was the instrument" has to be readable. Lifted, never
        # recomputed — ``_verify_frame_from_tracking`` already reduced it.
        frame = self._verify_frame or {}
        claims = self._verify_claims or {}
        absolute = claims.get("absolute") or {}
        log_event(
            logger, "correction.crossover_v2_verify_diag",
            session_id=self.session_id, accepted=verdict.accepted, code=verdict.code or "",
            max_db_notch_excluded=tracking.get("max_db_notch_excluded"),
            verify_tolerance_db=VERIFY_TOLERANCE_DB,
            verify_gate_window_ms=_gate_window_ms(analysis.summed_response),
            verify_gate_floor_source=_gate_floor_source(analysis.summed_response),
            # (No ``measure_gate_floor_source`` beside ``measure_gate_window_ms``
            # here on purpose: that window is RESTORED from persisted state on a
            # resumed session, and the floor source is not persisted, so the
            # pair could only be reported as a real window beside a null source.
            # MEASURE's own source is disclosed where it is computed — the
            # ``crossover_v2_measure_diag`` line and the retained sidecar.)
            measure_gate_window_ms=self._measure_gate_window_ms,
            validity_floor_hz=validity_floor_hz,
            tracking_band_lo_hz=tracking_band_lo_hz,
            tracking_band_hi_hz=tracking_band_hi_hz,
            rms_db=tracking.get("rms_db"),
            # The frame those two numbers were measured ACROSS, and the same
            # two numbers with its tilt removed — beside, never instead of.
            frame_offset_db=_rounded(frame.get("offset_db"), 3),
            frame_tilt_db_per_octave=_rounded(frame.get("tilt_db_per_octave"), 3),
            rms_db_tilt_removed=_rounded(frame.get("rms_db_tilt_removed"), 4),
            max_db_tilt_removed=_rounded(frame.get("max_db_tilt_removed"), 4),
            # §7's claims, on the SAME line an operator already greps for a
            # verify outcome (R18, #1868) — including the two that are
            # structurally not-evaluated, so a corpus sweep counts what was
            # judged instead of inferring it from a bare accepted=true. The
            # absolute scalars ride beside it because a band and a verdict
            # without the number are not a measurement. All lifted.
            claims=_claims_log_field(claims),
            absolute_worst_db=absolute.get("worst_db"),
            absolute_worst_hz=absolute.get("worst_hz"),
            absolute_max_db=absolute.get("max_db"),
            absolute_tolerance_db=absolute.get("tolerance_db"),
            absolute_band_lo_hz=_band_edge(absolute.get("band_hz"), 0),
            absolute_band_hi_hz=_band_edge(absolute.get("band_hz"), 1),
            pilot_transfer_db=(
                round(pilot_transfer_db, 3) if pilot_transfer_db is not None else None
            ),
            pilot_transfer_step_db=(
                round(self._verify_pilot_transfer_step_db, 3)
                if self._verify_pilot_transfer_step_db is not None else None
            ),
            # Issue #1810 — see ``_log_measure_diag``'s note. Read alongside
            # ``pilot_transfer_step_db``: the session that filed the issue
            # showed a null step next to an agc_behavioral_fail, and these two
            # fields together are what make that combination legible.
            pilot_snr_ok=analysis.pilot_snr_ok,
            pilot_snr_db=_worst_pilot_snr_db(analysis),
            # Capture integrity (#1971), disclosed on EVERY verify — pass or
            # fail. On a pass it is what makes "this capture was comparable" a
            # measured statement rather than an unexamined one; on a refusal
            # it names which check fired, which is what tells telemetry a
            # ``locate_failed`` came from this gate rather than from
            # ``_stimulus_locate_ok``, and a ``drift_baselines_disagree`` from
            # a splice rather than a clip. The two scalars are the measured
            # figures the verdict was drawn from, and are reported even where
            # the check they feed was ``not_evaluated``.
            integrity=_capture_integrity_log_field(integrity),
            integrity_not_evaluated=(
                ",".join(integrity.not_evaluated) if integrity is not None else ""
            ),
            integrity_locate_confidence_min=_rounded(
                integrity.locate_confidence_min if integrity is not None else None, 4
            ),
            integrity_residual_ms_worst=_rounded(
                integrity.schedule_residual_ms_worst if integrity is not None else None,
                3,
            ),
            guard=(
                "pilot_level_shift" if verdict.code == REASON_VERIFY_LEVEL_SHIFT else ""
            ),
        )

    # --- helpers -------------------------------------------------------------

    def _rearm_measure_after_transient(self, *, extra_backoff_db: float = 0.0) -> None:
        """Recompose the MEASURE program for the automatic retry (§5.10 t1)."""
        if self._gain_plan_db is not None:
            self._measure_program = self._compose_measure_program(
                self._gain_plan_db, extra_backoff_db=extra_backoff_db
            )

    def _measure_binding_response(self, analysis: ProgramAnalysis) -> Any | None:
        """The driver response whose gate window BINDS the MEASURE phase — the
        shortest (most restrictive) of them.

        One owner for "which response binds", so ``_measure_gate``'s window and
        ``_measure_gate_floor_source``'s provenance always describe the SAME
        capture. Two independent min-selections could drift into reporting one
        response's window beside another's floor source, which is the
        frame-mismatch class issue #1966 exists to close.
        """
        gated = [
            (window, resp)
            for resp, window in (
                (r, _gate_window_ms(r)) for r in analysis.driver_responses
            )
            if window is not None
        ]
        if not gated:
            return None
        return min(gated, key=lambda pair: pair[0])[1]

    def _measure_gate(self, analysis: ProgramAnalysis) -> float | None:
        return _gate_window_ms(self._measure_binding_response(analysis))

    def _measure_gate_floor_source(self, analysis: ProgramAnalysis) -> str | None:
        return _gate_floor_source(self._measure_binding_response(analysis))

    def _build_candidate(
        self, analysis: ProgramAnalysis, cloud: _CloudFitEvidence | None = None,
        *,
        candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
        source_preset: Any = None,
    ) -> tuple[Any, _LinearizationState]:
        """Build one candidate, and return what its linearization produced.

        The state is RETURNED rather than stashed (#2291 Phase 2b). It used to
        reach the accountability gate, the VERIFY prior and the proposal as
        seven ``self._last_*`` fields, which is why the Fc sweep had to
        snapshot and restore them around every candidate — see
        :class:`_LinearizationState`. Callers thread one value instead, so a
        state can only ever describe the candidate returned beside it.
        """
        from jasper.active_speaker.measured_crossover_candidate import (
            MeasuredCrossoverAlignment,
            MeasuredCrossoverCandidate,
        )

        # ABOVE the SF2 degrade handler on purpose: raised inside it this was
        # caught and degraded to a committable trims-only candidate (panel
        # B1/SF2) in the wrong polarity convention. Severity, per the
        # hearing lens: NOT a boost hazard — the degrade left
        # linearization={} and MeasuredCrossoverCandidate bounds trims
        # cut-only to [-60, 0] dB — but it was still offered for Apply.
        # Bare ValueError => internal_error.
        if (self._measurement_protection_sections_by_role is not None
                and not analysis.configured_path_composed):
            raise ValueError("protected-neutral capture reached the fitter uncomposed")

        cand = analysis.candidate
        if cand is None:
            # The residual. ``_measure_verdict`` hoisted this same check to the
            # capture that produces the analysis (2026-07-27 timing move), so
            # reaching it here means a caller that did not walk that path.
            raise CrossoverV2FlowError("MEASURE analysis produced no candidate")
        delay_us, delay_role, polarity = alignment_to_candidate_fields(
            analysis, woofer_role=self._woofer.role, tweeter_role=self._tweeter.role,
        )
        alignment = (
            MeasuredCrossoverAlignment(
                delay_us=delay_us, delay_role=delay_role, polarity=polarity,
            )
            if delay_role is not None
            else MeasuredCrossoverAlignment()
        )

        # Layer-1a driver linearization (#1668 PR-C). HARD GATE: reference-tier
        # mic AND both drivers paired N>=3 — anything else is byte-identical
        # to the pre-PR-C trims-only path (analysis.candidate.trim_db, empty
        # linearization dict). See _linearization_ineligible_reason /
        # _plan_linearization.
        role_attenuations_db: Mapping[str, float] = dict(cand.trim_db)
        linearization: Mapping[str, Any] = {}
        ineligible = self._linearization_ineligible_reason(analysis)
        state = _LinearizationState(outcome=ineligible or "")
        if ineligible is None:
            try:
                plan = self._plan_linearization(
                    analysis, cand, cloud, candidate_sections=candidate_sections,
                )
            except (
                ArithmeticError, AttributeError, RuntimeError, TypeError, ValueError,
                KeyError, IndexError,
            ) as exc:
                # SF2 (adversarial review, 2026-07-24): the fit path is
                # strictly additive — an eligible speaker with a bug in the
                # (still-young) fit engine must degrade EXACTLY to the
                # ineligible path, never fail the whole MEASURE accept.
                # Mirrors _safe_log_diag's "never let enrichment logic break
                # the primary path" posture, one layer earlier (this guards
                # the candidate build itself, not just its diagnostic log
                # line). The caught set matches _safe_log_diag's own
                # (attribute/key/index/type/value access on structured
                # data), extended with ArithmeticError since this call site
                # does floating-point curve fitting (division, log,
                # exponentiation), not plain field extraction, and with
                # RuntimeError because linearization_fit.fit_driver_linearization
                # (N1, this same review) raises exactly that on its own
                # cut-only invariant violation — without it here, N1's safety
                # net would escape SF2's and crash this accept instead of
                # degrading to it. Since #2291 Phase 2b it also catches the
                # planner's own typed refusals — ``NoCrossoverSectionsError``
                # for a candidate preset naming no crossover at all, and
                # ``CandidateFcDisagreementError`` for a section set naming two
                # corners — both ``CrossoverV2ContractError``, itself a
                # ``ValueError``. A prescription cannot be planned for a
                # crossover the candidate does not describe, and degrading to
                # the trims-only lane is the fail-closed answer: the household
                # still gets its measured trims, and nothing is fitted toward a
                # corner nobody committed to.
                log_event(
                    logger, "correction.crossover_v2_linearization_fit_failed",
                    level=logging.WARNING, session_id=self.session_id,
                    reason=type(exc).__name__, exc_info=True,
                )
                role_attenuations_db = dict(cand.trim_db)
                linearization = {}
                # PR-L4 item 1: a fit that raised part-way may already have
                # produced a partial verdict, and none of it survives — a
                # verdict about branches this candidate no longer carries is
                # worse than no verdict, because the accountability gate would
                # grade the wrong thing. A fresh state IS that clearing, and it
                # covers the linearized VERIFY prior too, which the field-based
                # degrade left behind.
                state = _LinearizationState(outcome="fit_failed")
            else:
                role_attenuations_db = dict(plan.role_attenuations_db)
                linearization = dict(plan.linearization)
                state = _LinearizationState.from_plan(plan)

        return MeasuredCrossoverCandidate(
            program_id=analysis.program_id,
            analysis=_analysis_json(analysis),
            source_preset=source_preset or self._preset,
            role_attenuations_db=role_attenuations_db,
            alignment=alignment,
            linearization=linearization,
            # The exclusion reason of record (plan PR-6b). Empty — the
            # pre-move shape — whenever no cloud evidence reached the fit,
            # INCLUDING when the fit itself failed above: a record of what the
            # envelope consumed must not ride a candidate whose corrections
            # came from the trims-only fallback instead.
            exclusion_evidence=(
                self._exclusion_evidence_json(cloud)
                if cloud is not None and linearization
                else {}
            ),
            # Gauge fix (2026-07-24): the single writer's own verdict,
            # stamped verbatim onto the candidate at the exact moment it
            # reaches its final value for this attempt — see
            # MeasuredCrossoverCandidate.linearization_outcome's own
            # docstring for why this module never re-derives it. Since #2291
            # Phase 2b the verdict is this build's own returned state rather
            # than a conductor field, so the candidate and the state beside it
            # cannot describe different builds.
            linearization_outcome=state.outcome,
        ), state

    def _exclusion_evidence_json(self, cloud: _CloudFitEvidence) -> dict[str, Any]:
        """The fit's cloud inputs, as the candidate's exclusion reason of record.

        Everything the two cloud envelope terms actually consumed, plus the
        registry that justifies the intervals — enough that a reader holding
        only ``candidate.json`` can re-derive ``spatial_exclusion_limit`` and
        ``position_stability_limit`` and see WHY a band went uncorrected. The
        registry is re-read from this group's own pipeline result —
        ``_group_cloud_result``, always its CURRENT value, refreshed on every
        close including a retake's re-close (issue #1872) — and serialized by
        :func:`_null_registry_to_dict`, the one owner of that shape, so the
        candidate's copy always describes the cloud actually retained at
        confirm time. ``cloud_measure.json``'s own copy can lag it: the
        evidence store's ``publish_cloud`` write is a per-phase SINGLETON
        (see :meth:`_run_cloud_pipeline`'s own guard), so a retake landing
        between the group's first close and the household's confirm leaves
        the PERSISTED file describing an earlier cloud than the candidate
        filed beside it. Accepted scope, not a defect this method owns: the
        evidence artifact is forensic, the candidate is the product, and
        #1872 is explicit that the store's write-once behavior — first
        artifact stands — is by design.

        ``band_spread`` is carried as the plain per-band numbers rather than
        the dataclass: this is persisted JSON, and the two fields the term
        reads (``sigma_db`` and the band edges it applies over) are the two a
        reader needs to check it. ``max_sigma_db`` rides along because
        ``position_stability_limit``'s docstring turns on the distinction
        between the two spreads, and a reader auditing the choice needs to see
        the number that was NOT used.

        ``validity_floor_hz`` and ``gated_spec_curve`` (room-correction regime
        plan RC1, issue #1787) ride here for a different consumer: the room
        layer. Both previously existed ONLY in the retention-prunable session
        bundle and the clearable v2 flow state, so once a bundle aged out the
        room layer could not tell where this speaker's gated measurement stops
        being trustworthy, nor what the speaker's own gated response is —
        which is exactly what Tier B residual correction must subtract to
        avoid re-flattening voicing the speaker layer already set. Carrying
        them on the candidate makes them travel with the correction they
        justify, and (because a non-empty ``exclusion_evidence`` is
        fingerprinted — see :meth:`MeasuredCrossoverCandidate._core`) makes
        them tamper-evident for free. Both are copied verbatim from this
        group's own CURRENT pipeline result — the same field
        ``cloud_measure.json`` was seeded from at publish time, but see the
        null-registry paragraph above for why a retake after that publish can
        leave the persisted file a close behind the candidate, and why that
        gap is accepted rather than fixed here.

        Cost, stated plainly: ``gated_spec_curve`` duplicates the already-
        decimated cloud curve (<=512 points, two float arrays), which adds
        roughly **15-20 KB of JSON per candidate**. That is a deliberate
        trade — the curve is small, bounded, and written once per commission,
        whereas the alternative (re-reading it from the session bundle) is
        exactly the retention-prunable dependency this extension exists to
        remove. If the curve ever grows unbounded, decimate at this boundary
        rather than dropping the field.
        """
        result = self._group_cloud_result.get(PHASE_CLOUD_MEASURE) or {}
        registry = result.get("null_registry")
        floor = result.get("validity_floor_hz")
        curve = result.get("curve")
        return {
            "phase": PHASE_CLOUD_MEASURE,
            "excluded_bands_hz": [list(band) for band in cloud.excluded_bands_hz],
            "n_positions": cloud.n_positions,
            "band_spread": [
                {
                    "center_hz": float(band.center_hz),
                    "f_lo": float(band.f_lo),
                    "f_hi": float(band.f_hi),
                    "sigma_db": float(band.sigma_db),
                    "max_sigma_db": float(band.max_sigma_db),
                    "n_bins": int(band.n_bins),
                }
                for band in cloud.band_spread
            ],
            "null_registry": dict(registry) if isinstance(registry, Mapping) else {},
            # None is a real, load-bearing value here: "the floor is
            # unverified", never "the floor is 0 Hz" (see
            # cloud_validity_floor_hz). The reader must treat it as absent.
            "validity_floor_hz": (
                float(floor)
                if isinstance(floor, (int, float)) and math.isfinite(float(floor))
                else None
            ),
            "gated_spec_curve": (
                {
                    "freqs_hz": [float(v) for v in curve.get("freqs_hz", ())],
                    "magnitude_db": [float(v) for v in curve.get("magnitude_db", ())],
                }
                if isinstance(curve, Mapping)
                else {}
            ),
        }

    def _linearization_ineligible_reason(self, analysis: ProgramAnalysis) -> str | None:
        """HARD GATE for the Layer-1a fit path, as a named reason or ``None``.

        Eligible means a reference-tier mic AND both drivers paired
        N >= :data:`LINEARIZATION_MIN_PAIRED_OCCURRENCES` in-capture
        occurrences. Anything else falls back to the plain trims-only
        candidate, byte-identical to the pre-PR-C path.

        **Returns the reason rather than stamping it** (#2291 Phase 2b). It
        used to answer ``bool`` and write ``self._last_linearization_outcome``
        on the way out, which made the caller's own outcome depend on a field
        it never named — the same implicit-return shape the planner extraction
        removed one layer up. The caller now holds the answer as a value and
        puts it on the candidate itself.
        """
        if analysis.mic_tier != "reference":
            return "ineligible_mic_tier"
        woofer_resp = _driver_response_by_role(analysis, self._woofer.role)
        tweeter_resp = _driver_response_by_role(analysis, self._tweeter.role)
        if woofer_resp is None or tweeter_resp is None:
            return "ineligible_repeats"
        woofer_n = 1 + len(woofer_resp.repeat_responses)
        tweeter_n = 1 + len(tweeter_resp.repeat_responses)
        if (
            woofer_n >= LINEARIZATION_MIN_PAIRED_OCCURRENCES
            and tweeter_n >= LINEARIZATION_MIN_PAIRED_OCCURRENCES
        ):
            return None
        return "ineligible_repeats"

    def _journal_linearization(self, record: JournalRecord) -> None:
        """Emit one planner record through this session's journal.

        The planner owns *what happened* and returns it as data; this owns
        *how it is said* — the logger and the session identity, neither of
        which a pure function has. Forwarding here rather than iterating
        ``plan.journal`` afterwards is what makes a fit that raises part-way
        still disclose the lines it had reached, including the ``fit_band``
        line naming the corner it ran at.

        ``record.fields`` is spread as keyword arguments rather than handed to
        ``log_event``'s ``fields=`` parameter so the rendered order matches
        what this module emitted before the extraction: ``session_id`` first,
        then the payload in the planner's own order. A payload key colliding
        with ``session_id`` or one of ``log_event``'s own keywords would raise
        ``TypeError`` — a stdlib type, so the planner's port guard contains it
        and names the loss on ``journal_dropped`` rather than costing a
        household its candidate.
        """
        log_event(
            logger, record.event, level=record.level,
            session_id=self.session_id, **record.fields,
        )

    def _plan_linearization(
        self,
        analysis: ProgramAnalysis,
        cand: Any,
        cloud: "_CloudFitEvidence | None",
        *,
        candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
    ) -> LinearizationPlan:
        """Assemble ONE candidate's planner request and run the pure planner.

        This method is the whole of what the conductor still owns about
        linearization: which measurement objects become which named planner
        input. The prescription itself — the σ policy, the fit, the anchored
        give-back, the ripple re-solve, the trim decision and the headroom
        charge — lives in
        :func:`~jasper.active_speaker.crossover_v2.intervention.plan_linearization`
        and is reached identically from both candidate paths (#2291 Phase 2b).

        **One corner, and it is the candidate's.** The context is built from
        the sections this candidate is realized with —
        ``candidate_sections`` for a swept corner, the session preset's own
        regions for the configured one — and
        :class:`~jasper.active_speaker.crossover_v2.contracts.CandidateAcousticContext`
        derives the corner FROM them. ``self._fc_hz`` is not read, and there is
        no second corner in scope for the planner to read either: that is the
        2026-08-10 defect made unrepresentable rather than merely fixed.

        A split or empty section set raises
        (``CandidateFcDisagreementError`` / ``NoCrossoverSectionsError``, both
        ``ValueError`` subclasses), which :meth:`_build_candidate`'s SF2 arm
        degrades to the trims-only lane. Fail-closed on purpose: a candidate
        whose own preset names no crossover has no crossover to plan for, and
        guessing one from the session would be the defect wearing a new hat.

        Only called after :meth:`_linearization_ineligible_reason` returns
        ``None`` — the planner assumes eligibility and does not re-check.
        """
        sections = (
            {role: tuple(regions) for role, regions in candidate_sections.items()}
            if candidate_sections is not None
            else sections_by_role(
                getattr(self._preset, "crossover_regions", ()) or ()
            )
        )
        context = CandidateAcousticContext.from_sections(sections)
        measure_program = self.program_for_phase(PHASE_MEASURE)
        seg_w = measure_program.segment("sweep_w")
        seg_t = measure_program.segment("sweep_t")
        # ProgramSegment.f1_hz/f2_hz are typed float | None (the general
        # ProgramSegment shape also covers non-stimulus/silence segments);
        # __post_init__ guarantees a KIND_SWEEP stimulus segment (which
        # "sweep_w"/"sweep_t" always are) never has either as None. Narrow
        # explicitly for mypy and as a defensive invariant check.
        assert seg_w.f1_hz is not None and seg_w.f2_hz is not None
        assert seg_t.f1_hz is not None and seg_t.f2_hz is not None
        request = request_from_analysis(
            analysis, cand,
            context=context,
            woofer_role=self._woofer.role,
            tweeter_role=self._tweeter.role,
            excited_band_hz={
                self._woofer.role: (seg_w.f1_hz, seg_w.f2_hz),
                self._tweeter.role: (seg_t.f1_hz, seg_t.f2_hz),
            },
            driver_class_by_role=self._driver_class_by_role,
            # Boost permission's ONE necessary condition, and the clause that
            # tells "no cloud by design" (R15's driver-only path) apart from
            # "a cloud was planned and lost". Both are the host's to answer —
            # the planner cannot see a session's phase list.
            post_apply_verifies=self.post_apply_verifies,
            cloud_phase_planned=PHASE_CLOUD_MEASURE in self._journey.plan.phases,
            cloud=cloud,
        )
        plan = plan_linearization(request, journal=self._journal_linearization)
        if plan.journal_dropped:
            # The port refused lines. Plain scalars only, so whatever broke one
            # record's payload cannot also swallow the notice about it. This is
            # ``journal_dropped``'s only reader — without it the planner would
            # be returning a loss nobody is told about.
            log_event(
                logger, "correction.crossover_v2_linearization_journal_dropped",
                level=logging.WARNING, session_id=self.session_id,
                dropped=len(plan.journal_dropped),
                detail="; ".join(plan.journal_dropped),
            )
        return plan


# --------------------------------------------------------------------------- #
# capture plan + session spec (§5.7, auto-advance policy §5.2)
# --------------------------------------------------------------------------- #

# Phone-side recording margin around each program (lead + tail), presentation /
# locator-window data — never a hard deadline (the session runner's timeout_s
# stays the backstop).
CAPTURE_ENTRY_MARGIN_MS = 2000
# The cancelable auto-advance countdown between an accepted CHECK and MEASURE
# (§5.2 — one tap per session is the design; the countdown protects validity
# because a user returning to the phone cold is the likeliest mic-displacement
# event). PROVISIONAL pending W6.
AUTO_ADVANCE_COUNTDOWN_S = 5

# Auto-advance policy vocabulary carried in the per-entry ``screen`` field
# (page policy, not a protocol change — the field is opaque to the schema).
AUTO_ADVANCE_TAP = "tap"            # requires the user's tap (first capture)
AUTO_ADVANCE_COUNTDOWN = "countdown"  # auto-begins behind a cancelable countdown
# Armed by the apply-complete host event. RETAINED but emitted by no plan
# since PR-T3 (D10): stage 1 has no VERIFY entry and stage 2 opens
# already-applied, so nothing waits on an apply inside a session any more. Kept
# as plan-grammar vocabulary — the page and the runner both still understand it
# — and deliberately not deleted; no new design may depend on it.
AUTO_ADVANCE_ON_APPLY = "on_apply"

# PROVISIONAL (W6.10 fold-in): phone-inactivity budget for the very FIRST begin
# of a v2 session (before any capture). The microphone-check screen's placement
# instructions alone legitimately take longer than the general 120 s
# ``DEFAULT_TIMEOUT_S`` to read — Chrome round 1 collapsed here — so the v2 runner
# widens only this first window. Every later window keeps the tight per-phase
# arm/upload backstop; re-derive from W6 bench observation.
V2_FIRST_BEGIN_TIMEOUT_S = 300.0


def _program_duration_ms(program: ExcitationProgram) -> int:
    return int(round(program.total_samples / program.sample_rate_hz * 1000))


def capture_progress_label(index: int, capture_target: int) -> str:
    """The ONE counter a step screen shows — "Measurement N of T".

    Server-derived and whole-session, per the flow-simplification redesign
    (§2.1). It replaces BOTH of the two counters the household used to read at
    once: the per-group "Spot i of n" that headlined each cloud entry, and the
    phone's own ``#status`` line, which counted the same walk differently and
    read as a contradiction. ``index`` is the entry's 1-based WIRE index (the
    relay's own index space), not the 0-based ``CapturePlanEntry.index``.
    """
    return f"Measurement {int(index)} of {int(capture_target)}"


def _cloud_entry_screen(
    *, progress: str, title: str, body: str, auto_advance: str,
) -> dict[str, str]:
    return {
        "progress": progress,
        "title": title,
        "body": body,
        "auto_advance": auto_advance,
    }


def build_v2_capture_plan(
    roles_bands: Sequence[RoleBand],
    fc_hz: float,
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
    include_cloud_measure: bool = True,
    include_lateral: bool = False,
    include_entry_baseline: bool = False,
) -> Any:
    """The STAGE-1 (measure) CapturePlan (§5.7 + flat-linearization PR-3b).

    CHECK and MEASURE are required; the pre-apply cloud, the lateral walk, and
    #2291's entry baseline are optional. The layout
    ``build_v2_cloud_index_phase_map`` documents, built from that same function
    so prompt and phase cannot disagree.

    When included, the pre-apply cloud ends stage 1 and holds for an explicit
    completion signal. R15 omits that cloud, so its CHECK/MEASURE plan closes
    normally. Both shapes leave Apply to the untimed jts.local review interlude;
    post-apply capture remains stage 2's own session.

    **Screen grammar (flow-simplification §2.1).** Every entry's ``screen``
    carries ``progress`` (the one server-derived counter), ``title`` (ONE
    imperative instruction) and ``body`` (at most one supporting clause).
    Screens are an opaque ``str -> str`` map, so none of this is a
    relay/protocol change.

    Entry durations derive from the composed programs (MEASURE sized from a
    nominal gain plan — sweep/gap lengths are gain-independent, so the duration
    is exact even before CHECK's solve) plus a lead/tail margin; each entry's
    ``screen`` carries the phase prompt AND the §5.2 auto-advance policy:
    CHECK and MEASURE each require a tap (MEASURE is the longest and loudest
    capture of the session — issue #1823; see the entry below), and every
    prompted cloud position requires the operator's tap — the mic has to be
    MOVED between them, so a countdown would fire into a hand still in flight.

    No phone-side mechanism is new: ``CapturePlanEntry.screen`` and
    ``AUTO_ADVANCE_TAP`` already carry per-entry copy the page renders and gates
    on, and the deployed page reads ``max_attempts``/``capture_target``
    generically with no plan-length cap of its own.
    """
    from jasper.capture_relay.spec import CapturePlan, CapturePlanEntry

    roles = tuple(roles_bands)
    # courtesy_prelude=COURTESY_PRELUDE_ENABLED on every composed program below
    # (issue #1677): this is the phone's DURATION BUDGET, so it must agree with
    # what the conductor actually plays — ``crossover_v2.programs``'s
    # ``SessionExcitation`` composers — or the phone stops recording before the
    # real (prelude-lengthened) program ends.
    check = build_check_program(roles, courtesy_prelude=COURTESY_PRELUDE_ENABLED)
    nominal_gains = {rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}
    measure = build_measure_program(
        nominal_gains, roles,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=COURTESY_PRELUDE_ENABLED,
    )
    # Every prompted cloud position plays the VERIFY-shaped summed sweep, so
    # its DURATION is the verify program's even though stage 1 runs no VERIFY
    # phase of its own.
    verify = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=COURTESY_PRELUDE_ENABLED,
    )
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    index_phase = build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=include_cloud_measure,
        include_lateral=include_lateral,
        include_entry_baseline=include_entry_baseline,
    )
    target = len(index_phase)
    verify_ms = _program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS
    measure_ms = _program_duration_ms(measure) + CAPTURE_ENTRY_MARGIN_MS
    entries: list[Any] = [
        CapturePlanEntry(
            index=0,
            kind_label="check",
            duration_ms=_program_duration_ms(check) + CAPTURE_ENTRY_MARGIN_MS,
            screen={
                "progress": capture_progress_label(1, target),
                "title": (
                    "Stand the microphone about 1 m in front of the speaker, "
                    "at tweeter height."
                ),
                # NOT "stay quiet" (work order D8, issue #1835). CHECK's 12 s
                # ambient window is the SESSION's room-noise measurement and it
                # is deliberately composed to run BEFORE anyone is asked to
                # hush (jasper.audio_measurement.program's module docstring);
                # the gain solve reads it, so a pre-hushed room reads quieter
                # than reality and the solve under-drives against the noise the
                # later sweeps actually face. The measurement-honest request is
                # to carry on. The speaker asks for quiet itself, on the
                # in-sweep windows where quiet genuinely is wanted — a
                # different window with a different purpose, which this must
                # not collapse into one sentence.
                "body": (
                    "JTS listens to the room exactly as it is first, so carry "
                    "on as you were — it will say when to be quiet."
                ),
                # The phone's OWN pre-arm floor window, which is a third
                # measurement again: a sub-second reading of what the
                # microphone hears, taken before the speaker plays anything.
                # It gets its own sentence for the same reason — asking for
                # quiet there hushes the room a moment before CHECK measures
                # it. Absent on every other entry, where the page's default
                # (quiet, because a sweep follows immediately) stays right.
                "noise_note": (
                    "Listening to the room as it normally is — carry on as "
                    "you were."
                ),
                "auto_advance": AUTO_ADVANCE_TAP,
            },
        ),
        CapturePlanEntry(
            index=1,
            kind_label="measure",
            duration_ms=measure_ms,
            screen={
                "progress": capture_progress_label(2, target),
                "title": "Keep the microphone still — this spot is the mark.",
                # MEASURE is the session's LONGEST capture and the one that
                # CAN be its loudest — since #1825/#1829 each driver's level is
                # solved to the SNR the fit needs in its own band, so a quiet
                # room gets a quiet MEASURE and a noisy one still gets the full
                # level. Until issue #1823 it rolled straight out of CHECK on a
                # countdown:
                # the household went from one capture into a much louder one
                # with no chance to say "not yet". Same-spot auto-advance was
                # the right instinct — no movement is needed — but it read as
                # the speaker taking a liberty. One tap, with copy that says
                # what is coming, buys the consent back. The countdown
                # vocabulary stays in the plan grammar (AUTO_ADVANCE_COUNTDOWN,
                # AUTO_ADVANCE_COUNTDOWN_S, and the page's renderPlanCountdown)
                # for a future same-spot transition that earns it.
                #
                # Household language, not ours (coordinator ruling, 2026-07-28):
                # the tail says what the level is FOR, not which internal stage
                # asked for it. "The fit" is a word for this file, not for
                # someone holding a phone.
                "body": (
                    "This one is longer, and can be the loudest — it measures "
                    "each driver alone at the level it needs to hear each one "
                    "clearly."
                ),
                "auto_advance": AUTO_ADVANCE_TAP,
            },
        ),
    ]
    # R16's lateral walk (plan §4.4). Same 0-based index arithmetic as the cloud
    # loop below; ``duration_ms`` is the MEASURE program's because each pose
    # replays it verbatim (``program_for_phase``), not the summed sweep's.
    lateral_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_LATERAL
    ]
    for offset, capture_index in enumerate(lateral_indexes):
        prompt = LATERAL_POSE_PROMPTS[offset]
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="lateral",
                duration_ms=measure_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=prompt.headline,
                    body=prompt.detail,
                    auto_advance=AUTO_ADVANCE_TAP,
                ),
            )
        )
    # The two prompted groups. ``index_phase`` is 1-based (the relay's own
    # index space); ``CapturePlanEntry.index`` is 0-based, hence the -1.
    cloud_measure_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_CLOUD_MEASURE
    ]
    for offset, capture_index in enumerate(cloud_measure_indexes):
        prompt = CLOUD_POSITION_PROMPTS[offset]
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_measure",
                duration_ms=verify_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=prompt.headline,
                    body=prompt.detail,
                    auto_advance=AUTO_ADVANCE_TAP,
                ),
            )
        )
    # #2291's "before" measurement, LAST. Its duration is the summed sweep's
    # (``verify_ms``) because it replays the VERIFY program verbatim — the
    # identity ``program_for_phase`` guarantees and the benefit comparison
    # depends on. A tap, like every other entry: the household has just walked
    # the lateral poses and has to come back to the mark first, so a countdown
    # would fire into a hand still in flight.
    for capture_index in [
        i for i, p in sorted(index_phase.items()) if p == PHASE_ENTRY_BASELINE
    ]:
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="entry_baseline",
                duration_ms=verify_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title="Back to the mark — one last measurement before tuning.",
                    # Says WHAT it buys, in the household's terms: this is the
                    # recording the speaker is compared against afterwards, so
                    # "it got better" becomes something measured rather than
                    # asserted. No internal vocabulary (no "baseline", no
                    # "summed sweep") — the same register as the MEASURE entry
                    # above.
                    body=(
                        "This records how the speaker sounds right now, so JTS "
                        "can tell you whether the tuning actually improved it."
                    ),
                    auto_advance=AUTO_ADVANCE_TAP,
                ),
            )
        )
    return CapturePlan(
        capture_target=target,
        # Derived from the entries this plan ACTUALLY emits rather than from the
        # shape's cloud-only arithmetic, so a walk that grows (R16's poses) grows
        # its retake budget with it. Byte-identical on both pre-R16 shapes:
        # with the cloud on and no lateral, ``target == measure_capture_target``,
        # so this reproduces ``shape.measure_max_attempts`` exactly; with the
        # cloud off it reproduces the previous ``target + CLOUD_RETAKE_ALLOWANCE``.
        max_attempts=(
            target
            + (GEOMETRY_RETRY_POSITIONS if include_cloud_measure else 0)
            + CLOUD_RETAKE_ALLOWANCE
        ),
        schema_version=2,
        entries=tuple(entries),
    )


def _index_of_phase(index_phase: Mapping[int, str], phase: str) -> int:
    for index, value in sorted(index_phase.items()):
        if value == phase:
            return index
    raise CrossoverV2FlowError(f"cloud index map has no {phase} entry")


def build_v2_verify_capture_plan(
    fc_hz: float, *, plan_shape: V2PlanShape | None = None,
) -> Any:
    """The post-apply (STAGE 2) plan — the tier's own verify walk, or the
    1-entry recovery re-arm.

    ``plan_shape is None`` is the shipped §5.2 recovery re-verify, byte-
    identical to what it has always been: one entry, ``CAPTURE_PLAN_MAX_
    ATTEMPTS``, and copy that LEADS with how cheap it is (§2.4 — the 2026-07-27
    hardware session abandoned this recovery because nothing on screen said
    "Try again" was one sweep rather than another walk). It is what a FAILED
    stage 2 offers, and what ``/crossover/v2/verify`` still does by default.

    A ``plan_shape`` builds **stage 2 of the two-stage commission flow** (work
    order D2, owner-confirmed 2026-07-29): ``M`` entries — VERIFY's design-axis
    anchor at the mark plus ``M − 1`` prompted post-apply positions — so each
    tier keeps its shipped verify shape. Express is ``M = 1`` (its whole
    post-apply check is the anchor, and it makes no cross-position claim);
    Full is the six-position spatial walk whose combined curve the after-chart,
    the post-apply spec verdict, and the delta probe all read. Running Full's
    stage 2 as a single position would leave its post-apply group with 0 curves
    and no combine at all, and would make ``_TIER_CLAIMS``' "re-check the
    result at several spots around the mark" untrue.

    **The §2.2 confirm-then-tone tap survives, re-anchored to stage 2's own
    begin** (work order D10). The anchor entry carries ``confirm_title`` /
    ``confirm_body`` verbatim, so the tone still waits for the household to
    say they are standing on the mark — what changed is only the ordering
    premise (there is no in-session apply to confirm *after* any more).
    """
    from jasper.capture_relay.spec import CapturePlan, CapturePlanEntry

    verify = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=COURTESY_PRELUDE_ENABLED,
    )
    verify_ms = _program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS
    if plan_shape is None:
        entry = CapturePlanEntry(
            index=0,
            kind_label="verify",
            duration_ms=verify_ms,
            screen={
                "progress": capture_progress_label(1, 1),
                "title": REVERIFY_NO_REWALK_HEADLINE,
                "body": "Put the microphone back on the mark and hold it still.",
                "auto_advance": AUTO_ADVANCE_TAP,
            },
        )
        return CapturePlan(
            capture_target=1,
            max_attempts=CAPTURE_PLAN_MAX_ATTEMPTS,
            schema_version=2,
            entries=(entry,),
        )
    index_phase = build_v2_verify_index_phase_map(plan_shape=plan_shape)
    target = plan_shape.verify_capture_target
    # The phone's END screen once every capture completes
    # (capture-page/js/main.js's renderPlanAllDone reads the FINAL wire index's
    # entry) — owner ruling, 2026-07-20: state the outcome plainly and point at
    # the speaker page for undo/compare, rather than the shared "All
    # measurements done" generic copy. This is STAGE-2 copy and moved here with
    # stage 2: it is the end of the journey, not the end of a measurement.
    #
    # WHICH entry is last depends on the tier: Full ends on the post-apply
    # group's tail, express (M = 1) ends on the anchor itself. Express also
    # says LESS, because it verified less — it confirmed the result at the mark
    # and made no cross-position post-apply claim at all (§1.3).
    #
    # **Every word here is written BEFORE THE FIRST TONE PLAYS** — this whole
    # plan is built when stage 2 is armed — so nothing on this screen may
    # assert a MEASURED outcome (issue #1964). Each tier's copy states only
    # what arming already establishes: the correction is applied (stage 2 only
    # exists because the household applied it), and reaching this screen at all
    # means the tracking comparator PASSED (a tracking fail returns
    # ``PhaseVerdict(False, …)``, so the phone renders a retry, never this).
    #
    # Full used to say "Verified and applied." That is the post-apply cloud's
    # SPEC verdict, which is computed after the last capture and can fail while
    # tracking passes — the household then read "Verified and applied" on the
    # phone and "Your speaker is tuned, BUT the result still measures further
    # from flat than the target…" on jts.local, for one session. The phone
    # cannot carry that caveat: its component vocabulary
    # (``capture_relay.spec.UI_COMPONENT_TYPES``) has no result-shaped member,
    # and the only runtime seam that could deliver one — the relay's
    # LAST-WRITE-WINS host-event slot — is routinely overwritten by
    # ``capture_set_complete`` before the phone's ~250 ms poll reads the final
    # ``capture_result``. So the verdict has ONE owner, jts.local's done
    # screen, and this screen says where it is rather than guessing it.
    #
    # **Express's upgrade-path phrase is COPIED, not authored** (B2 fix,
    # adversarial review of PR #1780). It read "for the verified-everywhere
    # result", which that review ruled an overclaim — a Full walk re-checks a
    # handful of prompted spots around the mark, never every point in the room
    # — and the corrected phrase already ships on jts.local in two places
    # (``crossover_envelope_v2``'s express ``done_verdict`` and
    # ``_TIER_CLAIMS[TIER_FULL]``). The phone kept the withdrawn wording, so
    # one journey said both things: the same cross-surface divergence the
    # paragraph above closes. Spell it the way the wizard spells it, so a
    # future re-wording has one place to start rather than three.
    done_screen = {
        "done_title": "Your speaker is tuned",
        "done_body": (
            "The speaker page has the result — manage or undo there."
            if plan_shape.has_cloud_verify_group
            else "Confirmed at the mark and applied. Run a Full measurement "
            "for the result checked at several spots around the mark, or "
            "manage this one on the speaker page."
        ),
    }
    anchor_screen: dict[str, str] = {
        "progress": capture_progress_label(1, target),
        "title": "Back at the mark — one sweep to check the result.",
        "body": "Same spot, same height, pointed at the speaker.",
        "auto_advance": AUTO_ADVANCE_TAP,
        # §2.2's confirm-then-tone tap, on stage 2's own begin (D10). Same two
        # strings the single-session plan carried, so the grammar the household
        # learned in stage 1 is the grammar stage 2 opens with.
        "confirm_title": "Back on the mark, holding still?",
        "confirm_body": "Same spot, same height, pointed at the speaker.",
    }
    if not plan_shape.has_cloud_verify_group:
        anchor_screen.update(done_screen)
    entries: list[Any] = [
        CapturePlanEntry(
            index=0,
            kind_label="verify",
            duration_ms=verify_ms,
            screen=anchor_screen,
        )
    ]
    cloud_verify_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_CLOUD_VERIFY
    ]
    for offset, capture_index in enumerate(cloud_verify_indexes):
        prompt = CLOUD_POSITION_PROMPTS[offset]
        screen = _cloud_entry_screen(
            progress=capture_progress_label(capture_index, target),
            title=prompt.headline,
            body=prompt.detail,
            auto_advance=AUTO_ADVANCE_TAP,
        )
        if offset == len(cloud_verify_indexes) - 1:
            screen.update(done_screen)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_verify",
                duration_ms=verify_ms,
                screen=screen,
            )
        )
    return CapturePlan(
        capture_target=target,
        max_attempts=plan_shape.verify_max_attempts,
        schema_version=2,
        entries=tuple(entries),
    )


def build_v2_verify_session_spec(
    fc_hz: float,
    *,
    acknowledgement_binding: str,
    plan_shape: V2PlanShape | None = None,
    **spec_kwargs: Any,
) -> Any:
    """The relay v3 spec for a post-apply session (stage 2, or §5.2 recovery).

    **The consent surface is chosen by the PLAN's own shape, not by the caller's
    intent**, so a one-sweep session and a walk can never advertise each other's
    copy. A single-capture plan — the recovery re-verify, and Express's stage 2,
    which really is one held-still sweep at the mark — keeps the stationary
    consent copy and LEADS with :data:`REVERIFY_NO_REWALK_HEADLINE`, because the
    2026-07-27 hardware session abandoned this recovery for want of that
    sentence (§2.4) and a household who has just walked a cloud needs it just
    as much. A multi-capture plan (Full's stage 2) is a walk, so it takes the
    guided consent surface with its own capture count and tier, exactly as
    :func:`build_v2_session_spec` does for stage 1.
    """
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    plan = build_v2_verify_capture_plan(fc_hz, plan_shape=plan_shape)
    walked = plan.capture_target > 1
    extra: dict[str, Any] = (
        {
            "guided_captures": plan.capture_target,
            "guided_tier": plan_shape.tier if plan_shape is not None else "",
            # Stage 2's walk is oriented on the same terms as stage 1's (work
            # order D7): a post-apply cloud discovered one prompt at a time is
            # the same defect, and the group walks the same table.
            "walk_shape": cloud_walk_shape(
                plan.capture_target, post_apply=True
            ),
        }
        if walked
        else {"reverify_lead": REVERIFY_NO_REWALK_HEADLINE}
    )
    return build_crossover_sweep_spec(
        driver_label="crossover verification",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=max(entry.duration_ms for entry in plan.entries),
        capture_plan=plan,
        **extra,
        **spec_kwargs,
    )


def session_wall_clock_ceiling_s(capture_plan: Any) -> float:
    """The walked-away volume ceiling for one plan, scaled by its length.

    ``session_volume_plan.DEFAULT_WALL_CLOCK_CEILING_S`` (1800 s ≈ 2× the relay
    TTL) was sized for the 3-entry flow. A 16-capture cloud is a genuinely
    longer session — the operator walks the mic to a new spot, reads a prompt,
    and taps, once per position — so a fixed 1800 s would force-drain the
    measurement volume mid-cloud and turn a good session into a
    volume_recovery screen.

    Scaling, not a bigger constant: the ceiling grows by
    :data:`WALL_CLOCK_CEILING_PER_ENTRY_S` for every accepted capture beyond
    the 3-entry baseline, and is hard-capped by the volume plan's own
    ``MAX_WALL_CLOCK_CEILING_S`` (which owns that bound, since it owns the
    walked-away guarantee). The per-entry number is a BUDGET ALLOWANCE, not a
    measured position time — nothing has yet timed a household walking a cloud
    (the hardware smoke after PR-4/PR-7 is where that number gets its first
    measurement); it is deliberately generous, because the failure it guards
    against is a false drain mid-session while the failure it trades against is
    a walked-away speaker returning to household volume a few minutes later
    than it might have. The restore ladder ("exact" then the -60 dBFS emergency
    floor) and the restore-once latch are untouched.

    Since the two-stage split (work order D2) each STAGE arms its own ceiling
    from its own plan, and this function is unchanged — it reads whatever plan
    it is handed. Full: stage 1 (10 entries) 1800 + 7*120 = 2640 s, stage 2
    (6) 1800 + 3*120 = 2160 s. Express: stage 1 (6) 2160 s, stage 2 (1) the
    plain 1800 s baseline. **The split lowers the worst case from 3360 s to
    2640 s and gives each stage its own fresh relay TTL; it does not make
    either stage fit inside that 900 s TTL, and this docstring must not be
    read as claiming it does.** At the 19-entry maximum the unclamped value
    would be 3720 s and the plan's hard cap binds at 3600 s.
    """
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_WALL_CLOCK_CEILING_S,
        MAX_WALL_CLOCK_CEILING_S,
    )

    target = int(getattr(capture_plan, "capture_target", CAPTURE_PLAN_TARGET) or 0)
    extra = max(0, target - CAPTURE_PLAN_TARGET)
    return min(
        MAX_WALL_CLOCK_CEILING_S,
        DEFAULT_WALL_CLOCK_CEILING_S + extra * WALL_CLOCK_CEILING_PER_ENTRY_S,
    )


# Per accepted capture beyond the 3-entry baseline. 120 s covers a prompt read,
# a deliberate mic move, a tap, the ~16 s sweep entry, and the upload with room
# to spare. See ``session_wall_clock_ceiling_s`` for why it is generous and
# what it is NOT (a measurement).
WALL_CLOCK_CEILING_PER_ENTRY_S = 120.0

# A fixed, representative 2-way RoleBand pair for :func:`tier_display_info`
# ONLY — never the household's actual excitation ceilings/topology. See that
# function's docstring for why a representative pair is honest here. The
# tweeter's lower edge is deliberately the CONSERVATIVE end of a physically
# plausible tweeter (~1.5-2 kHz, not the 300 Hz woofer/midrange territory an
# earlier revision used) — S3 review finding, adversarial review of PR #1780:
# a too-low f1 biased the estimated sweep duration (and so the displayed
# minutes) SHORT, the wrong failure direction for a number the household
# reads as a promise.
_DISPLAY_ROLES_BANDS = (
    RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
    RoleBand("tweeter", 1, FrequencyBand(1800.0, 20000.0)),
)
_DISPLAY_FC_HZ = 1600.0


def tier_display_info() -> dict[str, dict[str, int]]:
    """Per-tier ``{capture_target, estimated_minutes}`` for the wizard's
    pre-session tier chooser (flow-simplification §1.1/§3).

    The chooser must show the SAME derived duration a live session's own
    capture plan would display, never a hand-written prettier figure
    (§1.1). But at chooser time no session exists yet, and resolving the
    household's REAL excitation ceilings/topology
    (:func:`~jasper.web.correction_crossover_v2.resolve_conductor_context`)
    is refuse-if-not-ready and can regenerate the crossover preview file as
    a side effect — wrong for a value this module computes on every ~1.5 s
    poll of the ``microphone_check`` screen, which must render the chooser
    regardless of whether that heavier resolution would currently succeed.

    **A fixed representative :class:`RoleBand` pair is honest here, but NOT
    because program length is invariant to the band (S3 fix, adversarial
    review of PR #1780 — an earlier revision of this docstring overclaimed
    that).** The realized sweep length genuinely varies with the swept
    band's edges: each sweep's MESM inter-sweep gap
    (:func:`~jasper.audio_measurement.program.mesm_gap_samples`) and its own
    Novak-synchronized sample count both depend on ``f1``/``f2`` — a
    narrower or differently-centered band realizes a measurably different
    duration, not the same one. The invariant that actually makes a fixed
    pair honest is narrower: :meth:`CapturePlan.estimated_minutes`'s
    ceil-to-whole-minutes quantum absorbs that variance across the
    PLAUSIBLE 2-way band space. Swept empirically across several genuinely
    different plausible topologies (varying woofer/tweeter bands and
    ``fc_hz`` — see ``tests/test_crossover_v2_conductor.py``'s
    ``test_tier_display_info_minutes_hold_across_plausible_topologies``),
    Full displays 11 minutes and Express displays 6 minutes in every case
    checked, with Express the tighter margin (on the order of 10-15 s of
    headroom before the next minute boundary, at this representative pair —
    the number that would need re-deriving if a future change genuinely
    widened the plausible band space). Express's figure moved from 5 to 6 with
    the two-stage split, and the reason is the split's own arithmetic rather
    than a longer session: the journey is now TWO plans and each ceils to a
    whole minute separately, which is the deliberately conservative choice
    recorded below. ``capture_target`` needs no audio program at all — it is
    pure arithmetic on the resolved :class:`V2PlanShape`.

    **Memoized (N1 fix, adversarial review of PR #1780).** The representative
    inputs are fixed module constants, so the result never changes within a
    process — computing it fresh cost 4 :func:`build_v2_capture_plan` calls
    per envelope render (:func:`~jasper.active_speaker.crossover_envelope_v2._tier_choice_actions`
    calls this once per tier action) on every ~1.5 s wizard poll, ~8 ms on a
    fast Mac and worse on a Pi 5. :func:`functools.lru_cache` does not cache
    an exception, so a genuine regression in the representative build would
    otherwise re-raise on every poll forever; the try/except below is a
    one-time fallback specifically for that residual path (N5b), not a
    per-poll retry.
    """
    try:
        return _tier_display_info_cached()
    except (CrossoverV2FlowError, ValueError) as exc:
        log_event(
            logger, "correction.tier_display_info_failed",
            level=logging.WARNING, error=str(exc),
        )
        return {
            tier: {
                "capture_target": (_stage1_capture_target(shape)
                                   + shape.verify_capture_target),
                "estimated_minutes": 0,
                # Present even here: the chooser's copy reads both, and a
                # KeyError on the degraded path would take the whole
                # microphone_check screen down over a duration it already
                # knows how to render as unknown.
                "stage1_captures": _stage1_capture_target(shape),
                "stage2_captures": shape.verify_capture_target,
            }
            for tier, shape in ((t, resolve_plan_shape(t)) for t in TIERS)
        }


@lru_cache(maxsize=1)
def _tier_display_info_cached() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for tier in TIERS:
        shape = resolve_plan_shape(tier)
        # BOTH stages (two-stage commission D2). ``capture_target`` has always
        # been the whole journey's count, and after the split the duration has
        # to be the whole journey's too or the chooser quotes a Full tier's 16
        # measurements against stage 1's minutes alone. Two ceils rather than
        # one is deliberately conservative: this is a DISPLAY number and the
        # household really does pay two per-session set-ups.
        #
        # T4 owns the stage-aware WORDING this derivation makes possible ("N
        # now, M after you apply"); this is the arithmetic underneath it.
        stage1 = build_v2_capture_plan(
            _DISPLAY_ROLES_BANDS, _DISPLAY_FC_HZ, plan_shape=shape,
            include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
            include_lateral=STAGE1_INCLUDES_LATERAL,
            include_entry_baseline=STAGE1_INCLUDES_ENTRY_BASELINE,
        )
        stage2 = build_v2_verify_capture_plan(_DISPLAY_FC_HZ, plan_shape=shape)
        out[tier] = {
            # Summed from the plans: the shape's own `capture_target` counts a
            # pre-apply cloud stage 1 no longer walks.
            "capture_target": stage1.capture_target + shape.verify_capture_target,
            "estimated_minutes": (
                stage1.estimated_minutes() + stage2.estimated_minutes()
            ),
            # The per-stage split T4's chooser copy states, off the SHAPE's own
            # two targets — the same properties the two plan builders size
            # themselves from, so the chooser and the plans cannot quote
            # different sessions, and their sum is ``capture_target`` by
            # construction. Available without an audio program, which is what
            # lets the fallback path below answer with the same numbers rather
            # than omitting the keys the chooser copy reads.
            "stage1_captures": stage1.capture_target,
            "stage2_captures": shape.verify_capture_target,
        }
    return out


def build_v2_session_spec(
    roles_bands: Sequence[RoleBand],
    fc_hz: float,
    *,
    acknowledgement_binding: str,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
    include_cloud_measure: bool = True,
    include_lateral: bool = False,
    include_entry_baseline: bool = False,
    **spec_kwargs: Any,
) -> Any:
    """One relay v3 stage-1 spec, optionally including the pre-apply cloud (§5.7).

    Rides the existing ``build_crossover_sweep_spec`` (same kind, transport,
    and placement-acknowledgement machinery) with its stage-1 plan attached, and
    selects guided consent only when that plan includes the pre-apply cloud —
    the fixed-on-axis wording that builder emits by default promises a
    stationary mic for the whole session, which is exactly what a cloud
    breaks. The guided copy still names the mark as the starting point; the
    per-entry screens carry each prompted move from there. The spec-level
    stimulus duration is the longest entry so the per-capture deadline covers
    every phase.

    ``plan_shape`` is the ONE resolved (tier, N, M) value the caller also
    threads into :func:`build_v2_cloud_index_phase_map` — see
    :class:`V2PlanShape` for why that matters.
    """
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    plan = build_v2_capture_plan(
        roles_bands, fc_hz, plan_shape=shape,
        include_cloud_measure=include_cloud_measure,
        include_lateral=include_lateral,
        include_entry_baseline=include_entry_baseline,
    )
    longest_ms = max(entry.duration_ms for entry in plan.entries)
    # R16: EITHER group makes this a walk. The consent copy below was gated on
    # the cloud alone; leaving it there would promise a stationary microphone
    # to a household about to be prompted through five moves — the exact
    # dishonesty the guided wording exists to prevent.
    #
    # #2291's entry baseline is deliberately NOT a third term. A walk is a
    # session that prompts the household to MOVE the microphone, and the entry
    # baseline is one capture at the mark they are already standing at — it
    # asks them to come back, not to go anywhere new. ``walk_shape_for`` agrees
    # by construction: with neither group on it computes a reach of 0 cm and
    # returns "", so an entry-baseline-only session that claimed ``walked``
    # would emit guided consent copy with no shape line under it — a promise of
    # a walk with nothing to describe.
    walked = include_cloud_measure or include_lateral
    return build_crossover_sweep_spec(
        driver_label="crossover",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=longest_ms,
        capture_plan=plan,
        # The consent surface must describe the walk, not a stationary mic —
        # the count is every capture the household is prompted through, which
        # is the plan's own target.
        guided_captures=plan.capture_target if walked else 0,
        # …and which INSTRUMENT that walk is, so the announcement screen can
        # say "quick tune" vs "full measurement" without the spec builder
        # re-deriving a shape it does not own (§1.4 / §2.3).
        guided_tier=shape.tier if walked else "",
        # …and how far the walk reaches, in one line (work order D7's intent,
        # #1941 R1's presentation). Derived HERE, from the same table and the
        # same group size the per-entry screens above are built from, so the
        # orientation and the prompts cannot describe different walks. The
        # reach is the FURTHEST of whichever groups run, so a lateral-only
        # session quotes 40 cm rather than a cloud's ceiling it never walks.
        walk_shape=walk_shape_for(
            cloud_positions=(
                shape.cloud_measure_positions if include_cloud_measure else 0
            ),
            lateral=include_lateral,
        ),
        **spec_kwargs,
    )


# --------------------------------------------------------------------------- #
# production playback seams (binds W2's play_program to the real DSP boundary)
# --------------------------------------------------------------------------- #


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.priors`, which owns
#: it beside the priors that are its heaviest readers (#2291 Phase 5a-iii).
_role_transfers = _priors.role_transfers


async def confirm_graph_is_live(cam: Any, submitted_yaml: str) -> None:
    """Prove the graph CamillaDSP is running is the one just submitted.

    Contract: prove the SUBMITTED graph is live, tolerate benign serializer
    normalization, reject a different graph. Submitted TEXT vs ``GetConfig``
    cannot — a readback is a default-filled, normalized SUPERSET — so
    ``ReadConfig`` canonicalizes first and STRICT equality still applies.
    Evidence, and what was NOT measured:
    ``docs/HANDOFF-crossover-measurement-v2.md`` "Confirming a program graph
    is live".
    """
    from jasper.camilla import CamillaConfigRejected

    from .commissioning_admission import (
        ActiveCommissioningAdmissionError,
        running_graph_fingerprint,
    )
    from .program_playback import ProgramPlaybackError

    try:
        normalized = await cam.normalize_config_raw(submitted_yaml, best_effort=False)
        if not isinstance(normalized, str):
            raise CamillaConfigRejected("normalization returned no config")
    except CamillaConfigRejected as exc:
        raise ProgramPlaybackError("program graph normalization failed") from exc
    try:
        matched = running_graph_fingerprint(
            await cam.get_active_config_raw(best_effort=False)
        ) == running_graph_fingerprint(normalized)
    except ActiveCommissioningAdmissionError as exc:
        raise ProgramPlaybackError("program graph readback is invalid") from exc
    if not matched:
        raise ProgramPlaybackError("program graph load was not confirmed")


def bind_program_playback_seams(
    cam: Any,
    *,
    bundle_dir: str,
    artifact: Any,
    config_dir: str,
    program: ExcitationProgram,
    wav_path: str,
    topology: Any,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """The real CamillaController-backed seams for :func:`play_program` (W2's
    open wiring question, answered here).

    Returns the keyword mapping ``play_program(program, program_graph_yaml=...,
    session_volume_plan=..., **bind_program_playback_seams(...))`` consumes:

    * ``read_current_config_path`` — ``cam.get_config_file_path`` (the persisted
      statefile boot anchor, the restore target).
    * ``load_program_graph`` — INLINE ``cam.set_active_config_raw`` (CamillaDSP
      ``SetConfig``): applies, then confirms by fresh semantic readback without repointing the
      statefile, preserving the crash-recovery-MUTED structural invariant
      exactly as :func:`jasper.active_speaker.commission_wiring.commission_load_config`
      documents. A crash mid-program reboots onto the staged anchor, never the
      program graph.
    * ``restore_graph`` — reads the entry config path's bytes and re-applies
      them inline (same SetConfig transport; the statefile stays untouched).
    * ``play_wav`` — the verified-WAV source
      (:func:`jasper.active_speaker.program_playback.verified_program_aplay`):
      sha256-bound bytes through the stable-fd aplay path to
      ``correction_substream``.
    * ``readmit`` — :func:`jasper.active_speaker.program_admission.readmit_program_from_wav`
      from a FRESH byte readback (the play-time gate).
    * ``writer_lock`` — :func:`jasper.dsp_apply.dsp_writer_lock` on the shared
      generated-config dir, so the program load/restore serializes with every
      other DSP writer.

    NOT hardware-validated yet — W6 exercises this binding end-to-end on JTS3;
    until then it is the single place the real transport is named, and every
    orchestration test injects fakes instead.
    """
    from pathlib import Path

    from jasper.dsp_apply import dsp_writer_lock

    from .program_admission import readmit_program_from_wav
    from .program_playback import ProgramPlaybackError, verified_program_aplay

    async def _read_current_config_path() -> str | None:
        return await cam.get_config_file_path(best_effort=False)

    async def _load_program_graph(program_graph_yaml: str) -> bool:
        if not await cam.set_active_config_raw(program_graph_yaml, best_effort=False):
            raise ProgramPlaybackError("program graph load was not confirmed")
        await confirm_graph_is_live(cam, program_graph_yaml)
        return True

    async def _restore_graph(entry_config_path: str) -> bool:
        text = Path(entry_config_path).read_text(encoding="utf-8")
        return await cam.set_active_config_raw(text, best_effort=False)

    async def _play_wav() -> Any:
        return await verified_program_aplay(bundle_dir, artifact, timeout_s=timeout_s)

    async def _readmit() -> Any:
        # ``declared_sensitivities`` MUST match what the conductor composed
        # against: readmission re-resolves every cap, so a program composed at
        # the W6.5-derived HF ceiling would be refused here at the legacy one
        # if the mapping were dropped on this side.
        return readmit_program_from_wav(
            program,
            wav_path,
            topology=topology,
            safety_profile=safety_profile,
            role_targets=role_targets,
            session_volume_db=session_volume_db,
            declared_sensitivities=declared_sensitivities,
        )

    return {
        "read_current_config_path": _read_current_config_path,
        "load_program_graph": _load_program_graph,
        "restore_graph": _restore_graph,
        "play_wav": _play_wav,
        "readmit": _readmit,
        "writer_lock": lambda: dsp_writer_lock(
            config_dir, source="crossover_v2_program"
        ),
    }


# --------------------------------------------------------------------------- #
# session-volume lifecycle (one SessionVolumePlan per session, §5.5)
# --------------------------------------------------------------------------- #


def derive_session_volume_db(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Sequence[str],
    *,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> float:
    """The fixed session measurement volume — the SSOT derivation (§5.5).

    Thin pass-through to
    :func:`jasper.active_speaker.session_volume_plan.session_measurement_volume_db`
    so the conductor and its callers reach the one derivation path (least-
    sensitive driver reaches the reference level; more-sensitive drivers
    attenuate down digitally). Kept here so the flow imports one module.
    ``declared_sensitivities`` rides through so the caps feeding ``max(caps)``
    are the same W6.5-derived caps admission enforces.
    """
    from .session_volume_plan import session_measurement_volume_db

    return session_measurement_volume_db(
        safety_profile,
        target_fingerprints,
        declared_sensitivities=declared_sensitivities,
    )


async def open_measurement_volume(
    plan: Any,
    *,
    safety_profile: Mapping[str, Any],
    target_fingerprints: Sequence[str],
    set_main_volume_db: Any,
    get_main_volume_db: Any,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> Any:
    """Open the one session volume for a fresh v2 session (§5.5).

    Gates on ``plan.needs_recovery`` FIRST (not ``unresolved_volume_safety``
    alone — the W2 gate ruling: a crash-hydrated active plan needs draining but
    surfaces no unresolved payload), then derives the fixed volume via the SSOT
    and opens the plan. Refuses to open over a plan that needs recovery.
    """
    if plan.needs_recovery:
        raise CrossoverV2FlowError(
            "the session volume needs recovery; drain it before opening a session"
        )
    volume_db = derive_session_volume_db(
        safety_profile,
        target_fingerprints,
        declared_sensitivities=declared_sensitivities,
    )
    return await plan.open(volume_db, set_main_volume_db, get_main_volume_db)


async def abandon_measurement_volume(
    plan: Any, *, set_main_volume_db: Any, get_main_volume_db: Any,
) -> Any:
    """Session-death observation hook — drain the restore-once path (§5.5).

    The flow wires the relay session's death (TTL expiry / failure / explicit
    stop) to this so a walked-away user can never leave the speaker pinned at
    measurement volume. Delegates to the plan's ``abandon`` (the same
    fail-closed latch trio ``close`` uses).
    """
    return await plan.abandon(set_main_volume_db, get_main_volume_db)


__all__ = [
    "CrossoverV2Conductor",
    "CrossoverV2FlowError",
    "bind_program_playback_seams",
    "build_v2_capture_plan",
    "build_v2_session_spec",
    "build_v2_verify_capture_plan",
    "build_v2_verify_session_spec",
    "derive_session_volume_db",
    "open_measurement_volume",
    "abandon_measurement_volume",
    "V2ConductorSnapshot",
    "V2FlowSeams",
    "ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED",
    "ATTEMPT_REASON_NO_FLOOR",
    "attempt_history_from_state",
    "attempt_record_from_verify",
    "V2PlanShape",
    "TIER_FULL",
    "TIER_EXPRESS",
    "TIERS",
    "DEFAULT_TIER",
    "EXPRESS_CLOUD_VERIFY_POSITIONS",
    "express_cloud_measure_positions",
    "normalize_tier",
    "resolve_plan_shape",
    "tier_display_info",
    "capture_progress_label",
    "REVERIFY_NO_REWALK_HEADLINE",
    "PhaseVerdict",
    "ReasonSpec",
    "REASON_REGISTRY",
    "TRANSIENT_AUTO_RETRY_CODES",
    "PHASE_CHECK",
    "PHASE_MEASURE",
    "PHASE_APPLYING",
    "PHASE_VERIFY",
    "PHASE_DONE",
    # Control-page phases this module never evaluates itself, but re-exports
    # for ``crossover_envelope_v2`` and the web host, which both resolve a
    # persisted state to one of them. Listed here since #2291 Phase 4 moved the
    # vocabulary to ``crossover_v2.journey``: naming them is what says the
    # pass-through is deliberate rather than a stray import.
    "PHASE_REVIEW",
    "PHASE_CLOSING",
    "PHASE_LATERAL",
    "LATERAL_POSE_PROMPTS",
    "LATERAL_EVIDENCE_BAND_HZ",
    "LATERAL_EVIDENCE_POINTS_PER_OCTAVE",
    "LateralPose",
    "LateralPoseCurve",
    "lateral_evidence_grid_hz",
    "lateral_pose_curve",
    "STAGE1_INCLUDES_LATERAL",
    "PHASE_ENTRY_BASELINE",
    "REFERENCE_MARK_DESIGN_AXIS",
    "STAGE1_INCLUDES_ENTRY_BASELINE",
    "CAPTURE_PHASES",
    "CAPTURE_PLAN_TARGET",
    "CAPTURE_PLAN_MAX_ATTEMPTS",
    "V2_FIRST_BEGIN_TIMEOUT_S",
    "ALIGNMENT_CONFIDENCE_TRUST_FLOOR",
    "MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB",
    "SWEEP_SCHEDULE_RESIDUAL_CEILING_MS",
    "SWEEP_LOCATE_CONFIDENCE_FLOOR",
    "VERIFY_PILOT_TRANSFER_STEP_CEILING_DB",
    "alignment_to_candidate_fields",
    "back_off_gain",
    "TEMPLATE_SILENT_AUTO_RETRY",
    "TEMPLATE_FIX_AND_RETRY",
    "TEMPLATE_HARD_STOP",
    "TEMPLATE_SESSION_RESTART",
    "TEMPLATE_VERIFY_FAIL",
    "TEMPLATE_VOLUME_RECOVERY",
    "REASON_AGC_BEHAVIORAL_FAIL",
    "REASON_NOISY_ROOM_LINEARITY",
    "REASON_PILOT_LEVEL_COLLAPSE",
    "REASON_SNR_FLOOR",
    "REASON_CHANNEL_MAP_MISMATCH",
    "REASON_CLIPPED",
    "REASON_DRIFT_BASELINES_DISAGREE",
    "REASON_DELAY_EXCEEDS_SEARCH_WINDOW",
    "REASON_LOCATE_FAILED",
    "REASON_RELAY_TIMEOUT",
    "REASON_VOLUME_UNRESOLVED",
    "REASON_PROGRAM_UNPLAYABLE",
    "REASON_PROGRAM_PROFILE_NOT_CONFIRMED",
    "REASON_PROGRAM_PROFILE_MISSING",
    "REASON_PROGRAM_PROFILE_INCOMPLETE",
    "REASON_INTERNAL_ERROR",
    "REASON_VERIFY_OUT_OF_TOLERANCE",
    "REASON_VERIFY_CROSSOVER_REGION",
    "REASON_VERIFY_INCONCLUSIVE",
    "REASON_VERIFY_LEVEL_SHIFT",
    "verify_absolute_tolerance_db",
    "REASON_LOW_ALIGNMENT_CONFIDENCE",
    "REASON_APPLY_FAILED",
    "REASON_USER_STOPPED",
    "REASON_REVIEW_HOLD_TIMEOUT",
    "REASON_DRIVER_LEVELS_DISAGREE",
    "REASON_CORRECTION_NOT_AN_IMPROVEMENT",
    "LINEARIZATION_TRIM_SANITY_MARGIN_DB",
    "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
    "spec_report_for_predicted_sum",
    # The inconclusive-verdict copy, exported because it has TWO screens and
    # therefore cannot live inside either one (issue #1974).
    "verify_inconclusive_cause",
    "verify_inconclusive_message",
    # The copy selector and the second evidence-keyed sentence (#2085). Same
    # reason as the pair above, one surface further: ``locate_failed`` is
    # narrated by the relay verdict, the budget refusal, AND the envelope, so
    # the sentence cannot live in any of them.
    "locate_failed_message",
    "reason_message",
]
