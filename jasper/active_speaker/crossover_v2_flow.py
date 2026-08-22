# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover measurement session — state, seams, and the host's adapter.

**What this module is (#2291 Phase 5c-iv).** It owns ONE object,
:class:`CrossoverV2Session`, which holds a measurement session's mutable state,
the injected side-effect seams, the locks, and the irreversible acts (publish,
apply, commit) — and adapts all of it to the one caller that drives it,
:mod:`jasper.web.correction_crossover_v2`. **The decisions are not here.** Every
verdict rule, admission policy, prior, program composition, fit, sweep, spatial
close, and grade lives in :mod:`jasper.active_speaker.crossover_v2` — one module
per organ, each pure and separately testable. This module reads its session
state, calls those organs, and records what came back.

That split is the whole point of the Phase-5 migration, and it has a direction:
**this module imports the package; the package never imports this module** (or
the web host). ``test_no_domain_module_imports_the_host_or_the_legacy_flow``
holds that line. When a decision starts being made here, it belongs in an organ;
when session state or a seam starts being read there, it belongs here.

The predecessor class ``CrossoverV2Conductor`` was deleted in Phase 5c-iv. It
was a conductor in the sense of *making* the decisions; those left one at a
time over Phase 5, and what remained was a session owner, so it is named one.

``docs/crossover-measurement-productization-design.md`` §5 replaces the legacy
per-driver distributed transaction with this shape: the Pi compiles one
excitation program per phase, plays it as one continuous stream, and analyzes
``(program, capture) → analysis`` as a pure function. The session owns the
phase state machine that drives the relay session. At the shipped defaults a
FULL-tier commission is 8 captures (3 in stage 1, then 5) and an express one
is 4 (the same 3, then 1, ``TIER_EXPRESS``) — the tiers differ in stage 2
only. :func:`tier_display_info` derives both from the plans themselves and is
what the household-facing chooser reads; do not restate the numbers where a
plan change cannot reach them. The spatial cloud replaced the original three:

    CHECK → gain solve → MEASURE → the entry baseline
      → fit + candidate → [the household reviews, then POSTs the apply]
      → VERIFY → the post-apply position group → done

A 6-pose lateral walk sat between MEASURE and the entry baseline from R17 until
it was paused on 2026-08-18 and retired with the corner hunt it fed.  An
operator's staged angle walk still runs those poses as evidence for the forward
model; no stage-1 plan builds one.

**Owner decision (2026-07-27): the fit is the last thing before the apply.**
The candidate used to be built the moment MEASURE was accepted, which put it
eight captures BEFORE the pre-apply cloud whose honesty verdict it is supposed
to consume — so the two optional cloud terms in ``compose_envelope`` had no
reachable production caller. Building it at the group close instead lets the
fit correct the envelope around the interference the cloud identified and
refuse to fill it (flat-linearization plan, interpretation call (A)). MEASURE
keeps every trust gate it owned: they read the analysis, not the candidate, so
a session doomed at sweep two still fails at sweep two rather than after a nine
-position walk. A session with no pre-apply group (the shape this class
defaults to — and, since the lateral pause, the shipped stage 1 as well) has
nothing to wait for and still builds at
MEASURE, with the same accept, the same payload keys and the same apply timing
it had before the move — its ``candidate.json`` does gain an always-empty
``exclusion_evidence`` key, which leaves the fingerprint unchanged.
See :meth:`CrossoverV2Session._measure_verdict`.

**Owner ruling (2026-07-20), SUPERSEDED — kept for archaeology, not as
behaviour.** It ruled out a human mid-flow Apply gate and had the session
apply a trusted candidate itself. Two-stage T3 (commit ``61ba33ff1``,
#1806 / #1906) replaced that: the apply left the session entirely and is now
the household's explicit POST from the review screen, so nothing here applies
anything. Read that commit for what replaced it rather than this paragraph.
What did NOT change is :data:`ALIGNMENT_CONFIDENCE_TRUST_FLOOR`, still a hard
gate on the candidate rather than a review-screen nudge.

It is deliberately I/O-free: every side effect (playback, analysis, evidence
publish, apply-gate observation) crosses an INJECTED seam
(:class:`V2FlowSeams`), exactly as :func:`jasper.active_speaker.program_playback.play_program`
and :class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` inject
their DSP / volume seams. That keeps the whole state walk fixture-testable with
fake seams, and lets Wave 6 bind the real CamillaController-backed playback, the
``analyze_program_capture`` call, the verified-WAV source, and the
``commissioning_service`` publish/apply chain without touching this logic.

The session exposes the three ``run_capture_plan`` callbacks
(:meth:`authorize_begin`, :meth:`on_armed`, :meth:`consume_capture`) plus the
lifecycle hooks the host needs (:meth:`note_apply_complete`,
:meth:`snapshot`/:meth:`hydrate` for phase persistence + session binding). One
journey spans TWO relay sessions since the two-stage split (work order D1/D2,
issue #1806), each a heterogeneous ``CapturePlan``: **stage 1** is check /
measure / #2291's entry baseline (3 entries at either tier — the pre-apply
position group is off, and so is the lateral walk that ran between them until
the 2026-08-18 pause), and **stage 2** is verify / the post-apply position
group (5 at Full, 1 on express, which omits the group entirely). See
"position-group choreography" below. **Nothing is applied inside a session** —
stage 1 ends on the household's explicit set-completion signal, which closes
the group and publishes a candidate they then review and choose to apply on
jts.local. VERIFY's soft hold behind :class:`CaptureBeginDeferred` is retained
machinery that no shipped session reaches (D10): stage 1 has no VERIFY index
and stage 2 is constructed already-applied.

**Failure taxonomy (§5.10).** Terminal verdicts are internal reason codes, not
screens: :data:`REASON_REGISTRY` maps each code to one of the four screen
templates, its owning phase, and its retry budget. The session records the
code + accepted verdict its organs decided; the envelope
(:mod:`jasper.active_speaker.crossover_envelope_v2`)
renders the template. A woofer-repeat level disagreement REUSES
``drift_baselines_disagree`` — never a new user-facing code (§5.2).
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, replace
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
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        AlignmentPrescription,
    )
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        BlendPrescription,
    )
    from jasper.active_speaker.crossover_v2.coordinator import (
        RoundPorts,
        SeriesPosition,
    )
    from jasper.active_speaker.crossover_v2.driver_prescription import (
        DriverPrescription,
    )
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        TopologyPrescription,
    )
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
    VERDICT_FRAME_MISMATCH,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_SAFETY_ONLY,
    DeltaProbeMap,
    classify_delta_probe,
    spatial_cost_from_group_spreads,
)
from jasper.active_speaker.branch_chain import CrossoverSection
from jasper.active_speaker.camilla_yaml import role_polarity
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.crossover_v2 import accountability as _accountability
from jasper.active_speaker.crossover_v2 import admission as _admission
from jasper.active_speaker.crossover_v2 import attempt_grading as _grading
from jasper.active_speaker.crossover_v2 import candidates as _candidates
from jasper.active_speaker.crossover_v2 import capture_dispatch as _dispatch
from jasper.active_speaker.crossover_v2 import commanded as _commanded
from jasper.active_speaker.crossover_v2 import planning as _planning
from jasper.active_speaker.crossover_v2 import priors as _priors
from jasper.active_speaker.crossover_v2 import programs as _programs
from jasper.active_speaker.crossover_v2 import spatial as _spatial
from jasper.active_speaker.crossover_v2.contracts import (
    ENTRY_GRAPH_FINGERPRINT_UNKNOWN as _ENTRY_GRAPH_FINGERPRINT_UNKNOWN,
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
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
    JournalRecord,
    LinearizationPlan,
    driver_response_by_role as _driver_response_by_role,
    measure_validity_floor_hz as _measure_validity_floor_hz,
    # A live read, not a door: ``_plan_linearization`` passes it into the
    # organ as a port so THIS name stays the one production resolves.
    plan_linearization,
)
from jasper.active_speaker.crossover_v2.journey import (
    CAPTURE_PHASES,
    GROUP_PHASES,
    LATERAL_CONSUMER_FC_SELECTOR,
    LATERAL_CONSUMER_FORWARD_MODEL,
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
    validated_lateral_consumer,
)
from jasper.active_speaker.linearization_fit import worst_headroom_cost_db
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.gating import TRUSTED_FLOOR_MULTIPLIER
from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
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
    CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB,
    CHANNEL_MAP_MIN_ISOLATION_DB,
    INTEGRITY_CHECK_SWEEP_HEARD,
    AppliedAlignment,
    CaptureIntegrity,
    GainPlan,
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
    polarity_label,
)
from jasper.capture_relay.session import CaptureBeginDeferred, CaptureBeginRefused
from jasper.env_load import bounded_env_float
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

# The absolute VERIFY tracking error used by both the live attempts loop and
# the offline repeat-floor replay. Lower is better: zero is the model's
# prediction of perfect realization, while the analyzer's value is what the
# applied speaker actually realized.
ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED = "max_db_notch_excluded"

ATTEMPT_INTEGRITY_UNAVAILABLE = "capture_integrity_unavailable"

# Capture-plan index → phase. APPLYING is a control-page phase (no capture)
# that sits between MEASURE-accepted and VERIFY-armed, so it has no index.
# This is the pre-cloud 3-entry layout, kept as the fallback for a session
# constructed with no explicit ``index_phase_map``; the shipped session builds
# its map through ``build_v2_cloud_index_phase_map``.
_INDEX_PHASE = {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY}
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
# over. The arithmetic, spelled out for the WALK-ARMED case — which is the one
# that binds, and the reason this stays 11 through the lateral pause:
#
#     cloud_plan_max_attempts(N, M=6)                  = 1 + N + 6 + 2 + 5
#     + len(LATERAL_POSE_PROMPTS)                      = 6
#     + the entry baseline                             = 1
#     ------------------------------------------------------------------
#     N=12 -> 33, N=11 -> 32 = MAX_CAPTURE_PLAN_ATTEMPTS
#
# **There is no slack, and the table above is why.** No stage-1 plan builds that
# walk any more, but ``relay_plan_attempts_required`` counts its six poses
# unconditionally — an operator's staged angle walk adds them to any session,
# through this same index space — so the walk-armed row IS the binding one: at
# N=11, M=6 it lands on 32, which is ``MAX_CAPTURE_PLAN_ATTEMPTS`` exactly. At
# the shipped M=5 it is 31, one index under. Raising N by a single step spends
# that last index and puts a staged walk at the ceiling.
#
# Two different numbers get quoted at this ceiling and they answer different
# questions; do not read one as the other. ``assert_cloud_plan_fits_relay_capacity``
# sums ``cloud_capture_target`` (positions, not attempts) plus the poses, the
# entry baseline and the geometry retries — 26 at N=11 — while the doctor asks
# ``relay_plan_attempts_required`` for worst-case ATTEMPTS, which is the 31
# above and the figure the relay Worker's own ceiling has to carry.
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
#
# It sits AT :data:`MIN_CLOUD_VERIFY_POSITIONS` (owner ruling, 2026-08-18), so
# the shipped walk is exactly the shape the floor already validates — the anchor
# plus four prompted lateral moves, both wide offsets included — and it is the
# walk the remote tier has always taken
# (:func:`remote_cloud_verify_positions` derives the same 5). What it gives up
# is the fifth pose, ``12 cm ABOVE``: the journey's only above/below-mark-height
# sample, since the lateral walk excludes ``POSITION_ROLE_XOVR`` by
# construction. No claim reads that axis on its own — the group is combined into
# ONE curve and graded as a spatial average, and the tier's promise ("re-check
# the result at several spots around the mark") is about spread, not height.
DEFAULT_CLOUD_VERIFY_POSITIONS = 5
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

# Retake headroom a cloud plan carries ABOVE its entry count and its geometry
# retries. Deliberately the same ABSOLUTE spare the shipped 3-entry flow has
# always had (``CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET`` = 5), not the
# same RATIO: `capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`' own sizing note
# says longer sets getting proportionally fewer retakes each "is the intended
# direction — a 21-position session that needs 11 retakes has a problem retries
# will not fix."
CLOUD_RETAKE_ALLOWANCE = CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET

# The bounded-retry ruling (owner, 2026-08-03, issue #2086) and the two
# initiators its pooled bound is attributed to. Re-exported from
# :mod:`jasper.active_speaker.crossover_v2.admission`, which owns the ledger
# they belong to and states why each number is what it is (#2291 Phase 5a-vi).
MAX_EXTRA_ATTEMPTS_PER_POSITION = _admission.MAX_EXTRA_ATTEMPTS_PER_POSITION
ATTEMPT_INITIATOR_HOUSEHOLD = _admission.ATTEMPT_INITIATOR_HOUSEHOLD
ATTEMPT_INITIATOR_SPEAKER = _admission.ATTEMPT_INITIATOR_SPEAKER

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
# verify, which was running usefully at 4 positions of the 6 that tier declared
# then. Between this floor
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
    #: Which side of the design axis a LATERAL row sits on: ``-1`` LEFT,
    #: ``+1`` RIGHT, ``0`` for an at-mark or vertical row. Machine-readable
    #: because :func:`position_angle_deg` has to SIGN the bearing, and the only
    #: other statement of the side is the word "LEFT"/"RIGHT" inside
    #: ``headline`` — reading a bearing out of rendered copy is exactly the
    #: drift this table's derived ``wide`` property exists to avoid. Set by
    #: :func:`_pose` from the row's own ``side`` bearing, never by hand.
    lateral_sign: int = 0

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


# The sign convention for a horizontal bearing, in ONE place: negative is LEFT
# of the design axis, positive is RIGHT, as seen from the microphone looking at
# the speaker — the same viewpoint the prompt copy is written from, so a row
# that SAYS "LEFT" cannot be signed RIGHT.
_LATERAL_SIGNS = {"LEFT": -1, "RIGHT": 1}


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
        # Derived from the row's OWN bearing word, so the sign and the sentence
        # cannot disagree. A vertical row supplies ``updown`` instead and keeps
        # the neutral 0 — it has no horizontal bearing to sign.
        lateral_sign=_LATERAL_SIGNS.get(str(bearing.get("side") or ""), 0),
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

# --- remote tier: the same walk, stated as ANGLES (external positioner) ------ #
#
# The mark distance the CHECK screen asks for ("about 1 m in front of the
# speaker"). It is the reference length that turns this flow's lateral OFFSETS
# into the BEARINGS a positioner can act on, so it lives beside them rather than
# only inside that sentence.
MARK_DISTANCE_M = 1.0


def position_angle_deg(prompt: CloudPositionPrompt) -> int:
    """The signed horizontal bearing of one lateral pose, in WHOLE degrees.

    DERIVED from ``offset_cm`` and :data:`MARK_DISTANCE_M`, never tabulated: the
    remote tier walks the very same poses the hand-walked tiers do, so an edit
    to the offsets must move the angles with them instead of leaving a second,
    silently-stale table of numbers.

    **The convention, stated exactly, because a positioner acts on it.** The
    angle is the bearing from the speaker to the pose's stated LATERAL OFFSET,
    measured in the mark's own plane — ``atan(offset / mark distance)`` — with
    :data:`_LATERAL_SIGNS`' sign, so ``-7`` is 7° to the LEFT of the design
    axis. At the shipped offsets that is ±7° (12 cm) and ±22° (40 cm).

    Whole degrees, because that is the resolution the number is honest at: the
    offsets it comes from are tape-measure distances to a mark placed "about"
    1 m out, and a tenth of a degree here would claim a precision the placement
    never had. A positioner swinging at a constant radius is equidistant BY
    CONSTRUCTION — the property ``_WIDE_LATERAL_DETAIL`` asks a walking human to
    approximate — so it lands on the wide poses' intended arc rather than on the
    chord a hand-walked session settles for.

    Refuses a vertical row rather than returning ``0``: :data:`POSITION_ROLE_XOVR`
    has no horizontal bearing at all, and a silent zero would aim a positioner at
    the mark while the plan believed it had sampled the crossover axis.
    """
    if prompt.role == POSITION_ROLE_XOVR:
        raise CrossoverV2FlowError(
            "a vertical position has no horizontal bearing: an external "
            "positioner cannot raise or lower the microphone, so an "
            f"externally positioned walk must contain no {POSITION_ROLE_XOVR} "
            "pose (see remote_cloud_verify_positions)"
        )
    if float(prompt.offset_cm) != 0.0 and prompt.lateral_sign == 0:
        # An off-axis pose that never declared WHICH side it is on. Without this
        # the sign multiplies out and the pose reads back as 0° — "already on
        # the design axis" — so a driver would be told to stay put for a capture
        # the plan believes is off-axis, and the evidence would record an offset
        # the microphone never had. Every table row gets its sign from
        # :func:`_pose`; a row that bypassed it is a construction bug, and
        # silence here is exactly the mis-attribution the gate exists to stop.
        raise CrossoverV2FlowError(
            f"a lateral position {float(prompt.offset_cm):g} cm off the mark "
            "declares no side, so it has no signed bearing — build it through "
            "_pose (or set lateral_sign) rather than letting it read as 0°"
        )
    radians = math.atan2(float(prompt.offset_cm) / 100.0, MARK_DISTANCE_M)
    return int(round(prompt.lateral_sign * math.degrees(radians)))


def remote_position_prompt(prompt: CloudPositionPrompt) -> CloudPositionPrompt:
    """One hand-walked pose, restated as the ANGLE a positioner turns to.

    Same pose, same ``offset_cm``, same :data:`POSITION_ROLES` role — only the
    copy changes, so everything downstream that reads a position's role or
    distance (the wide-offset rule, the evidence sidecar, attribution) keeps
    reading exactly what Full's walk records. That is the whole point of
    deriving this instead of writing a parallel table: the remote tier is a
    different OPERATOR, not a different measurement.
    """
    degrees_ = position_angle_deg(prompt)
    if degrees_ == 0:
        headline = "Leave the microphone on the design axis (0°)."
        detail = f"On the mark, {MARK_DISTANCE_M:g} m out, pointed at the speaker."
    else:
        side = "LEFT" if degrees_ < 0 else "RIGHT"
        headline = (
            f"Turn the microphone to {degrees_:+d}° "
            f"({abs(degrees_)}° {side} of the design axis)."
        )
        detail = (
            f"Keep it {MARK_DISTANCE_M:g} m from the speaker and pointed at it."
        )
    return replace(prompt, headline=headline, detail=detail)

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


def walk_shape_for(
    *, cloud_positions: int, lateral: bool,
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> str:
    """The orientation sentence for a stage-1 session's ACTUAL groups (R16).

    One sentence for whichever groups run, quoting the FURTHEST reach of any of
    them — a household needs to know how much room the whole session wants, and
    two sentences quoting two ceilings would just make them pick one.

    ``lateral_prompts`` is the walk this session actually takes; ``None`` is the
    ratified table. A taken angle walk can reach far past that table's 40 cm.
    """
    table = LATERAL_POSE_PROMPTS if lateral_prompts is None else lateral_prompts
    reach = max(
        cloud_walk_reach_cm(cloud_positions) if cloud_positions else 0.0,
        cloud_walk_reach_cm_of(table) if lateral else 0.0,
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

# The named plan SHAPES a session can be opened with. A tier is not a
# loosened floor — it is a distinct, validated (N, M) pair with its own rules,
# so ``MIN_CLOUD_MEASURE_POSITIONS`` (the FULL tier's validated floor) never
# moves to accommodate express.
TIER_FULL = "full"
TIER_EXPRESS = "express"
# The EXTERNALLY DRIVEN tier (experimental). Full's stage-1 shape, walked by a
# mic positioner an external driver moves over HTTP instead of by a household
# moving a stand by hand — so it is the one tier whose entries auto-begin
# (:data:`AUTO_ADVANCE_COUNTDOWN`) behind a per-entry POSITION GATE, and the one
# whose positions are stated as ANGLES rather than as tape-measure offsets.
#
# **Not a household choice.** ``_tier_choice_actions`` offers exactly Full and
# Express; remote is reached only by POSTing ``{"tier": "remote"}`` to
# ``/correction/crossover/v2/session``, because consenting to it means owning a
# positioner the chooser cannot see. It is in :data:`TIERS` so
# ``normalize_tier`` admits that POST — never so a chooser can render it.
TIER_REMOTE = "remote"
TIERS = (TIER_FULL, TIER_EXPRESS, TIER_REMOTE)
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


def _vertical_prompt_indexes() -> list[int]:
    """Where the vertical poses sit in :data:`CLOUD_POSITION_PROMPTS`."""
    return [
        i for i, prompt in enumerate(CLOUD_POSITION_PROMPTS)
        if prompt.role == POSITION_ROLE_XOVR
    ]


def remote_cloud_measure_positions() -> int:
    """Remote's PRE-APPLY group size — Full's N, and the trip-wire that guards it.

    Remote takes Full's ``N`` because its stage 1 IS Full's stage 1. That is
    safe because :data:`STAGE1_INCLUDES_CLOUD_MEASURE` is ``False``, so the
    ``[:N - 1]`` prefix of :data:`CLOUD_POSITION_PROMPTS` — which at ``N = 9``
    DOES contain vertical rows — is never walked. That one flag is the whole
    load-bearing fact; the lateral walk is beside the point here, being lateral
    by construction whether it is armed or (as since 2026-08-18) paused.

    Flipping that flag back on would ask an external positioner for a pose it
    cannot reach. Today that surfaces as :func:`position_angle_deg` raising
    while the plan is built, which is loud but names the symptom rather than the
    cause, so this states the assumption where it can be read: the guard below
    refuses at the point the two facts stop agreeing, exactly as
    :func:`remote_cloud_verify_positions`' guard does for the verify walk.
    """
    positions = DEFAULT_CLOUD_MEASURE_POSITIONS
    verticals = _vertical_prompt_indexes()
    if STAGE1_INCLUDES_CLOUD_MEASURE and verticals and verticals[0] < positions - 1:
        raise CrossoverV2FlowError(
            "the remote tier cannot walk a pre-apply cloud of "
            f"{positions} positions: prompt {verticals[0]} is a "
            f"{POSITION_ROLE_XOVR} pose and an external positioner cannot "
            "raise or lower the microphone. Give remote its own N (the "
            "vertical-free prefix, as remote_cloud_verify_positions derives) "
            "before turning STAGE1_INCLUDES_CLOUD_MEASURE on."
        )
    return positions


def remote_cloud_verify_positions() -> int:
    """Remote's post-apply group size — DERIVED as "Full's walk, minus vertical".

    An external positioner swings the microphone around the speaker on ONE
    axis; it cannot raise or lower the capsule. So remote walks the longest
    prefix of :data:`CLOUD_POSITION_PROMPTS` that asks for no
    :data:`POSITION_ROLE_XOVR` move — one past the last purely-lateral prompt —
    and the group it cannot sample is disclosed rather than silently missing
    (:data:`REMOTE_VERTICAL_DISCLOSURE`).

    Derived rather than written down for the same reason
    :func:`express_cloud_measure_positions` is: reordering the table must move
    this number with it instead of silently shipping a walk whose prefix now
    contains a vertical move the positioner cannot make.
    """
    verticals = _vertical_prompt_indexes()
    # A group of size ``g`` walks prompts ``[:g - 1]``, so the largest
    # vertical-free group is one past the first vertical's index.
    positions = (verticals[0] + 1) if verticals else DEFAULT_CLOUD_VERIFY_POSITIONS
    if positions < MIN_CLOUD_VERIFY_POSITIONS:
        raise CrossoverV2FlowError(
            "the remote tier's vertical-free verify walk is "
            f"{positions} positions, below the validated floor of "
            f"{MIN_CLOUD_VERIFY_POSITIONS} — CLOUD_POSITION_PROMPTS must keep "
            "both wide lateral moves ahead of its first vertical one"
        )
    return min(positions, DEFAULT_CLOUD_VERIFY_POSITIONS)


# What a remote session states about the axis its positioner cannot reach.
# ONE sentence, disclosed once per session (never a block): a consumer that
# reads this group's roles finds no ``xovr`` member — the honest reading is
# "unsampled", not "flat".
#
# It used to add "the vertical spot Full measures was not sampled this time";
# that clause went on 2026-08-18 when :data:`DEFAULT_CLOUD_VERIFY_POSITIONS`
# came down to the floor and Full's walk became the same vertical-free prefix.
# The rest is unchanged and still owed: it states a fact about THIS walk.
REMOTE_VERTICAL_DISCLOSURE = (
    "Measured on the horizontal axis only — a remote positioner cannot raise "
    "or lower the microphone, so no vertical spot was sampled."
)


@dataclass(frozen=True)
class V2PlanShape:
    """The RESOLVED (tier, N, M) triple — one value, threaded everywhere.

    Before this existed, ``prepare_v2_session`` called
    :func:`build_v2_session_spec` and :func:`build_v2_cloud_index_phase_map`
    with independent defaults and passed counts to neither: two functions that
    MUST agree, agreeing only by luck. Resolving once and threading the result
    closes that desync hazard by construction — the plan the phone is handed
    and the index→phase map the session walks are derived from the same
    object or they are not built at all.
    """

    tier: str
    cloud_measure_positions: int
    cloud_verify_positions: int
    #: Whether a PERSON releases every begin — the second of the two facts the
    #: old single ``externally_positioned`` boolean carried at once. Set by the
    #: session host for a HAND-WALKED round running on the wired capture
    #: source, which has no capture page to pace it: nothing there taps, so
    #: without a hold the walk fires every capture back to back while the
    #: household is still walking to the next spot.
    #:
    #: It is not a tier. The tier still decides the (N, M) shape and whether a
    #: MACHINE advances the walk (:attr:`externally_positioned`); this says who
    #: releases each hold, which is the only thing that differs between the arm
    #: and a person holding a tape at the same bearings.
    hand_released_positions: bool = False

    def __post_init__(self) -> None:
        if self.hand_released_positions and self.externally_positioned:
            # Loud rather than idempotent: the arm's own driver releases its
            # holds, so a shape claiming BOTH movers is a caller that has not
            # decided which one is on the floor.
            raise CrossoverV2FlowError(
                f"tier {self.tier!r} is positioned by an external driver, so "
                "its holds cannot also be hand-released"
            )

    @property
    def measure_capture_target(self) -> int:
        """The cloud-INCLUSIVE shape target (``1 + N``) — CHECK plus the
        pre-apply cloud (anchor plus ``N − 1`` prompted positions), 10 at
        Full's defaults, 6 for express. NOT what stage 1 runs: the
        ``STAGE1_INCLUDES_*`` flags decide that, and the real count is
        :func:`_stage1_capture_target` — two readers have been misled by the
        older wording (#2098, and the remote session-open journal). Stage 1
        applies nothing (D1), so it carries no post-apply entry at all."""
        return 1 + self.cloud_measure_positions

    @property
    def verify_capture_target(self) -> int:
        """Accepted captures STAGE 2 runs (``M``) — VERIFY's anchor plus
        ``M − 1`` prompted post-apply positions. 5 at Full
        (:data:`DEFAULT_CLOUD_VERIFY_POSITIONS`), 1 for express (whose whole
        post-apply check is the anchor at the mark).
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

    @property
    def externally_positioned(self) -> bool:
        """Whether an EXTERNAL DRIVER moves the microphone between captures — see
        :func:`tier_is_externally_positioned`, which this delegates to so the
        live conductor (which holds a tier string, not a resolved shape) and the
        plan builders answer the question from ONE definition.

        **This is the ADVANCE axis, and only that.** It decides one behaviour:
        every entry auto-begins behind the cancelable countdown
        (:func:`_entry_advance`), because there is no hand to tap. It also
        implies :attr:`positions_gated` — a countdown alone would fire into an
        arm still in motion — but the converse does not hold, and that is the
        whole reason the two are separate properties: a person can walk the same
        bearings and release each hold by hand, which needs the gate and must
        NOT get the countdown.

        The combination this refuses is auto-advance WITHOUT a gate: that is
        the bug the countdown would have replaced the tap with, and it is
        unreachable by construction because :attr:`positions_gated` reads this
        property as one of its own disjuncts.
        """
        return tier_is_externally_positioned(self.tier)

    @property
    def positions_gated(self) -> bool:
        """Whether poses are stated as BEARINGS and every begin is HELD until
        something reports the microphone in place.

        **The POSE-STATEMENT axis** — the second of the two independent facts
        the old single boolean carried. It decides, together:

        * the prompt copy restates each pose as its angle
          (:func:`_positioned_prompt`), because whoever moves the microphone is
          working in degrees rather than tape-measure centimetres; and
        * every entry declares that angle in machine terms
          (:data:`POSITION_DEG_KEY` / :data:`POSITION_ROLE_KEY`,
          :func:`_entry_policy`), which is what the session host's position gate
          reads to name the target it is waiting for.

        Those two are one statement in two vocabularies — the sentence a person
        reads and the number the gate acts on — so they share one property
        rather than drifting as two.

        True for the arm (:attr:`externally_positioned`), whose driver POSTs the
        release, and for a hand-released round
        (:attr:`hand_released_positions`), where a person does. What separates
        those two is :attr:`externally_positioned` alone: the arm also gets the
        countdown, the person keeps the tap.
        """
        return self.externally_positioned or self.hand_released_positions


def tier_is_externally_positioned(tier: Any) -> bool:
    """Whether ``tier`` names a tier an EXTERNAL DRIVER positions.

    Deliberately LENIENT where :func:`normalize_tier` is strict: this is asked
    by surfaces that hold whatever tier string a durable state file carried —
    including ``""`` (a session written before tiers existed) and words from a
    later build — and none of those may take down a group close. An unknown tier
    is simply "not externally positioned", which is the safe answer: it keeps
    the hand-walked behaviour every such session already had.
    """
    return str(tier or "").strip().lower() == TIER_REMOTE


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


# The tiers that are ONE named (N, M) pair rather than a configurable range.
# Values are callables so each pair stays DERIVED at call time from the prompt
# table it mirrors — a table edit moves the shapes, it does not strand them.
#
# Remote takes FULL's pre-apply N (its stage 1 is Full's stage 1, walked by a
# positioner instead of by hand) and its own vertical-free M.
_FIXED_SHAPE_TIERS: dict[str, Callable[[], tuple[int, int]]] = {
    TIER_EXPRESS: lambda: (
        express_cloud_measure_positions(), EXPRESS_CLOUD_VERIFY_POSITIONS,
    ),
    TIER_REMOTE: lambda: (
        remote_cloud_measure_positions(), remote_cloud_verify_positions(),
    ),
}


def resolve_plan_shape(
    tier: Any = None,
    *,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> V2PlanShape:
    """Resolve (and validate) one plan shape from a tier and optional counts.

    Express and remote each admit EXACTLY one (N, M) pair
    (:data:`_FIXED_SHAPE_TIERS`) — they are named shapes, not configurable
    ranges, so an explicit count that disagrees is a caller bug rather than a
    preference. Full keeps the shipped ranges
    (``MIN_CLOUD_MEASURE_POSITIONS..MAX_CLOUD_MEASURE_POSITIONS``,
    ``M >= MIN_CLOUD_VERIFY_POSITIONS``).
    """
    name = normalize_tier(tier)
    if name in _FIXED_SHAPE_TIERS:
        n, m = _FIXED_SHAPE_TIERS[name]()
        for label, wanted, got in (
            ("cloud_measure_positions", n, cloud_measure_positions),
            ("cloud_verify_positions", m, cloud_verify_positions),
        ):
            if got is not None and int(got) != wanted:
                raise CrossoverV2FlowError(
                    f"the {name} tier is a fixed shape: {label} must be "
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
# Corner admissibility (plan §4.2 / #1894 / #1675)
#
# #2291 Phase 5a-v(b) moved the declaration half — the rejection vocabulary and
# the admissibility predicate — into ``crossover_v2.fc_sweep``, because a pure
# organ cannot import this module (the dependency runs one way: flow →
# crossover_v2, never back). They are re-exported here under their historical
# names, exactly as the Phase 2 planner constants above are, so every existing
# importer keeps resolving to the single definition rather than growing a second
# copy.
#
# These ARE doors, and unlike Phase 2b's two they are doors somebody walks
# through: ``tests/test_crossover_v2_fc_candidates.py`` reaches the declaration
# half by these names. That is the difference Phase 2b's own note turns on — it
# removed the imports no caller used, not the ones that carry a caller.
#
# The ``X as X`` spelling is the explicit-re-export form, which is why no lint
# suppression appears here: a plain ``import X`` would read as dead, and a
# suppression marker would spend the repository's frozen budget
# (``test_lint_contracts.test_noqa_debt_does_not_grow``) on something that is
# not a suppression at all.
#
# **These doors are READ-ONLY.** Substituting one of them here — a
# ``monkeypatch.setattr(flow, "_fc_rejection", …)`` — rebinds this module's
# name and NOTHING else: production reaches the declaration half through
# ``_fc``/``crossover_v2.fc_sweep``, so the patch would be vacuous while
# looking applied. Patch the owning module instead.
# --------------------------------------------------------------------------- #

from jasper.active_speaker.crossover_v2.fc_sweep import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND as FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR as FC_REJECT_BELOW_DECLARED_FLOOR,
    _fc_rejection as _fc_rejection,
)

# Two more doors, on the same terms, opened by Phase 5a-v(c): the candidate
# build's request assembly moved to :mod:`crossover_v2.planning` and took this
# module's own last read of each with it, but callers walk through both —
# ``flow.sections_by_role`` at three test sites, and
# ``LINEARIZATION_MIN_PAIRED_OCCURRENCES`` imported FROM here by two suites.
# READ-ONLY for the reason above: production resolves them inside the owning
# modules.
from jasper.active_speaker.branch_chain import (
    sections_by_role as sections_by_role,
)
from jasper.active_speaker.crossover_v2.intervention import (
    LINEARIZATION_MIN_PAIRED_OCCURRENCES as LINEARIZATION_MIN_PAIRED_OCCURRENCES,
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

    R16's walk counts UNCONDITIONALLY, and that is the honest worst case rather
    than a leftover: no stage-1 plan arms it, but an operator's staged angle
    walk builds one on any session and its poses ride the same blob-index space.
    A term guarded on a stage-1 flag would have said 0 for exactly the shape
    that still runs six of them.

    #2291's entry baseline is one more stage-1 entry and counts here —
    flag-guarded in one place so the guard and the doctor cannot disagree about
    it. Before one producer the two added their stage-1 terms separately, and a
    build that changed one would under-report and pass a Pi whose Worker ceiling
    sat just under the real count: green in the diagnostic, refused mid-session.
    """
    return (
        cloud_plan_max_attempts(
            cloud_measure_positions=cloud_measure_positions,
            cloud_verify_positions=cloud_verify_positions,
        )
        + len(LATERAL_POSE_PROMPTS)
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

    # R16's walk and #2291's entry baseline are entries too — the walk
    # unconditionally, for the reason the shared producer above gives. Both
    # counted through that one producer's rules, so this and jasper-doctor can
    # never disagree about the number.
    entries = (
        cloud_capture_target(
            cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
            cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
        )
        + len(LATERAL_POSE_PROMPTS)
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


def stage1_plan_max_attempts(
    capture_target: int, *, include_cloud_measure: bool,
) -> int:
    """The admission budget a stage-1 plan of ``capture_target`` entries emits.

    THE producer of that number, with two readers: ``build_v2_capture_plan``
    sets ``CapturePlan.max_attempts`` from it, and ``session_lateral_walk`` asks
    it whether a composed walk still fits ``MAX_CAPTURE_PLAN_ATTEMPTS``. A copy
    of it is a gate that refuses plans the relay would have taken.

    Geometry retakes are that cloud group's lever, so they are budgeted only
    when one is planned. Derived from the entries a plan ACTUALLY emits, never
    from the shape's cloud-only arithmetic.
    """
    return (
        capture_target
        + (GEOMETRY_RETRY_POSITIONS if include_cloud_measure else 0)
        + CLOUD_RETAKE_ALLOWANCE
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


# R16's 6-pose lateral walk (plan §4.4) is NOT a stage-1 group. It ran from R17
# to feed the lateral robustness term of a corner selector, was paused on
# 2026-08-18 because that statistic ranked below its own noise — same-candidate repeat
# noise 3.54 dB against a 0.004-2.13 dB rank-1-to-rank-2 gap over 8 banked
# rounds, none of which it ever moved off the configured Fc — and was retired
# with the corner hunt it fed. What is NOT in doubt is the measurement: the
# poses are clean (inter-driver drift 0.6-1.9 dB against a 0.09-0.32 dB
# mark-return floor), which is why an operator's staged angle walk still runs
# them as evidence for the forward model. The pose table, the prompts, the
# per-pose screens and ladder, ``lateral_pose_curve`` and
# ``lateral_mark_return_drift_db`` all serve that walk; only the stage-1 arming
# is gone, so the two builders below still take ``include_lateral`` from
# whatever a caller asks for, exactly like ``STAGE1_INCLUDES_CLOUD_MEASURE``.
def _stage1_capture_target(shape: Any) -> int:
    """Stage 1's REAL capture count, not the cloud-inclusive shape target."""
    return len(build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
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
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> dict[int, str]:
    """Capture-plan index → session phase for a STAGE-1 (measure) session.

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
    phase than the session believes it is running.

    ``lateral_prompts`` is the walk's own table (L is its length); ``None`` is
    the ratified one.
    """
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    n = shape.cloud_measure_positions
    lateral_table = LATERAL_POSE_PROMPTS if lateral_prompts is None else lateral_prompts
    mapping = {1: PHASE_CHECK, 2: PHASE_MEASURE}
    nxt = 3
    if include_lateral:
        for offset in range(len(lateral_table)):
            mapping[nxt + offset] = PHASE_LATERAL
        nxt += len(lateral_table)
    if include_cloud_measure:
        for offset in range(n - 1):
            mapping[nxt + offset] = PHASE_CLOUD_MEASURE
        nxt += n - 1
    if include_entry_baseline:
        mapping[nxt] = PHASE_ENTRY_BASELINE
    return mapping


def announced_capture_indexes(index_phase: Mapping[int, str]) -> tuple[int, ...]:
    """The 1-based captures of this plan that play the courtesy prelude.

    The consent screen tells the household what it will hear, and since the
    prelude announces a SESSION rather than a capture
    (:func:`~jasper.active_speaker.crossover_v2.programs.courtesy_prelude_for_phase`)
    "what it will hear" is no longer the same sentence for every measurement.
    Stage 1 announces its FIRST (CHECK) and its LAST (the entry baseline, which
    plays stage 2's anchor object); stage 2's walk announces its first alone.

    Derived from the SAME ``index -> phase`` map the plan's own entries are
    built from, so the sentence and the schedule cannot describe different
    sessions — the reason that map exists at all. Reading the plan's
    ``kind_label``s instead would work only because those strings happen to
    spell the phases, which is a coincidence, not a contract.
    """
    return tuple(
        index for index, phase in sorted(index_phase.items())
        if courtesy_prelude_for_phase(phase)
    )


def build_v2_verify_index_phase_map(
    *,
    plan_shape: V2PlanShape | None = None,
) -> dict[int, str]:
    """Capture-plan index → session phase for a STAGE-2 (verify) session.

    ::

        1                    VERIFY             (design-axis anchor, at the mark)
        2 .. M               CLOUD_VERIFY       (M-1 prompted positions)

    ``plan_shape is None`` is the shipped 1-entry recovery re-verify —
    ``{1: PHASE_VERIFY}``, byte-identical to what ``prepare_v2_verify``
    hardcoded before the split. A shape supplies the tier's own post-apply walk
    (work order D2, owner-confirmed 2026-07-29): express is ``M = 1`` and so
    resolves to the same single-entry map; Full is the multi-position spatial
    walk whose combined curve the after-chart, the post-apply spec verdict, and
    the delta probe all read.
    """
    m = 1 if plan_shape is None else plan_shape.verify_capture_target
    mapping = {1: PHASE_VERIFY}
    for offset in range(m - 1):
        mapping[2 + offset] = PHASE_CLOUD_VERIFY
    return mapping


# --------------------------------------------------------------------------- #
# failure taxonomy (§5.10) — re-exported, no longer defined here
#
# #2291 Phase 5c-ii moved the whole household vocabulary — the reason codes, the
# remediation templates, the ``ReasonSpec``/``RetryableReasonCopy`` carriers, the
# ``REASON_REGISTRY`` that binds a code to its sentence and its retry budget, the
# copy selectors, and ``PhaseVerdict`` — into
# :mod:`jasper.active_speaker.crossover_v2.refusal_copy`, and the four library
# clusters below into the package siblings that already own their decisions.
#
# Why the vocabulary had to move, when 5a-vii ruled it did not: that ruling was
# about ORGANS, and an organ answers with a kind. Phase 5c dissolved
# ``CrossoverV2Conductor``, and the spine that survived it lands in the
# package — whose whole
# job is building ``PhaseVerdict``s, while
# ``test_no_domain_module_imports_the_host_or_the_legacy_flow`` forbids any
# module there importing this one. Spine-in-package forces
# vocabulary-in-package. Where the vocabulary PHILOSOPHICALLY belongs is a
# separate, still-open question (issue #2390) — the envelope is its largest
# consumer, and 5c-ii settled only the mechanical half.
#
# The ``X as X`` spelling is the explicit-re-export form, exactly as the Phase
# 5a-v(b) fc_sweep block below uses it: a plain ``import X`` would read as dead,
# and a suppression marker would spend the repository's frozen ``noqa`` budget
# (``test_lint_contracts.test_noqa_debt_does_not_grow``) on something that is not
# a suppression at all. Every name keeps its historical spelling, so this move
# changed no importer anywhere — 3 production files and 17 test files reach these
# names and not one of them was edited.
#
# **These doors are READ-ONLY for anything the package resolves itself.**
# Substituting one here rebinds THIS module's name and nothing else, so it binds
# only for readers that are themselves in this module. Every reader of these
# names is, which is why the existing substitutions still work. A reader that
# lives in the package resolves the owning module directly and would not see the
# patch: patch that module, or inject through the ports its caller takes.
# --------------------------------------------------------------------------- #

from jasper.active_speaker.crossover_v2.refusal_copy import (
    DELTA_PROBE_REASON_BY_VERDICT as DELTA_PROBE_REASON_BY_VERDICT,
    NON_RETRIABLE_CODES as NON_RETRIABLE_CODES,
    REASON_AGC_BEHAVIORAL_FAIL as REASON_AGC_BEHAVIORAL_FAIL,
    REASON_ANCHOR_AMBIGUOUS as REASON_ANCHOR_AMBIGUOUS,
    REASON_APPLY_FAILED as REASON_APPLY_FAILED,
    REASON_CHANNEL_MAP_MISMATCH as REASON_CHANNEL_MAP_MISMATCH,
    REASON_CLIPPED as REASON_CLIPPED,
    REASON_CLOUD_GEOMETRY_LOCKED as REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_CORRECTION_LEVEL_SHORTFALL as REASON_CORRECTION_LEVEL_SHORTFALL,
    REASON_CORRECTION_MEASURED_REGRESSION as REASON_CORRECTION_MEASURED_REGRESSION,
    REASON_CORRECTION_MODEL_ERROR as REASON_CORRECTION_MODEL_ERROR,
    REASON_CORRECTION_ROLLBACK_FAILED as REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_CORRECTION_SPATIALLY_COSTLY as REASON_CORRECTION_SPATIALLY_COSTLY,
    REASON_CORRECTION_UNPROVEN_BOOST as REASON_CORRECTION_UNPROVEN_BOOST,
    REASON_CORRECTION_UNSAFE_RESULT as REASON_CORRECTION_UNSAFE_RESULT,
    REASON_CORRECTION_UNVERIFIABLE_RESULT as REASON_CORRECTION_UNVERIFIABLE_RESULT,
    REASON_DELAY_EXCEEDS_SEARCH_WINDOW as REASON_DELAY_EXCEEDS_SEARCH_WINDOW,
    REASON_DRIFT_BASELINES_DISAGREE as REASON_DRIFT_BASELINES_DISAGREE,
    REASON_DRIVER_LEVELS_DISAGREE as REASON_DRIVER_LEVELS_DISAGREE,
    REASON_INTERNAL_ERROR as REASON_INTERNAL_ERROR,
    REASON_LOCATE_FAILED as REASON_LOCATE_FAILED,
    REASON_LOW_ALIGNMENT_CONFIDENCE as REASON_LOW_ALIGNMENT_CONFIDENCE,
    REASON_NOISY_ROOM_LINEARITY as REASON_NOISY_ROOM_LINEARITY,
    REASON_PILOT_LEVEL_COLLAPSE as REASON_PILOT_LEVEL_COLLAPSE,
    REASON_PROGRAM_PROFILE_INCOMPLETE as REASON_PROGRAM_PROFILE_INCOMPLETE,
    REASON_PROGRAM_PROFILE_MISSING as REASON_PROGRAM_PROFILE_MISSING,
    REASON_PROGRAM_PROFILE_NOT_CONFIRMED as REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
    REASON_PROGRAM_UNPLAYABLE as REASON_PROGRAM_UNPLAYABLE,
    REASON_PROTECTION_NOT_SEPARABLE as REASON_PROTECTION_NOT_SEPARABLE,
    REASON_GEOMETRY_RETAKE_UNREACHABLE as REASON_GEOMETRY_RETAKE_UNREACHABLE,
    REASON_POSITION_HOLD_EXPIRED as REASON_POSITION_HOLD_EXPIRED,
    REASON_POSITION_TARGET_MISSING as REASON_POSITION_TARGET_MISSING,
    REASON_SESSION_CEILING_EXPIRED as REASON_SESSION_CEILING_EXPIRED,
    REASON_PROTECTION_SWEEP_TOO_LOW as REASON_PROTECTION_SWEEP_TOO_LOW,
    REASON_REGISTRY as REASON_REGISTRY,
    REASON_RELAY_TIMEOUT as REASON_RELAY_TIMEOUT,
    REASON_REVIEW_HOLD_TIMEOUT as REASON_REVIEW_HOLD_TIMEOUT,
    REASON_SNR_FLOOR as REASON_SNR_FLOOR,
    REASON_USER_STOPPED as REASON_USER_STOPPED,
    REASON_VERIFY_CROSSOVER_REGION as REASON_VERIFY_CROSSOVER_REGION,
    REASON_VERIFY_DETERMINISTIC_MISMATCH as REASON_VERIFY_DETERMINISTIC_MISMATCH,
    REASON_VERIFY_INCONCLUSIVE as REASON_VERIFY_INCONCLUSIVE,
    REASON_VERIFY_LEVEL_SHIFT as REASON_VERIFY_LEVEL_SHIFT,
    REASON_VERIFY_OUT_OF_TOLERANCE as REASON_VERIFY_OUT_OF_TOLERANCE,
    REASON_VOLUME_UNRESOLVED as REASON_VOLUME_UNRESOLVED,
    SCREEN_KIND_REASONS as SCREEN_KIND_REASONS,
    TEMPLATE_FIX_AND_RETRY as TEMPLATE_FIX_AND_RETRY,
    TEMPLATE_HARD_STOP as TEMPLATE_HARD_STOP,
    TEMPLATE_SESSION_RESTART as TEMPLATE_SESSION_RESTART,
    TEMPLATE_SILENT_AUTO_RETRY as TEMPLATE_SILENT_AUTO_RETRY,
    TEMPLATE_VERIFY_FAIL as TEMPLATE_VERIFY_FAIL,
    TEMPLATE_VOLUME_RECOVERY as TEMPLATE_VOLUME_RECOVERY,
    TRANSIENT_AUTO_RETRY_CODES as TRANSIENT_AUTO_RETRY_CODES,
    PhaseVerdict as PhaseVerdict,
    ReasonSpec as ReasonSpec,
    RetryableReasonCopy as RetryableReasonCopy,
    _retriable_reason as _retriable_reason,
    _screen_refusal_code as _screen_refusal_code,
    correction_rollback_failed_message as correction_rollback_failed_message,
    locate_failed_diagnosis as locate_failed_diagnosis,
    locate_failed_message as locate_failed_message,
    reason_diagnosis as reason_diagnosis,
    reason_message as reason_message,
    round_restore_reason as round_restore_reason,
    verify_inconclusive_cause as verify_inconclusive_cause,
    verify_inconclusive_diagnosis as verify_inconclusive_diagnosis,
    verify_inconclusive_message as verify_inconclusive_message,
)

from jasper.active_speaker.crossover_v2.spatial import (
    CLOUD_CLOSE_AWAITING_CONFIRM as CLOUD_CLOSE_AWAITING_CONFIRM,
    CLOUD_CLOSE_NONE as CLOUD_CLOSE_NONE,
    CLOUD_CLOSE_RUNNING as CLOUD_CLOSE_RUNNING,
    GEOMETRY_RETRY_POSITIONS as GEOMETRY_RETRY_POSITIONS,
)

from jasper.active_speaker.crossover_v2.attempt_grading import (
    ATTEMPT_REASON_NO_FLOOR as ATTEMPT_REASON_NO_FLOOR,
    PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB as PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
)

#: The pre-Apply improvement bar for a candidate carrying PRESCRIBED branches:
#: non-worsening (PR-B, conductor ruling 2026-08-20).
#:
#: Its sibling — :data:`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`, 0.5 dB — is
#: field evidence about the FIT and keeps its original subject untouched. This
#: one exists because that figure is a POOLED-RMS improvement and a per-driver
#: prescription is by construction a narrow high-Q filter aimed at ONE banked
#: feature: 0.077-0.152 dB pooled on realistic fixtures even when it is exactly
#: right, so the fitted bar would file the whole class as no improvement before
#: its first hardware exercise rather than judge it. Named and defined HERE,
#: beside the reader that chooses between the two,
#: because the choice is this module's (see ``_assert_accountable``) and the
#: gate it is handed to never branches on either.
#:
#: 0.0 rather than "no bar at all": a model cannot settle whether a narrow cut
#: helps, but it CAN say a proposal is predicted to make the speaker worse, and
#: that is worth writing down. It is a LEDGER boundary, not a stop — neither
#: bar refuses since the nanny burn-down (docs/measurement-loop-doctrine.md
#: deviation (c)) — deciding ``improved`` against ``not_an_improvement``.
PRESCRIBED_NON_WORSENING_DB: float = 0.0


def _prescribed_roles(candidate: Any) -> tuple[str, ...]:
    """Which of a candidate's driver branches are PRESCRIBED rather than fitted.

    Read off the persisted entry's own ``prescribed_by`` — the marker
    :func:`~.crossover_v2.driver_prescription.driver_prescription_to_candidate_fields`
    stamps and the same one a reader six weeks later uses — so "this graph
    carries a document" is answered by the graph rather than by session state.
    Empty for every automatic round, and defensive in the shape its neighbours
    are: a malformed or era-older entry is skipped rather than raising.
    """
    linearization = getattr(candidate, "linearization", None)
    if not isinstance(linearization, Mapping):
        return ()
    return tuple(
        str(role)
        for role, entry in linearization.items()
        if isinstance(entry, Mapping) and entry.get("prescribed_by")
    )

from jasper.active_speaker.crossover_v2.capture_dispatch import (
    SWEEP_LOCATE_CONFIDENCE_FLOOR as SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS as SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    _gate_disclosure as _gate_disclosure,
    _gate_floor_source as _gate_floor_source,
    _gate_moved_rms_db as _gate_moved_rms_db,
    _gate_record as _gate_record,
    _gate_reflection_delay_ms as _gate_reflection_delay_ms,
    _gate_trusted_band_hz as _gate_trusted_band_hz,
    _gate_window_ms as _gate_window_ms,
    _pilot_by_role as _pilot_by_role,
    _pilot_diag_fields as _pilot_diag_fields,
    _pilot_transfer_by_role as _pilot_transfer_by_role,
    _sweep_locate_confidence_ok as _sweep_locate_confidence_ok,
    _sweep_schedule_diag_fields as _sweep_schedule_diag_fields,
    _sweep_schedule_ok as _sweep_schedule_ok,
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
# and ``confidence_source`` in ``program_analysis.py``), the session refuses
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
# delay-plausibility backstop, the SNR/linearity/glitch verdicts and
# accountability's item 1 still REFUSE. This one number stopped being a veto
# because a bad ripple describes how well two branches can sum in this room on
# this rig — a thing the household cannot act on by moving anything — and not
# a defect in the capture that measuring again would fix.
#
# THE FRAME THIS NUMBER IS CALIBRATED IN, named because a corpus threshold is
# only comparable to captures measured the same way: the zero-residual summed
# branch sum at the polarity the candidate SHIPS. The delay stays pinned at
# zero residual — that is the documented evasion channel, since a candidate's
# own alignment could otherwise lower its own disclosure. Polarity is not a
# continuum a capture can shop along, and since #2598 it is a SELECTED
# quantity, so scoring coherence at a polarity the candidate does not ship
# would make a fine capture read as an incoherent one (the 2026-08-15 inverted
# rounds reported 14.13 dB for a pair that sums to a fraction of a dB the right
# way round). The 2026-07-22 corpus predates that selection, but every capture
# in it was graded at the polarity its own candidate shipped, so the frame is
# the same one.
#
# PROVISIONAL pending W6 bench validation, same status as every other
# MEASURE-phase threshold in this block.
MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB = 15.0

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

# Issue #1873's repeatability discriminator: how close two consecutive graded
# VERIFY attempts have to land before the mismatch between them is called
# DETERMINISTIC rather than transient.
#
# MEASURED, not chosen. Panel course-correction P0 (`captures/
# repeat-floor-20260731/README.md`) repeated the shipped stage-2 VERIFY
# instrument back-to-back with the microphone bolted in place, through the real
# deconvolution / gating / smoothing / grading path, and reported the repeat
# floor of THIS EXACT metric — ``max_db_notch_excluded`` over 1000-4000 Hz — as
# 0.052 dB median / 0.085 dB p95 between consecutive measurements. Its own table
# then states the honest per-attempt claim threshold for that metric as
# **0.2 dB, twice the consecutive-pair p95**. That number is what is used here;
# this module derives nothing.
#
# **The rule already has an owner, and this is its second spelling — so it says
# so.** :data:`~jasper.active_speaker.attempts_loop.CLAIM_FLOOR_P95_MULTIPLE`
# (2.0) owns "an honest per-attempt claim floor is twice the observed
# consecutive-pair p95", and its own comment records that the p95 over the 13
# accepted pairs is 0.08508 dB — so the rule computes 0.17016 dB, and the
# README's 0.2 is that same rule at conservative display rounding, not a second
# threshold. The kernel there COMPUTES the floor from a banked repeat study;
# this constant HARDCODES the rounded value instead, because a live VERIFY
# sitting has no such bank to read — it holds two attempts of its own and
# nothing else, and importing a kernel that needs a study it cannot supply would
# buy a dependency rather than a number.
#
# **Which way the 17.5% gap cuts, stated carefully, because the intuition runs
# backwards.** The discriminator declares determinism when the separation is
# ``<=`` the floor, so a BIGGER floor is a WIDER agreement window: 0.2 fires
# marginally MORE readily than the derived 0.17016 would, and against the
# kernel's own rule it slightly OVER-claims determinism rather than under-
# claiming it. That is acceptable, but not for the reason the arithmetic
# suggests — the margin comes from the fixed-mic caveat below, which puts the
# true same-sitting floor for a hand-held phone at or above 0.2. Measured
# against reality rather than against the bench number, 0.2 is still the
# conservative end.
#
# So a maintainer must NOT "tighten" this toward 0.17016 believing it moves
# safe-ward: it moves the other way, narrowing the window until real repeats at
# a hand-held mic stop being recognised as repeats and the household is handed
# back the dead retry this whole change removes. If that kernel ever gains a
# VERIFY-time source for its floor, this constant is what should go — replaced
# by the computed value, not hand-edited toward it.
#
# **It is a fixed-mic floor, and that direction is the safe one.** The same
# README is explicit that the mic-replacement arm — remove, replace, re-aim — is
# unmeasured and is the dominant cross-session term (the panel's 3.2 dB bound).
# A household CAN nudge the phone between in-session attempts, so the true
# same-sitting floor is somewhere at or above 0.2 dB. Using the tightest
# measured value therefore makes this discriminator HARDER to trigger: it
# under-claims determinism, which costs a household one more retry it did not
# need, where over-claiming would remove a retry that could have helped. Only
# the second is a wrong answer about the speaker.
#
# **Consecutive, never a fixed baseline** — the README's finding 1, in its own
# words: against a fixed early baseline the floor walks with drift
# (+0.0046 dB/repeat, r = +0.81, ~0.07 dB over 15 repeats), against the
# predecessor it is flat (-0.0021 dB/repeat). So the comparison below is always
# this attempt against the one before it, and the stored value is refreshed on
# every graded attempt. That is the opposite of G3's frozen
# ``_verify_pilot_baseline`` above, deliberately: G3 asks whether the recording
# chain has moved SINCE the sitting began, and this asks whether the speaker
# gives the same answer TWICE. Different questions, different baselines.
VERIFY_REPEAT_FLOOR_DB = 0.2

#: ``terminal_outcome`` for the verdict above — the relay contract's slot for
#: WHY the host ended the set at consume time. Its siblings are the admission
#: ladder's settle kinds ("this position ran out of tries"); this one says the
#: opposite kind of thing, which is why it is named rather than borrowed: the
#: captures were fine and they agreed, and it is the agreement that ends the
#: set. Read by the journal (``capture_relay.plan_terminal_result``); the phone
#: carries it without branching on it.
VERIFY_TERMINAL_OUTCOME_DETERMINISTIC = "verify_result_is_deterministic"

# Re-exported from :mod:`jasper.active_speaker.crossover_v2.programs`, which
# owns it and states why it has no switch, why the prelude announces a SESSION
# rather than a capture, and why both the phone's duration budget and the actual
# playback must read the SAME rule (#2291 Phase 5a-ii). The two capture-plan
# builders in this module are the other pair of readers.
courtesy_prelude_for_phase = _programs.courtesy_prelude_for_phase


class CrossoverV2FlowError(RuntimeError):
    """The v2 session could not form a safe phase transition."""


# --------------------------------------------------------------------------- #
# pure helpers (fixture-testable in isolation)
# --------------------------------------------------------------------------- #


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.programs`, which
#: owns it beside the three composers that are its only production callers
#: (#2291 Phase 5a-ii). Every existing ``flow.back_off_gain`` import resolves to
#: that one function.
back_off_gain = _programs.back_off_gain


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.planning`, which
#: owns it beside the build that is its reason to exist (#2291 Phase 5a-v(c)).
#: This module's own ``_log_measure_diag`` still calls it through this name, as
#: does every existing ``flow.alignment_to_candidate_fields`` import — one
#: definition either way.
alignment_to_candidate_fields = _planning.alignment_to_candidate_fields


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
    never a hardcoded delay literal. The v2 session is scoped to a single
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


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.planning`,
#: which owns it beside the build that freezes it onto the candidate (#2291
#: Phase 5a-v(c)). Every existing ``flow._analysis_json`` import resolves to
#: that one function.
_analysis_json = _planning.analysis_json


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


def _flatness_tilt_log_field(flatness: Any) -> str:
    """The band-to-band level step as one logfmt token — issue #1857's
    frame-free reading, beside a ``flatness_max_db`` that is not.

    ``flatness_max_db`` and ``flatness_bands`` are both distances from a
    reference pooled ACROSS bands, so a uniformly-off band drags that zero
    and inflates the others: on the corpus session this event's own
    forensics started from, a woofer flat to +/-0.1 dB logged
    ``+4.84 dB @ 1339.6 Hz`` because a ~5 dB dark tweeter had already pulled
    the frame down. A step BETWEEN two band levels cannot be moved by the
    frame -- the reference cancels in the subtraction -- so this token says
    the same thing under whichever anchor #1857's still-open Q-E eventually
    picks.

    Shape: ``<step>dB:<lo>-<hi>Hz><lo>-<hi>Hz``, the higher-sitting band
    first, no space or bracket for logfmt to quote. ``""`` (never a
    fabricated reading) when the gauge carried no tilt -- an older
    persisted block, or fewer than two bands with a measured level. Copied
    from :func:`~jasper.active_speaker.flat_spec.spec_band_tilt`'s own
    output; nothing here is recomputed and no verdict moves.
    """
    if not isinstance(flatness, Mapping):
        return ""
    tilt = flatness.get("tilt")
    if not isinstance(tilt, Mapping) or tilt.get("evaluable") is not True:
        return ""
    step_db = tilt.get("step_db")
    high, low = tilt.get("high_band_hz"), tilt.get("low_band_hz")
    if (
        not isinstance(step_db, (int, float)) or isinstance(step_db, bool)
        or not isinstance(high, (list, tuple)) or len(high) != 2
        or not isinstance(low, (list, tuple)) or len(low) != 2
    ):
        return ""
    edges = [_band_edge(high, 0), _band_edge(high, 1), _band_edge(low, 0), _band_edge(low, 1)]
    if any(edge is None for edge in edges):
        return ""
    high_lo, high_hi, low_lo, low_hi = edges
    return (
        f"{step_db:.2f}dB:{high_lo:.0f}-{high_hi:.0f}Hz>{low_lo:.0f}-{low_hi:.0f}Hz"
    )


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






def _driver_snr_fields(
    resp: Any | None,
) -> tuple[float | None, str | None, str | None]:
    """``(estimated_snr_db, verdict, band_id)`` from a driver's worst SNR band.

    The band identity travels because the number and the verdict alone cannot
    say WHICH band produced them (#2613): fourteen consecutive jts3 rounds
    logged ``tweeter_snr_db=-1.2 tweeter_snr_verdict=insufficient`` and the
    band that actually limited them — one the tweeter sweep never entered —
    had to be re-derived from the crossover frequency and the declared driver
    bands instead of read off the line. ``band_id`` stays ``None`` when
    ``worst_band_verdict`` selected a band carrying no id (it filters on
    overlap and verdict rank, never on identity), so a real band is never
    confused with an absent one.
    """
    if resp is None or resp.snr is None:
        return None, None, None
    worst = resp.snr.get("worst_relevant") or {}
    return (
        worst.get("estimated_snr_db"), worst.get("verdict"), worst.get("band_id"),
    )




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
# `intervention.SIGMA_TOLERABLE_DB`. The eligibility gate (mic tier + paired
# repeat count) moved to `planning.py` and the accountability decision to
# `accountability.assess_accountability`; this module calls them and owns the
# irreversible half. See
# docs/active-speaker-tuning-layers-design.md "Layer 1a concretely".

# The level-frame agreement tolerance used to live here, as a flow-side alias
# the planner and the accountability gate both read. It is deleted with the
# arbitration it gated (single-datum-owner migration, #2609): the raw per-branch
# trim solve places the pair, NOT the summed at-the-mark capture (incoherent
# frames — `intervention.plan_linearization`'s anchor block; #2653), the two
# per-driver estimates became
# an advisory consistency check, and the one surviving tolerance is owned by
# `crossover_v2.intervention.LEVEL_ESTIMATOR_TOLERANCE_DB` — which still
# resolves to `program_analysis.REALIZED_LEVEL_MATCH_TOLERANCE_DB`, for the
# reason it always did. Nothing in this file holds a level tolerance.


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
    from it; fakes may pass raw bytes). ``geometry`` is the session's
    declared MeasurementGeometry so the parallax correction actually reaches
    analyze_program_capture — a seam that dropped it would silently analyze
    with zero spacing.

    ``phase`` is REQUIRED and keyword-only: the SESSION's own flow phase
    (issue #1855) — NOT ``program.phase``. The two are different
    vocabularies: every cloud position plays ``self._cloud_program`` (see
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
    """Banks one model-predicted/realized pair outside the session."""

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
    """The session's injected I/O boundary (all side effects)."""

    play: PlayProgram
    analyze: AnalyzeCapture
    publish_check: PublishCheck
    publish_candidate: PublishCandidate
    apply_complete: ApplyGate
    apply_failed: ApplyFailureGate
    # Position-group evidence retention (PR-3b), called once per ACCEPTED cloud
    # capture with ``(position_id, capture_result, metadata)``. Optional so
    # every pre-cloud construction site (and every session unit test) stays
    # valid; ``None`` means the group runs with no durable per-position
    # artifact, which is the correct behaviour for a session with no evidence
    # store rather than a reason to fail a capture.
    retain_position: Callable[[str, Any, Mapping[str, Any]], None] | None = None
    # PR-4: the cloud honesty-pipeline bundle publisher, called once per
    # CLOSED group with ``(phase, cloud_group_result_dict)``. Optional for the
    # same reason ``retain_position`` is: every pre-PR-4 construction site
    # (and every session unit test) stays valid, and ``None`` means the
    # group's result is computed and readable via
    # :meth:`CrossoverV2Session.group_cloud_result` but not published as a
    # bundle artifact.
    publish_cloud: Callable[[str, Mapping[str, Any]], None] | None = None
    # #1866 frame-gate ruling: the banked level-frame disagreement, called at
    # most once per session with the flow's evidence record, from
    # :meth:`CrossoverV2Session._commit_measure_candidate` — AFTER
    # ``publish_candidate``, so the artifact the finding cites already exists.
    # Optional exactly like the two seams above: a session with no evidence
    # store still banks the number in its journal and still PROCEEDS, it just
    # writes no durable finding. That degraded mode is the ordinary state of
    # every session unit test and is not a reason to refuse a run.
    publish_findings: Callable[[Mapping[str, Any]], None] | None = None
    # PR-L5: undo the applied correction, called with the delta-probe reason
    # code when the post-apply map does not match. Returns True when the
    # previous profile was restored. Optional like the two seams above — a
    # session with no rollback binding still CLASSIFIES and refuses (the
    # household sees the verdict and the Undo button the failure screen
    # already offers), it just cannot press the button itself. That degraded
    # mode is disclosed on the verdict's own event, never silent.
    rollback: Callable[[str], bool] | None = None
    # #1811: the whole-band level move the APPLY made and did not command as
    # part of the correction's shape — the pre-split headroom the applied graph
    # charges for its own boost. Read at probe time (like ``apply_complete`` /
    # ``apply_failed``, off durable state) rather than passed at construction,
    # because the apply happens on a background thread AFTER this session is
    # built. Optional: ``None`` means "nothing known", which
    # ``classify_delta_probe`` treats honestly — the whole shift stays visible
    # as ``residual_offset_db`` instead of being silently claimed as accounted.
    applied_offset_db: Callable[[], float] | None = None
    # #2611: the Layer-A profile the speaker is playing RIGHT NOW — the graph an
    # apply would replace. Read once per candidate EVALUATED, which is ONCE in a
    # session: a round builds the candidate for the corner it was opened at.
    # Always at MEASURE time — the apply
    # has not happened yet, so "currently applied" IS the previous graph — and
    # turned into the PREVIOUS side of the commanded axis by
    # :meth:`CrossoverV2Session._previous_graph_predicted_sum`. The answer does
    # not change between corners; the repeat reads are a cheap durable-state
    # load, and the disclosure they produce is deduplicated at that method.
    #
    # Optional, and its absence is NOT a fallback to the old raw-crossover axis:
    # a session that cannot learn what the speaker is playing cannot state what
    # an apply commands, so the commanded delta is ``None`` and the delta probe
    # reports ``unavailable`` — no evidence to refuse on, and no permission
    # granted either. Grading against a graph nobody ran is what rolled a
    # measured-better tune back on 2026-08-16; refusing to grade is the honest
    # direction, and the reason is named on the journal every time.
    applied_profile: Callable[[], Mapping[str, Any] | None] | None = None
    # S3 attempts loop: called once for each newly accepted applied-candidate
    # VERIFY. Optional so a session without a durable host still grades its
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
    # grading time, because the grading session cannot answer it from its own
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

    Persisted under the session's commissioning run; :meth:`CrossoverV2Session.hydrate`
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
    # tiers existed, or a session constructed without one — and readers must
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
    #
    # SURVIVING is not the same as being COMPARABLE, and #2081 is the gap
    # between the two: these records also survive ``reset_v2_journey_state``,
    # which preserves them deliberately so a second tune has a predecessor to
    # grade against — but the mic was re-placed in between, and the claim floor
    # was measured with it bolted down. So each record now carries the sitting
    # that produced it, and the kernel refuses the pair rather than reporting
    # an improvement no study licenses. The history still rides across; what
    # changed is that the loop can now tell it did.
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
    """Restore the session-owned attempt history from durable journey state.

    Invalid rows are dropped as unavailable history, never partially trusted.
    The floor is intentionally absent from this shape: it has one owner in
    :mod:`jasper.active_speaker.model_error_store` and is read afresh by the
    host when it constructs the session.
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
                # #2081. Absent on every row written before it, and ``""`` is
                # exactly what the kernel refuses on — so an upgraded speaker
                # stops claiming improvement against its pre-upgrade attempt
                # instead of claiming one whose sitting nothing recorded.
                sitting_id=str(row.get("sitting_id") or ""),
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
    analysis: ProgramAnalysis, *, attempt_id: str, sitting_id: str,
) -> AttemptRecord:
    """Map one VERIFY analysis into the pure kernel's realized record (#2033).

    VERIFY necessarily leaves repeat-only checks ``not_evaluated`` because it
    contains one summed sweep. Their names still ride as reasons, but they do
    not make an otherwise clean capture incomparable. Any evaluated failure
    does, and carries both the failed and not-evaluated check names so the
    kernel's STOP_EVIDENCE record never loses what the analyzer knew.

    ``sitting_id`` is the relay session that captured this sweep, and it is a
    **required** argument rather than a defaulted one because the default that
    would be available here — ``""`` — is the value the kernel reads as
    "unrecorded" and refuses on (#2081). A caller that forgets is then a caller
    whose speaker silently stops claiming improvement, which is the failure
    this signature makes impossible to reach by omission.

    Why the relay session is the right proxy for one continuous microphone
    sitting: the household holds the phone for the whole of a session's
    captures, and a new session means it was put down and picked up. That is
    already the reason :meth:`CrossoverV2Session.hydrate` invalidates CHECK and
    MEASURE evidence across a rebind — "mic position is unverifiable across
    sessions" — so this reuses that established boundary rather than inventing
    a second one.
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
        sitting_id=str(sitting_id),
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


# One prompted position's attempt ledger (owner ruling #2086). Re-exported from
# :mod:`jasper.active_speaker.crossover_v2.admission`, which owns the ledger and
# the admission decision that reads it (#2291 Phase 5a-vi). Kept importable from
# the flow because that is where the endpoints suite and the capture-sequence
# pins name it.
SlotAttempts = _admission.SlotAttempts


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
    # (every position in one group shares the same session-derived values —
    # see ``CrossoverV2Session.__init__``) is what lets
    # :func:`combine_cloud_positions` derive the right bands from
    # ``positions`` alone, with no caller (``_close_cloud_group``'s single
    # combine, ``cloud_geometry_verdict``'s convenience wrapper) needing to
    # pass them explicitly or risk two call sites drifting apart.
    # ``None`` means "use the module defaults" — the pre-PR-4 behaviour, still
    # exercised by every corpus/unit test that builds a ``_CloudPosition``
    # without these two kwargs.
    echo_band_hz: tuple[float, float] | None = None
    signal_band_hz: tuple[float, float] | None = None


# R16's lateral evidence types and their shared log basis (plan §4.4).
# Re-exported from :mod:`jasper.active_speaker.crossover_v2.spatial`, which owns
# them beside the screens that admit a pose and the records a retained take
# writes — the same "what does this take record" charter, and the module that
# already documents ``lateral_pose_curve`` in its own prose. Kept importable
# from the flow because that is where the R16/R17 suites name them —
# ``_primary_sweep_bands`` included, underscore and all: it moved verbatim, and
# renaming it to dodge the private access here would break the suite that reads
# it off this module without making it any less internal.
LATERAL_EVIDENCE_BAND_HZ = _spatial.LATERAL_EVIDENCE_BAND_HZ
LATERAL_EVIDENCE_POINTS_PER_OCTAVE = _spatial.LATERAL_EVIDENCE_POINTS_PER_OCTAVE
LateralPoseCurve = _spatial.LateralPoseCurve
LateralPose = _spatial.LateralPose
lateral_evidence_grid_hz = _spatial.lateral_evidence_grid_hz
lateral_pose_curve = _spatial.lateral_pose_curve
_primary_sweep_bands = _spatial._primary_sweep_bands


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
        # §4.2's one line. The role was written to the position RECORD and the
        # persisted row and read by nothing analytical — the combiner's only
        # per-position struct dropped it here, so nothing that decides or
        # remembers a round ever saw a position's KIND. Carrying it changes no
        # combination (the reduction stays unweighted; see
        # ``PositionCapture.role``) and is what lets the per-position residual
        # say "on-axis" rather than "position 3".
        role=str(position.role or ""),
    )


def combine_cloud_positions(positions: Sequence[_CloudPosition]) -> Any:
    """Assemble a closed group and combine it — the whole PR-4 seam.

    Returns a :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`,
    or ``None`` when the group cannot be combined (no positions, or a malformed
    one). Called exactly ONCE per group-close event, from
    :meth:`CrossoverV2Session._close_cloud_group`: PR-3b reads one field off
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
    # Every position in one group carries the SAME session-derived bands
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
    2026-07-26) so :meth:`CrossoverV2Session._close_cloud_group` can
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
    the session itself does NOT call this (see
    :meth:`CrossoverV2Session._close_cloud_group`'s own single combine).

    **Reason-string divergence, documented not silently left (N4 review
    finding, 2026-07-27).** An empty ``positions`` short-circuits HERE with
    ``reason="no_positions"`` before ever reaching the combiner, while
    :func:`_geometry_verdict_from_combined` called directly with a
    ``combined=None`` and ``n_positions=0`` (e.g. because
    ``combine_cloud_positions([])`` was called some other way) reports
    ``reason="combine_failed"`` for the exact same "there were zero
    positions" fact. Unreachable through the session today (a group only
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
    that composition, added here because it is session-owned wiring policy
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
    session's cloud-group analysis") -- this is that wiring layer's owned
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
    ``[graded_lo_hz, f_hi_hz)`` span — the span actually graded, not the
    nominal row — so a null straddling a band edge appears under both bands it
    actually carves, and one sitting entirely below the session's trusted floor
    appears under none, because it removed no bin any verdict was taken from.

    **What this does NOT include: the gate's trusted-floor clamp.** Bins
    below the group's ``trusted_floor_hz`` also leave the spec evaluation,
    but they are not an interference verdict and are deliberately kept out of
    the honesty instruments' own accounting — the same separation
    ``_compact_cloud_status`` carries for exactly this reason. Since #2551
    that separation is structural rather than a convention the reader has to
    hold: the clamp raises each band's lower EDGE, so a sub-floor bin is not
    in the band to be excluded FROM. A band's ``n_excluded`` is therefore
    exactly what these records cover, and the floor shows up as the spec
    report's ``graded_lo_hz`` beside the nominal ``band_hz`` here rather than
    hiding inside a count.
    """
    records = _carve_out_records(null_report, screen_bands_hz)
    out: list[dict[str, Any]] = []
    for band in spec_report.bands:
        f_lo, f_hi = float(band.f_lo_hz), float(band.f_hi_hz)
        # Overlap is tested against the edge this band was GRADED from, not
        # its nominal row: a null below the trusted floor carved nothing out
        # of this band's grading, because those bins were never in it. That
        # is what makes the equality claimed above ("n_excluded is exactly
        # what these records cover") true rather than approximate. `band_hz`
        # below stays the nominal pair, since it is the join key a consumer
        # uses against ``spec["bands"]`` — which carries `graded_lo_hz`
        # itself, so this payload does not copy it and cannot drift from it.
        graded_lo = f_lo if band.graded_lo_hz is None else float(band.graded_lo_hz)
        in_band = [
            record
            for record in records
            if record["f_lo_hz"] < f_hi and record["f_hi_hz"] > graded_lo
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


# --------------------------------------------------------------------------- #
# what one candidate build produced — see crossover_v2.candidates
# --------------------------------------------------------------------------- #
#
# The three values a build's product travels on. They moved to the package in
# #2291 Phase 5a-v, which is where the machinery that reads them went. Re-bound
# here under their historical private names so every call site in this module,
# and the prose that cites them, keeps resolving.
_CloudFitEvidence = _candidates.CloudFitEvidence
_LinearizationState = _candidates.LinearizationState
_SpeculativeClose = _candidates.SpeculativeClose


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


def cloud_trusted_floor_hz(validity_floor_hz: float | None) -> float | None:
    """The group's TRUSTED floor (``2.5/T``) from its validity floor
    (``1/T``) — the number the flat spec is graded above (issue #2551).

    ``1/T`` is where a reflection-free window of ``T`` has one full cycle of
    resolution; ``2.5/T`` is where the gated magnitude is actually
    trustworthy, and the E4 gate-stability sweep is why the distinction is
    not academic — the 1-4 kHz band moved **2.1 dB** across 3/5/7/10 ms
    gates purely because part of it sat below the shorter windows' trusted
    floor, while everything above it held to <=0.006 dB
    (:data:`~jasper.audio_measurement.gating.TRUSTED_FLOOR_MULTIPLIER`).
    The gate's own delta probe already prices itself over this floor and
    refuses to grade below it; before #2551 the spec evaluator did not, so
    one capture was read by two graders against two honesty floors.

    Derived rather than plumbed, deliberately. Both floors come from the
    same window — ``f_trusted = 2.5 * f_valid`` exactly
    (:func:`~jasper.audio_measurement.gating.f_trusted_floor_hz` is that
    multiply) — and the multiplier is monotonic, so the trusted floor of the
    group's WORST validity floor is the worst of the positions' trusted
    floors. One input, one owner, and no caller that passes a validity floor
    can forget to pass the trusted one and silently grade lower.

    ``None`` in, ``None`` out; likewise for a non-finite or non-positive
    floor, which is "no floor was established" and never "a floor of zero".
    Callers clamp nothing then, and say so — see
    :func:`assemble_cloud_group_result`.
    """
    if validity_floor_hz is None:
        return None
    floor = float(validity_floor_hz)
    if not math.isfinite(floor) or floor <= 0.0:
        return None
    return TRUSTED_FLOOR_MULTIPLIER * floor


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
    graded_spec_sink: Callable[[Any], None] | None = None,
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
    the session passes it alongside the band it came from. ``None`` when a
    caller did not state one — "not stated", never "not clamped", the same
    unknown-vs-zero rule ``validity_floor_hz`` follows below.

    **The spec is graded above the group's TRUSTED floor, not its validity
    floor** (issue #2551). ``validity_floor_hz`` is the group's own gated
    ``1/T`` (:func:`cloud_validity_floor_hz`); :func:`cloud_trusted_floor_hz`
    turns it into the ``2.5/T`` the gate's delta probe already refuses to
    grade below, and THAT is what
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` intersects
    every band's lower edge with -- the reference band's included, since a
    bin the gate cannot support must not be able to re-centre the target
    either. Both floors are published: ``validity_floor_hz`` for provenance,
    ``trusted_floor_hz`` as the number the verdicts were actually taken
    above. Three properties this deliberately keeps:

    * **The intersection is a band EDGE, not a mask entry.** A sub-floor bin
      is not in the band at all, so ``spec.n_excluded`` stays exactly the
      honesty instruments' own count (screen union identified nulls) and a
      gate artifact can never inflate it. Which edge each band was graded
      from is disclosed per band as ``graded_lo_hz``, delta-probe style, and
      the report echoes ``trusted_floor_hz`` and the clamped
      ``reference_band_hz`` on its face. ``merged_excluded_bands_hz`` is
      likewise untouched: ``excluded_interval_count`` on `/state` remains the
      "how much interference did we find" number.
    * **A band left entirely below the floor is ``evaluable=False``, never
      ``passed=False``.** There is no evidence there, which is not a
      failure; ``graded_lo_hz >= f_hi_hz`` is the tell that distinguishes it
      from a band the axis never reached.
      :attr:`~jasper.active_speaker.flat_spec.FlatSpecReport.overall_passed`
      still treats unevaluable as not-passed, so nothing is flattered by the
      distinction.
    * **A ``None`` floor clamps NOTHING and is reported as ``None``.** The
      alternative -- withholding the whole gauge, which is what the retired
      per-capture ``_flatness_tracking`` did when a capture had no floor --
      would throw away the 2-16 kHz evidence over an unverified lower edge.

    Regime, measured on the S0 main leg 2026-07-27, re-derived 2026-08-02
    (#2045) and re-derived again for #2551: **all ten** of that session's
    positions gate to a 142.857 Hz validity floor, i.e. a **357.14 Hz**
    trusted floor, which sits ABOVE the spec table's 250 Hz edge and
    therefore clamps 987 bins out of the low band. Before #2551 the
    evaluator was handed the 142.857 Hz number instead, which sits below
    250 Hz and changed no graded figure at all -- a clamp in name only, on a
    corpus whose worst deviation bins were beneath the floor its own gate
    disclosure printed. ``test_flat_spec_ssot`` pins both halves: that the
    positions no longer collapse a gate, and what intersecting at the
    trusted floor costs.

    **Clamping is not free, and it moves the headline in the flattering
    direction.** That is the mechanism's own behaviour and it is stated here
    rather than discovered later; measured on the S0 corpus at a 1777.8 Hz
    trusted floor supplied explicitly
    (``test_flat_spec_ssot.CLAMP_TRUSTED_FLOOR_HZ``, pinned by
    ``test_the_trusted_floor_clamp_costs_the_low_band``), clamping:

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
    fewer bins, which is exactly what the gauge's ``n_bins`` exists to keep
    visible (``ConvergenceResidual``'s own "a residual that fell because the
    denominator shrank is not convergence" rule). Its sibling ``n_excluded``
    reports a different thing and deliberately does not move here: the clamp
    is an edge, not a mask entry. One short gate in a group is therefore
    expensive by design, and the group takes the WORST position's floor.

    **Deferred alternative, recorded rather than dismissed:** the honest
    third option is per-position, per-bin validity masking INSIDE
    ``combine_positions`` -- mask each position's contribution below that
    position's OWN floor and combine the survivors, so nine good captures
    keep contributing at 500 Hz instead of one bad one costing the band. It
    is strictly better than a group-wide clamp and is out of scope here only
    because it is a ``spatial_combine`` signature and estimator change (the
    power mean would need per-bin weights), not a wiring one. Revisit
    trigger: a real session where one short gate meaningfully shrinks the
    graded band -- the S0 corpus is that evidence already now that the floor
    is the trusted one (357.14 Hz clears the table's 250 Hz edge on every
    position), so this is queued on measured grounds, not speculation.

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
            GradedSpec,
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
        # NOTE: ``crossover_registry`` is deliberately absent from this union.
        # See its builder for why classification there may never become
        # gating.
        # The mask handed to the evaluator is EXACTLY what the honesty
        # instruments found. The gate's floor rides beside it as a band-edge
        # intersection instead (#2551), so ``n_excluded`` cannot conflate an
        # interference verdict with a short window — see this function's
        # docstring.
        trusted_floor_hz = cloud_trusted_floor_hz(validity_floor_hz)
        spec_report = evaluate_flat_spec(
            combined.freqs_hz, combined.power_mean_spec_db, merged_mask,
            trusted_floor_hz=trusted_floor_hz,
        )
        # #2291/#2160: hand the LIVE report to a caller that needs the object
        # rather than the serialized copy below. ``evaluate_spec`` reads
        # ``overall_passed`` and each band's ``evaluable``/``passed``, which
        # ``to_dict`` flattens away, and the round's spec verdict must be the
        # SAME report this function already built — re-evaluating it from
        # ``combined`` in the session would be a second owner of the merged
        # honesty mask, which is exactly what this function exists to prevent.
        # A sink rather than a second return value because every other caller
        # (and every test) reads the dict, and widening the return type would
        # change all of them to serve one consumer.
        if graded_spec_sink is not None:
            # The curve, the mask, and the verdict as ONE record: decision 10's
            # blend correction reads all three, and this is the only place all
            # three exist together. Handing them over separately would let a
            # consumer pair a curve with a mask from a different evaluation.
            graded_spec_sink(GradedSpec(
                combined.freqs_hz, combined.power_mean_spec_db, merged_mask,
                spec_report,
            ))
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
            # #2551: the floor the spec was actually graded above — 2.5x the
            # one directly above, and the same number the gate's own delta
            # probe prices itself over. Published beside its input rather
            # than in place of it, so a reader can see both the window's
            # resolution limit and its trust limit.
            "trusted_floor_hz": trusted_floor_hz,
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


def _commanded_delta(previous_predicted_sum: Any, predicted_sum: Any) -> Any:
    """``(freqs_hz, delta_db)`` — what the applied correction COMMANDS on the
    summed response, or ``None`` (PR-L5's delta probe, the commanded half).

    The applied graph's predicted sum minus **the predicted sum of the graph it
    replaces**, both built from the SAME measured branches with the SAME
    summation model (``program_analysis.predicted_branch_sum``) against the SAME
    alignment anchor, so the branch measurements and the summation model divide
    out and what is left is every element the apply commands: the emitted
    filters, the role gains, the polarity, and the delay. The anchor does NOT
    divide out and is not meant to — it is shared so both graphs are stated in
    one phasing frame; see
    :func:`jasper.active_speaker.crossover_v2.commanded.graph_predicted_sum`.

    The previous side is built by
    :meth:`CrossoverV2Session._previous_graph_predicted_sum`; this function is
    only the subtraction, and
    :mod:`jasper.active_speaker.crossover_v2.commanded` owns both the model and
    the argument for why the previous side is the graph rather than the raw
    crossover (#2611).

    ``None`` — the probe reports ``unavailable``, which is not a pass — when
    either curve is missing or the two cannot be put on one grid. A MISSING
    curve is already named on the journal by whoever failed to build it. The
    other case is this function's own to name, and it does: two curves that both
    arrived and still would not subtract is a defect in one of them, and it used
    to disappear silently here.
    """
    delta = _commanded.commanded_delta(previous_predicted_sum, predicted_sum)
    if delta is None and None not in (previous_predicted_sum, predicted_sum):
        log_event(
            logger, "correction.crossover_v2_commanded_delta_failed",
            level=logging.WARNING,
            previous_points=_curve_points(previous_predicted_sum),
            applied_points=_curve_points(predicted_sum),
        )
    return delta


def _curve_points(curve: Any) -> int | None:
    """How many points a ``(freqs, values)`` pair carries, or ``None``.

    Only ever a log field, so it answers rather than raises: the one thing a
    reader of ``crossover_v2_commanded_delta_failed`` wants first is whether the
    two curves were even the same length, and a diagnostic that could itself
    throw inside a failure path would be worse than no diagnostic.
    """
    try:
        return int(np.asarray(curve[1], dtype=float).size)
    except (ValueError, TypeError, IndexError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# the session
# --------------------------------------------------------------------------- #


class CrossoverV2Session:
    """One measurement session: its state, its seams, and its host contract.

    Construct with the session identity, the declared drivers, the crossover Fc,
    the safety caps + session volume, and the injected :class:`V2FlowSeams`.
    Hand :meth:`authorize_begin`, :meth:`on_armed`, and :meth:`consume_capture`
    to :func:`jasper.capture_relay.session.run_capture_plan`; call
    :meth:`note_apply_complete` once an apply lands (the deferred VERIFY then
    arms) — an optional synchronous shortcut for a caller that already holds
    this session; the seam-based ``apply_complete``/``apply_failed`` checks
    in :meth:`authorize_begin` are the durable path and work even without this
    call. Since the two-stage split (D10) no shipped session reaches that hold,
    so neither is on the critical path. :meth:`snapshot` / :meth:`hydrate`
    carry phase persistence.

    **What belongs on this class, and what does not.** Three things do: the
    session's own mutable state (the ``self._…`` fields ``__init__`` declares);
    the reads of that state which the web host needs, as the properties and
    accessors below — that is the adapter half of the job, and it is why a
    one-line ``return self._x`` here is a contract rather than scaffolding; and
    the acts that cannot be undone or repeated (playing, publishing, applying,
    committing, journalling), each behind a seam. What does not belong is any
    RULE: which reason code a bad capture earns, whether an attempt is
    admitted, what a fit should propose, when a group has heard enough. Those
    are the organs' — see the module docstring for the direction that keeps.
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
        measure_declared_transfer: Any = None,
        measure_entry_baseline: "EntryBaseline | None" = None,
        measure_gate_window_ms: float | None = None,
        measure_proposal_fingerprint: str = "",
        measure_alignment_objective: str = "",
        verify_pilot_transfer_prior: Mapping[str, Any] | None = None,
        driver_class_by_role: Mapping[str, str] | None = None,
        radiating_diameter_mm_by_role: Mapping[str, float] | None = None,
        measurement_protection_sections_by_role: Mapping[
            str, Sequence[CrossoverSection]
        ] | None = None,
        tweeter_measurement_band_hz: tuple[float, float] | None = None,
        attempt_history: Sequence[AttemptRecord] = (),
        series_position: "SeriesPosition | None" = None,
        attempt_floor: FloorStats | None = None,
        last_attempt_decision: Mapping[str, Any] | None = None,
        speaker_id: str = "",
        tuning_attempt_id: str = "",
        sound_design_revision: int | None = None,
        alignment_prescription: "AlignmentPrescription | None" = None,
        topology_prescription: "TopologyPrescription | None" = None,
        blend_prescription: "BlendPrescription | None" = None,
        blend_prescription_sha256: str = "",
        driver_prescription: "DriverPrescription | None" = None,
        lateral_consumer: str = LATERAL_CONSUMER_FC_SELECTOR,
        lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
    ) -> None:
        roles = tuple(roles_bands)
        if len(roles) != 2:
            raise CrossoverV2FlowError("the v2 session is a 2-way flow")
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
        # #2662. Already validated by the request boundary that accepted it —
        # this session holds it, it does not re-judge it, and a session that was
        # handed none runs the automatic alignment exactly as before. Held as
        # the record rather than the bare float so the round receipt can name
        # the measured basis without a second copy of it living anywhere.
        self._alignment_prescription = alignment_prescription
        # The topology twin of the line above, held on identical terms —
        # validated by the request boundary that accepted it, never re-judged
        # here. It differs from its siblings in WHERE it already took effect:
        # the boundary opened this session AT the pinned corner and order (see
        # ``fc_sweep.recornered_preset``), so ``self._fc_hz`` and
        # ``self._preset`` above are already the pin. What the session still
        # needs the record for is to know it is pinned at all — the two ports
        # below close the search and suppress the selector — and to bank the
        # provenance on the round's receipt.
        self._topology_prescription = topology_prescription
        # A9. The blend-region twin of the line above, and held on the same
        # terms: already validated by the boundary that took it out of the
        # spool, re-validated there against the region it was checked against,
        # and NOT re-judged here. A session handed none runs decision 10's
        # deterministic instruction exactly as before. The digest travels beside
        # it rather than inside it because it names the DOCUMENT, not the
        # correction — see ``blend_prescription_record``. Named for what it IS
        # rather than for the ctor argument, because ``_blend_prescription`` is
        # already the METHOD that decides which source wins and a field of that
        # name would shadow it.
        self._prescribed_blend = blend_prescription
        self._prescribed_blend_sha256 = str(blend_prescription_sha256 or "")
        # A9/PR-B. The THIRD prescription this session may hold, on the two
        # above's terms — validated by the boundary that took it out of the
        # spool, not re-judged here — and it differs from them in exactly two
        # ways worth stating. It is per-ROLE rather than per-region, so its door
        # is the candidate's ``linearization`` map and the merge that folds it
        # onto the fit lives where the fit is final
        # (``crossover_v2.planning.build_candidate``), not in a reader here.
        # And it carries no digest field: the blend one exists only because the
        # round receipt banks it, this class has no receipt lane yet, and a
        # field nothing reads is the permissive-default trap this file's
        # neighbours name. The document's digest is journal-visible at the take
        # instead.
        self._prescribed_driver = driver_prescription
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
        # session owns the bounded history; the injected floor/store writer
        # keep persistence outside it and the decision kernel pure.
        self._attempt_history = list(attempt_history)[
            -AttemptBudget().hard_cap_attempts:
        ]
        # #2602's series memory, resolved by the host from durable state — on
        # BOTH stages since #2698, because the two readers run on different
        # ones. ``_grade_round_once`` (stage 2) normalizes ``None`` to the
        # opening round and imports ``coordinator`` to do it (see the
        # TYPE_CHECKING note above — importing it here would pull
        # ``flat_spec`` into every flow import); ``_blend_prescription``
        # (stage 1, since #2687) reads ``previous_blend_correction`` and takes
        # its own documented direction on ``None``.
        self._series_position = series_position
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
        # role, the ka/beaming prior. Collected since #1665; the Fc selector
        # that read it is retired (ticket 2.4) along with the corner hunt whose
        # proposal grid it clamped, so the diameter rides the receipt as
        # provenance and no admissibility bound reads it — it reaches here
        # by the SAME draft path ``driver_class_by_role`` takes. Empty means
        # undeclared, which a consumer must DISCLOSE rather than fill in: there
        # is no conservative default diameter.
        self._radiating_diameter_mm_by_role = (
            dict(radiating_diameter_mm_by_role) if radiating_diameter_mm_by_role else {}
        )
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
        # ordinary path is one session per run consuming CHECK then
        # MEASURE, so the field is simply there. A session REHYDRATED past
        # an accepted CHECK (same session id, gain plan restored — the shape
        # that can compose a MEASURE program without re-running CHECK) has no
        # ambient of its own, and MEASURE's SNR verdict is then honestly
        # ABSENT rather than graded against a floor this session never
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
        # ``self._lateral_prompts``; each pose holds two curves on the fixed
        # ~120-point basis, so the whole walk is a few thousand complex values.
        self._lateral_poses: list[LateralPose] = []
        # Defaults reproduce the shipped session exactly. Judged by the
        # vocabulary's owner; re-raised so construction refuses in one type.
        try:
            self._lateral_consumer = validated_lateral_consumer(
                lateral_consumer, states_own_poses=lateral_prompts is not None,
            )
        except ValueError as exc:
            raise CrossoverV2FlowError(str(exc)) from exc
        self._lateral_prompts: tuple[CloudPositionPrompt, ...] = (
            tuple(lateral_prompts) if lateral_prompts is not None
            else LATERAL_POSE_PROMPTS
        )
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
        self._group_graded_spec: dict[str, Any] = {}
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
        # #2609 SF5 and the design brief's §4.2, both per closed group and both
        # read by exactly one caller (:meth:`_grade_round_once`, for
        # ``PHASE_CLOUD_VERIFY``).
        #
        # ``_group_trusted_floor_hz`` is the frame the group's spec bands were
        # graded in — the SAME number ``assemble_cloud_group_result`` derived
        # and intersected them with, recomputed here from the same two owners
        # rather than dug back out of the serialized result, so the round banks
        # the floor its own report used.
        #
        # ``_group_position_residuals`` is one number per position: how far it
        # sat from the combined curve. Computed at close, when both operands are
        # already in memory on the combine, because the combine does not survive
        # to grading time on the tier that needs it.
        self._group_trusted_floor_hz: dict[str, float | None] = {}
        self._group_position_residuals: dict[str, tuple[Mapping[str, Any], ...]] = {}
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
        # The prompted position groups' twin of it: same sweep, same clamp, no
        # courtesy prelude (``crossover_v2.programs.courtesy_prelude_for_phase``
        # — a position does not open a session). Held for the same reason as the
        # other three: ``program_for_phase`` answers by identity.
        self._cloud_program = self._excitation.cloud_program()

        # Per-SLOT attempt bookkeeping + the last failure reason. A slot is the
        # phase for a single-capture phase and the ``phase:index`` pair inside a
        # position group (``_slot_of_index``), so a rejected position spends its
        # own retry budget instead of the whole group's.
        #
        # ONE meter per slot (:class:`SlotAttempts`, owner ruling #2086). It
        # replaced a pair — a cumulative attempt count measured against
        # ``ReasonSpec.retry_budget``, plus a per-slot geometry-rejection
        # DISCOUNT that existed only to keep the session's own retakes from
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
        # the re-arm builds a fresh session which never runs a fit, so
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
        # VERIFY, which a re-arm reaches with a freshly constructed session.
        self._measure_commanded_delta: Any = measure_commanded_delta
        # #2614: the applied graph's OWN transfer against the uncorrected
        # crossover — the STATE axis, beside the CHANGE axis above. The delta
        # probe's two directional safety rules read it, because since #2611 the
        # commanded axis is a change and a repeat round therefore commands ~0
        # across every band it leaves alone, including one the applied graph
        # still boosts. Same route and same reason as the field above.
        self._measure_declared_transfer: Any = measure_declared_transfer
        # What ``_previous_graph_predicted_sum``'s INFO line last disclosed, so
        # repeated reads of one unchanged applied profile are one journal line
        # rather than one per read.
        self._previous_graph_disclosed: tuple[Any, ...] | None = None
        # #2291's "before" measurement. WRITTEN by stage 1, whose
        # ``PHASE_ENTRY_BASELINE`` capture reduces it (``_consume_entry_baseline``);
        # PASSED IN on stage 2, which never captures one and rehydrates it from
        # durable state so the post-apply verdict has something to compare
        # against. Same field, same two routes, same reason as
        # ``_measure_commanded_delta`` directly above.
        self._measure_entry_baseline: "EntryBaseline | None" = measure_entry_baseline
        # #2392's proposal identity, on exactly ``_measure_commanded_delta``'s
        # two routes and for exactly its reason. WRITTEN by the stage that
        # commits (:meth:`commit_intervention_proposal`); PASSED IN on the
        # stage that GRADES, which builds a fresh session from durable state
        # and has no candidate to re-derive one from. It must travel as the
        # fingerprint rather than as the ingredients: a proposal reassembled at
        # VERIFY from decimated priors would digest to a different value, and a
        # receipt naming a proposal that never existed is worse than one naming
        # the candidate honestly.
        #
        # ``""`` means "this session has proposed nothing yet", which is true
        # of every session before its commit and of a stage-2 re-arm whose
        # stage 1 predates #2392. The receipt reads it as the candidate,
        # never as a missing proposal.
        self._measure_proposal_fingerprint: str = str(measure_proposal_fingerprint or "")
        # WHICH commitment produced the committed candidate's delay (#2662),
        # travelling the same route as the fingerprint above and for the same
        # reason: it is written by the stage that FITS and read by the stage
        # that GRADES, and those are different sessions. ``""`` is "no
        # candidate has been committed yet", which is a third answer from
        # either "the prescription was committed" or "it was not" — a round
        # receipt that collapsed them would let a candidate the machinery never ran
        # be graded as one that did.
        self._measure_alignment_objective: str = str(measure_alignment_objective or "")
        # The proposal itself, for THIS session only — an
        # ``InterventionProposal``, a ``PlanRefusal``, or ``None`` before the
        # commit. Deliberately not persisted: the fingerprint above is the
        # durable identity, and a second copy of the payload would be a second
        # owner of the same fact.
        self._intervention_proposal: Any = None
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
        # VERIFY's own measured-vs-predicted curve pair and the band that
        # capture's own gate says it can be judged over, held so the post-apply
        # group's close can re-run the probe with the spatial arm without
        # re-analyzing a capture.
        #
        # The band is the gate disclosure's, never a floor this file derives
        # (#2521): one owner for "which bins is this capture a measurement in",
        # and ``None`` when that owner has no answer — which leaves the probe
        # unavailable rather than falling back to the raw grid edges.
        self._verify_tracking_curve: Any = None
        self._verify_trusted_band_hz: tuple[float, float] | None = None
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
        # which is the ONE piece of this session that runs off the relay
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
        # #1927. The FIRST usable VERIFY attempt of THIS session's own
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
        # #1873's repeatability discriminator: the PREVIOUS VERIFY attempt's
        # out-of-tolerance ``max_db_notch_excluded``, in this session only.
        #
        # A MISMATCH, not merely a grade. The claim the discriminator makes is
        # "this mismatch repeats", so only an attempt that WAS out of tolerance
        # can be the thing it repeats. An attempt graded inside tolerance clears
        # this back to ``None`` — otherwise a pass at 1.4 dB and a fail at
        # 1.55 dB, 0.15 dB apart and straddling the 1.5 dB threshold, would be
        # called deterministic when what they actually show is a speaker sitting
        # exactly on the line, where one more take genuinely can land under it.
        #
        # SESSION-SCOPED for the same ruling that made ``_verify_pilot_baseline``
        # above session-scoped (#1927 / owner 2026-07-31), and the argument is
        # stronger here: ``VERIFY_REPEAT_FLOOR_DB`` is a FIXED-MIC number, and a
        # verify-only re-arm is a fresh sitting whose microphone has plausibly
        # been re-placed. Rehydrating a previous session's grade would compare
        # two numbers across exactly the term that floor does not cover, and
        # would then call the result deterministic. Nothing writes this field
        # but the tracking comparison in ``_verify_verdict``, by construction
        # rather than by a check — so the discriminator can only ever fire on
        # two attempts of one sitting.
        #
        # Cleared to ``None`` by an attempt whose tracking number was absent or
        # non-numeric: that capture graded nothing, so it is not a mismatch
        # anything can agree WITH, and leaving a stale value standing would let
        # an older attempt supply the agreement. An attempt that early-returns
        # BEFORE the tracking comparison (locate, pilot level, integrity, gate
        # comparability, G3) never reaches it at all and leaves the last
        # mismatch standing — it neither refreshes nor invalidates it, because
        # it produced no grade to do either with. One intervening ungraded
        # capture is ~21 s of the drift the repeat-floor bench measured at
        # 0.0046 dB per repeat, which is inside the noise of a 0.2 dB window.
        self._verify_last_mismatch_max_db: float | None = None
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
    # carry. What is left here is this object's own reading of its session
    # state — which is exactly the part that could not move.

    def _check_priors(self) -> MeasurementPriors:
        return _priors.check_priors(fc_hz=self._fc_hz)

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
            applied_alignment=self._applied_alignment(),
            explicit_alignment_delay_us=(
                None if self._alignment_prescription is None
                else self._alignment_prescription.delay_us
            ),
            # The basin half of the same record. Translated by the record's own
            # ``polarity_sign`` rather than here, so the candidate's word and the
            # analysis frame's sign keep the single mapping they already share.
            explicit_alignment_polarity_sign=(
                None if self._alignment_prescription is None
                else self._alignment_prescription.polarity_sign
            ),
        )

    def _applied_alignment(self) -> AppliedAlignment | None:
        """What this speaker's applied graph plays, for the low-SNR refusal.

        The only prior that comes from the SPEAKER rather than the session, and
        the only one that reads a file, so it is read HERE and handed down: the
        analysis is a pure function of (program, WAV, priors) and must not
        acquire a side channel to Layer-A state (issue #2617's architecture
        constraint).

        Three answers, and the middle one is why this returns a wrapper rather
        than a bare float: ``None`` is "no graph is applied", an
        :class:`AppliedAlignment` carrying a delay is "hold this", and one
        carrying ``None`` is "a graph IS applied and its record does not say
        what it plays". The refusal commits no delay for the last two, but only
        the second may be disclosed as the design's own answer.

        **How reachable is that third answer?** Narrowly, and the honest bound
        is worth stating rather than assuming away.
        ``baseline_profile.persist_applied_baseline_profile`` REFUSES to write
        a profile without a ``recomposition_snapshot`` mapping, and
        ``build_baseline_profile_candidate`` always puts ``corrections`` in it,
        so no machine-written profile lands here unreadable. What can is a
        hand-edited or truncated state file, or a record from an era before
        those keys — and since #2617 routes through
        ``baseline_profile.profile_driver_corrections``, the older top-level
        mirror is read too, so an era gap is no longer one of them. It is a
        fail-safe with a disclosure rather than a path with a frequency.

        Never raises. A missing or unparseable state file is already ``None``
        from the reader; this also catches one that is valid JSON but
        structurally wrong (hand-edited), which must read as "nothing applied"
        rather than crash a MEASURE analysis over a fact one refusal path
        consults — the same posture ``resolve_applied_speaker_evidence`` takes
        on the same read.

        Read per MEASURE analysis rather than cached at session open, since
        that is the moment the answer has to be true: one small JSON read a
        few times per session, against a stale field that would silently
        outlive an out-of-band reconcile.
        """
        from jasper.active_speaker.baseline_profile import (
            load_applied_baseline_profile_state,
        )

        try:
            applied = load_applied_baseline_profile_state()
        except (OSError, TypeError, ValueError):
            return None
        if applied is None:
            return None
        return AppliedAlignment(
            delay_us=_planning.applied_profile_delay_us(
                applied,
                woofer_role=self._woofer.role,
                tweeter_role=self._tweeter.role,
            ),
        )

    def _applied_blend_correction(self) -> tuple[Mapping[str, Any], ...] | None:
        """The blend correction the post-apply capture rode, or ``None``.

        Read from the applied-profile SSOT rather than from a session field,
        for the reason ``applied_boosts`` is: the stage that GRADES a round is
        a fresh session holding no candidate, so a session read would be
        ``None`` on every shipped round and the incumbent would silently be
        assumed empty — which is the one assumption this quantity must never
        make (see ``profile_blend_correction``).

        Fails to ``None`` — refuse to prescribe — on every unreadable path,
        which is the opposite direction from ``applied_boosts``'s fail-closed
        ``True`` and correct for the same reason: there, not knowing must
        restore a graph; here, not knowing must leave the graph alone.

        **Through the STRICT reader** (``blend_filters_from_mapping``), which
        both panel lenses independently found missing here. The profile reader
        answers the structural question ("is there a list, and where"); the
        strict one answers the question this quantity actually turns on — is
        every entry a record this system wrote? Without it, a corrupt entry
        took one of two wrong paths and neither was ``no_incumbent``: a
        non-numeric ``freq`` RAISED out of ``evaluate_round`` into the
        coordinator's broad except, costing the round its receipt AND its
        restore; and garbage entries collapsed to ``()``, which claims the
        capture rode a flat graph — the unknown-vs-empty conflation this module
        forbids in four docstrings. ``blend_filters_from_mapping``'s
        None-on-unreadable IS the contract, so this is the reader that belongs
        on the path that establishes the incumbent.
        """
        from jasper.active_speaker.baseline_profile import (
            load_applied_baseline_profile_state,
            profile_blend_correction,
        )

        from .crossover_v2.blend_correction import blend_filters_from_mapping

        try:
            raw = profile_blend_correction(load_applied_baseline_profile_state())
        except (OSError, TypeError, ValueError):
            return None
        if raw is None:
            return None
        return blend_filters_from_mapping(list(raw))

    def _blend_prescription(self) -> tuple[Mapping[str, Any], ...]:
        """The blend correction the next candidate should carry (decision 10).

        Three sources, in this order, and the order below the first is the
        panel's second ruling:

        0. **An accepted BLEND prescription staged for THIS round** (A9), if the
           preparer took one out of the spool. The class is named because the
           spool carries two: a per-driver document staged for this round is
           taken by the same door and reaches the candidate's ``linearization``
           map instead, and never this list. It supersedes the series'
           instruction for exactly one round, and that precedence is the whole
           point of staging one: an operator who read the round's evidence and
           wrote a correction is answering the same question decision 10's
           solver answered, with more of the evidence in front of them, and a
           deterministic instruction that quietly won would make the staging
           step a no-op nobody could see. It cannot persist past this round —
           :func:`~.crossover_v2.prescription_spool.take_staged_prescription`
           consumed the document before this session was built — so the series
           resumes on its own at the next round with no state to unwind.

        1. **The series' instruction**, if the previous round left one —
           ``SeriesPosition.previous_blend_correction``. A round that KEEPS its
           graph banks the total it solved, and that supersedes everything.
        2. **What the speaker is already playing**, otherwise. ``None`` from
           the series means there is no instruction: no previous round, a
           receipt from before decision 10, an unreadable one, or a round that
           RESTORED. Reverting to nothing there would be wrong in every one of
           those cases — most sharply after a restore, where the round threw
           away the graph its prescription was derived through, and after a
           fresh series on a speaker that already carries an adopted
           correction.

        The fallback reads the same applied-profile SSOT, through the same
        strict reader, that ``_applied_blend_correction`` uses to establish the
        incumbent — so "what we hold" and "what we measured through" can never
        be two different answers derived two different ways. An unreadable
        profile yields ``()``: at that point nothing about the applied graph
        can be established, and the candidate build is deriving the whole graph
        from that same unreadable profile anyway.
        """
        from .crossover_v2.blend_prescription import (
            BLEND_CANDIDATE_FIELD,
            blend_prescription_to_candidate_fields,
        )

        if self._prescribed_blend is not None:
            # Through the route rather than off the object's ``filters``, so the
            # promise that a boost can never populate this field is a property
            # of every path into it. The seam re-asks the route for exactly this
            # reason (see ``blend_prescription_to_candidate_fields``), and a
            # caller that reached past it here would be the second entry point
            # that makes the promise untrue.
            fields = blend_prescription_to_candidate_fields(self._prescribed_blend)
            return tuple(fields[BLEND_CANDIDATE_FIELD])
        instruction = (
            None if self._series_position is None
            else self._series_position.previous_blend_correction
        )
        if instruction is not None:
            return tuple(instruction)
        return self._applied_blend_correction() or ()

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

    # --- journey delegation --------------------------------------------------

    @property
    def post_apply_verifies(self) -> bool:
        """Will this session's correction be MEASURED after it is applied?

        The boost-permission evidence gate (passed into the planner request by
        ``crossover_v2.planning.plan_for_candidate``): a round nobody will verify may
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
    def measure_declared_transfer(self) -> Any:
        """The applied graph's own transfer against the raw crossover (#2614).

        The delta probe's STATE axis, on exactly
        :attr:`measure_commanded_delta`'s two routes and for its reason: the
        stage that grades is not the stage that fit, so durable state is the
        only channel it has.
        """
        return self._measure_declared_transfer

    @property
    def measure_proposal_fingerprint(self) -> str:
        """This round's :class:`InterventionProposal` identity, or ``""``.

        Read by the host's durable persist and handed back to the stage that
        grades the round, exactly like :attr:`measure_commanded_delta` (#2392).
        ``""`` is "nothing proposed", never "the proposal is unknown".
        """
        return self._measure_proposal_fingerprint

    @property
    def measure_alignment_objective(self) -> str:
        """Which commitment produced this round's delay, or ``""`` (#2662).

        Read by the host's durable persist and handed back to the stage that
        grades the round, exactly like :attr:`measure_proposal_fingerprint`.
        Its one consumer is the round receipt, which pairs it with the
        prescription so an adopted candidate's record says not only what was
        ASKED for but whether the machinery actually committed it — the
        difference between a candidate that ran and one that silently did not.
        """
        return self._measure_alignment_objective

    @property
    def alignment_prescription_record(self) -> dict[str, Any] | None:
        """This session's delay prescription as the receipt banks it (#2662).

        Read by the host's durable persist and handed back to the stage that
        grades the round, exactly like :attr:`measure_proposal_fingerprint`:
        the grading stage builds a fresh session and holds no candidate, so
        durable state is the only channel a stage-1 fact has.

        ``None`` is "no prescription was made" — the automatic path — and is
        what an ordinary round banks. The receipt's absence of a provenance
        block and its presence therefore mean exactly one thing each.
        """
        if self._alignment_prescription is None:
            return None
        return self._alignment_prescription.to_dict()

    @property
    def topology_prescription_record(self) -> dict[str, Any] | None:
        """This session's crossover pin as the receipt banks it.

        The sibling of :attr:`alignment_prescription_record` in every respect,
        including the ``None``-means-the-automatic-path rule, so a series read
        back later can tell a round whose corner and order were PINNED from one
        that ran the speaker's commissioned crossover — which is the whole
        attribution a pre-registered Fc/slope tournament is made of.

        Exactly ``to_dict()``, with nothing added, for
        :attr:`blend_prescription_record`'s reason: the record has to survive a
        round trip through
        :func:`~.crossover_v2.topology_prescription.topology_prescription_from_mapping`
        for the grading stage to rehydrate it, and that reader refuses an
        unknown field rather than ignoring it — so one extra key would make the
        whole record unreadable.
        """
        if self._topology_prescription is None:
            return None
        return self._topology_prescription.to_dict()

    @property
    def blend_prescription_record(self) -> dict[str, Any] | None:
        """This session's blend prescription as the receipt banks it (A9).

        The sibling of :attr:`alignment_prescription_record` in every respect,
        including the ``None``-means-the-automatic-path rule — so a series read
        back later can tell a round whose blend correction was PRESCRIBED from
        one whose was solved, which is the attribution the whole prescriber loop
        exists to make comparable.

        **Exactly ``to_dict()``, with nothing added.** An earlier shape folded
        the document digest in beside it and that was a real defect, not a
        stylistic one: the durable record has to survive a round trip through
        :func:`~.crossover_v2.blend_prescription.blend_prescription_from_mapping`
        so stage 2 can rehydrate it, and that reader refuses an unknown field
        rather than ignoring it — by design, because a misspelled ``filters``
        that was quietly dropped would leave a receipt claiming provenance it
        does not have. One extra key made the whole record unreadable. The
        digest travels as :attr:`blend_prescription_sha256` instead.
        """
        if self._prescribed_blend is None:
            return None
        return self._prescribed_blend.to_dict()

    @property
    def blend_prescription_sha256(self) -> str:
        """The digest of the document this round's prescription came from (A9).

        Its own value rather than a field inside the record above, on
        :attr:`measure_alignment_objective`'s rule and for the same reason: the
        prescription is WHAT was asked for and the digest names the DOCUMENT
        that asked, they are two facts, and nesting one in the other is what
        broke the record's round trip. ``""`` when this round prescribed
        nothing, which is what the record's own ``None`` already says.
        """
        return self._prescribed_blend_sha256

    @property
    def last_intervention_proposal(self) -> Any:
        """This session's proposal, its refusal, or ``None`` before the commit.

        In-memory and session-scoped: the durable identity is
        :attr:`measure_proposal_fingerprint`. Exposed because a refusal that
        only ever reached a log line is how the Phase-1 facade became
        write-only in the first place — a test, and any future review surface,
        can now ask the session what it proposed.
        """
        return self._intervention_proposal

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
    def delta_probe(self) -> DeltaProbeMap | None:
        """This session's realized-vs-commanded verdict (PR-L5), or ``None``
        when no post-apply capture has been consumed yet."""
        return self._delta_probe

    @property
    def verify_tracking_curve(self) -> Any:
        """The VERIFY capture's ``(freqs_hz, measured_db, predicted_db)``, or
        ``None`` (#2522).

        The pair :meth:`_run_delta_probe` graded, exposed so the host can
        persist it beside the priors it was graded against. Before that, a
        disputed verdict could only be re-examined by measuring the speaker
        again: the commanded and predicted curves were durable and the MEASURED
        one was not, so no grading change could be tested against the evidence
        that produced the complaint.

        Both curves travel, not just the measured one, because the graded error
        is their difference (``realized − commanded == measured − predicted``)
        and reconstructing ``predicted_db`` from the persisted ``predicted_sum``
        would mean re-running the analysis's own interpolation and smoothing on
        an already-decimated curve — a second derivation of a number this one
        can simply carry.

        Returned as held, uncopied: the arrays are read-only evidence and the
        host serializes them immediately. The band and the offset the probe used
        are NOT restated here — they are already on the persisted
        ``delta_probe`` record (``requested_band_hz`` / ``expected_offset_db``),
        and a second copy is a second thing to keep true.
        """
        return self._verify_tracking_curve

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
        class, so a caller cannot reach back into the session's state.
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

        Selects WHICH pair the rule is applied to; the rule itself is
        :func:`~jasper.active_speaker.crossover_v2.admission.pilot_heard_for`.
        With ``slot``, the pair owned by that capture position; without it, the
        global pair used by persisted terminal state, assembled here from the
        ``_last_failure_*`` fields. A global read with no failure code recorded
        has no pair at all — the same answer the pairing rule gives a code that
        does not match, and the reason this passes ``None`` rather than a
        triple with a ``None`` head.
        """
        if slot is not None:
            paired = self._last_pilot_evidence.get(slot)
        elif self._last_failure_code is None:
            paired = None
        else:
            paired = (
                self._last_failure_code, self._last_failure_pilot_heard, None,
            )
        return _admission.pilot_heard_for(code, paired)

    def _reflection_measured_for(
        self, code: str | None, *, slot: str,
    ) -> bool | None:
        """The gate discriminator recorded with ``code`` at ``slot``."""
        return _admission.reflection_measured_for(
            code, self._last_pilot_evidence.get(slot)
        )

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
        # R16: the lateral walk has its own table and its own length. Same
        # front-loading rule, same builder enumeration order.
        table = (
            self._lateral_prompts if phase == PHASE_LATERAL
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

    def note_restore_observed(self) -> None:
        """The restore-observed host event — disarms the VERIFY hold (#2616).

        :meth:`note_apply_complete`'s mirror, and the same split of duties: the
        journey owns the flag and says nothing, this says it. The host calls it
        when the DURABLE state shows a restore this in-memory session did not
        see — the delta probe's rollback seam and the round's adoption restore
        both clear the durable flag through ``observe_restore``, which holds no
        conductor and so could not tell the owner.
        """
        self._journey.mark_restored()
        log_event(
            logger, "correction.crossover_v2_restore_observed",
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
    ) -> "CrossoverV2Session":
        """Rebuild a session, applying the §5.6 session-binding rule.

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

        The DECISION is
        :func:`~jasper.active_speaker.crossover_v2.admission.assess_begin`,
        which owns the bounded-retry ruling and its reasons. What stays here is
        every irreversible half: the seam call and its guard, the household
        sentence rendered from ``REASON_REGISTRY``, the ``_last_failure_*``
        stamping, the two ledger mutations, the armed-capture identity, and the
        journal line.

        **Since the two-stage split (work order D10) no shipped session reaches
        the VERIFY hold**: stage 1 has no VERIFY index at all, and stage 2's
        session is constructed ``applied=True``, so ``_apply_observed``
        short-circuits before either the deferral or the ``apply_failed``
        refusal. The machinery is retained rather than deleted — no new design
        may depend on it, and a session built without a prior apply still
        gets the honest hold.
        """
        phase = self._phase_of_index(index)
        slot = self._slot_of_index(index)
        # READ, never create: a begin held at the VERIFY anchor must not leave a
        # meter behind for a capture that never started. The entry is created
        # below, on the admitted path, exactly where the charge happens.
        ledger = self._slot_attempts.get(slot)

        def apply_failure_code() -> str:
            """The apply seam's TERMINAL reason, or ``""`` — guarded here.

            A local rather than an argument because the guard belongs to the
            seam, and the seam is the session's: an apply binding that raises
            must read as "no named failure" and fall through to the honest
            hold, never take down the begin. Asked only on the hold branch, so
            an ordinary begin still makes no seam call.
            """
            try:
                return str(self._seams.apply_failed() or "")
            except (OSError, RuntimeError, ValueError):
                return ""

        decision = _admission.assess_begin(
            verify_hold=phase == PHASE_VERIFY and not self._apply_observed(),
            apply_failure_code=apply_failure_code,
            ledger=ledger,
            last_reason=self._last_reason.get(slot),
            non_retriable=NON_RETRIABLE_CODES,
            default_code=REASON_LOCATE_FAILED,
            geometry_locked_code=REASON_CLOUD_GEOMETRY_LOCKED,
        )
        if decision.kind == _admission.REFUSE_APPLY_FAILED:
            self._last_failure_code = decision.code
            # The apply seam's own verdict — no capture ran, so there is
            # no pilot evidence to pair with it (#2085). Written rather
            # than left alone so a previous capture's evidence cannot
            # trail into this failure's copy.
            self._last_failure_pilot_heard = None
            spec = REASON_REGISTRY.get(decision.code)
            message = (
                reason_message(decision.code, spec) if spec else decision.code
            )
            self.relay_published_refusal = True
            raise CaptureBeginRefused(decision.code, message)
        if decision.kind == _admission.DEFER_AWAITING_APPLY:
            raise CaptureBeginDeferred("awaiting_apply", VERIFY_ANCHOR_HOLD_MESSAGE)
        if decision.kind == _admission.REFUSE_NON_RETRIABLE:
            spec = REASON_REGISTRY[decision.code]
            self.relay_published_refusal = True
            raise CaptureBeginRefused(
                spec.code,
                reason_message(
                    spec.code,
                    spec,
                    pilot_heard=self._pilot_heard_for(decision.code, slot=slot),
                ),
            )
        if decision.kind == _admission.REFUSE_EXTRAS_SPENT:
            # Only reachable with a meter in hand — the decision is derived from
            # this ledger's own spent extras.
            assert ledger is not None
            code = decision.code
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
        if decision.kind != _admission.ADMIT:
            # One explicit arm per :data:`admission.DECISION_KINDS` member above,
            # and this fallback is LOUD rather than a catch-all that admits. A
            # kind arriving here unhandled is a wiring defect — a new decision
            # shipped without an arm — and on a BEGIN gate the silent direction
            # is the dangerous one: falling through starts a capture and charges
            # a try nobody decided to spend. It refuses under the most
            # conservative code available, the same choice
            # ``_screen_refusal_code`` makes for an unmapped screen kind.
            log_event(
                logger, "correction.crossover_v2_begin_decision_kind_unmapped",
                level=logging.ERROR, session_id=self.session_id,
                phase=phase, index=index, kind=str(decision.kind),
            )
            self.relay_published_refusal = True
            raise CaptureBeginRefused(
                REASON_LOCATE_FAILED,
                reason_message(
                    REASON_LOCATE_FAILED, REASON_REGISTRY[REASON_LOCATE_FAILED],
                ),
            )
        ledger = self._slot_attempts.setdefault(slot, SlotAttempts())
        if decision.spends_extra:
            try:
                ledger.spend(decision.initiator)
            except _admission.AttemptOverspendError as exc:
                # The flow's own error type is what every caller (and the relay
                # runner above them) already handles; the ledger is pure and has
                # no business knowing it — the same translation
                # ``program_for_phase`` makes for the program selector.
                raise CrossoverV2FlowError(str(exc)) from exc
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

    @staticmethod
    def _extras_spent_message(
        ledger: SlotAttempts, *, diagnosis: str, outcome: str,
    ) -> str:
        """The household sentence for a position whose extras are gone."""
        return _admission.extras_spent_message(
            ledger, diagnosis=diagnosis, outcome=outcome,
        )

    def _spent_slot_outcome(self, phase: str, index: int) -> str:
        """The state after an exhausted slot, derived from session state.

        The three facts the sentence turns on are read here — the phase's
        group-ness, the positions this session gave up on, and the ones still
        represented by an earlier take — and stated to
        :func:`~jasper.active_speaker.crossover_v2.admission.spent_slot_outcome`.
        """
        is_group = self._journey.plan.is_group(phase)
        return _admission.spent_slot_outcome(
            is_group=is_group,
            index=index,
            unresolved=self._group_unresolved[phase] if is_group else (),
            retained=self._retained_group_indexes(phase) if is_group else (),
        )

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
        session play", which the host's duration budgeting, the identity
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
                cloud=self._cloud_program,
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
        # phone-reported setup/device, and the session's declared geometry
        # rides along so the parallax correction reaches the analysis.
        # ``phase=phase`` (issue #1855): the flow's OWN phase, threaded
        # explicitly because ``program.phase`` is not a reliable stand-in —
        # every cloud position plays ``self._cloud_program`` and so always
        # carries ``program.phase == "verify"`` (see ``program_for_phase``).
        analysis = self._seams.analyze(
            program, result, priors, self._geometry, phase=phase,
        )
        if phase == PHASE_CHECK:
            verdict = self._consume_check(analysis)
        elif phase == PHASE_MEASURE:
            verdict = self._consume_measure(analysis)
        elif phase == PHASE_LATERAL:
            verdict = self._consume_lateral_pose(index, attempt, analysis, result)
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
            # own attribution fallback, and
            # :func:`~jasper.active_speaker.crossover_v2.admission.extra_initiator`
            # at the next begin (a geometry rung is only identifiable from the
            # rejection that produced it).
            self._last_reason[slot] = verdict.code
            self._last_pilot_evidence[slot] = (
                verdict.code,
                verdict.pilot_heard,
                verdict.reflection_measured,
            )
            # SETTLE HERE, not at the next begin (owner ruling #2086 item 3).
            # If this rejection closed the slot — its last extra spent, or a
            # condition no further take can clear — the position is decided now:
            # dropped and the group advanced, or the honest end named. So the
            # household is never shown a retry screen whose button only leads to
            # a pre-play refusal.
            #
            # UNLESS the verdict already ended the set on its own finding
            # (#1873's deterministic mismatch is the one that can). The settle
            # would then describe the same ending twice and keep the weaker
            # account. BOTH of its rungs would: ``_terminal_spent_verdict``
            # replaces the reason with the exhaustion sentence, whose "still
            # could not get a clean read" is simply false about captures that
            # were clean and agreed; and the condition rung — whose codes
            # #1873's own finding joins — overwrites the verdict's
            # ``terminal_outcome`` with ``condition_not_retriable``, losing the
            # specific token the page and the journal were given. Same call
            # ``_settled_group_verdict`` already makes for a close-time product
            # gate — publish the specific finding, not the settle's summary of
            # it. Inert for every path that predates this: the only other
            # writers of ``terminal`` are reached FROM this method.
            if verdict.payload.get("terminal") is not True:
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
        """Act on a rejection the next begin would refuse — attribute, then degrade.

        The ladder — which outcomes exist, in which order, why the condition is
        asked about before the meter, and why a retained earlier take has to be
        asked about before the floor is counted — belongs to
        :func:`~jasper.active_speaker.crossover_v2.admission.settle_spent_slot`
        and its group half. What stays here is what each outcome CAUSES:
        rendering the diagnosis the household reads, the journal line, the
        ``_group_unresolved`` attribution, the group close under
        :attr:`_close_lock`, and building the ``PhaseVerdict`` the phone reads.

        The ladder's two halves bracket the lock exactly, because its group
        rungs read facts that are only true while it is held.

        A group phase never reaches a terminal outcome from EXHAUSTION with
        anything left to measure, which is why the 2026-08-03 shape — a pre-play
        refusal at a cloud position while the screen read "step 6, one last
        time" — is unreachable from ordinary retries. The condition rung does
        end a phase outright, positions left or not, and that is its point: it
        fires only for codes the registry marks unclearable, and continuing a
        group past one would be continuing past a fault the flow just named.
        """
        ledger = self._slot_attempts.get(slot)
        kind = _admission.settle_spent_slot(
            ledger=ledger,
            is_group=lambda: self._journey.plan.is_group(phase),
            code=verdict.code,
            non_retriable=NON_RETRIABLE_CODES,
        )
        if kind == _admission.SETTLE_RETRY_REMAINS:
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
        if kind == _admission.SETTLE_CONDITION_NOT_RETRIABLE:
            # The condition rung — nothing was spent, so nothing here reads the
            # meter, and the copy stays the code's OWN sentence. It already
            # names the one action that helps, and the exhaustion sentence
            # ("JTS measured this spot N times") would be false about a
            # position that was rejected on its first take.
            self._log_condition_settled(phase, index, observed, kind, diagnosis)
            return replace(
                verdict,
                payload={
                    **verdict.payload,
                    # Same runner/page contract the spent terminals use:
                    # publish this capture_result, then finish rather than wait
                    # for a next begin ``authorize_begin`` will refuse.
                    "terminal": True,
                    "terminal_outcome": kind,
                },
            )
        # Past this rung the meter is empty — the ladder answers
        # ``SETTLE_RETRY_REMAINS`` for a slot with no ledger — which is what
        # lets the terminal builder below index ``_slot_attempts`` directly.
        if kind != _admission.SETTLE_GROUP_CLOSE_REQUIRED:
            # One arm per :data:`admission.SETTLE_KINDS` member, and this is
            # both ``SETTLE_PHASE_CANNOT_PROCEED``'s arm and the LOUD fallback
            # for a kind nobody wired. Unlike the begin gate, the dangerous
            # direction here is the PERMISSIVE one: returning the verdict
            # unchanged hands back a retry screen whose button leads to a
            # pre-play refusal — the exact 2026-08-03 shape this ladder exists
            # to make unreachable. So an unknown kind ends the phase honestly,
            # with the diagnosis and count it already has in hand.
            if kind != _admission.SETTLE_PHASE_CANNOT_PROCEED:
                log_event(
                    logger, "correction.crossover_v2_settle_kind_unmapped",
                    level=logging.ERROR, session_id=self.session_id,
                    phase=phase, index=index, kind=str(kind),
                )
                kind = _admission.SETTLE_PHASE_CANNOT_PROCEED
            self._log_slot_spent(
                phase, index, observed, kind,
                diagnosis=diagnosis,
                pilot_heard=verdict.pilot_heard,
                reflection_measured=verdict.reflection_measured,
            )
            return self._terminal_spent_verdict(
                phase, index, slot, verdict,
                diagnosis=diagnosis,
                outcome=kind,
            )
        with self._close_lock:
            retained = self._retained_group_indexes(phase)
            kind = _admission.settle_group_position(
                index=index,
                retained=retained,
                floor=self._group_position_floor(phase),
                unwalked_count=lambda: len(
                    self._journey.unresolved_in_group(phase, excluding=index)
                ),
            )
            if kind == _admission.SETTLE_KEPT_EARLIER_TAKE:
                self._log_slot_spent(
                    phase, index, observed, kind,
                    diagnosis=diagnosis,
                    pilot_heard=verdict.pilot_heard,
                    reflection_measured=verdict.reflection_measured,
                )
                return self._settled_group_verdict(
                    phase, index, {"kept_earlier_take": True}
                )
            if kind == _admission.SETTLE_POSITION_UNRESOLVED:
                self._group_unresolved[phase][index] = observed
                self._log_slot_spent(
                    phase, index, observed, kind,
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
            # ``SETTLE_BELOW_POSITION_FLOOR``'s arm, and the group half's LOUD
            # fallback, degrading in the same conservative direction as above:
            # a group that cannot be shown to still reach its floor ends
            # honestly rather than advancing on an answer nobody wired.
            if kind != _admission.SETTLE_BELOW_POSITION_FLOOR:
                log_event(
                    logger, "correction.crossover_v2_settle_kind_unmapped",
                    level=logging.ERROR, session_id=self.session_id,
                    phase=phase, index=index, kind=str(kind),
                )
                kind = _admission.SETTLE_BELOW_POSITION_FLOOR
            self._log_slot_spent(
                phase, index, observed, kind,
                diagnosis=diagnosis,
                pilot_heard=verdict.pilot_heard,
                reflection_measured=verdict.reflection_measured,
            )
            return self._terminal_spent_verdict(
                phase, index, slot, verdict,
                diagnosis=diagnosis,
                outcome=kind,
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
            # R16: a dropped LAST pose must still close the walk, so the journal
            # records that the walk ENDED rather than leaving its absence to be
            # read as a walk that never finished. Nothing is published either
            # way — MEASURE already published the round's candidate, and the
            # anchor's coefficients were never the poses' to withhold. Same
            # "settled looks accepted on the wire" contract.
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

    def _log_condition_settled(
        self, phase: str, index: int, observed: str, outcome: str, diagnosis: str,
    ) -> None:
        """The journal line for a slot closed by its CONDITION, not its meter.

        Its own event rather than ``_log_slot_spent``: that line is named
        ``position_attempts_spent`` and carries ``extra_allowed``, and a
        rejection settled on the first take spent nothing — a support read that
        found it there would count a session's exhausted positions wrong.
        """
        log_event(
            logger, "correction.crossover_v2_position_not_retriable",
            level=logging.WARNING,
            session_id=self.session_id, phase=phase, index=index,
            observed=observed, outcome=outcome, diagnosis=diagnosis,
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
        """CHECK's verdict: the ladder's answer, then the accept-side banking.

        The rungs — which screens run, in which order, and why "too quiet" has
        to be asked before the microphone is blamed — belong to
        :func:`~jasper.active_speaker.crossover_v2.capture_dispatch.check_screens`.
        What stays here is what an acceptance CAUSES: banking the solved gains
        and this capture's ambient report, composing the MEASURE program from
        them, and publishing CHECK evidence across the seam.
        """
        gain_plan = analysis.gain_plan
        kind = _dispatch.check_screens(
            _dispatch.CheckScreens(
                stimulus_located=_stimulus_locate_ok(analysis),
                anchor_ambiguous=analysis.anchor_ambiguous,
                channel_map_ok=analysis.channel_map_ok,
                pilot_snr_ok=analysis.pilot_snr_ok,
                linearity_ok=analysis.linearity_ok,
                gain_plan_present=gain_plan is not None,
                # Read only when a plan exists; ``False`` is the value the
                # ladder is documented to ignore in that case, never a claim
                # that an absent solve cleared its floor.
                gain_plan_snr_floor_ok=(
                    bool(gain_plan.snr_floor_ok) if gain_plan is not None else False
                ),
            )
        )
        if kind is not None:
            return PhaseVerdict(False, _screen_refusal_code(kind))
        # mypy: the ladder's final rung refuses an absent plan, so reaching
        # here proves one exists — restated because the checker cannot see it.
        assert gain_plan is not None
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
        screen = _dispatch.measure_screens(
            _dispatch.MeasureScreens(
                stimulus_located=_stimulus_locate_ok(analysis),
                pilot_snr_ok=analysis.pilot_snr_ok,
                sweep_locate_confidence_ok=_sweep_locate_confidence_ok(analysis),
                glitch_detected=bool(analysis.glitch_detected),
                # A CALLABLE, and the rung whose eager resolution would be
                # OBSERVABLE: ``program_for_phase`` RAISES when MEASURE has no
                # composed program, and the shipped ladder never reaches this
                # rung on a capture the three above it already refused. (It is
                # also the already type-narrowed accessor — the bare
                # ``self._measure_program`` is ``ExcitationProgram | None``.)
                sweep_schedule_ok=lambda: _sweep_schedule_ok(
                    analysis, self.program_for_phase(PHASE_MEASURE).sample_rate_hz
                ),
                any_sweep_clipped=_any_sweep_clipped(analysis),
                linearity_ok=analysis.linearity_ok,
                alignment_present=analysis.alignment is not None,
                alignment_status_ok=(
                    analysis.alignment is not None
                    and analysis.alignment.status == ALIGNMENT_OK
                ),
                # The trust gate (owner ruling 2026-07-20): GCC's capture/seed
                # confidence, not confidence in the refined delay. ``True`` with
                # no estimate at all, which is what makes the trims-only path
                # skip all three alignment rungs rather than fail them.
                alignment_confidence_ok=(
                    analysis.alignment is None
                    or analysis.alignment.confidence
                    >= ALIGNMENT_CONFIDENCE_TRUST_FLOOR
                ),
                # Also a callable: the physical backstop (Fix 3) is asked ONLY
                # of an estimate that already cleared the two rungs above, and
                # it reads the preset's declared search bound.
                delay_physically_plausible=lambda: (
                    analysis.alignment is None
                    or alignment_delay_plausible(
                        analysis.alignment.delay_us, self._preset
                    )
                ),
            ),
            clip_retry_backoff_db=CLIP_RETRY_BACKOFF_DB,
        )
        if screen is not None:
            if screen.guard:
                self._last_measure_guard = screen.guard
            if screen.rearm:
                self._rearm_measure_after_transient(
                    extra_backoff_db=screen.rearm_backoff_db
                )
            return PhaseVerdict(False, _screen_refusal_code(screen.kind))
        # Measurement-honesty DISCLOSURE G1 (2026-07-22; owner ruling
        # 2026-08-03, issue #2087). **This does not refuse.** The capture is
        # ACCEPTED and carries an honest reservation to the household instead
        # of sending them to move a microphone that was never the problem, so
        # the reservation changes what the household is TOLD and nothing about
        # what is built, fitted, gated, or applied. Every accountability gate
        # below still runs unchanged on this candidate, which is what keeps
        # "proceed" from meaning "unchecked" — one of the three still REFUSES
        # (realized-level); level-frame and predicted-improvement now bank.
        #
        # The candidate presence check is the caller's because reading the
        # ripple off it requires it; the alignment half of the shipped skip
        # belongs to the predicate. See
        # :func:`~jasper.active_speaker.crossover_v2.capture_dispatch.ripple_reservation_due`.
        candidate = analysis.candidate
        if candidate is not None and _dispatch.ripple_reservation_due(
            predicted_ripple_db=candidate.predicted_ripple_db,
            has_alignment=analysis.alignment is not None,
            disclosure_threshold_db=MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB,
        ):
            self._note_ripple_reservation(candidate.predicted_ripple_db)
        if analysis.candidate is None:
            # Fail FAST, at the capture that produced the unusable analysis.
            # Until the 2026-07-27 timing move this raise happened one call
            # deeper and one line later (``_build_candidate``'s own identical
            # check, still there — and kept deliberately, see its own note) —
            # same exception, same message, same phase, so the host's
            # ``program_unplayable`` mapping is unchanged. (That mapping was
            # named ``internal_error`` here until #2291 Phase 5c-iii; the whole
            # program/admission/flow family was folded into one code until
            # #1820 defect 4 split it, and this comment had not caught up.)
            # Hoisting it is what keeps that behaviour at MEASURE:
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
        # A lateral walk is NOT a deferring shape. It was one while its close
        # adjudicated a corner; an operator's staged angle walk produces
        # evidence for the forward model and its close publishes nothing the fit
        # waits for, so MEASURE is still the last capture the proposal depends
        # on and fitting here ages nothing.
        if PHASE_CLOUD_MEASURE in self._journey.plan.phases:
            self._measure_analysis = analysis
            return PhaseVerdict(True, payload={"measurement_phase": PHASE_MEASURE})
        # The no-deferral shape, and the SHIPPED stage-1 branch: with the cloud
        # flag off ``prepare_v2_session`` builds ``CHECK, MEASURE,
        # ENTRY_BASELINE``, so the configured Fc's candidate is published right
        # here.
        #
        # #2291's entry baseline follows MEASURE without joining the condition
        # above, and deliberately: it is not the fit's input — it is the "before"
        # the round grades its "after" against, screened and retained by
        # ``_consume_entry_baseline`` alone. Deferring the fit past it would buy
        # no evidence and would only age the proposal. The bold rule still
        # holds; what it protects against is a group whose captures the fit
        # would otherwise predate.
        #
        # It keeps folding the candidate payload into this verdict, but note
        # that since flow-simplification §2.6 moved the trigger onto the confirm
        # seam, the host no longer reads ``auto_apply`` off a capture verdict —
        # the apply is still the household's explicit POST, on this branch
        # exactly as on the deferring ones.
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
        self, index: int, attempt: int, analysis: ProgramAnalysis, result: Any,
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
            curves=tuple(curves),
        )
        log_event(
            logger, "correction.crossover_v2_lateral_pose",
            session_id=self.session_id, pose_id=pose.pose_id, index=index,
            attempt=attempt, offset_cm=pose.offset_cm, position_role=pose.role,
            at_mark=pose.at_mark, curves=len(pose.curves),
        )
        # Outside the lock below, unlike the cloud's in-lock retention: that
        # one writes ``_group_position_meta``, which the close reads; this
        # writes nothing any close reads.
        self._retain_lateral_pose(pose, prompt, result)
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

    def _retain_lateral_pose(
        self, pose: LateralPose, prompt: CloudPositionPrompt, result: Any,
    ) -> None:
        """Bank one accepted pose's WAV + sidecar. Fail-soft; never a gate.

        ``prompt`` must be :meth:`_prompt_shown_for`'s result — the sidecar's
        bearing names where the operator was sent, not where the table wanted.
        Same shape for both consumers, so their poses stay comparable.
        """
        self._hand_to_retention(
            pose.pose_id, PHASE_LATERAL, result,
            _spatial.lateral_pose_record(
                pose,
                position_deg=position_angle_deg(prompt),
                lateral_consumer=self._lateral_consumer,
                session_id=self.session_id,
                wav_sha256=_capture_wav_sha256(result),
            ),
        )

    def _close_lateral_walk(self) -> dict[str, Any]:
        """Record that the walk finished. Publishes nothing.

        The poses are evidence for the forward model — an operator's staged
        angle walk is the only shape that runs them — and that evidence is read
        off the banked round rather than off this close. So the close has no
        candidate to fold into its verdict: MEASURE already published the
        configured corner's, and §4.4 is explicit that side evidence may not
        become the fit target.

        A walk where nothing was captured still closes: the anchor already owns
        the coefficients (see :meth:`_group_position_floor`).

        Returns ``{}`` at both routes in (an accepted last pose, a settled one),
        so a third route cannot come to a different conclusion about whether
        there is anything to add.
        """
        log_event(
            logger, "correction.crossover_v2_lateral_walk_closed",
            session_id=self.session_id,
            consumer=self._lateral_consumer,
            planned=len(self._journey.plan.group_offsets(PHASE_LATERAL)),
            captured=len(self._lateral_poses),
            mark_return_drift_db=self.lateral_mark_return_drift_db(),
        )
        return {}

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
        # All SEVEN screens stated, though this ladder reads three. Of the three
        # sweep-domain predicates, TWO are vacuous here and one is not: a cloud
        # position plays the summed VERIFY program, so its stimulus locations
        # are ``KIND_SUMMED_SWEEP`` plus the leading ``KIND_PILOT`` pair and
        # never ``KIND_SWEEP`` — which is the whole domain of
        # ``_sweep_locate_confidence_ok`` and ``_sweep_schedule_ok``, so both
        # answer true over an empty set. ``_any_sweep_clipped`` filters on
        # ``STIMULUS_KINDS``, which CONTAINS both kinds a cloud position
        # produces, so it is a live verdict that this ladder simply does not
        # read. Stating it anyway is the point rather than an accident: a fact
        # about the capture is the caller's to state, and the day a rung reads
        # it the answer is already the true one. See
        # :class:`~jasper.active_speaker.crossover_v2.spatial.CaptureScreens`
        # for what a permissive default would cost instead.
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
            gate_moved_rms_db=_gate_moved_rms_db(position.response),
            gate_reflection_delay_ms=_gate_reflection_delay_ms(position.response),
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
        self._hand_to_retention(position.position_id, phase, result, metadata)

    def _hand_to_retention(
        self, take_id: str, phase: str, result: Any, metadata: Mapping[str, Any],
    ) -> bool:
        """Hand one take's bytes to the evidence seam; return whether it stored.

        The ONE fail-soft boundary for all three retained kinds (cloud position,
        lateral pose, entry baseline). No seam bound is not an error, and a
        bound seam that raises costs a WARN: a full disk must not turn a good
        capture into a retake.
        """
        if self._seams.retain_position is None:
            return False
        try:
            self._seams.retain_position(take_id, result, metadata)
        except (OSError, RuntimeError, TypeError, ValueError):
            log_event(
                logger, "correction.crossover_v2_position_retain_failed",
                level=logging.WARNING,
                session_id=self.session_id, phase=phase,
                position_id=take_id, exc_info=True,
            )
            return False
        return True

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
        if retake is not None and tier_is_externally_positioned(self._tier):
            # REFUSE rather than prompt for a move this operator cannot make
            # (owner ruling: refuse, don't mislead). Both rungs of
            # ``CLOUD_GEOMETRY_RETRY_PROMPTS`` are out of an external
            # positioner's reach — rung 1 is 75 cm off the mark, past every
            # pose in the walk, and rung 2 adds a move ABOVE mark height, the
            # exact axis this tier excludes by construction.
            #
            # Prompting anyway did three dishonest things at once: it asked a
            # driver for a pose it cannot reach, it recorded the un-made pose's
            # 75 cm offset as the position's durable evidence, and the position
            # gate published the PREVIOUS entry's stale angle as the target. The
            # retry budget is deliberately NOT spent and no take is dropped —
            # nothing here is a retry, so the group keeps the evidence it
            # legitimately has for whatever the session does with it next.
            log_event(
                logger,
                "correction.crossover_v2_geometry_retake_unreachable",
                level=logging.WARNING,
                session_id=self.session_id,
                phase=phase,
                tier=self._tier,
                median_tau_us=verdict.get("median_tau_us"),
                clustered_fraction=verdict.get("clustered_fraction"),
            )
            return PhaseVerdict(
                False,
                REASON_GEOMETRY_RETAKE_UNREACHABLE,
                payload={"geometry": dict(verdict)},
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
        # journal, the group's geometry verdict is on the session, and the
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
            self._run_delta_probe()
            # **The probe reports; the ROUND decides.** This arm used to call
            # a second seam that restored the graph itself and returned a
            # refusal code, ending the session BEFORE ``run_round`` — so a
            # rollback class wrote no receipt at all, and the adoption table
            # never saw the evidence. The ethos's fifth principle makes that
            # unacceptable ("every round, kept or restored or refused, banks
            # its measurement into the series state"), and the seam was also a
            # second owner of "restore the previous graph" beside
            # ``coordinator._run_round_restore``.
            #
            # Both are gone. The probe's verdict now reaches
            # ``evaluate_round_quality``, the same three classes restore
            # through the one restore owner, and the receipt records what the
            # restore did — because ``_write_round_receipt`` runs last.
            #
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
        thread) and is the only part of this session that does. It takes
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
        was accepted by a DIFFERENT session instance — the same-session
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
        # already failing, and the session is about to be discarded.
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
        BEFORE ``self._candidate`` is set and ``publish_candidate`` fires, so an
        item-1 refusal leaves no candidate for anything downstream to apply, and
        the confirm seam's ``CaptureBeginRefused`` arm persists a named reason
        with its own household copy. Item 2 GRADES rather than refuses (#2854),
        so it reaches the same seam without ever being the reason nothing
        published.

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
        half that MUTATES this session waits for them. This method is the
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
        moots can simply be dropped, leaving the session exactly as it was.

        The accountability gate DOES run here, and deliberately: it is part of
        producing a candidate, not part of proposing one. It raises
        ``CaptureBeginRefused`` before anything is banked, which on the eager
        path means the bank stays empty and the confirm refits — see
        :meth:`run_speculative_group_close` for why that costs a failing
        session one extra fit and buys an unchanged failure path.

        **It writes no session state at all** since #2291 Phase 2b. The fit's
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
        # emitted graph now carries — never the raw-branch one. Since PR-B
        # "the exact thing" survives a per-driver PRESCRIPTION too: the merge
        # recomposes this number through the fit module's own single
        # composition, so a prescribed role's filters are modelled here rather
        # than the fitted ones the graph no longer carries.
        # The ineligible/fit_failed path is untouched: the state's
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
            # Read off the CANDIDATE rather than off ``self._prescribed_driver``:
            # the bar below is a statement about the graph this apply would
            # emit, and the candidate is what says which branches it carries.
            # The two agree on every path today; the candidate is the one that
            # stays true if a document is ever held without being merged.
            prescribed=_prescribed_roles(candidate),
        )
        return _SpeculativeClose(
            candidate=candidate,
            predicted_sum=predicted_sum,
            analysis=analysis,
            cloud=cloud,
            level_frame_finding=level_frame_finding,
            linearization=linearization,
        )

    def _previous_graph_predicted_sum(self, analysis: Any, capture_fc_hz: float) -> Any:
        """The graph an apply REPLACES, modelled on this capture's branches (#2611).

        The PREVIOUS side of the commanded axis: the currently-applied Layer-A
        profile's own correction filters, role gains, polarity and delay,
        evaluated on the same measured branch pair and against the same
        alignment anchor the applied side uses. See
        :mod:`jasper.active_speaker.crossover_v2.commanded` for why that — and
        not the raw crossover at the applied candidate's own parameters — is
        what the probe's measured side is a change against.

        ``capture_fc_hz`` is the crossover corner THIS candidate's branches were
        composed at.  Every shipped route passes the session's own corner, so it
        equals ``self._fc_hz`` today; it stays a PARAMETER rather than a read of
        that field because the two are different questions — one is the corner
        the branches in front of this call were composed at, the other is the
        corner the session happens to hold — and the guard below is exactly the
        check that they agree with a third number, the corner the applied
        profile ran.  A caller that composed branches somewhere else would have
        to say so here rather than be silently credited with the session's.

        ``None`` whenever the previous graph cannot be named, with the reason on
        the journal. Every ``None`` here becomes an ``unavailable`` delta probe
        rather than a grade against an incomplete expectation; there is no
        fallback to the pre-#2611 axis, because that axis IS the defect.
        """
        def _absent(reason: str, **fields: Any) -> None:
            log_event(
                logger, "correction.crossover_v2_previous_graph_unavailable",
                level=logging.WARNING, session_id=self.session_id,
                reason=reason, **fields,
            )
            return None

        seam = self._seams.applied_profile
        if seam is None:
            return _absent("no_applied_profile_seam")
        try:
            profile = seam()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _absent("applied_profile_unreadable", error=str(exc))
        # The corner check, BEFORE the model is built: a previous graph modelled
        # on branches composed through a crossover it never ran is wrong by up
        # to 5.88 dB against this probe's 1.5 dB tolerance (adversarial panel,
        # PR #2614), and the two doors that reach it — a ``/sound`` corner edit
        # between rounds, and an operator's TOPOLOGY PIN, which opens the
        # session at the pinned corner while the applied profile still holds the
        # incumbent — are both live. (A third, every alternative-Fc candidate,
        # closed with the corner hunt.) This check never counted doors: it
        # compares corners, so it covers whichever ones exist.
        applied_fc_hz = _commanded.profile_crossover_fc_hz(profile)
        if applied_fc_hz is None:
            return _absent("applied_profile_names_no_corner")
        # A relative tolerance, not equality: both numbers are floats that have
        # been through a JSON round trip, and the case this refuses is 1500 vs
        # 1800, never 1500 vs 1500.0000001.
        if not math.isclose(applied_fc_hz, float(capture_fc_hz), rel_tol=1e-6):
            return _absent(
                "crossover_corner_moved",
                applied_fc_hz=round(applied_fc_hz, 3),
                capture_fc_hz=round(float(capture_fc_hz), 3),
            )
        woofer_role, tweeter_role = self._woofer.role, self._tweeter.role
        # The DRAFT's declared per-role polarity, which the measured branches
        # already carry (``program_analysis._compose_configured_path_ir``). The
        # profile records absolute flags, so without this the previous side is
        # stated in a different frame from the applied side's
        # ``alignment.polarity_sign`` on any speaker whose draft declares an
        # inverted branch.
        try:
            draft_inverted = role_polarity(self._preset)
        except ActiveSpeakerConfigError as exc:
            return _absent("draft_polarity_unreadable", error=str(exc))
        graph = _commanded.profile_graph_summation(
            profile, woofer_role=woofer_role, tweeter_role=tweeter_role,
            draft_inverted_by_role=draft_inverted,
        )
        if graph is None:
            return _absent("applied_profile_names_no_graph")
        responses = {
            response.role: response
            for response in (analysis.driver_responses or ())
        }
        if woofer_role not in responses or tweeter_role not in responses:
            return _absent("capture_has_no_branch_pair")
        alignment = analysis.alignment
        predicted = _commanded.graph_predicted_sum(
            # The WOOFER's grid, which is the grid ``plan_linearization`` builds
            # the applied side on (``freqs = responses[woofer_role].freqs_hz``),
            # so both sides of the subtraction land on one grid without an
            # interpolation nobody asked for.
            responses[woofer_role].freqs_hz,
            {role: response.complex_tf for role, response in responses.items()},
            graph,
            woofer_role=woofer_role,
            tweeter_role=tweeter_role,
            # The SAME gate the applied side's residual is derived through
            # (``program_analysis._build_candidate``): an anchor the aligner
            # refused is no anchor, and both sides then model the frame the
            # independently-aligned branch pair is already in.
            anchor_delay_us=(
                alignment.anchor_delay_us
                if alignment is not None and alignment.status == ALIGNMENT_OK
                else None
            ),
        )
        if predicted is None:
            return _absent("previous_graph_model_failed")
        # INFO, and it carries the four numbers the model turned on: a disputed
        # rollback should never need a second session to establish which graph
        # the round was graded against.
        #
        # ONCE per distinct answer, not once per call. The applied profile does
        # not change within a session, so a caller that asked repeatedly used to
        # put one identical line in the journal per ask — six of them, back when
        # a sweep scored six corners (adversarial panel, PR #2614). A round asks
        # once now, so the guard is idle on the shipped path; it is kept because
        # it is keyed on the FIELDS, which makes it correct for any number of
        # asks rather than for a particular one, and a graph that genuinely
        # differs still gets its own line.
        disclosed = (
            tuple(sorted((r, round(v, 4)) for r, v in graph.trim_db.items())),
            round(graph.delay_us, 3),
            graph.polarity_sign,
            tuple(sorted(
                (role, len(entries))
                for role, entries in graph.linearization.items()
            )),
        )
        if disclosed != self._previous_graph_disclosed:
            self._previous_graph_disclosed = disclosed
            log_event(
                logger, "correction.crossover_v2_previous_graph",
                level=logging.INFO, session_id=self.session_id,
                trim_db={r: round(v, 4) for r, v in graph.trim_db.items()},
                delay_us=round(graph.delay_us, 3),
                polarity_sign=graph.polarity_sign,
                linearization_filters={
                    role: len(entries)
                    for role, entries in graph.linearization.items()
                },
            )
        return predicted

    def _commanded_delta_for(
        self, analysis: Any, predicted_sum: Any, capture_fc_hz: float,
    ) -> Any:
        """This candidate's commanded axis: applied graph minus previous graph.

        The one place the two halves meet, so no caller can build the axis
        differently. ``analysis`` is the candidate's OWN analysis, because the
        previous graph has to be modelled on the same branches the applied side
        was or the branch measurement stops cancelling. ``capture_fc_hz`` is the
        corner those branches were composed at, and it travels rather than being
        read off the session for the reason
        :meth:`_previous_graph_predicted_sum` gives: the previous graph is only
        nameable while that corner matches the one the applied profile ran, and
        this call is where the two are put side by side.
        """
        return _commanded_delta(
            self._previous_graph_predicted_sum(analysis, capture_fc_hz),
            predicted_sum,
        )

    @staticmethod
    def _declared_transfer_for(analysis: Any, predicted_sum: Any) -> Any:
        """This candidate's STATE axis: applied graph minus the RAW crossover.

        What the applied graph declares it does, as opposed to what this apply
        changes — the delta probe's two directional safety rules need the first
        question and ``_commanded_delta_for`` above answers the second (#2614).
        A repeat round that leaves an existing boost band exactly as it was
        commands nothing there and still declares a boost there, and the
        speaker can over-realize it either way.

        The same subtraction, through the same owner, against the same applied
        side — only the reference graph differs, and here it is
        ``analysis.predicted_sum``: this capture's branches summed at the
        applied candidate's own polarity, delay and trims, with no correction.
        That is the axis the probe had before #2611, kept for exactly the one
        question it was right for.
        """
        return _commanded_delta(getattr(analysis, "predicted_sum", None), predicted_sum)

    def commit_intervention_proposal(
        self,
        candidate: Any,
        *,
        predicted_sum: Any,
        commanded_delta: Any,
        level_frame_finding: Mapping[str, Any] | None,
        realized_branch_level: Mapping[str, Any] | None = None,
        declared_transfer: Any = None,
    ) -> None:
        """The ONE seam through which a planned candidate becomes real (#2291).

        **One commit route reaches it**, :meth:`_commit_measure_candidate`, and
        that is the whole shape now: the corner a round commits is the one the
        household declared or an operator pinned.  It was built as a seam
        because there were TWO routes — the configured-Fc walk and an
        alternative-Fc selection — installing near-duplicate inline blocks that
        had already drifted; the second route went with the corner hunt, and the
        seam stays because it is where the proposal is assembled and
        fingerprinted (below), not merely where duplication was folded.

        **What this seam covers, exactly:** the three session state writes
        (``_candidate``, ``_measure_predicted_sum``, ``_measure_commanded_delta``),
        the proposal assembly #2392 added, and the two irreversible seam fires
        (``publish_candidate`` then ``_publish_level_frame_finding``).

        **What it deliberately does NOT cover:** ``_measure_predicted_spec_
        report``, and the ``correction.crossover_v2_candidate_built``
        disclosure.  The walk installs the spec report out-of-band, from the
        decision :meth:`_assert_accountable` replays (#2291 Phase 5a-v made it a
        value on that decision rather than a second session method), and it says
        its own log line.  Folding either in would be a behavior change, which
        no phase of this migration has sanctioned; they stay at the call site.

        Ordering is preserved rather than merely similar: every session
        attribute write still completes before ``publish_candidate``, the first
        observable side effect, so a re-entrant reader sees exactly what it saw
        before.

        **The proposal is assembled here, and its fingerprint is what the round
        receipt names (#2392).**  This is the one moment every value the
        contract needs is in one place, and it is the moment the prescription
        is decided — a receipt built later from durable state could only
        re-identify the *candidate*, which is exactly the confusion #2392
        closed.  ``realized_branch_level`` is the argument #2291 Phase 5c-iii
        removed with the write-only facade and this issue re-supplies: the walk
        reads it off its build's :class:`_LinearizationState`.

        **Assembly cannot fail this commit.**
        :func:`~jasper.active_speaker.crossover_v2.proposal.plan_intervention_proposal`
        returns a refusal rather than raising, and the refusal is *recorded*,
        not swallowed: the receipt then names the candidate under an explicit
        ``proposal_fingerprint_kind="candidate"``.  Ordering is preserved
        rather than merely similar — every session attribute write, the new one
        included, still completes before ``publish_candidate``, the first
        observable side effect, so a re-entrant reader sees exactly what it saw
        before.
        """
        from jasper.active_speaker.crossover_v2.contracts import InterventionProposal
        from jasper.active_speaker.crossover_v2.proposal import (
            plan_intervention_proposal,
        )

        self._candidate = candidate
        self._measure_predicted_sum = predicted_sum
        self._measure_commanded_delta = commanded_delta
        # #2614's STATE axis, written here for the CHANGE axis's reason and on
        # the same route. It is deliberately NOT part of the proposal: the
        # proposal states what the round asks for, and this states what the
        # graph declares — the delta probe's safety mask is its only reader.
        self._measure_declared_transfer = declared_transfer
        planned = plan_intervention_proposal(
            candidate,
            session_id=self.session_id,
            predicted_response_after=predicted_sum,
            commanded_delta=commanded_delta,
            accountability=level_frame_finding,
            realized_branch_level=realized_branch_level,
            evidence_identities={
                "session_id": self.session_id,
                "tier": self._tier,
                "speaker_id": self._speaker_id,
            },
        )
        self._intervention_proposal = planned
        self._measure_proposal_fingerprint = (
            planned.fingerprint
            if isinstance(planned, InterventionProposal)
            else ""
        )
        # #2662. Read off the candidate's own frozen evidence rather than from
        # a live analysis object: ``planning.analysis_json`` already puts
        # ``alignment_objective`` there, so this is the SAME answer the
        # candidate's fingerprint covers and not a second reading of it. Both
        # commit sites reach this seam, so both record it.
        analysis_evidence = getattr(candidate, "analysis", None)
        self._measure_alignment_objective = str(
            (analysis_evidence or {}).get("alignment_objective") or ""
            if isinstance(analysis_evidence, Mapping)
            else ""
        )
        self._seams.publish_candidate(candidate)
        self._publish_level_frame_finding(level_frame_finding)

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
        self.commit_intervention_proposal(
            candidate,
            predicted_sum=predicted_sum,
            # The configured walk's branches are composed at the session's own
            # corner, which is the corner the guard checks the applied profile
            # against.
            commanded_delta=self._commanded_delta_for(
                analysis, predicted_sum, self._fc_hz,
            ),
            declared_transfer=self._declared_transfer_for(analysis, predicted_sum),
            level_frame_finding=built.level_frame_finding,
            # #2392's other half of the same one-line re-supply: the walk reads
            # the verdict off its own build's state.
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
            # candidate rather than a session field since #2291 Phase 2b:
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
          retakes past, or one item 1 refuses after the frame gate banked,
          leaves no finding: the record describes the frame behind a specific
          proposal. Item 2 is not on that list — it no longer refuses.
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
                logger, "correction.crossover_v2_level_estimator_finding_failed",
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

    def _position_residual_rows(
        self, combined: Any, floor_hz: float | None,
    ) -> tuple[Mapping[str, Any], ...]:
        """§4.2: how far each position sat from the combined curve, labelled.

        One number per position per round, banked on the receipt so the next
        bite can separate a miss that is OURS from one that is the room's: a
        residual large at every position is broadband — a role-level trim or
        model defect, which is fixable — while one large at a single position
        is placement, which gets commanded for the achievable instead. The role
        is what makes that readable; "off-axis 2.9 dB" is an instruction where
        a bare spread number is a mood.

        Graded over the same frame the group's spec bands were: this group's
        trusted floor below, and the mic tier's own ceiling above (#2649 — the
        probe may not grade where the fitter may not command, and neither may
        this). An absent floor or ceiling widens the band rather than narrowing
        it, which is the honest direction: no evidence of an edge is not an
        edge at zero.

        **Never raises and never gates.** This is disclosure riding a close
        that already decided; losing the whole group's result to an arithmetic
        surprise here would be exactly the trade the pipeline refuses.
        """
        from jasper.audio_measurement.spatial_combine import position_residuals

        try:
            freqs = np.asarray(getattr(combined, "freqs_hz", ()), dtype=float)
            if freqs.size == 0:
                return ()
            ceiling_hz = self._mic_trust_ceiling_hz(freqs)
            band_hz = (
                float(floor_hz) if floor_hz is not None else float(freqs[0]),
                float(ceiling_hz) if ceiling_hz is not None else float(freqs[-1]),
            )
            return tuple(
                row.to_dict() for row in position_residuals(combined, band_hz=band_hz)
            )
        except (ValueError, TypeError, IndexError, AttributeError):
            log_event(
                logger, "correction.crossover_v2_position_residual_failed",
                level=logging.WARNING, session_id=self.session_id, exc_info=True,
            )
            return ()

    def _mic_trust_ceiling_hz(self, freqs: Any) -> float | None:
        """The frequency above which the FITTER was not allowed to command.

        Read off the envelope module's own ``mic_trust_limit`` curve rather than
        from a table copied here: the first grid bin where the allowed depth is
        exactly 0 dB IS the ceiling, by that function's construction, so the
        probe's ceiling and the fit's cannot drift. On a ``reference`` mic that
        lands at about 16.4 kHz — the first bin past the table's 16 kHz taper
        zero.

        **The fitter may not command there; the probe may not grade there**
        (#2649). Grading bins above it graded a microphone nobody trusts against
        a command that was never issued: on the 2026-08-16 round that produced
        ~90% of the squared error behind a 0.664 pooled realization, while the
        trusted HF had realized 96-101% of commanded.

        The tier reaches VERIFY through the published candidate's own entries —
        ``_analyze_verify`` does not stamp ``mic_tier`` on its analysis, and the
        MEASURE analysis that carries one is released after the fit. Same route
        and same guard shape as :meth:`_candidate_headroom_cost_db`. The
        candidate is also the ONLY carrier that crosses into the grading stage,
        which is why the tier is read here rather than off a session field: a
        stage-2 session has no MEASURE analysis to ask. The scan takes the first
        entry that has one because one round is measured by one microphone, so
        every entry that carries a tier carries the same one. Since PR-B a
        PRESCRIBED entry carries it too — replacing a role's filters does not
        change which microphone measured, and a document naming every role would
        otherwise take the ceiling away silently
        (:func:`~.crossover_v2.driver_prescription.driver_prescription_to_candidate_fields`
        is the second writer of that key).

        ``None`` — no ceiling, so the graded band is the caller's requested one,
        byte-identically to before this existed — in four cases, and each one
        now SAYS so on the journal rather than only the third:

        1. no candidate is bound;
        2. no entry recorded a tier, which is every round whose fit was
           ineligible or failed (``linearization`` is then ``{}``) and any
           prescribed round on top of such a fit;
        3. the tier is not one this build knows;
        4. the curve never reaches zero on this grid.

        Cases 1 and 2 were silent before PR-B, and case 2 is the arm a
        fully-prescribed candidate reaches. Silence there is the worst available
        answer: the probe grades untrusted HF and nothing distinguishes "the mic
        is trusted everywhere" from "nobody told us which mic it was".
        """
        from jasper.active_speaker.linearization_fit import MIC_TIER_FIELD

        def unavailable(reason: str, tier: str = "") -> None:
            log_event(
                logger, "correction.crossover_v2_mic_trust_ceiling_unavailable",
                level=logging.WARNING, session_id=self.session_id,
                reason=reason, mic_tier=tier,
            )

        linearization = getattr(self._candidate, "linearization", None)
        if not isinstance(linearization, Mapping):
            unavailable("no_candidate_linearization")
            return None
        tier = ""
        for entry in linearization.values():
            if isinstance(entry, Mapping) and entry.get(MIC_TIER_FIELD):
                tier = str(entry[MIC_TIER_FIELD])
                break
        if not tier:
            unavailable("no_entry_recorded_a_mic_tier")
            return None
        from jasper.active_speaker.linearization_envelope import mic_trust_limit

        try:
            grid = np.asarray(freqs, dtype=float)
            allowed = mic_trust_limit(grid, tier=tier)
        except (ValueError, TypeError):
            # An unknown tier raises by design in the envelope module. Here that
            # is missing evidence, not a broken session: fall back to no ceiling
            # and keep grading what the gate trusted.
            unavailable("mic_tier_not_recognised", tier)
            return None
        zeros = np.flatnonzero(allowed <= 0.0)
        if zeros.size == 0:
            unavailable("trust_curve_never_reaches_zero", tier)
            return None
        return float(grid[zeros[0]])

    def _refuse(self, code: str) -> "CaptureBeginRefused":
        """Build the refusal for ``code``, with that code's household copy, and
        record it as this session's failure code.

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
        evidence-keyed TODAY — the accountability refusal holds a literal — but
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
        prescribed: tuple[str, ...] = (),
    ) -> Mapping[str, Any] | None:
        """The three accountability assertions — see
        :func:`~jasper.active_speaker.crossover_v2.accountability.assess_accountability`,
        which owns which refusal fires, what is said, and what is banked.

        What stays here is everything the decision cannot do for itself, and
        each half is irreversible in a way a pure function must not be: the
        stash the host later persists, the journal's logger and
        ``session_id``, and the ``CaptureBeginRefused`` construction — whose
        ``_last_failure_code`` stamp is what makes a refusal reach the
        household as its own sentence rather than as "the measurement link
        timed out" (see :meth:`_refuse`).

        The two inputs the gate is TOLD rather than reaches for are the
        prediction threshold and item 1's household reason code; that module's
        docstring records why each stays owned here. It used to be three:
        item 2 was handed a reason code to refuse under, and the nanny
        burn-down took both the code and the refusal.

        **Write-then-say, and the ordering that matters.** The stash is
        installed before the journal is emitted, which differs from the
        method this replaced only where nothing can observe it: a decision
        carries a stash only when it got past the realized-level gate, so no
        refusal arm writes one. That is pinned rather than argued in
        ``test_crossover_v2_accountability``. It used to say "both arms": the
        estimator-consistency gate had a refusal arm of its own until #2609,
        and now banks and proceeds without one.

        **The bar has two values, and choosing between them is THIS method's
        job** (PR-B, conductor ruling 2026-08-20). ``prescribed`` names the
        roles whose branches this candidate carries by prescription rather than
        by fit; it is empty on every automatic round, which keeps that path
        byte-identical.

        * **Empty — the fitted class — keeps
          :data:`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB` (0.5 dB) exactly as it
          is.** That number is field evidence about the FIT, which is what it
          was measured on, and nothing here touches its original subject.
        * **Non-empty — the prescribed class — requires NON-WORSENING (0.0).**
          The 0.5 dB bar is a pooled-RMS figure, and a per-driver prescription
          is by construction a narrow high-Q filter aimed at ONE banked feature:
          on realistic fixtures such a filter predicts 0.077-0.152 dB of pooled
          improvement even when it is exactly the right correction, so the
          fitted bar would file the whole class as no improvement before its
          first hardware exercise. It is not the same question
          being asked more leniently — it is the same question asked of a
          proposal that already carries its OWN admission evidence (the
          classification verdict bar, the per-filter depth cap, the composed
          cap, and a digest proving the accepted bytes ran). What adjudicates a
          prescription is the measured round with its pre-registered
          keep/rollback, not a model-vs-model screen sized for another class.

        **Neither bar stops anything any more** (the nanny burn-down, doctrine
        deviation (c)). The bar used to decide a refusal; it now chooses which
        LEDGER value the round banks — ``improved`` or ``not_an_improvement``
        — and the round proceeds to the measurement that decides. The field
        evidence that overruled the non-worsening arm's "worth refusing before
        measuring" argument is on :mod:`~.crossover_v2.accountability`.

        The gate itself never learns any of this — it is handed a number, per
        its own docstring — so the journal line carries ``required_db`` and the
        two bars are told apart on the wire by the value that decided.
        """
        prescribed_graph = bool(prescribed)
        decision = _accountability.assess_accountability(
            predicted_sum=predicted_sum,
            raw_predicted_sum=raw_predicted_sum,
            state=linearization,
            grade_prediction=spec_report_for_predicted_sum,
            material_improvement_db=(
                PRESCRIBED_NON_WORSENING_DB if prescribed_graph
                else PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
            ),
            reason_levels_disagree=REASON_DRIVER_LEVELS_DISAGREE,
        )
        if decision.spec_report_written:
            self._measure_predicted_spec_report = decision.spec_report
        for record in decision.journal:
            self._journal_linearization(record)
        if decision.refusal_reason is not None:
            raise self._refuse(decision.refusal_reason)
        return decision.finding

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
        (:func:`cloud_validity_floor_hz`), from which
        :func:`cloud_trusted_floor_hz` derives the ``2.5/T`` the spec bands'
        lower edges are intersected with (#2551).

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
            graded_spec_sink=lambda graded: self._group_graded_spec.__setitem__(
                phase, graded
            ),
        )
        self._group_cloud_result[phase] = result
        # #2609 SF5 / §4.2: the two things the ROUND needs from this group that
        # the serialized result above does not carry, taken while the combine
        # is still in hand. Both are recorded whichever phase this is; only
        # ``PHASE_CLOUD_VERIFY``'s are read, and stashing the pre-apply group's
        # too costs one float and a short list while making the two closes
        # symmetrical rather than special-cased.
        floor_hz = cloud_trusted_floor_hz(cloud_validity_floor_hz(positions))
        self._group_trusted_floor_hz[phase] = floor_hz
        self._group_position_residuals[phase] = self._position_residual_rows(
            combined, floor_hz,
        )
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
            # The one figure above that the frame CANNOT move (issue #1857):
            # the step between two band levels, in which the shared
            # reference cancels. Every other flatness field on this line is
            # a distance from that reference, and a uniformly-off band drags
            # it. See ``_flatness_tilt_log_field``.
            flatness_tilt=_flatness_tilt_log_field(flatness),
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
        stored = self._hand_to_retention(
            take_id, PHASE_ENTRY_BASELINE, result, metadata,
        )
        artifact_ref = take_id if stored else ""
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
        """Narrow this session's seams down to the five a round may call.

        The coordinator is handed capabilities rather than the session, so
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

    def _applied_candidate_id(self) -> str:
        """The APPLIED candidate's fingerprint, by the one honest chain.

        ``_tuning_attempt_id`` first — on the stage that grades a round it is
        the only one populated, read from the durable state's candidate
        fingerprint at prepare time — then this session's own candidate for the
        stage that committed one.

        A method rather than the inline expression it used to be because #2392
        made the receipt read it TWICE: once as the fallback for
        ``proposal_fingerprint`` and once as the candidate identity that now
        rides in the evidence identities. Two copies of a chain whose ordering
        is load-bearing is how the two quietly disagree.
        """
        return self._tuning_attempt_id or str(
            getattr(self._candidate, "fingerprint", "") or ""
        )

    def _grade_round_once(self, verdict: PhaseVerdict) -> PhaseVerdict:
        """Grade this round and act on the adoption table. Once per session.

        **One owner, two triggers**, because "stage 2's post-apply evidence is
        complete" happens at two different moments:

        * Express — the end of :meth:`_consume_verify`, when this session plans
          no post-apply cloud. VERIFY is all the evidence there will be.
        * Full — the end of :meth:`_close_cloud_group` for
          ``PHASE_CLOUD_VERIFY``, when the spatial arm has landed too.

        **Both triggers require an ACCEPTED capture**, and a rejection reaches
        one of two ends. A RETRIABLE one does not end the session — VERIFY and a
        position group each still have takes to offer — so grading it would burn
        this guard on evidence the household then replaced, and the receipt,
        which is write-once, would describe a capture the round did not end on.
        A rejection no take can clear ends the session on that verdict instead
        (#2086's condition rung), and then no round receipt is written at all,
        which is the honest record: its post-apply evidence never completed.

        The fire-once guard is here rather than in the coordinator because it
        is a fact about THIS SESSION rather than about the round: only the
        this object knows a second trigger is its own.

        Everything after it belongs to
        :func:`~jasper.active_speaker.crossover_v2.coordinator.run_round`,
        which grades, acts, and banks; this stamps the results onto the
        session's own state and maps the coordinator's refusal KIND to the
        :data:`REASON_REGISTRY` code whose copy the household reads. The
        vocabulary stays here because the registry does.
        """
        from jasper.active_speaker.crossover_v2 import coordinator

        if self._round_evaluated:
            return verdict
        self._round_evaluated = True
        # #2602. ``None`` is a host that resolved nothing — a fresh series, or
        # a construction path predating the ruling — and the opening round is
        # the fail-safe reading of both: it can only offer another round, never
        # suppress a stop the evidence asked for.
        position = self._series_position or coordinator.SeriesPosition.first()
        graded_verify = self._group_graded_spec.get(PHASE_CLOUD_VERIFY)
        decision = coordinator.run_round(
            coordinator.RoundEvidence(
                session_id=self.session_id,
                tier=self._tier,
                post_analysis=self._verify_analysis,
                entry_baseline=self._measure_entry_baseline,
                # The post-apply CLOUD's report — ``None`` on a tier that walks
                # no cloud, which the evaluator reads as "no report" rather than
                # as a pass (#2160's honest wire).
                spec_report=(
                    None if graded_verify is None else graded_verify.report
                ),
                # Decision 10's evidence: the SAME evaluation the spec verdict
                # reads, with its curve and merged honesty mask.
                graded_spec=graded_verify,
                applied_blend_correction=self._applied_blend_correction(),
                previous_blend_residual_db=position.previous_blend_residual_db,
                # #2662. Rehydrated from stage 1's durable ``verify_priors`` on
                # the same route as the entry baseline and the commanded delta,
                # for the same reason stated below about the proposal
                # fingerprint: this stage builds a fresh session and holds no
                # candidate to derive one from.
                alignment_prescription=self._alignment_prescription,
                # …and whether the machinery COMMITTED it. Rehydrated on the
                # same route, because a candidate's provenance without its
                # outcome is a receipt that can credit a round it never ran.
                alignment_objective=self._measure_alignment_objective,
                # The crossover pin, rehydrated on the identical route and for
                # the identical reason. It needs no companion outcome field:
                # the boundary opened both stages at the pinned topology, so
                # this receipt's numbers were measured there by construction —
                # see ``coordinator._round_measurements``.
                topology_prescription=self._topology_prescription,
                # WHAT THIS ROUND PROPOSED (#2392), preferred over what it
                # applied. The fingerprint travelled here from the committing
                # stage through durable ``verify_priors``, exactly as the
                # commanded delta and the entry baseline do, because the stage
                # that grades a round builds a fresh session and holds no
                # candidate to derive one from.
                #
                # The candidate below is the fallback, and it is a real one
                # rather than a formality: it serves a stage-2 re-arm whose
                # stage 1 ran before #2392, and a commit whose proposal
                # assembly was refused. ``_tuning_attempt_id`` leads it for
                # :meth:`_grade_verify_attempt`'s reason and by the same chain
                # — on the grading stage it is the only one populated, read
                # from the durable state's candidate fingerprint at prepare
                # time. Reading ``self._candidate`` alone was the dead stage-2
                # read that emptied this field, which the contract refuses, so
                # every production round lost its receipt to the fail-soft
                # handler.
                proposal_fingerprint=(
                    self._measure_proposal_fingerprint or self._applied_candidate_id()
                ),
                # The receipt says which of the two it got, because they are
                # indistinguishable by inspection — both are 64-hex SHA-256.
                proposal_fingerprint_kind=(
                    "intervention_proposal"
                    if self._measure_proposal_fingerprint
                    else "candidate"
                ),
                # Kept on the record either way, so taking the field above for
                # the proposal does not cost the receipt its candidate identity.
                candidate_fingerprint=self._applied_candidate_id(),
                commanded_delta_present=self._measure_commanded_delta is not None,
                realization_tolerance_db=VERIFY_TOLERANCE_DB,
                reference_mark=REFERENCE_MARK_DESIGN_AXIS,
                # The map this session's own probe produced, or ``None`` when it
                # never ran one (#2537). Both triggers reach this AFTER
                # :meth:`_run_delta_probe` has stamped ``self._delta_probe``, so
                # the round's safety axis and the probe's own refusal read the
                # same evidence rather than two reads a capture apart.
                delta_probe=self._delta_probe,
                # Where this round sits in the household's flattening series,
                # and what the previous one measured (#2602). Both come from
                # the durable receipt the previous round banked — this session
                # cannot derive either, because a stage-2 grading run is a
                # fresh session that has seen no earlier round.
                round_ordinal=position.ordinal,
                previous_objectives=position.previous_objectives,
                # #2609 SF5: the frame those objectives were graded in, this
                # round's from the group that produced the report and the
                # previous round's from the receipt it banked. Without the
                # pair, a 7-vs-10 ms gate change reads as 0.518 dB of progress.
                trusted_floor_hz=self._group_trusted_floor_hz.get(
                    PHASE_CLOUD_VERIFY
                ),
                previous_trusted_floor_hz=position.previous_trusted_floor_hz,
                # §4.2, from the same close as the spec report above — ``()``
                # on a tier that walks no post-apply cloud.
                position_residuals=self._group_position_residuals.get(
                    PHASE_CLOUD_VERIFY, (),
                ),
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

        Both rulings are stated in
        :mod:`~jasper.active_speaker.crossover_v2.attempt_grading` (#2291
        5b-ii), which brackets the durable write rather than driving it. Every
        act stays here: building the record, the ``record_model_error`` seam
        call with the guard that decides whether to make it, its ``except``
        arms, all of its log lines, the identity conflict it can report, the
        decision payload the household reads, and the history append. "How many
        times can that write fire" is still answered by reading this method.

        **Exactly-once survives a failed write, and that is why the seam call
        catches broadly** (#2386). The rung that stops a second write is the
        ``ATTEMPT_ALREADY_RECORDED`` answer above, and it can only see a repeat
        once the attempt is in ``_attempt_history`` — which this method appends
        at its END. So a store exception that ESCAPES the seam call skips that
        append, and the next capture of the same applied candidate is assessed
        as new and asks the seam again. The guard therefore contains every
        ``Exception``, not an enumeration of the classes today's binding
        happens to raise: the seam is a Protocol any host may implement, so the
        property has to hold against the interface rather than against one
        implementation of it. It does NOT rely on
        :mod:`~jasper.active_speaker.model_error_store` de-duplicating by
        observation identity — that store stays the independent owner of
        prediction/realization error, and leaning on its dedup to keep this
        method's own guard honest is exactly the coupling #2291 forbids.
        """

        identity = _grading.assess_attempt_identity(
            tuning_attempt_id=self._tuning_attempt_id,
            # A callable for the ladder's stated call-count reason: the shipped
            # flow reaches into the candidate only when no tuning attempt id is
            # in hand, and ``None`` is how "there is no candidate to ask" is
            # spelled.
            candidate_fingerprint=lambda: (
                str(getattr(self._candidate, "fingerprint", "") or "")
                if self._candidate is not None else None
            ),
            session_id=self.session_id,
            capture_attempt=capture_attempt,
            recorded_ids=(item.attempt_id for item in self._attempt_history),
        )
        if identity.kind != _grading.ATTEMPT_NEW:
            # ``ATTEMPT_ALREADY_RECORDED``'s arm, and the LOUD fallback for a
            # kind nobody wired. One arm per :data:`attempt_grading.IDENTITY_KINDS`
            # member, and the dangerous direction here is the PERMISSIVE one:
            # falling through reaches the model-error write below, so a kind
            # from the future would bank a second durable observation of one
            # candidate identity — the exact double-write this rung exists to
            # make unreachable. An unknown kind therefore skips, and says so.
            if identity.kind != _grading.ATTEMPT_ALREADY_RECORDED:
                log_event(
                    logger,
                    "correction.crossover_v2_attempt_identity_kind_unmapped",
                    level=logging.ERROR,
                    session_id=self.session_id,
                    attempt_id=identity.attempt_id,
                    kind=str(identity.kind),
                )
            return
        attempt_id = identity.attempt_id

        record = attempt_record_from_verify(
            analysis,
            attempt_id=attempt_id,
            # The session that captured THIS sweep — see #2081 and the
            # constructor's own note on why a relay session is the sitting.
            sitting_id=self.session_id,
        )
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
            except Exception:  # noqa: BLE001 - the fall-through below is the point
                # Any OTHER store failure, contained for the SAME ruling the arm
                # above states — and containing it is what makes the write
                # exactly-once (#2386). An escape does two things, not one: it
                # reverses a VERIFY the measurement gate accepted, and it skips
                # the ``_attempt_history`` append at the end of this method, so
                # the next capture of the same applied candidate is assessed as
                # ``ATTEMPT_NEW`` and asks the seam a SECOND time.
                #
                # The append is the property, and it must run even though this
                # write may have failed: a raising seam says nothing about
                # whether the durable record landed, so the honest thing to
                # record is that this identity was ASKED. That is already the
                # shipped outcome for the classes above — a contained failure
                # banks the attempt ungraded-by-the-store rather than dropping
                # it — and this arm extends it to the classes the tuple does not
                # name, rather than inventing a second policy for them.
                #
                # NOT ``BaseException``: ``KeyboardInterrupt``/``SystemExit``
                # must keep propagating, and a dying process persists nothing
                # this method appends anyway.
                #
                # Its sibling answers the same lint question the other way,
                # deliberately: ``crossover_v2.planning._JOURNAL_ERRORS``
                # ENUMERATES eight classes instead of catching broadly, because
                # what it protects is an in-memory plan carrying a
                # ``journal_dropped`` disclosure rather than a second durable
                # record. Different property, different catch width.
                #
                # Its own event at ERROR, because the arm above means "the store
                # had an outage" and this one means "the seam raised something
                # nobody enumerated" — one is operational and the other is a
                # defect, and filing them under one name would cost the
                # distinction on the surface that has to act.
                log_event(
                    logger,
                    "correction.crossover_v2_model_error_write_unexpected",
                    level=logging.ERROR,
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
        # Evidence refusal outranks grading preconditions — the ladder's ruling,
        # stated in :func:`attempt_grading.grade_attempt_outcome`. ``decide_next``
        # requires a real floor, but #2033's integrity result is meaningful
        # even on a speaker that has not adopted one. What stays here is the
        # construction: the kernel's typed evidence verdict from its own
        # vocabulary, and the flow-owned no-floor status that must not mask a
        # rejected capture.
        grade = _grading.grade_attempt_outcome(
            comparable=record.integrity.comparable,
            floor_present=self._attempt_floor is not None,
        )
        if grade not in _grading.GRADE_KINDS:
            # Checked against the declared set rather than by an ``else`` arm,
            # because the arms below keep the ladder's own order and its LAST
            # one is the permissive direction: a kind from the future reaching
            # ``decide_next`` would put an improvement claim nobody wired in
            # front of a household. Degrade to the evidence refusal instead —
            # "the loop could not judge this attempt" is what a wiring defect
            # actually means — and say so loudly.
            log_event(
                logger,
                "correction.crossover_v2_attempt_grade_kind_unmapped",
                level=logging.ERROR,
                session_id=self.session_id,
                attempt_id=record.attempt_id,
                kind=str(grade),
            )
            grade = _grading.GRADE_NOT_COMPARABLE
        if grade == _grading.GRADE_NOT_COMPARABLE:
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
        elif grade == _grading.GRADE_NO_FLOOR:
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
            # ``GRADE_DECIDE_NEXT`` is returned only for a comparable capture on
            # a speaker that HAS a floor, so this restates the ladder's own
            # postcondition for the type checker rather than re-deciding it.
            assert self._attempt_floor is not None
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
        reproduced the consequence on the real session: an attempt-1
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

    def _note_verify_mismatch(self, max_db: Any) -> str:
        """Which out-of-tolerance code this attempt earns (#1873).

        The single owner of both halves of the discriminator — reading the
        previous attempt's mismatch and recording this one — so "what counts as
        the predecessor" cannot be answered differently in two places.

        ``verify_out_of_tolerance`` is the honest answer for a FIRST mismatch:
        one bad take really can produce it, and the household is entitled to
        try again. ``verify_deterministic_mismatch`` is the honest answer once a
        second attempt has landed within :data:`VERIFY_REPEAT_FLOOR_DB` of the
        first — at that separation the instrument cannot tell the two apart, so
        what the household is looking at is the speaker's own answer, twice.
        The finding is the same either way; what changes is whether "try again"
        is still a real option, and that is exactly what the two codes' copy
        and retry budgets differ on.

        **The comparison is against the predecessor, never a fixed baseline** —
        see :data:`VERIFY_REPEAT_FLOOR_DB` for the measured reason. So this
        writes on every graded mismatch, including the one that returns the
        deterministic code: nothing downstream re-reads it, and leaving the
        pair stale would be a second rule to keep true.

        **The non-finite guard is load-bearing for NaN — do not read it as
        defensive tidying.** Delete it and a NaN grade arriving with a
        predecessor in hand returns the DETERMINISTIC code: ``abs(nan - x)`` is
        ``nan``, ``nan > VERIFY_REPEAT_FLOOR_DB`` is ``False``, so the
        comparison below falls THROUGH to the mismatch branch and a household
        is told an unmeasurable capture agreed with a real one. (The
        infinities are the inert case — ``+inf`` reaches here and answers
        ``out_of_tolerance`` with or without the guard, and ``-inf`` never
        reaches here at all.)

        It is unreachable for NaN today, and only by an upstream accident this
        method must not depend on: the caller gates on
        ``max_db > VERIFY_TOLERANCE_DB``, which is ``False`` for ``nan``, so a
        NaN grade takes the PASS branch instead of arriving here. That is a
        property of one comparison in one caller, not a contract — and the
        answer it protects is the one that would be wrong.

        Clearing the pair is the second half: a value nothing can agree with
        must not become the thing a later attempt agrees WITH either.
        """
        if not isinstance(max_db, (int, float)) or not math.isfinite(float(max_db)):
            # No usable grade: this attempt is a mismatch nothing can agree
            # with, and it cannot agree with anything either.
            self._verify_last_mismatch_max_db = None
            return REASON_VERIFY_OUT_OF_TOLERANCE
        current = float(max_db)
        previous = self._verify_last_mismatch_max_db
        self._verify_last_mismatch_max_db = current
        if previous is None or abs(current - previous) > VERIFY_REPEAT_FLOOR_DB:
            return REASON_VERIFY_OUT_OF_TOLERANCE
        return REASON_VERIFY_DETERMINISTIC_MISMATCH

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
        # #1974), but it only becomes session state through
        # ``_set_verify_outcome``, alongside the outcome and code it belongs
        # to. See that method for the desync this ordering prevents.
        #
        # Named for what it is, not "gate": ``verify_gate`` below is this same
        # capture's window in MILLISECONDS, and in the one method where
        # confusing the two produced a household-visible bug they should not
        # share a name.
        gate_record = _gate_record(analysis.summed_response)
        # The pre-grade ladder — locate, pilot level, capture integrity (issue
        # #1971), linearity — belongs to
        # :func:`~jasper.active_speaker.crossover_v2.capture_dispatch.verify_integrity_screens`,
        # which owns the order and the two-code split the integrity record
        # produces. It runs ahead of EVERY grade below it for the reason
        # ``_measure_verdict`` puts the same class of check ahead of its own: a
        # spliced or clipped recording is not evidence about the speaker, so no
        # verdict drawn from it — linearity, the gate-window comparison, G3's
        # transfer step, or the tracking max — is worth reporting.
        #
        # What stays here is every rung that reads state outliving ONE capture:
        # the gate-comparability rule against MEASURE's window, G3's
        # pilot-transfer baseline, and the tracking comparison.
        integrity_screen = _dispatch.verify_integrity_screens(
            analysis, stimulus_located=_stimulus_locate_ok(analysis),
        )
        if integrity_screen is not None:
            payload = (
                dict(integrity_screen.integrity_payload)
                if integrity_screen.integrity_payload is not None else {}
            )
            return PhaseVerdict(
                False,
                _screen_refusal_code(integrity_screen.kind),
                payload=payload,
            )
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
        # usable attempt of this session's own lifetime (never pilots
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
            code = self._note_verify_mismatch(max_db)
            self._set_verify_outcome("fail", code, gate_record)
            # Its own name: the integrity-screen branch above already binds a
            # ``payload`` in this scope, and the two describe different
            # captures' verdicts.
            mismatch_payload: dict[str, Any] = {"tracking": dict(tracking)}
            if code == REASON_VERIFY_DETERMINISTIC_MISMATCH:
                # The runner's own contract for "no later capture can make this
                # set usable" (``capture_relay.session.run_capture_plan``): it
                # publishes this exact ``capture_result`` and returns, instead
                # of waiting for a next begin whose only answer is a refusal.
                # That is what stops the retry loop riding the relay session
                # into TTL expiry — the session closes on the verdict rather
                # than on the clock. The phone has rendered a terminal
                # ``capture_result`` since build 20260803.4 (#2097), so this
                # needs no page change: it drops the live "Try again" and shows
                # the host's own ``reason`` with a route back to the speaker
                # page, where Undo and Re-measure live.
                mismatch_payload["terminal"] = True
                mismatch_payload["terminal_outcome"] = (
                    VERIFY_TERMINAL_OUTCOME_DETERMINISTIC
                )
            return PhaseVerdict(False, code, payload=mismatch_payload)
        # Graded, and inside tolerance: the mismatch did NOT repeat, so the pair
        # #1873's discriminator would draw its claim from is broken. A later
        # failure in the same session starts a fresh pair rather than agreeing
        # with a grade that has a passing attempt between it and now.
        self._verify_last_mismatch_max_db = None
        # PR-L5's delta probe. Runs only once tracking has PASSED — a session
        # that already failed at the handoff band does not need a second
        # verdict about the same capture, and its retry budget (2) still means
        # something. What this adds on top is the band tracking cannot see: the
        # whole span the correction commands, which is where a realization
        # defect like the 2026-07-27 shelf lives.
        self._verify_tracking_curve = analysis.verify_tracking_curve
        summed = analysis.summed_response
        if summed is not None:
            self._verify_trusted_band_hz = _gate_trusted_band_hz(summed)
        # The probe reports here and the ROUND decides — see the
        # ``PHASE_CLOUD_VERIFY`` arm for the seam that used to sit between them
        # and why it is gone. This CAPTURE passed tracking, which is what this
        # verdict answers; whether the graph it measured stays on the speaker
        # is the adoption table's question, one call later.
        self._run_delta_probe()
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

    def _declared_transfer_db(self, freqs: Any) -> Any | None:
        """The applied graph's own declared transfer, on the VERIFY grid (#2614).

        The delta probe's STATE axis — what the graph on the speaker declares it
        does against the uncorrected crossover — interpolated onto the probe's
        grid exactly as ``commanded_db`` is beside the call, so the two masks the
        probe builds from them are bin-for-bin comparable.

        Returns ``None`` when the axis never crossed into this stage or cannot
        be put on the grid, and says so on the journal rather than degrading
        quietly: the probe then falls back to the CHANGE axis alone for its two
        directional safety rules, which on a repeat round stops watching every
        band the apply left alone. That is a real narrowing of a hearing-safety
        finding, so it belongs in front of whoever reads the round.
        """
        declared = self._measure_declared_transfer
        if declared is None:
            log_event(
                logger, "correction.crossover_v2_declared_transfer_unavailable",
                level=logging.WARNING, session_id=self.session_id,
                reason="no_declared_transfer",
            )
            return None
        try:
            return np.interp(
                np.asarray(freqs, dtype=float),
                np.asarray(declared[0], dtype=float),
                np.asarray(declared[1], dtype=float),
            )
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            log_event(
                logger, "correction.crossover_v2_declared_transfer_unavailable",
                level=logging.WARNING, session_id=self.session_id,
                reason="declared_transfer_ungriddable", error=str(exc),
            )
            return None

    def _entry_delta_db(
        self, freqs: Any, predicted_s: Any, commanded_db: Any,
    ) -> Any | None:
        """This round's PRE-apply capture, in the realized curve's frame (#2533).

        ``measured_pre − predicted_previous``, on the VERIFY grid — the exact
        counterpart of the ``realized_db`` reconstruction beside the call, with
        the entry baseline's magnitude in place of the post-apply one, so
        ``classify_delta_probe`` can measure its residual as a CHANGE across the
        apply instead of as an absolute disagreement with the model. The
        previous-graph prediction is recovered the same way that reconstruction
        recovers it: ``predicted_previous == predicted_post − commanded``.

        Since #2611 that recovered curve is a model of the graph the entry
        capture ACTUALLY went through, so this term and ``commanded`` share one
        reference and the probe's residual is a measurement-minus-measurement
        difference. It used to be the raw crossover, which is what put an
        unbounded chained-round contaminant in the residual (see
        :func:`~jasper.active_speaker.delta_probe.classify_delta_probe`).

        The curve is #2291's ``verify_priors.entry_baseline``, already retained
        and already rehydrated into stage 2 — nothing new is captured, persisted,
        or asked of the household for this.

        **Bins the entry capture EXCLUDED become NaN, not values.** They are the
        bins that capture could not trust, and the probe drops non-finite anchor
        bins rather than anchoring a level claim on them. Interpolation spreads
        each NaN to its two neighbours, which is the conservative direction.

        Returns ``None`` — "no comparable before", which leaves the probe
        measuring exactly what it measured before this existed — for a round with
        no entry baseline, and for any arithmetic this cannot complete. Fail-soft
        on the same terms as :meth:`_applied_offset_db`: an unusable optional
        accounting term is nothing known, never a lost verdict.
        """
        baseline = self._measure_entry_baseline
        if baseline is None:
            # NAMED, not silent (series-2 D1 fix round). Since D1 this arm
            # decides whether the realized-energy half of the safety axis runs,
            # so a reader asking "why did nothing check the driver" must find an
            # answer here rather than infer it from an absent field.
            #
            # **Not a first-ever round** — that one never arrives here at all.
            # It has no nameable previous graph, so its commanded axis is
            # absent, and the caller takes the ``state_axis_only`` branch below
            # without ever calling this method. What reaches this arm is a round
            # that HAS a commanded axis and no usable baseline record: the three
            # cases ``correction_crossover_v2.entry_baseline_prior_from_state``
            # enumerates — a state file written before that key shipped, a
            # stage 1 whose baseline capture never landed, and a truncated or
            # hand-edited record. All three are exceptional, which is why this
            # is WARNING.
            log_event(
                logger, "correction.crossover_v2_delta_probe_no_entry_anchor",
                level=logging.WARNING, session_id=self.session_id,
                reason="no_entry_baseline",
            )
            return None
        try:
            # COMPARABLE, or it is not an anchor. An anchor is a subtraction, so
            # a curve measured through another program, or from another mic
            # position, cancels a real finding as readily as a phantom — and
            # since D1 the subtrahend feeds a hard stop rather than only the
            # disclosed residual.
            #
            # BOTH identity fields, asked through the rule's own owner:
            # ``verification.identity_mismatch`` is the identity half of
            # ``_comparability_mismatch``, so the order and the two reason
            # constants live in one place rather than in a parallel spelling
            # here. The MARK is the field that earned the second clause — a
            # program mismatch usually changes the grid and surfaces in the
            # arithmetic below, while a baseline captured at another position is
            # the same program on the same grid and subtracts a different room
            # bin by bin, which nothing else on this path would catch.
            #
            # Unknown on either side is "nothing known" and does not refuse —
            # the module's rule everywhere. A MAGNITUDE bound is deliberately
            # not added: identity is answerable from the record, plausibility is
            # not, and a curve that is comparable and merely wrong is what
            # capture integrity screens for.
            from jasper.active_speaker.crossover_v2.verification import (
                identity_mismatch,
            )

            want_program = str(
                getattr(
                    self.program_for_phase(PHASE_VERIFY), "program_id", "",
                ) or ""
            )
            got_program = str(getattr(baseline, "program_id", "") or "")
            got_mark = str(getattr(baseline, "reference_mark", "") or "")
            mismatch = (
                identity_mismatch(
                    program_id=got_program,
                    reference_mark=got_mark,
                    other_program_id=want_program,
                    other_reference_mark=REFERENCE_MARK_DESIGN_AXIS,
                )
                if want_program and got_program and got_mark
                else None
            )
            if mismatch is not None:
                log_event(
                    logger, "correction.crossover_v2_delta_probe_no_entry_anchor",
                    level=logging.WARNING, session_id=self.session_id,
                    reason=mismatch,
                    baseline_program_id=got_program,
                    baseline_reference_mark=got_mark,
                    verify_program_id=want_program,
                )
                return None
            curve = baseline.curve
            entry_hz = np.asarray(curve.hz, dtype=float)
            entry_db = np.asarray(curve.db, dtype=float)
            excluded = np.asarray(baseline.excluded, dtype=bool)
            if entry_hz.size == 0 or entry_db.size != entry_hz.size:
                return None
            if excluded.size == entry_hz.size:
                entry_db = np.where(excluded, np.nan, entry_db)
            measured_pre = np.interp(freqs, entry_hz, entry_db)
            return (measured_pre - predicted_s) + commanded_db
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            # One arm for the whole body, and the identity read is INSIDE it on
            # purpose: ``program_for_phase`` is bound at construction and cannot
            # raise today, but a fail-soft method that computes anything outside
            # its own guard is one refactor from losing a verdict to an
            # accounting term. The reason names the record, not the curve,
            # because the arm now covers both.
            log_event(
                logger, "correction.crossover_v2_delta_probe_no_entry_anchor",
                level=logging.WARNING, session_id=self.session_id,
                reason="unusable_record", error=str(exc),
            )
            return None

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

        **The band is the capture's own trusted band, and there is no
        fallback** (#2521). It comes from the gate disclosure
        (:func:`_gate_trusted_band_hz`) — this capture's gate-derived trusted
        floor intersected with the band its stimulus actually radiated — and
        the raw grid edges this used to pass instead were wider at BOTH ends:
        on the first remote JTS3 session the disclosure said 357-20,000 Hz and
        the probe graded 325-22,480, then rolled the correction back with its
        whole headline — ``worst_hz=21,266``, ``max_error_db=23.4`` — sitting
        above that ceiling, at a frequency nothing had measured. A capture with no
        trusted band leaves the probe unavailable, which is the same answer
        ``_verify_absolute_result`` gives (``no_trusted_crossover_region``) for
        the same missing fact.

        **A missing CHANGE axis no longer silences the hearing-safety half**
        (#2614). A round whose branches are composed through a crossover the
        applied graph never ran has no nameable previous graph — an operator's
        topology pin is the live producer of that shape, and a first-ever round
        reaches it with no applied graph at all — so the commanded delta is
        absent, and until this the whole probe was absent with it: the two
        directional findings never ran, and ``evaluate_applied_safety`` reported
        SAFE on a round where nothing had looked. The STATE axis needs no corner
        match, so when it is present the probe runs its safety half on that
        alone and reports
        :data:`~jasper.active_speaker.delta_probe.VERDICT_SAFETY_ONLY` — which
        is not a pass, carries no shape grade, and says on the record that the
        shape check did not run and why.

        Returns ``None`` when the tracking curve, the trusted band, or BOTH
        axes are missing. ``None`` is the same thing
        :data:`~jasper.active_speaker.delta_probe.VERDICT_UNAVAILABLE` is: no
        evidence to refuse on, and no permission granted either.
        """
        tracked = self._verify_tracking_curve
        commanded = self._measure_commanded_delta
        band_hz = self._verify_trusted_band_hz
        if tracked is None:
            return None
        if band_hz is None:
            log_event(
                logger, "correction.crossover_v2_delta_probe_no_trusted_band",
                level=logging.WARNING, session_id=self.session_id,
            )
            return None
        try:
            freqs, measured_s, predicted_s = tracked
            freqs = np.asarray(freqs, dtype=float)
            measured_s = np.asarray(measured_s, dtype=float)
            predicted_s = np.asarray(predicted_s, dtype=float)
            declared_db = self._declared_transfer_db(freqs)
            commanded_db = None if commanded is None else np.interp(
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

        spatial = spatial_cost_from_group_spreads(
            {"band_spread": self._group_band_spread.get(PHASE_CLOUD_MEASURE, ())},
            {"band_spread": self._group_band_spread.get(PHASE_CLOUD_VERIFY, ())},
        )
        if commanded_db is None:
            if declared_db is None:
                # Neither axis. Unchanged behaviour, and
                # ``_declared_transfer_db`` has already named the reason.
                return None
            # The STATE axis in the commanded slot, and the classifier told so.
            # ``realized − commanded`` is still ``measured − predicted``, which
            # is what the two directional findings are measured on; no entry
            # anchor goes with it, because that is a change measurement and
            # shares no reference with a state axis.
            probe = classify_delta_probe(
                freqs, (measured_s - predicted_s) + declared_db, declared_db,
                band_hz=band_hz, spatial=spatial,
                trust_ceiling_hz=self._mic_trust_ceiling_hz(freqs),
                expected_offset_db=self._applied_offset_db(),
                state_axis_only=True,
            )
        else:
            # realized − commanded == measured − predicted (the previous-graph
            # prediction cancels), so the realized curve is reconstructed from
            # the three quantities this session actually holds.
            realized_db = (measured_s - predicted_s) + commanded_db
            probe = classify_delta_probe(
                freqs, realized_db, commanded_db, band_hz=band_hz,
                declared_transfer_db=declared_db,
                trust_ceiling_hz=self._mic_trust_ceiling_hz(freqs),
                spatial=spatial,
                expected_offset_db=self._applied_offset_db(),
                entry_delta_db=self._entry_delta_db(freqs, predicted_s, commanded_db),
            )
        self._delta_probe = probe
        log_event(
            logger, "correction.crossover_v2_delta_probe",
            # The two non-rollback findings produce no refusal by design, so
            # WARNING is the only thing that puts them in front of anyone
            # reading the journal for a session that otherwise "passed"
            # (#1811, #2521). ``safety_only`` joins them for the same reason
            # one layer over (#2614): the round passed, and the shape check
            # never ran.
            level=(
                logging.WARNING
                if probe.rollback
                or probe.verdict in (
                    VERDICT_LEVEL_MISMATCH, VERDICT_FRAME_MISMATCH,
                    VERDICT_SAFETY_ONLY,
                )
                else logging.INFO
            ),
            session_id=self.session_id,
            verdict=probe.verdict,
            reason=probe.reason,
            rollback=probe.rollback,
            # Both bands, because they answer different questions: the trusted
            # one is what this capture supports, the probe one is what cleared
            # the commanded floor inside it. A disputed verdict should not need
            # a second session to establish which bins it was reached over
            # (#2521).
            trusted_band_hz=tuple(round(v, 1) for v in probe.requested_band_hz),
            probe_band_hz=tuple(round(v, 1) for v in probe.probe_band_hz),
            n_bins=probe.n_bins,
            max_error_db=round(probe.max_error_db, 3),
            rms_error_db=round(probe.rms_error_db, 3),
            worst_hz=round(probe.worst_hz, 1),
            exceedance_octaves=round(probe.exceedance_octaves, 3),
            # Was the frame removed at all, and if so what it was and what the
            # grade became without it — the demotion in #2521's policy turns on
            # exactly these, so they travel with the verdict that used them.
            frame_removed=probe.frame.fitted,
            frame_offset_db=(
                None if probe.frame.offset_db is None
                else round(probe.frame.offset_db, 3)
            ),
            frame_tilt_db_per_octave=(
                None if probe.frame.tilt_db_per_octave is None
                else round(probe.frame.tilt_db_per_octave, 4)
            ),
            # ``frame_fit``'s own ill-conditioning defence, and it has to travel
            # with the two terms it qualifies: a tilt fitted over a narrow quiet
            # span is free to be large and mean nothing (measured over 200 seeds
            # of a 10-bin quiet region, p95 |tilt| 10.5 dB/octave). It cannot
            # wrongly demote — the gate only narrows — but it does set the
            # ``frame_removed_*`` numbers a reader quotes, so the span they were
            # taken over belongs beside them.
            frame_n_bins=probe.frame.n_bins,
            frame_band_hz=(
                None if probe.frame.band_hz is None
                else tuple(round(v, 1) for v in probe.frame.band_hz)
            ),
            # Whether the realized-energy half ran at all (series-2 D1). The
            # forensic surface an operator greps, beside the numbers it governs:
            # ``boost_over_declared_bound=false`` reads as "measured, nothing
            # found" and is "not measured" whenever this is false. Two routes
            # reach that, and a first-ever round takes the FIRST: no nameable
            # previous graph, so no commanded axis, so the state-axis branch
            # above — which has no change reference to anchor against by
            # construction. The second is an ordinary round whose banked entry
            # baseline is missing or incomparable, which
            # ``…delta_probe_no_entry_anchor`` names on its own line.
            safety_anchored=probe.safety_anchored,
            frame_removed_max_db=(
                None if probe.frame_removed_max_db is None
                else round(probe.frame_removed_max_db, 3)
            ),
            frame_removed_rms_db=(
                None if probe.frame_removed_rms_db is None
                else round(probe.frame_removed_rms_db, 3)
            ),
            frame_removed_exceedance_octaves=(
                None if probe.frame_removed_exceedance_octaves is None
                else round(probe.frame_removed_exceedance_octaves, 3)
            ),
            gain_factor=(
                round(probe.gain_factor, 4)
                if probe.gain_factor is not None else None
            ),
            gain_intercept_db=(
                round(probe.gain_intercept_db, 3)
                if probe.gain_intercept_db is not None else None
            ),
            expected_offset_db=round(probe.expected_offset_db, 3),
            residual_offset_db=(
                None if probe.residual_offset_db is None
                else round(probe.residual_offset_db, 3)
            ),
            # WHAT the residual removed and WHERE it was measured (#2533). The
            # anchor is the standing pre-apply disagreement the residual is no
            # longer reporting as a level move, and ``None`` means it was not
            # measured and therefore not removed. The quiet terms bound the
            # residual's claim exactly as ``frame_n_bins``/``frame_band_hz``
            # bound the frame's: ``quiet_core_band_hz`` is the INTERQUARTILE span
            # (``frame_band_hz`` is the min/max, which two stray bins defeat) and
            # the coverage is what decides between a whole-band reason and a
            # band-scoped one.
            entry_anchor_offset_db=(
                None if probe.entry_anchor_offset_db is None
                else round(probe.entry_anchor_offset_db, 3)
            ),
            quiet_n_bins=probe.quiet_n_bins,
            quiet_core_band_hz=(
                None if probe.quiet_core_band_hz is None
                else tuple(round(v, 1) for v in probe.quiet_core_band_hz)
            ),
            quiet_probe_coverage=(
                None if probe.quiet_probe_coverage is None
                else round(probe.quiet_probe_coverage, 3)
            ),
            spatial_available=probe.spatial.available,
            spatial_widened=probe.spatial.widened,
            spatial_worst_center_hz=round(probe.spatial.worst_center_hz, 1),
            spatial_worst_widening_db=round(probe.spatial.worst_widening_db, 3),
        )
        return probe

    # --- diagnostic logging (Part 1) ------------------------------------------
    #
    # One ``log_event`` per consumed capture, on the accepted path AND every
    # rejection — pure observability, read-only against ``analysis``/the
    # session's own state. None of these calls choose a verdict or a retry;
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
            woofer_channel_map_isolation_db=woofer["channel_map_isolation_db"],
            tweeter_snr_db=tweeter["snr_db"],
            tweeter_captured_delta_db=tweeter["captured_delta_db"],
            tweeter_programmed_delta_db=tweeter["programmed_delta_db"],
            tweeter_channel_map_target_rise_db=tweeter["channel_map_target_rise_db"],
            tweeter_channel_map_cross_rise_db=tweeter["channel_map_cross_rise_db"],
            tweeter_channel_map_isolation_db=tweeter["channel_map_isolation_db"],
            # The two constants the isolation figures above are GRADED against,
            # on the same line as the numbers. The bound is what the ratio had
            # to clear; the threshold is the target rise ABOVE WHICH the ratio
            # was judged at all. Both are needed to read a line honestly: below
            # the threshold an isolation figure is published but decided
            # nothing, so the bound alone would let a sub-bound number read as
            # the cause of a refusal that never happened. A future retune of
            # either constant must not silently reinterpret a journal of old
            # lines, which is the other reason they are printed and not implied.
            channel_map_min_isolation_db=CHANNEL_MAP_MIN_ISOLATION_DB,
            channel_map_isolation_judged_above_db=(
                CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB
            ),
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
        woofer_snr_db, woofer_snr_verdict, woofer_snr_band = _driver_snr_fields(
            _driver_response_by_role(analysis, self._woofer.role)
        )
        tweeter_snr_db, tweeter_snr_verdict, tweeter_snr_band = _driver_snr_fields(
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
            # The (polarity, delay) pair is one selection on one objective
            # (#2598). ``polarity`` above is what shipped; these three say who
            # chose it, what the GCC correlation answered, and whether the two
            # agreed. A disagreement is ordinary operation — the flat-sum
            # objective outranking a correlation sign is the fix — so this line
            # is where it is legible rather than a refusal.
            alignment_objective=(cand.alignment_objective if cand else None),
            seed_polarity=(
                None if cand is None or cand.seed_polarity_sign is None
                else polarity_label(int(cand.seed_polarity_sign))
            ),
            polarity_agrees_with_sum=(
                align.polarity_agrees_with_sum if align else None
            ),
            left_anchor_lobe=(bool(cand.left_anchor_lobe) if cand else None),
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
            woofer_snr_band=woofer_snr_band,
            tweeter_snr_db=tweeter_snr_db,
            tweeter_snr_verdict=tweeter_snr_verdict,
            tweeter_snr_band=tweeter_snr_band,
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
        # the mutated session state) and the step vs baseline
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
        """Build one candidate, and return what its linearization produced — see
        :func:`~jasper.active_speaker.crossover_v2.planning.build_candidate`,
        which owns the eligibility gate, the SF2 degrade arm and the assembly.

        **The two preconditions stay HERE**, and that is the host/organ line
        rather than an accident of the move: both are facts about this SESSION
        stated in this module's own refusal vocabulary. A declared measurement
        protection map is session state the candidate never sees, and
        :class:`CrossoverV2FlowError` is this module's phase-transition error,
        which a pure module has no business raising.

        The first is ABOVE the SF2 degrade handler on purpose: raised inside it
        this was caught and degraded to a committable trims-only candidate
        (panel B1/SF2) in the wrong polarity convention. Severity, per the
        hearing lens: NOT a boost hazard — the degrade left ``linearization={}``
        and ``MeasuredCrossoverCandidate`` bounds trims cut-only to [-60, 0] dB
        — but it was still offered for Apply. Bare ``ValueError`` =>
        ``internal_error``.

        ``_plan_linearization`` and ``_exclusion_evidence_json`` are passed as
        bound attributes rather than reached for inside the organ, so a
        substituted one still binds on production (#2354) — nine substitution
        sites across two suites reach the first of them — six substituting the
        class attribute, three substituting it on a session instance.
        """
        if (self._measurement_protection_sections_by_role is not None
                and not analysis.configured_path_composed):
            raise ValueError("protected-neutral capture reached the fitter uncomposed")
        if analysis.candidate is None:
            # ``_measure_verdict`` hoisted this same check to the capture that
            # produces the analysis (2026-07-27 timing move), so reaching it
            # here means a caller that did not walk that path.
            #
            # #2291 Phase 5c-iii examined this as a duplicate and KEPT it, on
            # two measured findings rather than on caution:
            #
            # 1. The fallback is NOT the same answer. Delete this and
            #    ``build_candidate`` receives ``None``; whatever it eventually
            #    raises is a bare builtin, and
            #    ``correction_crossover_v2.classify_program_failure`` returns
            #    ``None`` for those — the catch-all arm's ``internal_error``.
            #    This raise is claimed by that classifier as
            #    ``program_unplayable``. Two different sentences for the
            #    household, so "duplicate" was never true.
            # 2. The organ CONTRACTS on it.
            #    :func:`~...crossover_v2.planning.build_candidate` takes ``cand``
            #    as an argument rather than re-reading ``analysis.candidate``,
            #    and its docstring gives the reason: the caller has already
            #    refused an analysis carrying none, under its own error
            #    vocabulary. Removing the check here does not move that
            #    responsibility anywhere — it drops it.
            #
            # Pinned by ``test_crossover_v2_planner_wiring
            # .test_the_no_candidate_refusal_is_not_the_same_as_its_fallback``.
            raise CrossoverV2FlowError("MEASURE analysis produced no candidate")
        return _planning.build_candidate(
            analysis, analysis.candidate, cloud,
            candidate_sections=candidate_sections,
            source_preset=source_preset or self._preset,
            woofer_role=self._woofer.role,
            tweeter_role=self._tweeter.role,
            plan=self._plan_linearization,
            exclusion_evidence=self._exclusion_evidence_json,
            journal=self._journal_linearization,
            # Decision 10: what the previous round's summed evidence
            # prescribed, or — when there is no instruction — what the speaker
            # is already playing. See ``_blend_prescription``.
            blend_correction=self._blend_prescription(),
            # A9/PR-B: the staged per-driver instruction, handed over RAW rather
            # than through a reader like its neighbour's. The blend field has
            # three sources to rank; this one has none — the fit is the only
            # other producer of the map it lands in, and merge-by-role IS that
            # precedence, decided where the fit is final. A method here would
            # be a pass-through with nothing to decide.
            driver_prescription=self._prescribed_driver,
        )

    def _exclusion_evidence_json(self, cloud: _CloudFitEvidence) -> dict[str, Any]:
        """The fit's cloud inputs, as the candidate's exclusion reason of record
        — see
        :func:`~jasper.active_speaker.crossover_v2.planning.exclusion_evidence_json`,
        which owns the shape and the reasons every field is in it.

        This group's pipeline result is read HERE, at call time, and that is why
        the build reaches this as a PORT rather than holding its answer as a
        value: the read must be of ``_group_cloud_result``'s CURRENT value,
        refreshed on every close including a retake's re-close (issue #1872), so
        a mapping captured when the build was wired would file an earlier
        close's registry beside this candidate.
        """
        return _planning.exclusion_evidence_json(
            cloud,
            cloud_result=self._group_cloud_result.get(PHASE_CLOUD_MEASURE) or {},
        )

    def _linearization_ineligible_reason(self, analysis: ProgramAnalysis) -> str | None:
        """HARD GATE for the Layer-1a fit path, as a named reason or ``None`` —
        see :func:`~jasper.active_speaker.crossover_v2.planning.ineligible_reason`.

        A session attribute rather than a bare module call so a substitution
        binds on production, per the sibling rule (#2354). The build calls the
        module function directly, since nothing substitutes it there — both
        routes resolve to the one definition.
        """
        return _planning.ineligible_reason(
            analysis,
            woofer_role=self._woofer.role, tweeter_role=self._tweeter.role,
        )

    def _journal_linearization(
        self,
        record: (
            JournalRecord | _accountability.GateRecord | _planning.FailureRecord
        ),
    ) -> None:
        """Emit one planner, gate or build record through this session's journal.

        A pure module owns *what happened* and returns it as data; this owns
        *how it is said* — the logger and the session identity, neither of
        which a pure function has, and neither of which travels with a record.
        (That is also why this method did not move with the organ it serves:
        two suites pin these lines to the ``crossover_v2_flow`` logger by name.)

        Three producers reach it, and they carry their payloads in deliberately
        different record types: the planner's detaches (its payload becomes
        JSON), the accountability gate's does not (its payload's logfmt bytes
        are the contract, and detaching rewrote a tuple as a list), and the
        build's carries the live exception behind an SF2 degrade, because a
        traceback cannot be reconstructed from a payload. All three expose
        ``event``/``level``/``fields``; only the third has ``exc_info``, which
        is read structurally so the other two need not grow a field they would
        never set.

        For the planner, forwarding here rather than iterating ``plan.journal``
        afterwards is what makes a fit that raises part-way still disclose the
        lines it had reached, including the ``fit_band`` line naming the corner
        it ran at.

        ``record.fields`` is spread as keyword arguments rather than handed to
        ``log_event``'s ``fields=`` parameter so the rendered order matches
        what this module emitted before the extraction: ``session_id`` first,
        then the payload in the producer's own order. (``exc_info`` binds
        ``log_event``'s own reserved parameter and never becomes a rendered
        field, so a record with a traceback renders exactly the line it did
        inline.) A payload key colliding with ``session_id``, ``exc_info`` or
        one of ``log_event``'s other keywords would raise ``TypeError`` — a
        stdlib type, so the planner's port guard contains it and names the loss
        on ``journal_dropped`` rather than costing a household its candidate.
        """
        log_event(
            logger, record.event, level=record.level,
            exc_info=getattr(record, "exc_info", False),
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
        """Assemble ONE candidate's planner request and run the pure planner —
        see :func:`~jasper.active_speaker.crossover_v2.planning.plan_for_candidate`,
        which owns the corner derivation and the request shape, and through it
        the guarantee that ``self._fc_hz`` is not merely unread but unreachable.

        :meth:`program_for_phase` is passed rather than called here because it
        can raise (before the CHECK gain solve there is no MEASURE program), and
        it must raise AFTER the candidate's own section set has been judged: a
        split or empty section set is the more specific answer and is what the
        SF2 line's ``reason=`` should name. ``plan_linearization`` is passed
        rather than imported by the organ so THIS module's name stays the one
        production resolves (#2354).

        **The ``journal_dropped`` notice stays HERE and cannot move.** It reports
        on the journal port itself, so saying it through that port would lose it
        in exactly the case it exists for — a host formatter that throws on
        every record throws on the notice too.
        """
        plan = _planning.plan_for_candidate(
            analysis, cand, cloud,
            candidate_sections=candidate_sections,
            preset=self._preset,
            program_for_phase=self.program_for_phase,
            woofer_role=self._woofer.role,
            tweeter_role=self._tweeter.role,
            driver_class_by_role=self._driver_class_by_role,
            post_apply_verifies=self.post_apply_verifies,
            cloud_phase_planned=PHASE_CLOUD_MEASURE in self._journey.plan.phases,
            plan_linearization=plan_linearization,
            journal=self._journal_linearization,
        )
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


def v2_first_begin_timeout_s() -> float:
    """The first-begin budget in force — the constant above, env-overridable.

    Four commissioning sessions on the 2026-08-16 walk died at exactly the 300 s
    default in ``phase=awaiting_begin``. Widening it is a ``jasper.env`` edit
    (``JASPER_V2_FIRST_BEGIN_TIMEOUT_S``) rather than a rebuild; out-of-range or
    unparseable values fall back to the default, mirroring the
    ``JASPER_CAPTURE_ALIGNMENT_THRESHOLD`` pattern.

    The ceiling is DERIVED from ``capture_relay.session.MAX_TTL_S`` rather than
    written here: nothing outliving the longest link the Worker grants can be
    honoured, whatever this knob says, and a second copy of that bound would be
    free to drift from it. Below the ceiling a hand-walked stage still spends
    this window out of its own ``DEFAULT_TTL_S`` link — ``.env.example`` carries
    what an operator has to weigh, and is the only place that says it.
    """

    from jasper.capture_relay.session import MAX_TTL_S

    return bounded_env_float(
        "JASPER_V2_FIRST_BEGIN_TIMEOUT_S", V2_FIRST_BEGIN_TIMEOUT_S,
        lo=30.0, hi=float(MAX_TTL_S),
    )


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


def _positioned_prompt(
    prompt: CloudPositionPrompt, shape: V2PlanShape | None,
) -> CloudPositionPrompt:
    """One pose's prompt, in the vocabulary the shape's OPERATOR acts on.

    A tap-paced shape keeps the tape-measure copy verbatim (byte-identical); a
    GATED one (:attr:`V2PlanShape.positions_gated` — the arm, or a person
    releasing each hold by hand) restates the SAME pose as its angle, because
    that is the number the gate publishes and the operator is asked for. Only
    the sentence differs — ``offset_cm`` and ``role`` are untouched, so the
    durable evidence a gated session records stays comparable with a
    tape-measured one's.
    """
    if shape is not None and shape.positions_gated:
        return remote_position_prompt(prompt)
    return prompt


def _entry_advance(shape: V2PlanShape | None) -> dict[str, str]:
    """The §5.2 auto-advance fields one plan entry carries, from its SHAPE.

    Hand-advanced shapes get :data:`AUTO_ADVANCE_TAP` and no countdown key —
    BYTE-IDENTICAL to what every entry emitted when the policy was a literal at
    each site, which is what ``_GOLDEN_V2_PLAN_BYTES`` pins. That includes a
    hand-RELEASED shape (:attr:`V2PlanShape.hand_released_positions`): its
    begins are gated, but a person is there to tap, so a countdown would fire
    while they are still walking. An externally positioned shape
    (:attr:`V2PlanShape.externally_positioned`) gets the countdown instead,
    because no hand is there to tap; its per-entry begin is then held by the
    position gate until the driver reports the angle reached, so the countdown
    only ever runs out into a capture the gate has released.

    ``shape is None`` is the recovery re-verify, which has no tier and keeps the
    tap.

    ``countdown_s`` is a STRING because ``CapturePlanEntry.screen`` is a
    ``str -> str`` map on the wire; the page does ``Number(screen.countdown_s)``.
    """
    if shape is not None and shape.externally_positioned:
        return {
            "auto_advance": AUTO_ADVANCE_COUNTDOWN,
            "countdown_s": str(AUTO_ADVANCE_COUNTDOWN_S),
        }
    return {"auto_advance": AUTO_ADVANCE_TAP}


#: The per-entry screen keys that state a remote entry's TARGET POSITION in
#: machine terms. ``screen`` is an opaque ``str -> str`` bag the page ignores
#: unknown keys in (the same seam ``auto_advance`` / ``noise_note`` /
#: ``confirm_title`` already ride), so this is not a protocol change.
#:
#: Emitted ONLY by a GATED shape (:attr:`V2PlanShape.positions_gated`). They
#: exist because the
#: position gate has to name the angle it is waiting for, and the alternative —
#: re-deriving it from the entry's index against a second copy of the plan's
#: index arithmetic — is two sources of truth for one number. The PLAN is the
#: source; the gate and the envelope read it back off the entry the runner
#: already hands them.
POSITION_DEG_KEY = "position_deg"
POSITION_ROLE_KEY = "position_role"


def _entry_policy(
    shape: V2PlanShape | None, prompt: CloudPositionPrompt | None = None,
) -> dict[str, str]:
    """One entry's non-copy ``screen`` fields: advance policy + target position.

    ``prompt is None`` means an entry with no prompted pose of its own — CHECK,
    MEASURE, the entry baseline, and stage 2's anchor — every one of which is a
    0° design-axis capture, so that is what it declares. Gating those too is
    deliberate: a gated session's operator has to put the microphone back on the
    axis for them exactly as they moved it away, and a gate that trusted "it is
    probably still there" would measure whatever the last pose left behind.

    The two halves answer different questions and so read different facts: the
    advance policy is the MOVER's (:func:`_entry_advance`), the target position
    is the GATE's (:attr:`V2PlanShape.positions_gated`).
    """
    policy = _entry_advance(shape)
    if shape is None or not shape.positions_gated:
        return policy
    degrees = position_angle_deg(prompt) if prompt is not None else 0
    role = prompt.role if prompt is not None else POSITION_ROLE_ONAX
    return {
        **policy,
        POSITION_DEG_KEY: str(degrees),
        POSITION_ROLE_KEY: role,
    }


def _cloud_entry_screen(
    *, progress: str, title: str, body: str, policy: Mapping[str, str],
) -> dict[str, str]:
    return {
        "progress": progress,
        "title": title,
        "body": body,
        **policy,
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
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
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
    # Every program below asks ``courtesy_prelude_for_phase`` for its OWN phase
    # (issue #1677): this is the phone's DURATION BUDGET, so it must agree with
    # what the session actually plays — ``crossover_v2.programs``'s
    # ``SessionExcitation`` composers, which ask the same function — or the
    # phone stops recording before the real (prelude-lengthened) program ends.
    check = build_check_program(
        roles, courtesy_prelude=courtesy_prelude_for_phase(PHASE_CHECK),
    )
    nominal_gains = {rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}
    # MEASURE's own entry and every lateral pose, which replays it verbatim.
    measure = build_measure_program(
        nominal_gains, roles,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_MEASURE),
    )
    # The entry baseline plays the VERIFY-shaped summed sweep, so its DURATION
    # is the verify program's even though stage 1 runs no VERIFY phase of its
    # own — and it is the ANNOUNCED one, because its program object is stage 2's
    # anchor (``program_for_phase``'s compared pair).
    verify = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_ENTRY_BASELINE),
    )
    # A prompted position's twin of it: same sweep, no prelude.
    cloud = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_MEASURE),
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
        lateral_prompts=lateral_prompts,
    )
    target = len(index_phase)
    verify_ms = _program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS
    cloud_ms = _program_duration_ms(cloud) + CAPTURE_ENTRY_MARGIN_MS
    measure_ms = _program_duration_ms(measure) + CAPTURE_ENTRY_MARGIN_MS
    # One policy for every entry of this plan, from the shape (§5.2). The FIRST
    # entry's value is inert either way — the page starts round 1 from the
    # spec's own begin button, and only ever reads the policy of the entry AFTER
    # an accepted capture — so a uniform value is both simplest and safe.
    advance = _entry_policy(shape)
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
                **advance,
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
                # what is coming, buys the consent back. That ruling is
                # about a HOUSEHOLD's consent, so it binds the hand-walked
                # tiers only: an externally positioned shape has no hand to
                # take a liberty with, and its entries auto-advance through
                # the same countdown vocabulary (AUTO_ADVANCE_COUNTDOWN,
                # AUTO_ADVANCE_COUNTDOWN_S, and the page's
                # renderPlanCountdown) behind the position gate. The remote
                # tier is that path's first shipped consumer — the comment
                # beside renderPlanCountdown in capture-page/js/main.js still
                # says nothing reaches it, and is stale as of this tier; it is
                # left for the next deliberate page publish rather than
                # forcing a republish (and a cache-invalidation stamp bump)
                # for a comment.
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
                **advance,
            },
        ),
    ]
    # R16's lateral walk (plan §4.4). Same 0-based index arithmetic as the cloud
    # loop below; ``duration_ms`` is the MEASURE program's because each pose
    # replays it verbatim (``program_for_phase``), not the summed sweep's.
    lateral_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_LATERAL
    ]
    lateral_table = LATERAL_POSE_PROMPTS if lateral_prompts is None else lateral_prompts
    for offset, capture_index in enumerate(lateral_indexes):
        prompt = _positioned_prompt(lateral_table[offset], shape)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="lateral",
                duration_ms=measure_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=prompt.headline,
                    body=prompt.detail,
                    policy=_entry_policy(shape, prompt),
                ),
            )
        )
    # The two prompted groups. ``index_phase`` is 1-based (the relay's own
    # index space); ``CapturePlanEntry.index`` is 0-based, hence the -1.
    cloud_measure_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_CLOUD_MEASURE
    ]
    for offset, capture_index in enumerate(cloud_measure_indexes):
        prompt = _positioned_prompt(CLOUD_POSITION_PROMPTS[offset], shape)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_measure",
                duration_ms=cloud_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=prompt.headline,
                    body=prompt.detail,
                    policy=_entry_policy(shape, prompt),
                ),
            )
        )
    # #2291's "before" measurement, LAST. Its duration is the summed sweep's
    # (``verify_ms``) because it replays the VERIFY program verbatim — the
    # identity ``program_for_phase`` guarantees and the benefit comparison
    # depends on. A tap, like every other entry — and it stays a tap through the
    # lateral pause. The original reason was that the household had just walked
    # the poses and had to come back to the mark first; with the walk off they
    # arrive here straight from MEASURE, already at the mark, so what a tap now
    # buys is the same thing every other entry gets it for: nothing starts
    # recording until a person says they are ready.
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
                    title=(
                        "Back to the design axis (0°) — one last measurement "
                        "before tuning."
                        if shape.positions_gated
                        else "Back to the mark — one last measurement before "
                        "tuning."
                    ),
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
                    policy=advance,
                ),
            )
        )
    return CapturePlan(
        capture_target=target,
        max_attempts=stage1_plan_max_attempts(
            target, include_cloud_measure=include_cloud_measure,
        ),
        schema_version=2,
        entries=tuple(entries),
    )


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
    Full is the multi-position spatial walk whose combined curve the
    after-chart, the post-apply spec verdict, and the delta probe all read.
    Running Full's
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

    # The anchor is stage 2's OPENING capture, so it is announced; the prompted
    # positions behind it are not (``courtesy_prelude_for_phase``). Two nominal
    # programs because the phone budgets each entry from the program that entry
    # will actually record.
    verify = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_VERIFY),
    )
    cloud = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_VERIFY),
    )
    verify_ms = _program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS
    cloud_ms = _program_duration_ms(cloud) + CAPTURE_ENTRY_MARGIN_MS
    if plan_shape is None:
        entry = CapturePlanEntry(
            index=0,
            kind_label="verify",
            duration_ms=verify_ms,
            screen={
                "progress": capture_progress_label(1, 1),
                "title": REVERIFY_NO_REWALK_HEADLINE,
                "body": "Put the microphone back on the mark and hold it still.",
                # No shape, so no tier: the recovery re-arm keeps the tap.
                **_entry_advance(None),
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
    advance = _entry_policy(plan_shape)
    # The two facts asked separately, because this screen reads both: the COPY
    # follows the pose statement (a gated operator is given the bearing), the
    # confirm tap below follows the advance policy (only a machine-advanced
    # session has no hand to answer it).
    positions_gated = plan_shape is not None and plan_shape.positions_gated
    externally_positioned = (
        plan_shape is not None and plan_shape.externally_positioned
    )
    anchor_screen: dict[str, str] = {
        "progress": capture_progress_label(1, target),
        "title": (
            "Back on the design axis (0°) — one sweep to check the result."
            if positions_gated
            else "Back at the mark — one sweep to check the result."
        ),
        "body": (
            f"{MARK_DISTANCE_M:g} m out, pointed at the speaker."
            if positions_gated
            else "Same spot, same height, pointed at the speaker."
        ),
        **advance,
    }
    if not externally_positioned:
        # §2.2's confirm-then-tone tap, on stage 2's own begin (D10). Same two
        # strings the single-session plan carried, so the grammar the household
        # learned in stage 1 is the grammar stage 2 opens with.
        #
        # OMITTED for an externally positioned shape, and the omission is
        # load-bearing rather than cosmetic: ``entryConfirmsBeforeArming``
        # (capture-page/js/main.js) treats a present ``confirm_title`` as "hold
        # the tone until somebody taps", so carrying it into an unattended
        # session would park the anchor on a confirm screen with no hand to
        # answer it and burn the runner's ``awaiting_arm`` budget. The promise
        # the tap makes — the microphone is where the plan says before the tone
        # plays — is the POSITION GATE's promise here, made by the driver that
        # actually moved it.
        #
        # A hand-RELEASED shape keeps the confirm, which is why this arm reads
        # the advance policy rather than ``positions_gated``: there IS a hand
        # there, and the strings are byte-identical to what every tap-paced
        # shape has always emitted.
        anchor_screen.update({
            "confirm_title": "Back on the mark, holding still?",
            "confirm_body": "Same spot, same height, pointed at the speaker.",
        })
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
        prompt = _positioned_prompt(CLOUD_POSITION_PROMPTS[offset], plan_shape)
        screen = _cloud_entry_screen(
            progress=capture_progress_label(capture_index, target),
            title=prompt.headline,
            body=prompt.detail,
            policy=_entry_policy(plan_shape, prompt),
        )
        if offset == len(cloud_verify_indexes) - 1:
            screen.update(done_screen)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_verify",
                duration_ms=cloud_ms,
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
            # Stage 2 announces its anchor and nothing behind it — same
            # derivation as stage 1's, off the same index -> phase map.
            "announced_captures": announced_capture_indexes(
                build_v2_verify_index_phase_map(plan_shape=plan_shape)
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
    TTL) was sized for the 3-entry flow. A 15-capture commission is a genuinely
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
    it is handed. The per-stage entry counts are
    :func:`tier_display_info`'s ``stage1_captures`` / ``stage2_captures``, and
    the ceiling for any one of those plans is this function applied to it —
    derive them there rather than restating numbers here, where a plan change
    cannot reach them. (An earlier revision of this docstring attributed
    ``build_v2_capture_plan()``'s BARE-DEFAULTS scenario — 10 entries, 2640 s —
    to the shipped Full tier's stage 1, which is a different and smaller plan.
    Two valid scenarios; only one of them is what ships.) **A HAND-WALKED stage
    rides ``capture_relay.session.DEFAULT_TTL_S``, and neither the split nor
    this ceiling makes it fit inside that link — this docstring must not be read
    as claiming it does.** A REMOTE stage does fit, because
    ``jasper.web.correction_crossover_v2.relay_link_ttl_s`` mints its link from
    this ceiling rather than from the default (issue #2509). At the 19-entry
    maximum the unclamped value would be 3720 s and the plan's hard cap binds at
    3600 s.
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
#
# NOT derived from the household's declared driver low limit (#2603), and that
# is deliberate rather than an oversight. A declaration DOES exist by the time
# this screen renders, but the only resolution path for it
# (``resolve_conductor_context``) is refuse-if-not-ready and can regenerate the
# crossover preview file as a SIDE EFFECT -- unacceptable for a value this
# module recomputes on every ~1.5 s poll. Reading it into this memoized,
# argument-less function instead would go stale the moment the operator edits
# the declaration, which is a worse failure than a fixed representative pair.
# So this stays a display-only fallback with its own safe-bias rationale, and
# a per-poll side-effect-free read is tracked as separate cosmetic work.
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
    each tier displays the SAME whole minutes in every case checked. The figure
    itself is this function's output; do not write it down elsewhere, and note
    that the test pins the sweep against ``tier_display_info()`` itself, so it
    cannot catch a docstring that quotes a stale figure — an earlier revision of
    this one did exactly that. The invariant is empirical rather than
    structural: the binding margin is the TIGHTEST per-stage headroom before the
    next minute boundary (the two stages ceil separately), and across that sweep
    it is a fraction of the 60 s quantum — which is what would need re-deriving
    if a future change genuinely widened the plausible band space. The
    two-stage split moved the displayed figures by its own arithmetic rather
    than by a longer session: the journey is TWO plans and each ceils to a whole
    minute separately, which is the deliberately conservative choice recorded
    below. ``capture_target`` needs no audio program at all — it is pure
    arithmetic on the resolved :class:`V2PlanShape`.

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
        # to be the whole journey's too or the chooser quotes the Full tier's
        # whole-journey count against stage 1's minutes alone. Two ceils rather
        # than one is deliberately conservative: this is a DISPLAY number and the
        # household really does pay two per-session set-ups.
        #
        # T4 owns the stage-aware WORDING this derivation makes possible ("N
        # now, M after you apply"); this is the arithmetic underneath it.
        stage1 = build_v2_capture_plan(
            _DISPLAY_ROLES_BANDS, _DISPLAY_FC_HZ, plan_shape=shape,
            include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
            include_lateral=False,
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
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
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
        lateral_prompts=lateral_prompts,
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
        # …and WHICH of those announce themselves, so the consent screen
        # states what this session plays rather than a shape that was true
        # when every capture announced.
        announced_captures=(
            announced_capture_indexes(
                build_v2_cloud_index_phase_map(
                    plan_shape=shape,
                    include_cloud_measure=include_cloud_measure,
                    include_lateral=include_lateral,
                    include_entry_baseline=include_entry_baseline,
                    lateral_prompts=lateral_prompts,
                )
            )
            if walked
            else ()
        ),
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
            lateral_prompts=lateral_prompts,
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
        # ``declared_sensitivities`` MUST match what the session composed
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
    so the session and its callers reach the one derivation path (least-
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
    "CrossoverV2Session",
    "CrossoverV2FlowError",
    # Re-exported, not used here, since #2291 Phase 5a-vii moved VERIFY's
    # integrity ladder to :mod:`.crossover_v2.capture_dispatch` (the entry
    # baseline's went to :mod:`.crossover_v2.spatial` before it). Three test
    # modules reach it as ``flow.INTEGRITY_CHECK_SWEEP_HEARD``; listing it here
    # is what says the import survived on purpose rather than as an orphan.
    "INTEGRITY_CHECK_SWEEP_HEARD",
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
    "stage1_plan_max_attempts",
    "LATERAL_CONSUMER_FC_SELECTOR",
    "LATERAL_CONSUMER_FORWARD_MODEL",
    "LATERAL_POSE_PROMPTS",
    "LATERAL_EVIDENCE_BAND_HZ",
    "LATERAL_EVIDENCE_POINTS_PER_OCTAVE",
    "LateralPose",
    "LateralPoseCurve",
    "lateral_evidence_grid_hz",
    "lateral_pose_curve",
    "PHASE_ENTRY_BASELINE",
    "REFERENCE_MARK_DESIGN_AXIS",
    "STAGE1_INCLUDES_ENTRY_BASELINE",
    "CAPTURE_PHASES",
    "CAPTURE_PLAN_TARGET",
    "CAPTURE_PLAN_MAX_ATTEMPTS",
    "V2_FIRST_BEGIN_TIMEOUT_S",
    "v2_first_begin_timeout_s",
    "ALIGNMENT_CONFIDENCE_TRUST_FLOOR",
    "MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB",
    "SWEEP_SCHEDULE_RESIDUAL_CEILING_MS",
    "SWEEP_LOCATE_CONFIDENCE_FLOOR",
    "VERIFY_PILOT_TRANSFER_STEP_CEILING_DB",
    "VERIFY_REPEAT_FLOOR_DB",
    "VERIFY_TERMINAL_OUTCOME_DETERMINISTIC",
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
    "PRESCRIBED_NON_WORSENING_DB",
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
    "REASON_VERIFY_DETERMINISTIC_MISMATCH",
    "REASON_VERIFY_CROSSOVER_REGION",
    "REASON_VERIFY_INCONCLUSIVE",
    "REASON_VERIFY_LEVEL_SHIFT",
    "verify_absolute_tolerance_db",
    "REASON_LOW_ALIGNMENT_CONFIDENCE",
    "REASON_APPLY_FAILED",
    "REASON_USER_STOPPED",
    "REASON_REVIEW_HOLD_TIMEOUT",
    "REASON_POSITION_HOLD_EXPIRED",
    "REASON_GEOMETRY_RETAKE_UNREACHABLE",
    "REASON_POSITION_TARGET_MISSING",
    "REASON_SESSION_CEILING_EXPIRED",
    "REASON_DRIVER_LEVELS_DISAGREE",
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
