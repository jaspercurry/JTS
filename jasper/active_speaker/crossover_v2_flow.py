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
FULL-tier commission is 9 captures (3 in stage 1, then 6) and an express one
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
:data:`ALIGNMENT_CONFIDENCE_TRUST_FLOOR` outlived that ruling as a hard gate
and no longer is one: the nanny burn-down made it a DISCLOSURE threshold, so
it decides what a receipt says and nothing about what is built.

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
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
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
    REASON_ATTEMPT_NOT_COMPARABLE,
    STOP_EVIDENCE,
    AttemptBudget,
    AttemptRecord,
    FloorStats,
    LoopDecision,
    decide_next,
)
from jasper.active_speaker.delta_probe import DeltaProbeMap
from jasper.active_speaker.branch_chain import CrossoverSection
from jasper.active_speaker.camilla_yaml import role_polarity
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.crossover_v2 import accountability as _accountability
from jasper.active_speaker.crossover_v2 import admission as _admission
from jasper.active_speaker.crossover_v2 import candidates as _candidates
from jasper.active_speaker.crossover_v2 import capture_dispatch as _dispatch
from jasper.active_speaker.crossover_v2 import capture_plan as _plan
from jasper.active_speaker.crossover_v2 import commanded as _commanded
from jasper.active_speaker.crossover_v2 import diagnostics as _diagnostics
from jasper.active_speaker.crossover_v2 import (
    delta_probe_run as _delta_probe_run,
)
from jasper.active_speaker.crossover_v2 import durable_state as _durable_state
from jasper.active_speaker.crossover_v2 import planning as _planning
from jasper.active_speaker.crossover_v2 import priors as _priors
from jasper.active_speaker.crossover_v2 import programs as _programs
from jasper.active_speaker.crossover_v2 import spatial as _spatial
from jasper.active_speaker.crossover_v2 import verification as _verification
from jasper.active_speaker.crossover_v2 import contracts as _contracts
from jasper.active_speaker.crossover_v2.contracts import (
    ENTRY_GRAPH_FINGERPRINT_UNKNOWN as _ENTRY_GRAPH_FINGERPRINT_UNKNOWN,
    REFERENCE_MARK_DESIGN_AXIS as _REFERENCE_MARK_DESIGN_AXIS,
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
    # A live read, not a door: ``_plan_linearization`` passes it into the
    # organ as a port so THIS name stays the one production resolves.
    plan_linearization,
)
from jasper.active_speaker.crossover_v2.plan_assembly import JournalRecord, LinearizationPlan
from jasper.active_speaker.crossover_v2.journey import (
    GROUP_PHASES,
    LATERAL_CONSUMER_FC_SELECTOR,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
    CommissionJourney,
    JourneyPlan,
    validated_lateral_consumer,
)
from jasper.active_speaker.linearization_fit import worst_headroom_cost_db
from jasper.audio_measurement.program import (
    STIMULUS_KINDS,
    ExcitationProgram,
    RoleBand,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    INTEGRITY_CHECK_SWEEP_HEARD,
    MEASURE_PAIR_SINGLE_DRIVER,
    AppliedAlignment,
    GainPlan,
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
)
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
)
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

ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED = (
    _contracts.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED
)

# Re-exported. It selects an excitation PROGRAM rather than a place in the walk,
# so #2291 Phase 5a-ii moved it to
# :mod:`jasper.active_speaker.crossover_v2.programs` alongside the composer whose
# min-cap clamp is the only level guard for all four of its members — see that
# module for why ``PHASE_ENTRY_BASELINE``'s membership is a correctness
# condition rather than an efficiency.
SUMMED_SWEEP_PHASES = _programs.SUMMED_SWEEP_PHASES

#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.contracts`, which
#: owns it alongside the two receipt fields it fills (#2291 Phase 5).  Every
#: ``flow.ENTRY_GRAPH_FINGERPRINT_UNKNOWN`` read keeps resolving to that one
#: object; see the contract for why the sentinel is a word rather than ``""``.
ENTRY_GRAPH_FINGERPRINT_UNKNOWN = _ENTRY_GRAPH_FINGERPRINT_UNKNOWN

# Re-exported from :mod:`jasper.active_speaker.crossover_v2.admission`, which
# owns them and states why each number is what it is (#2291 Phase 5a-vi).
MAX_EXTRA_ATTEMPTS_PER_POSITION = _admission.MAX_EXTRA_ATTEMPTS_PER_POSITION
ATTEMPT_INITIATOR_HOUSEHOLD = _admission.ATTEMPT_INITIATOR_HOUSEHOLD
ATTEMPT_INITIATOR_SPEAKER = _admission.ATTEMPT_INITIATOR_SPEAKER

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


# --------------------------------------------------------------------------- #
# the walk this session will do — see crossover_v2.capture_plan
# --------------------------------------------------------------------------- #
#
# The whole plan region — the position counts, the prompt table, the position
# geometry, the walk and plan shapes, the attempt budgets, the index-to-phase
# maps and the capture-plan and session-spec builders — moved to
# :mod:`jasper.active_speaker.crossover_v2.capture_plan` in wave 3 rank 4. It
# decides where the microphone goes before anything plays; this module drives
# the walk once it starts.
#
# Re-bound here under their historical names because that is where the session
# below, the endpoints suite, the conductor suite and the relay spec name them.
# A name, not a second route: there is one implementation, in the module above.
#
# **These doors are READ-ONLY**, on the same terms as the fc_sweep block above.
# A ``monkeypatch.setattr(flow, "<name>", …)`` rebinds THIS module's name and
# nothing the moved code reads, so the patch is vacuous while looking applied.
# Two suites had to be repointed at :mod:`~.crossover_v2.capture_plan` when this
# region moved for exactly that reason; patch the owning module instead.
AUTO_ADVANCE_COUNTDOWN = _plan.AUTO_ADVANCE_COUNTDOWN
AUTO_ADVANCE_COUNTDOWN_S = _plan.AUTO_ADVANCE_COUNTDOWN_S
AUTO_ADVANCE_ON_APPLY = _plan.AUTO_ADVANCE_ON_APPLY
AUTO_ADVANCE_TAP = _plan.AUTO_ADVANCE_TAP
CAPTURE_ENTRY_MARGIN_MS = _plan.CAPTURE_ENTRY_MARGIN_MS
CAPTURE_PLAN_MAX_ATTEMPTS = _plan.CAPTURE_PLAN_MAX_ATTEMPTS
CLOUD_GEOMETRY_RETRY_PROMPTS = _plan.CLOUD_GEOMETRY_RETRY_PROMPTS
CLOUD_GEOMETRY_RETRY_RISE_CM = _plan.CLOUD_GEOMETRY_RETRY_RISE_CM
CLOUD_POSITION_PROMPTS = _plan.CLOUD_POSITION_PROMPTS
CLOUD_RETAKE_ALLOWANCE = _plan.CLOUD_RETAKE_ALLOWANCE
CLOUD_VERIFY_POSE_PROMPTS = _plan.CLOUD_VERIFY_POSE_PROMPTS
CLOUD_WALK_SHAPE_TAIL = _plan.CLOUD_WALK_SHAPE_TAIL
CLOUD_WALK_SHAPE_TAIL_POST_APPLY = _plan.CLOUD_WALK_SHAPE_TAIL_POST_APPLY
CloudPositionPrompt = _plan.CloudPositionPrompt
DEFAULT_CLOUD_MEASURE_POSITIONS = _contracts.DEFAULT_CLOUD_MEASURE_POSITIONS
DEFAULT_CLOUD_VERIFY_POSITIONS = _plan.DEFAULT_CLOUD_VERIFY_POSITIONS
DEFAULT_TIER = _plan.DEFAULT_TIER
GEOMETRY_RETRY_OFFSET_CM = _plan.GEOMETRY_RETRY_OFFSET_CM
LATERAL_MARK_PROMPT = _plan.LATERAL_MARK_PROMPT
LATERAL_MARK_RETURN_PROMPT = _plan.LATERAL_MARK_RETURN_PROMPT
LATERAL_POSE_PROMPTS = _plan.LATERAL_POSE_PROMPTS
MARK_DISTANCE_M = _spatial.MARK_DISTANCE_M
MAX_CLOUD_MEASURE_POSITIONS = _plan.MAX_CLOUD_MEASURE_POSITIONS
MIN_CLOUD_MEASURE_POSITIONS = _plan.MIN_CLOUD_MEASURE_POSITIONS
MIN_CLOUD_OFFSET_CM = _plan.MIN_CLOUD_OFFSET_CM
MIN_CLOUD_VERIFY_POSITIONS = _plan.MIN_CLOUD_VERIFY_POSITIONS
POSITION_DEG_KEY = _plan.POSITION_DEG_KEY
POSITION_ROLES = _spatial.POSITION_ROLES
POSITION_ROLE_KEY = _plan.POSITION_ROLE_KEY
POSITION_ROLE_OFFAX = _spatial.POSITION_ROLE_OFFAX
POSITION_ROLE_ONAX = _spatial.POSITION_ROLE_ONAX
POSITION_ROLE_XOVR = _spatial.POSITION_ROLE_XOVR
REMOTE_VERTICAL_DISCLOSURE = _plan.REMOTE_VERTICAL_DISCLOSURE
REVERIFY_NO_REWALK_HEADLINE = _plan.REVERIFY_NO_REWALK_HEADLINE
STAGE1_INCLUDES_CLOUD_MEASURE = _plan.STAGE1_INCLUDES_CLOUD_MEASURE
STAGE1_INCLUDES_ENTRY_BASELINE = _plan.STAGE1_INCLUDES_ENTRY_BASELINE
TIERS = _plan.TIERS
TIER_EXPRESS = _plan.TIER_EXPRESS
TIER_FULL = _plan.TIER_FULL
TIER_REMOTE = _plan.TIER_REMOTE
V2PlanShape = _plan.V2PlanShape
V2_FIRST_BEGIN_TIMEOUT_S = _plan.V2_FIRST_BEGIN_TIMEOUT_S
VERIFY_ANCHOR_HOLD_MESSAGE = _plan.VERIFY_ANCHOR_HOLD_MESSAGE
WALL_CLOCK_CEILING_PER_ENTRY_S = _plan.WALL_CLOCK_CEILING_PER_ENTRY_S
WIDE_OFFSET_MIN_CM = _plan.WIDE_OFFSET_MIN_CM
_DISPLAY_FC_HZ = _plan._DISPLAY_FC_HZ
_DISPLAY_ROLES_BANDS = _plan._DISPLAY_ROLES_BANDS
_LATERAL_POSE = _plan._LATERAL_POSE
_LATERAL_POSE_OFFSETS_CM = _plan._LATERAL_POSE_OFFSETS_CM
_min_positions_for_two_wide_offsets = _plan._min_positions_for_two_wide_offsets
_pose = _plan._pose
_program_duration_ms = _plan._program_duration_ms
announced_capture_indexes = _plan.announced_capture_indexes
assert_cloud_plan_fits_relay_capacity = _plan.assert_cloud_plan_fits_relay_capacity
build_v2_capture_plan = _plan.build_v2_capture_plan
build_v2_cloud_index_phase_map = _plan.build_v2_cloud_index_phase_map
build_v2_session_spec = _plan.build_v2_session_spec
build_v2_verify_capture_plan = _plan.build_v2_verify_capture_plan
build_v2_verify_index_phase_map = _plan.build_v2_verify_index_phase_map
build_v2_verify_session_spec = _plan.build_v2_verify_session_spec
capture_progress_label = _plan.capture_progress_label
cloud_capture_target = _plan.cloud_capture_target
cloud_geometry_retry_reach_cm = _plan.cloud_geometry_retry_reach_cm
cloud_plan_max_attempts = _plan.cloud_plan_max_attempts
cloud_walk_reach_cm = _plan.cloud_walk_reach_cm
cloud_walk_reach_cm_of = _plan.cloud_walk_reach_cm_of
cloud_walk_shape = _plan.cloud_walk_shape
express_cloud_measure_positions = _plan.express_cloud_measure_positions
format_position_distance = _plan.format_position_distance
normalize_tier = _plan.normalize_tier
position_angle_deg = _plan.position_angle_deg
position_elevation_deg = _plan.position_elevation_deg
position_geometry = _plan.position_geometry
relay_plan_attempts_required = _plan.relay_plan_attempts_required
remote_cloud_measure_positions = _plan.remote_cloud_measure_positions
remote_cloud_verify_positions = _plan.remote_cloud_verify_positions
remote_position_prompt = _plan.remote_position_prompt
resolve_plan_shape = _plan.resolve_plan_shape
session_wall_clock_ceiling_s = _plan.session_wall_clock_ceiling_s
stage1_base_entries = _plan.stage1_base_entries
stage1_plan_max_attempts = _plan.stage1_plan_max_attempts
tier_display_info = _plan.tier_display_info
tier_is_externally_positioned = _plan.tier_is_externally_positioned
v2_first_begin_timeout_s = _plan.v2_first_begin_timeout_s
verify_pose_table = _plan.verify_pose_table
walk_shape_for = _plan.walk_shape_for
wall_clock_ceiling_s = _plan.wall_clock_ceiling_s


# :mod:`jasper.active_speaker.crossover_v2.refusal_copy`
#
# #2291 Phase 5c-ii moved the whole household vocabulary there — the reason
# codes, the remediation templates, the ``ReasonSpec``/``RetryableReasonCopy``
# carriers, the ``REASON_REGISTRY`` that binds a code to its sentence and its
# retry budget, the copy selectors, and ``PhaseVerdict``. Spine-in-package
# forced vocabulary-in-package:
# ``test_no_domain_module_imports_the_host_or_the_legacy_flow`` forbids any
# module there importing this one. Where the vocabulary PHILOSOPHICALLY
# belongs is a separate, still-open question (issue #2390).
#
# What follows is what THIS module reads. Every other consumer imports from
# the owning module directly.
#
# Substituting one of these names here rebinds THIS module's name and nothing
# else, so it binds only for readers inside this module. A reader that lives in
# the package resolves the owning module directly and would not see the patch:
# patch that module, or inject through the ports its caller takes.
# --------------------------------------------------------------------------- #

from jasper.active_speaker.crossover_v2.refusal_copy import (
    NON_RETRIABLE_CODES,
    REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_LOCATE_FAILED,
    REASON_GEOMETRY_RETAKE_UNREACHABLE,
    REASON_REGISTRY,
    REASON_VERIFY_DETERMINISTIC_MISMATCH,
    REASON_VERIFY_INCONCLUSIVE,
    REASON_VERIFY_LEVEL_SHIFT,
    REASON_VERIFY_OUT_OF_TOLERANCE,
    PhaseVerdict,
    _screen_refusal_code,
    reason_diagnosis,
    reason_message,
    round_restore_reason,
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
    PRESCRIBED_NON_WORSENING_DB as _PRESCRIBED_NON_WORSENING_DB,
)


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
    _gate_entanglement_floor as _gate_entanglement_floor,
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


from jasper.audio_measurement import measurement_geometry as _measurement_geometry

#: Where this module reads the operator's declared rig from. Its own name so a
#: test can point the capture path at a file of its own; the writer's default
#: is :data:`~jasper.audio_measurement.measurement_geometry.DEFAULT_PATH`.
DECLARED_GEOMETRY_PATH = _measurement_geometry.DEFAULT_PATH


def _declared_first_bounce_s(distance_m: float | None) -> float | None:
    """The operator-declared rig's first bounce at ONE capture's distance.

    Read FRESH on every capture rather than resolved once at session start:
    ``jasper-declare-geometry`` is a separate process writing a wizard-owned
    file under :data:`DECLARED_GEOMETRY_PATH`, and a session that cached it
    would keep publishing a floor the operator has since corrected. ``None``
    when nothing is declared, which every consumer publishes as
    ``entanglement_floor_source = unknown`` (#3502) — absent is the ordinary
    state and warns about nothing.

    **A file that exists and cannot be read is unknown HERE, not a raised
    round.** The reader raises on one, deliberately, so ``jasper-declare-geometry
    show`` can report it; on the capture path the same exception would abort
    every attempt and every seat of the round over a fact that clamps nothing,
    so it is journaled once and read as undeclared.
    """
    try:
        return _measurement_geometry.declared_first_bounce_s(
            distance_m, path=DECLARED_GEOMETRY_PATH
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log_event(
            logger, "correction.crossover_v2_declared_geometry_unreadable",
            level=logging.WARNING,
            path=str(DECLARED_GEOMETRY_PATH),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None


# --------------------------------------------------------------------------- #
# tuning constants
# --------------------------------------------------------------------------- #

# Re-exported from :mod:`jasper.active_speaker.crossover_v2.programs`, which
# owns the level policy it belongs to (#2291 Phase 5a-ii).
GAIN_CAP_BACKOFF_DB = _programs.GAIN_CAP_BACKOFF_DB
# Per gain-adjusted clip retry, drop the offending program's level by this much.
CLIP_RETRY_BACKOFF_DB = 3.0
# Re-exported; see ``crossover_v2.programs`` (#2291 Phase 5a-ii).
PILOT_LEVEL_DELTA_DB = _programs.PILOT_LEVEL_DELTA_DB
# Re-exported from :mod:`jasper.active_speaker.crossover_v2.capture_dispatch`,
# which owns it beside the screen that reads it.
LOCATE_MIN_CONFIDENCE = _dispatch.LOCATE_MIN_CONFIDENCE
# Re-exported from :mod:`jasper.active_speaker.crossover_v2.contracts`,
# which owns it.
VERIFY_TOLERANCE_DB = _contracts.VERIFY_TOLERANCE_DB


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.verification`,
#: which owns it beside the claim record that reads it.
verify_absolute_tolerance_db = _verification.verify_absolute_tolerance_db


# The prescribed on-axis mic distance the parallax correction assumes (§5.2).
MEASUREMENT_DISTANCE_M = 1.0
# Below this GCC-seed/capture confidence (see ``AlignmentEstimate.confidence``
# and ``confidence_source`` in ``program_analysis.py``), the capture is
# ACCEPTED and the confidence is banked as a reservation — see
# ``_note_alignment_confidence_reservation``. **It is a disclosure trigger, not
# a gate**, since the nanny burn-down: it refused MEASURE and spent a retry
# until then, and the one live bench datum undercut it (two captures
# at ~0.677, one accepted and one refused 58 s apart). Converting it did not
# recalibrate it — 0.6 is the same number, and only what crossing it does
# changed.
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
# wildly outside it.
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
# delay-plausibility backstop, and the SNR/linearity/glitch verdicts still
# REFUSE. Accountability's item 1 is no longer on that list — doctrine
# deviation (i) demoted it to a disclosure of its own, on the same reasoning
# this paragraph makes for the ripple. This one number stopped being a veto
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
# builders that used to read it here moved to
# :mod:`~jasper.active_speaker.crossover_v2.capture_plan` in wave 3 rank 4 and
# import it from the owner, so **nothing in this module reads this name any
# more**. It survives as a door, pinned by
# ``test_crossover_v2_programs.test_the_flow_re_exports_resolve_to_the_one_definition``
# — do not delete it as dead on an importer grep alone.
courtesy_prelude_for_phase = _programs.courtesy_prelude_for_phase


# Re-exported from :mod:`jasper.active_speaker.crossover_v2.contracts`, which
# owns it: :mod:`~jasper.active_speaker.crossover_v2.capture_plan` raises it too
# and cannot import this module, so the class had to sit below both.
CrossoverV2FlowError = _contracts.CrossoverV2FlowError


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
#: Every existing ``flow.alignment_to_candidate_fields`` import resolves to
#: that one function.
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


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.capture_dispatch`,
#: which owns the CHECK/MEASURE/VERIFY screens this predicate serves.
_stimulus_locate_ok = _dispatch._stimulus_locate_ok




def _any_sweep_clipped(analysis: ProgramAnalysis) -> bool:
    return any(
        loc.clipped for loc in analysis.locations if loc.kind in STIMULUS_KINDS
    )


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.verification`,
#: which owns it beside the spec report it reads a band out of.
_band_edge = _verification._band_edge

#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.verification`,
#: which owns both beside the spec report they read.
_flatness_tilt_log_field = _verification._flatness_tilt_log_field
_per_band_flatness_log_field = _verification._per_band_flatness_log_field


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


#: Re-exported from the modules that own the plan §7 claim vocabulary: the
#: three states in :mod:`~jasper.active_speaker.crossover_v2.contracts`, the
#: ungraded-branch reason beside the record that grades it.
CLAIM_PASS = _contracts.CLAIM_PASS
CLAIM_FAIL = _contracts.CLAIM_FAIL
CLAIM_NOT_EVALUATED = _contracts.CLAIM_NOT_EVALUATED
CLAIM_NO_PER_BRANCH_CAPTURE = _verification.CLAIM_NO_PER_BRANCH_CAPTURE

#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.verification`,
#: which owns the four reducers that build one VERIFY capture's record.
_verify_evidence_from_tracking = _verification._verify_evidence_from_tracking
_verify_graded_band_from_tracking = (
    _verification._verify_graded_band_from_tracking
)
_verify_claims = _verification._verify_claims
_verify_frame_from_tracking = _verification._verify_frame_from_tracking


# (``_flatness_evidence_from_tracking`` lived here until the
# flat-linearization plan's PR-5. It repackaged one VERIFY capture's own
# grid-and-band-mean flatness number for the RESULT/verify_fail screens; that
# number is retired along with ``program_analysis._flatness_tracking``, and the
# flatness the household sees now comes from the cloud group's spec evaluation
# — ``assemble_cloud_group_result``'s ``flatness`` key, one construction, one
# owner. See that function and ``flat_spec.spec_flatness_gauge``.)


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.diagnostics`,
#: which owns them beside the diag lines that read them.
PILOT_SNR_UNUSABLE_DB = _diagnostics.PILOT_SNR_UNUSABLE_DB
_worst_pilot_snr_db = _diagnostics._worst_pilot_snr_db


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
    ``jasper.active_speaker.playback.TonePlaybackBackend``.
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
# Banks one accepted capture: ``(capture_result, record)`` -> the store id that
# finds the record again, or ``""`` when nothing was stored. The record carries
# its own ``take_id`` (:func:`~.crossover_v2.spatial.take_id_for`), so the store
# names the take rather than the seam re-minting it.
BankTake = Callable[[Any, Mapping[str, Any]], str]


def _no_bank_take(_result: Any, _record: Mapping[str, Any]) -> str:
    """The unbound record seam: nothing is stored, and that is not an error.

    A session with no evidence store runs its groups with no durable per-take
    artifact — the correct behaviour, and the ordinary state of every session
    unit test. Answering ``""`` here rather than leaving the seam ``None`` is
    what lets every retention site call it unguarded: the one caller that
    reads the answer (:meth:`CrossoverV2Session._retain_entry_baseline`) cannot
    tell an unbound seam from a store that refused, and does not need to.
    """
    return ""


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
    # Evidence retention, called once per ACCEPTED capture of every retained
    # kind — the cloud position, the lateral pose, the entry baseline, and
    # the three phases that prompt no spot. The PRODUCTION binding is the
    # fail-soft one, so no call site here guards it: see
    # :func:`_no_bank_take` for what the default answers and why "" is the
    # whole vocabulary a caller needs.
    bank_take: BankTake = _no_bank_take
    # PR-4: the cloud honesty-pipeline bundle publisher, called once per
    # CLOSED group with ``(phase, cloud_group_result_dict)``. Optional so every
    # pre-PR-4 construction site (and every session unit test) stays valid, and
    # ``None`` means the group's result is computed and readable via
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
    # #2291: is a prior candidate recorded to restore TO? The STATE half of
    # the adoption table's ``rollback_available``; the SEAM half is
    # ``rollback`` above being bound at all, and
    # :func:`~jasper.active_speaker.crossover_v2.coordinator.rollback_available`
    # ANDs them. Optional, and its absence reads as "cannot confirm a prior
    # candidate" rather than as "there is one" — see that function for why the
    # pessimistic direction is the safe one here.
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


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.durable_state`,
#: which owns the durable document this snapshot is persisted into and the
#: readers that take it apart again.
V2ConductorSnapshot = _durable_state.V2ConductorSnapshot
attempt_history_from_state = _durable_state.attempt_history_from_state
attempt_record_from_verify = _durable_state.attempt_record_from_verify
_attempt_optional_float = _durable_state._attempt_optional_float


# One prompted position's attempt ledger (owner ruling #2086). Re-exported from
# :mod:`jasper.active_speaker.crossover_v2.admission`, which owns the ledger and
# the admission decision that reads it (#2291 Phase 5a-vi). Kept importable from
# the flow because that is where the endpoints suite and the capture-sequence
# pins name it.
SlotAttempts = _admission.SlotAttempts


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.spatial`,
#: which owns what a retained take records.
_CloudPosition = _spatial._CloudPosition


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
LateralPose = _spatial.LateralPose
lateral_evidence_grid_hz = _spatial.lateral_evidence_grid_hz
lateral_pose_curve = _spatial.lateral_pose_curve
_primary_sweep_bands = _spatial._primary_sweep_bands


# One retained position -> a capture, and the combined group's verdict
# reduction. Re-exported from
# :mod:`jasper.active_speaker.crossover_v2.spatial`.
cloud_position_capture = _spatial.cloud_position_capture
_geometry_verdict_from_combined = _spatial._geometry_verdict_from_combined


def combine_cloud_positions(positions: Sequence[_CloudPosition]) -> Any:
    """Combine a closed group, and journal a combiner failure.

    The emitting half of
    :func:`~jasper.active_speaker.crossover_v2.spatial.combine_cloud_positions`,
    which owns the seam and its whole contract — one combine per group close,
    never raising, ``None`` for an unusable cloud. Kept as a function rather
    than a bare re-export for two reasons: this is where the journal call is
    made, and ``_close_cloud_group`` reaches it as a module global, which is
    what lets ``test_close_cloud_group_calls_the_combiner_exactly_once`` count
    the calls.
    """
    result = _spatial.combine_cloud_positions(positions)
    _diagnostics._emit_cloud_combine_diagnostics(logger, result.diagnostics)
    return result.combined


def cloud_geometry_verdict(positions: Sequence[_CloudPosition]) -> dict[str, Any]:
    """Combine, read ``.geometry``, and journal a combiner failure.

    The emitting half of
    :func:`~jasper.active_speaker.crossover_v2.spatial.cloud_geometry_verdict`,
    which owns the verdict shape and the reason-string divergence it discloses.
    """
    result = _spatial.cloud_geometry_verdict(positions)
    _diagnostics._emit_cloud_combine_diagnostics(logger, result.diagnostics)
    return result.verdict


# --------------------------------------------------------------------------- #
# PR-4: contract-derived analysis bands + the live-flow honesty pipeline
# --------------------------------------------------------------------------- #
#
# The reduction itself — the contract-derived echo band, the carve-out
# disclosure, the group's validity and trusted floors, and the assembled group
# result — lives in :mod:`jasper.active_speaker.crossover_v2.spatial`, beside
# the ``_CloudPosition`` group and the combiner whose answer it reduces. What
# stays here is the EMITTING half, the same split :func:`combine_cloud_positions`
# above already carries: the pure module hands its journal fields back as data
# and this module owns the event names.
cloud_validity_floor_hz = _spatial.cloud_validity_floor_hz
cloud_trusted_floor_hz = _spatial.cloud_trusted_floor_hz


def _derive_cloud_echo_band_hz(
    signal_band_hz: tuple[float, float],
    tweeter_measurement_band_hz: tuple[float, float] | None,
) -> _spatial._CloudEchoBand:
    """Derive the echo band, and journal whichever clamp produced it.

    The emitting half of
    :func:`~jasper.active_speaker.crossover_v2.spatial._derive_cloud_echo_band_hz`,
    which owns the derivation and its whole contract. Each of the three
    disclosed corners has its own event name, picked here off the provenance
    the derivation returns rather than passed down into it, so a reader who
    greps for the slug lands on the module that owns the vocabulary.
    """
    band = _spatial._derive_cloud_echo_band_hz(
        signal_band_hz, tweeter_measurement_band_hz,
    )
    if band.diagnostics is None:
        return band
    if band.source == "passband_fallback":
        event = "correction.crossover_v2_cloud_echo_band_degenerate"
    elif band.source == "clamp_degenerate_default":
        event = "correction.crossover_v2_cloud_echo_band_clamp_degenerate"
    else:
        event = "correction.crossover_v2_cloud_echo_band_clamped_to_hf_regime"
    log_event(logger, event, level=logging.WARNING, **band.diagnostics)
    return band


def assemble_cloud_group_result(
    combined: Any,
    *,
    echo_band_hz: tuple[float, float],
    echo_band_provenance: Mapping[str, Any] | None = None,
    validity_floor_hz: float | None = None,
    trusted_ceiling_hz: float | None = None,
    tier: str = "",
    position_records: Sequence[Mapping[str, Any]] = (),
    crossover_region_hz: tuple[float, float] | None = None,
    graded_spec_sink: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Assemble the closed group's result, and journal a pipeline failure.

    The emitting half of
    :func:`~jasper.active_speaker.crossover_v2.spatial.assemble_cloud_group_result`,
    which owns the wiring contract (issue #1742 item 4) and every property of
    the payload. Kept as a function rather than a bare re-export for the two
    reasons :func:`combine_cloud_positions` states: this is where the journal
    event lives, and ``_run_cloud_pipeline`` reaches it as a module global,
    which is what lets the conductor suite substitute a failing pipeline.
    """
    answer = _spatial.assemble_cloud_group_result(
        combined,
        echo_band_hz=echo_band_hz,
        echo_band_provenance=echo_band_provenance,
        validity_floor_hz=validity_floor_hz,
        trusted_ceiling_hz=trusted_ceiling_hz,
        tier=tier,
        position_records=position_records,
        crossover_region_hz=crossover_region_hz,
        graded_spec_sink=graded_spec_sink,
    )
    if answer.diagnostics is not None:
        log_event(
            logger, "correction.crossover_v2_cloud_pipeline_failed",
            level=logging.WARNING, **answer.diagnostics,
        )
    return answer.result


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


# The committed region the crossover-region null registry is asked about.
# Re-exported from :mod:`jasper.active_speaker.crossover_v2.verification`.
committed_crossover_region_hz = _verification.committed_crossover_region_hz


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
    both are normalized to their OWN low-mid reference
    (``flat_spec.REFERENCE_BAND_HZ``), so what survives the comparison is
    SHAPE — which is what the spec grades. It is a coarse direction check,
    and the threshold its caller applies is sized to that.

    Graded with neither clamp: this is a MODEL curve, so it has no gate to
    derive a trusted floor from and no microphone to derive a ceiling from.
    Both sides of the ledger comparison this feeds are graded the same way,
    which is what keeps them comparable.
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
        fc_hz: float | None,
        driver_caps_dbfs: Mapping[str, float],
        session_volume_db: float,
        seams: V2FlowSeams,
        driver_sweep_duration_limits_s: Mapping[str, float] | None = None,
        tier: str = "",
        positions_gated: bool = False,
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
        lateral_claims: Sequence["_spatial.TakeClaim"] = (),
        verify_prompts: Sequence[CloudPositionPrompt] | None = None,
    ) -> None:
        roles = tuple(roles_bands)
        if not 1 <= len(roles) <= 2:
            raise CrossoverV2FlowError("a v2 session walks one or two drivers")
        self.session_id = str(session_id)
        self.sound_design_revision = sound_design_revision
        # Which INSTRUMENT this session is running. Empty = unknown (a caller
        # that never declared one), never silently ``TIER_FULL`` — see
        # ``V2ConductorSnapshot.tier`` for why guessing is the dishonest
        # option. Validated so an unknown id fails at construction rather than
        # riding into the durable state and out to `/state`.
        self._tier = normalize_tier(tier) if tier else ""
        # #2879, the POSE-STATEMENT axis (:attr:`V2PlanShape.positions_gated`):
        # are this walk's begins HELD until the microphone is reported in place?
        # Resolved by the HOST, the half that knows the capture source, and ORed
        # with the tier's own answer so no caller can drop the arm's own gate.
        self._positions_gated = (
            tier_is_externally_positioned(self._tier) or bool(positions_gated)
        )
        self._preset = source_preset
        self._roles = roles
        # The declared roles, lowest first. ``_tweeter`` is ``None`` on a 1-way
        # main, never aliased to ``_woofer``, which would double count it.
        self._role_names = tuple(band.role for band in roles)
        self._woofer = roles[0]
        self._tweeter: RoleBand | None = roles[1] if len(roles) == 2 else None
        self._tweeter_role = None if self._tweeter is None else self._tweeter.role
        # Why this session evaluates no driver PAIR, or ``None`` when it does.
        self._pair_reason = None if len(roles) > 1 else MEASURE_PAIR_SINGLE_DRIVER
        self._fc_hz = None if fc_hz is None else float(fc_hz)
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
        # PR-4: the contract-derived analysis bands for the cloud-group honesty
        # pipeline (combine's echo/signal bands, the null gate's search band) --
        # computed once so every group-close event uses the SAME values. See
        # ``programs.measurement_band_hz`` / _derive_cloud_echo_band_hz.
        self._cloud_signal_band_hz = _programs.measurement_band_hz(roles)
        # The band AND its provenance travel as one value (issue #1763), so
        # the pipeline payload can never publish an applied band without the
        # disclosure of how it was derived.
        self._cloud_echo_band = _derive_cloud_echo_band_hz(
            self._cloud_signal_band_hz, tweeter_measurement_band_hz,
        )
        self._caps = dict(driver_caps_dbfs)
        # Per-role longest admissible ONE sweep, so the MEASURE composer cannot
        # overshoot the ceiling admission judges it against (#2921). A role
        # absent here composes at its nominal duration.
        self._sweep_duration_limits_s = dict(driver_sweep_duration_limits_s or {})
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
        # (``correction_crossover_v2.prepare_v2_session``, either stage, from
        # ``_resolve_driver_class_by_role``); the empty default remains for
        # callers with no declaration, matching
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
                index_phase_map if index_phase_map is not None
                else _plan.DEFAULT_INDEX_PHASE_MAP,
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
        # What each pose of the walk was measured UNDER (#3498) — the banked
        # candidate, and the graph axes its stop carried — in the same order as
        # the prompts above. Empty on every walk that measures the speaker as
        # it stands, which is every shipped one.
        self._lateral_claims: tuple[_spatial.TakeClaim, ...] = tuple(lateral_claims)
        # The POST-APPLY walk's pose set, resolved through the same one resolver
        # the plan builder uses so the session and the plan cannot read
        # different tables — the desync ``V2PlanShape`` exists to close, applied
        # to the poses rather than to their count.
        self._verify_prompts: tuple[CloudPositionPrompt, ...] = verify_pose_table(
            verify_prompts
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

        # What this session may play, how loud, and for how long — frozen
        # together so a subset cannot drift (#2291 Phase 5a-ii;
        # ``crossover_v2.programs`` owns the level policy).
        self._excitation = _programs.SessionExcitation(
            roles=self._roles,
            caps_dbfs=self._caps,
            session_volume_db=self._session_volume_db,
            fc_hz=self._fc_hz,
            sweep_duration_limits_s=self._sweep_duration_limits_s,
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
        # verify-only re-arm REHYDRATED this from the previous session's
        # persisted ``verify_priors``, so the reference
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
        # comparator. The verify-only re-arm threads it so this session can
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
        self._measure_alignment_reservation: dict[str, Any] | None = None
        # Audit gauntlet 5a: the accepted MEASURE ran with no resolved
        # measurement-mic calibration. ``True`` when reservation-worthy,
        # ``None`` otherwise — same reset lifecycle and the same reason as
        # the two reservations above (describes THE ACCEPTED CAPTURE alone).
        # See ``_note_mic_calibration_reservation``.
        self._measure_calibration_reservation: bool | None = None

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
        consults.

        Read per MEASURE analysis rather than cached at session open, since
        that is the moment the answer has to be true: one small JSON read a
        few times per session, against a stale field that would silently
        outlive an out-of-band reconcile.
        """
        from jasper.active_speaker.baseline_profile import (
            load_applied_baseline_profile_state,
        )

        tweeter_role = self._tweeter_role
        if tweeter_role is None:
            # An inter-driver arrival gap needs two drivers.
            return None
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
                tweeter_role=tweeter_role,
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

    def _measure_sweep_bounds(self) -> tuple[float, float] | None:
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

        The closing pose is the first at-mark pose AFTER the walk's last
        off-mark one, so a program whose at-mark repeats all sit before the
        first move brackets nothing and answers ``None``.
        """
        poses = self._lateral_poses
        left_the_mark = max(
            (i for i, p in enumerate(poses) if not p.at_mark), default=-1
        )
        opening = next((p for p in poses if p.at_mark), None)
        closing = next((p for p in poses[left_the_mark + 1:] if p.at_mark), None)
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
        instrument from the one the accountability seam grades on.
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
    def measure_alignment_reservation(self) -> dict[str, Any] | None:
        """The banked reservation about an accepted low-confidence alignment.

        ``{"confidence": float, "delay_us": float, "trust_floor": float}``, or
        ``None`` when the alignment cleared the floor or no estimate existed.
        Provenance for the prescriber to weigh, which is what §4 says a
        confidence heuristic is. It renders no household sentence: this is a
        capture that was ACCEPTED, and spending a household's attention on a
        demoted heuristic is the non-event the sibling property above declines
        to report.

        Copied on the way out, like every other dict-valued property here.
        """
        reservation = self._measure_alignment_reservation
        return dict(reservation) if reservation else None

    @property
    def measure_calibration_reservation(self) -> bool | None:
        """``True`` when the accepted MEASURE ran with no resolved
        measurement-mic calibration; ``None`` when it was calibrated, or the
        fact was never resolved (audit gauntlet 5a).

        Unlike its two dict-shaped siblings above there is no number to
        carry — the disclosure IS the fact — so this is a bare tri-state
        rather than a reservation record.
        """
        return self._measure_calibration_reservation

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
        group's indexes map onto its own table from the front: the group's
        ``i``-th index (0-based) takes ``table[i]``, exactly as the matching
        plan builder enumerates them. Running off the end is refused UPSTREAM
        for each group — the pre-apply cloud by ``_validated_cloud_counts``, the
        post-apply walk by :func:`build_v2_verify_capture_plan`, which refuses a
        shape and a pose set that disagree — but a defensive fallback keeps a
        prompt-less capture from being a crash rather than a retake, for a
        session constructed with an index map no plan builder produced.
        """
        offsets = self._journey.plan.group_offsets(phase)
        try:
            position = offsets.index(index)
        except ValueError:
            position = 0
        # Three groups, three tables, one front-loading rule and one builder
        # enumeration order. R16's lateral walk and (since the 2026-08-24
        # geometry ruling) the post-apply walk each have their own; the
        # pre-apply cloud is the shared table's own walker.
        table = (
            self._lateral_prompts if phase == PHASE_LATERAL
            else self._verify_prompts if phase == PHASE_CLOUD_VERIFY
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
            index_ = min(used - 1, len(CLOUD_GEOMETRY_RETRY_PROMPTS) - 1)
            rung = CLOUD_GEOMETRY_RETRY_PROMPTS[index_]
            # Rung 2 is COMPOUND — 75 cm sideways AND 30 cm up — so its rise is
            # stated rather than left to read as mark height, which is what a
            # record built from ``offset_cm`` alone would claim.
            rise_cm = CLOUD_GEOMETRY_RETRY_RISE_CM[index_]
            return CloudPositionPrompt(
                rung,
                offset_cm=GEOMETRY_RETRY_OFFSET_CM,
                role=POSITION_ROLE_OFFAX,
                vertical_sign=1 if rise_cm else 0,
                vertical_offset_cm=rise_cm,
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
        see — a restore door that clears the durable flag holds no conductor
        and so could not tell the owner. (The round's adoption restore no
        longer clears the flag at all: it re-applies the prior candidate
        through the normal path, so the speaker stays applied.)
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
            measure_sweep_durations_s=_priors.measure_sweep_durations_s(
                self._measure_program
            ),
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
        self, index: int, attempt: int, result: Any,
    ) -> dict[str, Any]:
        """Analyze one uploaded capture and advance (or reject) the phase."""
        phase = self._phase_of_index(index)
        slot = self._slot_of_index(index)
        # ONE table answers both "which priors is this capture analyzed
        # against" and "which consumer grades it" — the chains it replaces
        # dispatched on the same phase with different arm sets, and their
        # fallbacks graded a phase neither enumerated as post-apply VERIFY
        # against empty priors. Group members are keyed per phase because
        # ``PHASE_LATERAL`` is in ``GROUP_PHASES`` yet reads its own priors
        # and consumer — never dispatch on group membership here. ``partial``
        # binds the two phase-aware consumers to the shared call shape.
        dispatch: dict[
            str,
            tuple[
                Callable[[], MeasurementPriors],
                Callable[[int, int, ProgramAnalysis, Any], PhaseVerdict],
            ],
        ] = {
            PHASE_CHECK: (self._check_priors, self._consume_check),
            PHASE_MEASURE: (self._measure_priors, self._consume_measure),
            PHASE_LATERAL: (self._lateral_priors, self._consume_lateral_pose),
            PHASE_CLOUD_MEASURE: (
                self._cloud_priors,
                partial(self._consume_cloud_position, PHASE_CLOUD_MEASURE),
            ),
            PHASE_CLOUD_VERIFY: (
                self._cloud_priors,
                partial(self._consume_cloud_position, PHASE_CLOUD_VERIFY),
            ),
            PHASE_ENTRY_BASELINE: (
                self._entry_baseline_priors, self._consume_entry_baseline,
            ),
            PHASE_VERIFY: (
                self._verify_priors,
                partial(self._consume_verify, phase=PHASE_VERIFY),
            ),
        }
        if phase not in dispatch:
            raise CrossoverV2FlowError(
                f"no capture consumer for phase {phase!r}"
            )
        priors_of, consume = dispatch[phase]
        program = self.program_for_phase(phase)
        priors = priors_of()
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
        verdict = consume(index, attempt, analysis, result)
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
            # Read, never recomputed: the one verdict carrying this code
            # stashed THIS capture's record via ``_set_verify_outcome``.
            gate_record = self._verify_gate
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
            _diagnostics._log_condition_settled(
                logger, phase, index, observed, kind, diagnosis,
                session_id=self.session_id,
            )
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
            _diagnostics._log_slot_spent(
                logger, phase, index, observed, kind,
                session_id=self.session_id,
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
                _diagnostics._log_slot_spent(
                    logger, phase, index, observed, kind,
                    session_id=self.session_id,
                    diagnosis=diagnosis,
                    pilot_heard=verdict.pilot_heard,
                    reflection_measured=verdict.reflection_measured,
                )
                return self._settled_group_verdict(
                    phase, index, {"kept_earlier_take": True}
                )
            if kind == _admission.SETTLE_POSITION_UNRESOLVED:
                self._group_unresolved[phase][index] = observed
                _diagnostics._log_slot_spent(
                    logger, phase, index, observed, kind,
                    session_id=self.session_id,
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
            _diagnostics._log_slot_spent(
                logger, phase, index, observed, kind,
                session_id=self.session_id,
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

    def _note_accepted(self, phase: str, index: int) -> None:
        # The journey's group-close rule: a position the flow gave up on
        # (``_group_unresolved``) counts as resolved too, because the relay
        # advanced past it and the phase would otherwise never close.
        # ``_group_positions`` remains the sole record of what was MEASURED.
        self._journey.accept(phase, index)

    # --- per-phase verdicts --------------------------------------------------
    #
    # Each ``_consume_<phase>`` is a thin wrapper around the verdict logic in
    # ``_<phase>_verdict``, which is where every accept/reject decision lives
    # and the only place one may be made. A wrapper does three things and
    # decides nothing: take the verdict, bank the capture when it was accepted,
    # and log that capture's full numeric diagnostics (on the accepted path AND
    # every rejection) through ``_safe_log_diag`` — never the raw
    # ``_log_*_diag`` call directly, so a bug in the logging path can neither
    # crash nor flip a verdict already decided above it. Retention gets the
    # same protection from the other direction — see ``V2FlowSeams.bank_take``,
    # which states the fail-soft rule once for every site that banks.

    def _consume_unprompted(
        self,
        phase: str,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
        verdict: PhaseVerdict,
        log_diag: Any,
    ) -> PhaseVerdict:
        """Bank an accepted unprompted capture, journal every one, decide nothing.

        The shared body of the three arms whose phase prompts no spot. They
        differed only in which verdict they took and which diagnostic they
        logged, and a fourth copy is where the accepted-only rule starts being
        true in two places and false in the third.
        """
        if verdict.accepted:
            self._bank_phase_capture(phase, index, attempt, analysis, result)
        _diagnostics._safe_log_diag(
            logger, log_diag, analysis, verdict, session_id=self.session_id,
        )
        return verdict

    def _consume_check(
        self, index: int, attempt: int, analysis: ProgramAnalysis, result: Any,
    ) -> PhaseVerdict:
        return self._consume_unprompted(
            PHASE_CHECK, index, attempt, analysis, result,
            self._check_verdict(analysis), self._log_check_diag,
        )

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

    def _consume_measure(
        self, index: int, attempt: int, analysis: ProgramAnalysis, result: Any,
    ) -> PhaseVerdict:
        return self._consume_unprompted(
            PHASE_MEASURE, index, attempt, analysis, result,
            self._measure_verdict(analysis), self._log_measure_diag,
        )

    def _bank_phase_capture(
        self,
        phase: str,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> None:
        """Bank one take for a phase with no prompted spot.

        CHECK, MEASURE and VERIFY play from wherever the microphone already is,
        so what is banked is the CAPTURE — its bytes, the identity that finds
        them again, and the complex responses it measured
        (:meth:`_banked_curves`). Each phase's own VERDICT stays where the
        phase puts it: those are rewritten inside a round, and a take is what
        survives it.

        On an ACCEPTED verdict only, which is the rule every other retained
        kind already follows. A refused capture is not evidence of the speaker,
        it is evidence of the room or the phone, and the journal is where that
        is recorded.

        Fail-soft is the binding's, exactly as it is for every other retained
        kind — see the seam's own field for the one statement of that rule.

        **The baseline comparand is stated when the session has one**, which is
        what lets ``take_kind`` classify a VERIFY take rather than leaving it
        unresolved: by VERIFY the round's pre-apply graph is known, and it is
        the one fact that separates a post-apply re-measure from a baseline.
        CHECK and MEASURE reach here before any baseline exists and bank an
        honestly empty kind — CHECK has none by design, and MEASURE's
        comparand is minted after it banks.
        """
        baseline = self._measure_entry_baseline
        self._seams.bank_take(
            result,
            _spatial.phase_capture_record(
                phase=phase,
                index=index,
                attempt=attempt,
                curves=self._banked_curves(phase, analysis),
                claim=_spatial.TakeClaim(
                    baseline_fingerprint=(
                        baseline.graph_fingerprint if baseline is not None else ""
                    ),
                ),
                **self._capture_stamp(result),
            ),
        )

    def _capture_stamp(self, result: Any) -> dict[str, Any]:
        """The four facts every banked take states about its OWN capture.

        Whose session it belongs to, which graph it went through, when it was
        taken, and the digest that verifies the bytes. Assembled once because
        the builders take exactly these four keyword-for-keyword, and a
        fourth hand-assembly is where the clock format or the fingerprint
        source starts to differ between kinds that a single index has to sort.
        """
        return {
            "session_id": self.session_id,
            "graph_fingerprint": self._entry_graph_fingerprint(),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wav_sha256": _capture_wav_sha256(result),
        }

    def _banked_curves(
        self, phase: str, analysis: ProgramAnalysis,
    ) -> list[dict[str, Any]]:
        """WHAT THIS CAPTURE MEASURED, or nothing — never the take.

        Ruling S3 for the three kinds that are not a walk pose: the complex
        responses an analysis computed land in no file at all unless they land
        on the take. See
        :func:`~jasper.active_speaker.crossover_v2.spatial.analysis_curve_records`
        for the shape and for which analysis produces which responses.

        **Guarded, and not decoratively.** The walk reaches the resampler only
        after a ladder that has already refused every degenerate response
        (``test_the_resampler_really_does_raise_on_an_empty_axis`` pins the
        ``IndexError``); the three sites that call this have their own screens,
        which are not that one. A raise here would cost the CAPTURE — the sweep
        played, the operator is standing at the mark, and a verdict already
        decided above would be lost to a serialization failure. The caught
        tuple is concrete: ``IndexError`` from the resampler on an empty axis,
        ``AttributeError``/``TypeError`` from a foreign or half-populated
        analysis, ``ValueError`` from a malformed band. ``program_for_phase``
        is NOT in it — every caller of this reaches it through
        :meth:`consume_capture`, which resolved the same phase's program before
        any verdict existed, so a session that could not name it never got here.

        **``[]`` from this method means the curves were LOST**, which reads the
        same in the record as CHECK measuring none. The journal line is the
        discriminator; the record does not carry one, and it should not grow a
        sentinel unless this arm is ever seen to fire.
        """
        try:
            return _spatial.analysis_curve_records(
                analysis, self.program_for_phase(phase),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            log_event(
                logger, "correction.crossover_v2_take_curves_failed",
                level=logging.WARNING, session_id=self.session_id, phase=phase,
                exc_info=True,
            )
            return []

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

    def _note_mic_calibration_reservation(self) -> None:
        """Bank the disclosure that the accepted MEASURE had no resolved mic
        calibration (audit gauntlet 5a).

        Same contract as :meth:`_note_ripple_reservation`, deliberately: it
        records and decides nothing, and must never acquire a branch that
        could change what the caller already decided — the capture is
        ACCEPTED either way. ``bind_production_analyze``'s own docstring
        states why: "the analysis still runs — relative timing/level stay
        valid" without a resolved calibration, so nothing here is a
        component-damage or hearing-safety mechanism (measurement-loop-
        doctrine.md §4's closed list, which this is not on) — only the
        absolute-SPL commissioning stop (``mic_calibration_unavailable``,
        a DIFFERENT calibration concern from a DIFFERENT flow) hard-stops on
        missing calibration, and this disclosure leaves it untouched.

        WARNING for :meth:`_note_ripple_reservation`'s reason: the session
        proceeds, so this is not an error, but the household is being handed
        a tuning built on an uncalibrated reading and an operator reading the
        journal at INFO would have to know to look for it.
        """
        self._measure_calibration_reservation = True
        log_event(
            logger, "correction.crossover_v2_mic_calibration_disclosed",
            level=logging.WARNING, session_id=self.session_id,
        )

    def _note_alignment_confidence_reservation(
        self, confidence: float, delay_us: float
    ) -> None:
        """Bank the reservation about an accepted low-confidence alignment.

        The GCC trust floor used to REFUSE here and spend a retry. It named no
        damage mechanism, and §4 names its exact category as excluded — "geometry
        blindness, beaming priors, **confidence heuristics**, prediction-engine
        rankings — is provenance, not a gate". The one live bench datum
        undercuts it directly: the 2026-08-03 validation found two captures
        both at ~0.677 confidence, one accepted and one refused, "so confidence
        was never the discriminator the reused reason code claimed it was".

        Same contract as :meth:`_note_ripple_reservation`, deliberately: it
        records and decides nothing, and must never acquire a branch that could
        change what the caller already decided. What still refuses is the
        PHYSICS half, which now has its own screen kind
        (``capture_dispatch.SCREEN_DELAY_IMPLAUSIBLE``) and its own sentence —
        a confidently wrong lag is a measured failure mode, not a prior.

        WARNING for :meth:`_note_ripple_reservation`'s reason: the session
        proceeds, so this is not an error, but the alignment term the fit
        builds on is less trustworthy than usual and an operator reading at
        INFO would have to know to look for it.
        """
        self._last_measure_guard = "alignment_confidence_disclosure"
        self._measure_alignment_reservation = {
            "confidence": float(confidence),
            "delay_us": float(delay_us),
            # The floor rides WITH the value, for the reason the ripple
            # disclosure's threshold does: a rendered "0.41, below 0.6" becomes
            # a lie the moment the constant moves.
            "trust_floor": float(ALIGNMENT_CONFIDENCE_TRUST_FLOOR),
        }
        log_event(
            logger, "correction.crossover_v2_alignment_confidence_disclosed",
            level=logging.WARNING,
            session_id=self.session_id,
            confidence=round(float(confidence), 3),
            delay_us=round(float(delay_us), 1),
            trust_floor=float(ALIGNMENT_CONFIDENCE_TRUST_FLOOR),
        )

    def _measure_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        # Reset every call — a stale value from a PRIOR attempt must never
        # leak into THIS attempt's diagnostic (see __init__'s comment).
        self._last_measure_guard = ""
        self._measure_ripple_reservation = None
        self._measure_alignment_reservation = None
        self._measure_calibration_reservation = None
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
                # A callable: the physical backstop (Fix 3) is asked ONLY of an
                # estimate that already cleared the rung above, and it reads
                # the preset's declared search bound.
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
        # "proceed" from meaning "unchecked". None of the three refuses now:
        # level-frame, realized-level (deviation (i)) and predicted-improvement
        # all bank, and each says what it measured.
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
        if (
            analysis.alignment is not None
            and analysis.alignment.confidence < ALIGNMENT_CONFIDENCE_TRUST_FLOOR
        ):
            self._note_alignment_confidence_reservation(
                analysis.alignment.confidence, analysis.alignment.delay_us
            )
        # Audit gauntlet 5a: disclose, never block — an explicit ``False``
        # only (never ``None``, which is "not resolved either way" per
        # ``ProgramAnalysis.mic_calibrated``'s own contract, and must not be
        # guessed into a reservation it never earned).
        if analysis.mic_calibrated is False:
            self._note_mic_calibration_reservation()
        pair_claim: dict[str, Any] = {}
        solo_reason = analysis.measure_pair_not_evaluated
        if solo_reason is not None:
            # A candidate is still built and published below: the solo's own
            # evidence is what the linearization is fitted from.
            log_event(
                logger, "correction.crossover_v2_measure_solo",
                session_id=self.session_id,
                reason=solo_reason,
                roles=",".join(self._role_names),
                responses=len(analysis.driver_responses),
            )
            pair_claim = _contracts.measure_pair_claim(solo_reason)
        elif analysis.candidate is None:
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
            return PhaseVerdict(True, payload={
                "measurement_phase": PHASE_MEASURE, **pair_claim,
            })
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
                **pair_claim,
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
        return _spatial.group_position_floor(phase)

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
            at_mark=prompt.at_mark,
            curves=tuple(curves),
        )
        log_event(
            logger, "correction.crossover_v2_lateral_pose",
            session_id=self.session_id, pose_id=pose.pose_id, index=index,
            attempt=attempt, offset_cm=pose.offset_cm, position_role=pose.role,
            vertical_deg=position_elevation_deg(prompt),
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
            payload: dict[str, Any] = {"pose_id": pose.pose_id}
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

        Both angles come off that prompt through the shipped derivations, so a
        pose the operator was sent BOTH sideways and up records both numbers
        rather than claiming mark height for a microphone somebody raised.
        """
        self._seams.bank_take(
            result,
            _spatial.lateral_pose_record(
                pose,
                position_deg=position_angle_deg(prompt),
                vertical_deg=position_elevation_deg(prompt),
                lateral_consumer=self._lateral_consumer,
                claim=self._lateral_claim(pose.index),
                **self._capture_stamp(result),
            ),
        )

    def _lateral_claim(self, index: int) -> "_spatial.TakeClaim":
        """What the pose at this capture index was measured under.

        Positioned by ``group_offsets(PHASE_LATERAL).index``, the derivation
        :meth:`_cloud_prompt` uses to pick this index's prompt out of the same
        walk table, so the claim and the prompt can never come from two
        different positions of one walk. An empty claim for every pose no walk
        stated one for, which is every shipped walk.
        """
        offsets = self._journey.plan.group_offsets(PHASE_LATERAL)
        try:
            position = offsets.index(index)
        except ValueError:
            return _spatial.TakeClaim()
        claims = self._lateral_claims
        return claims[position] if position < len(claims) else _spatial.TakeClaim()

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
        _diagnostics._safe_log_diag(
            logger,
            lambda a, v: _diagnostics._log_cloud_diag(
                logger, phase, index, a, v,
                session_id=self.session_id,
                positions_in=len(self._group_positions.get(phase, ())),
            ),
            analysis, verdict, session_id=self.session_id,
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
            geometry=position_geometry(prompt),
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

        **The record is assembled whether or not a retention seam is bound**,
        because ``_group_position_meta`` below reads it either way — see
        :func:`~jasper.active_speaker.crossover_v2.spatial.cloud_position_record`
        for the two consumers that ordering serves, and for what each field of
        the record is.

        The added cost when no seam is bound is one small dict, one SHA-256 of
        the capture's WAV bytes (:func:`_capture_wav_sha256`), and one
        resample-plus-serialize of the summed response (:meth:`_banked_curves`,
        ~120 points × 3 arrays) — a few milliseconds per accepted position,
        ~10 times per session.

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
        # THIS seat's distance, not the rig's: the room floor rises with
        # distance, so the pose's own mark distance is what the declared
        # geometry is evaluated at.
        bounce_s = _declared_first_bounce_s(position.geometry.mark_distance_m)
        gate = _gate_record(position.response, declared_first_bounce_s=bounce_s) or {}
        # The room survives a capture with no gating block, which is the one
        # state ``_gate_record`` reports as no record at all — see
        # ``_gate_entanglement_floor``.
        entanglement_floor_hz, entanglement_floor_source = (
            (gate["entanglement_floor_hz"], gate["entanglement_floor_source"])
            if gate
            else _gate_entanglement_floor(
                position.response, declared_first_bounce_s=bounce_s
            )
        )
        metadata = _spatial.cloud_position_record(
            position_id=position.position_id,
            phase=phase,
            index=position.index,
            attempt=position.attempt,
            prompt=position.prompt,
            wide=position.wide,
            role=position.role,
            geometry=position.geometry,
            captured_at=position.captured_at,
            session_id=self.session_id,
            gate_window_ms=_gate_window_ms(position.response),
            gate_floor_source=_gate_floor_source(position.response),
            gate_disclosure=gate.get("disclosure"),
            gate_moved_rms_db=gate.get("moved_rms_db"),
            gate_reflection_delay_ms=gate.get("reflection_delay_ms"),
            gate_entanglement_floor_hz=entanglement_floor_hz,
            gate_entanglement_floor_source=entanglement_floor_source,
            validity_floor_hz=getattr(
                position.response, "validity_floor_hz", None
            ),
            gating_applied=bool(gating.get("applied")),
            summed_ripple_db=analysis.summed_ripple_db,
            glitch_detected=bool(analysis.glitch_detected),
            wav_sha256=_capture_wav_sha256(result),
            curves=self._banked_curves(phase, analysis),
        )
        self._group_position_meta.setdefault(phase, {})[
            position.position_id
        ] = metadata
        self._seams.bank_take(result, metadata)

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
        if retake is not None and self._positions_gated:
            # REFUSE rather than prompt (owner ruling: refuse, don't mislead) —
            # for EITHER gated shape, because what decides is "these begins are
            # HELD", not "an arm is moving". Prompting did three dishonest
            # things at once, and the third belongs to the gate, not the arm:
            #
            #   1. it asked for a pose an external positioner cannot reach —
            #      ``CLOUD_GEOMETRY_RETRY_PROMPTS`` rung 1 is 75 cm off the
            #      mark, past every pose in the walk, and rung 2 goes ABOVE it;
            #   2. it recorded that un-made pose's 75 cm offset as the
            #      position's durable evidence; and
            #   3. the retry re-authorizes the SAME plan entry, so the position
            #      gate republishes that entry's ORIGINAL bearing while the
            #      screen names the wider spot. Two answers to where the
            #      microphone should be — and a person, who COULD walk to the
            #      wider spot, is exactly who cannot be told which to believe.
            #      Rung 2 has no bearing spelling at all: it is a HEIGHT.
            #
            # The retry budget is deliberately NOT spent and no take is dropped
            # — nothing here is a retry, so the group keeps the evidence it
            # legitimately has for whatever the session does with it next.
            log_event(
                logger,
                "correction.crossover_v2_geometry_retake_unreachable",
                level=logging.WARNING,
                session_id=self.session_id,
                phase=phase,
                tier=self._tier,
                # `tier` cannot carry this: stage 2 is constructed without one,
                # so both gated stage-2 shapes log `tier=""`. This names the
                # PREDICATE that refused, not WHICH shape — that is constant-true
                # here by construction, and the mover is recoverable from this
                # session's `…_remote_session_open` line (`hand_released=`).
                gated=self._positions_gated,
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
            # never saw the evidence. The ethos's "least-bad measured, honed
            # in bites" ruling makes that unacceptable ("every round, kept or
            # restored or refused, banks its measurement into the series
            # state"), and the seam was also a second owner of "restore the
            # previous graph" beside ``coordinator._run_round_restore``.
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
        the confirm refits from scratch, so a household that hits a fit bug
        sees the identical failure, raised from the identical place, at the
        identical moment they would have seen it before this rider existed.
        (Accountability used to be on that list; neither of its items refuses
        any more — deviations (c) and (i).) The cost is one wasted fit on a session
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
                # on guessing the fit's raise surface. The accountability
                # veto used to be the one raise outside the named families
                # this file's other boundaries use; both its items now bank
                # instead, which narrows what can arrive here but does not
                # make it enumerable. ``Exception``, not
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
        load-bearing measurements live — the realized inter-driver level
        (item 1) and the spec-graded prediction (item 2). **NEITHER refuses.**
        Item 2 stopped with the nanny burn-down (#2854, deviation (c)) and
        item 1 with the realized-level demotion (deviation (i)); both now GRADE,
        bank what they measured, and let the round proceed. They still run
        AFTER the build and BEFORE ``self._candidate`` is set and
        ``publish_candidate`` fires, because that is where the numbers they
        grade exist.

        They live here and not inside :meth:`_build_candidate` on purpose: that
        method's SF2 arm catches a fit-engine failure and degrades to the
        trims-only path, which is the right answer for a BUG in the fit and the
        wrong answer for an accountability verdict — a verdict banked about a
        candidate nobody built describes a graph that was never proposed.

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
        producing a candidate, not part of proposing one. It refuses nothing
        (deviations (c) and (i)) — what it produces is the level-frame record
        this method returns, banked against the candidate it is about.

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
        # PR-L4: the last GRADING before a candidate can be proposed at all.
        # It refuses nothing since deviations (c) and (i); what it returns is
        # the level-frame record, which the publish below banks so the review
        # screen offers the proposal WITH what was measured about it.
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

    def _previous_graph_predicted_sum(self, analysis: Any, capture_fc_hz: float | None) -> Any:
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

        roles = self._role_names
        if len(roles) > 1 and capture_fc_hz is None:
            # The commanded axis over a PAIR is a statement about a crossover.
            # A 1-way main is not that case: its lone branch IS the graph.
            return _absent("no_crossover_to_command")
        seam = self._seams.applied_profile
        if seam is None:
            return _absent("no_applied_profile_seam")
        try:
            profile = seam()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _absent("applied_profile_unreadable", error=str(exc))
        corner = _commanded.corner_disagreement(profile, capture_fc_hz)
        if corner is not None:
            return _absent(corner.reason, **corner.fields)
        # The DRAFT's declared per-role polarity, which the measured branches
        # already carry (``program_analysis._compose_configured_path_ir``). The
        # profile records absolute flags, so without this the previous side is
        # stated in a different frame from the applied side's
        # ``alignment.polarity_sign``.
        try:
            draft_inverted = role_polarity(self._preset)
        except ActiveSpeakerConfigError as exc:
            return _absent("draft_polarity_unreadable", error=str(exc))
        previous = _commanded.previous_graph_prediction(
            profile,
            roles=roles,
            draft_inverted_by_role=draft_inverted,
            responses={
                response.role: response
                for response in (analysis.driver_responses or ())
            },
            alignment=analysis.alignment,
        )
        if isinstance(previous, str):
            return _absent(previous)
        graph, predicted = previous
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
        self, analysis: Any, predicted_sum: Any, capture_fc_hz: float | None,
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
            realized_branch_level=_contracts.realized_branch_level(
                built.linearization.realized_branch_level,
                pair_reason=self._pair_reason,
            ),
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
          retakes past leaves no finding: the record describes the frame behind
          a specific proposal. Neither accountability item is on that list any
          more — both stopped refusing (deviations (c) and (i)), so a level
          verdict arrives here as a record to bank, never as a missing
          candidate.
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
            # LEVEL FRAME, not level estimator: this publish carries
            # realized-only records too. `…_publish_failed` and not
            # `…_finding_failed`, because `…_level_frame_finding` is a RETIRED
            # name (#2609) that must not start matching a live line again.
            log_event(
                logger, "correction.crossover_v2_level_frame_publish_failed",
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
        self, combined: Any, floor_hz: float | None, ceiling_hz: float | None,
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
        this). Both are TOLD to this method rather than read here, so the
        frame these rows are stated in and the one the spec graded in are the
        same two numbers rather than two derivations of them. An absent floor
        or ceiling widens the band rather than narrowing it, which is the
        honest direction: no evidence of an edge is not an edge at zero.

        **Never raises and never gates.** This is disclosure riding a close
        that already decided; losing the whole group's result to an arithmetic
        surprise here would be exactly the trade the pipeline refuses.
        """
        from jasper.audio_measurement.spatial_combine import position_residuals

        try:
            freqs = np.asarray(getattr(combined, "freqs_hz", ()), dtype=float)
            if freqs.size == 0:
                return ()
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
        lands at 20 kHz — the table's own taper zero, and the grid's own top
        edge (2026-08-29 horn-droop correction ruling; was ~16.4 kHz, the
        first bin past the table's then-16 kHz taper zero).

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

        **No production path calls this today.** Its one caller was the
        accountability level refusal, deleted by doctrine deviation (i); the
        admission seam builds ``CaptureBeginRefused`` inline, already holding a
        classified ``decision``. Kept because it alone owns the stamp below, so
        the next refusal here reaches for a correct constructor rather than a
        fifth inline one — and the endpoint test that drives the host's refusal
        plumbing raises through it instead of re-implementing that stamp.

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
        capture's own relay verdict did. Asking the selector rather than the
        spec is what stops the two accounts diverging again the first time a
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

        The ONE input the gate is TOLD rather than reaches for is the
        prediction threshold; that module's docstring records why it stays
        owned here. It used to be three: item 2 was handed a reason code to
        refuse under, and the nanny burn-down took both the code and the
        refusal; item 1's went the same way with the realized-level demotion
        (doctrine deviation (i)), and there is no level refusal left to name.

        **Write-then-say, and the ordering that matters.** The stash is
        installed before the journal is emitted. That used to be observable
        only in the negative — no refusal arm reached the stash — and now it is
        not observable at all, because no arm here refuses. The ordering is
        still pinned rather than argued in ``test_crossover_v2_accountability``,
        which is what keeps it from drifting back.

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
                _PRESCRIBED_NON_WORSENING_DB if prescribed_graph
                else PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
            ),
        )
        # Unconditional: item 2 is reached on every path the gate takes, so
        # `spec_report` is always written — `None` meaning "graded nothing",
        # which is the value the stash should then hold. The
        # `spec_report_written` guard went with the deleted level refusal, the
        # one return that could reach here without item 2 having run.
        self._measure_predicted_spec_report = decision.spec_report
        for record in decision.journal:
            self._journal_linearization(record)
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
        # One reading of the tier's trust ceiling for this group, spent twice
        # below: the spec may not GRADE above where the fitter may not
        # COMMAND (#2649), and the per-position residuals are already read
        # over that same frame.
        ceiling_hz = (
            None if combined is None
            else self._mic_trust_ceiling_hz(getattr(combined, "freqs_hz", ()))
        )
        result = assemble_cloud_group_result(
            combined,
            echo_band_hz=self._cloud_echo_band.band_hz,
            echo_band_provenance=self._cloud_echo_band.disclosure(),
            position_records=tuple(
                self._group_position_meta.get(phase, {}).values()
            ),
            validity_floor_hz=cloud_validity_floor_hz(positions),
            trusted_ceiling_hz=ceiling_hz,
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
            combined, floor_hz, ceiling_hz,
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
        _diagnostics._safe_log_diag(
            logger,
            lambda a, v: _diagnostics._log_entry_baseline_diag(
                logger, index, a, v,
                session_id=self.session_id,
                baseline=self._measure_entry_baseline,
            ),
            analysis, verdict, session_id=self.session_id,
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
            reference_mark=_REFERENCE_MARK_DESIGN_AXIS,
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

        ``bank_take`` is reused rather than duplicated even though this is not
        a :data:`GROUP_PHASES` member, so an entry baseline lands in
        ``refs["position_artifacts"]`` beside every other retained take and one
        replay path covers both. Being outside the group bookkeeping is exactly
        why the call is explicit here: nothing else would make it.

        **The only retention site that reads the seam's answer.** Evidence retention is forensics, never a gate — the binding
        fail-softs, so a full disk cannot turn an acoustically-good baseline
        into a retake, and the reduced record (which is what the round actually
        grades) is banked whether or not any bytes were stored. What the answer
        decides is only whether this baseline can CITE a durable artifact.

        **The retained take carries the reduced CURVE, not only its scalars**
        (fragment ``02`` duplication #2). The same arrays go into the flow
        state file, which is what stage 2 reads inside this round; the take is
        what survives it, because a take is write-once and the state file is
        rewritten on every persist. Both are written from ``measured`` here, so
        neither is a copy of the other.
        """
        metadata = _spatial.entry_baseline_record(
            index=index,
            attempt=attempt,
            program_id=measured.program_id,
            reference_mark=measured.reference_mark,
            **self._capture_stamp(result),
            freqs_hz=measured.curve.hz,
            magnitude_db=measured.curve.db,
            excluded=measured.excluded,
            validity_floor_hz=getattr(
                analysis.summed_response, "validity_floor_hz", None
            ),
            gate_window_ms=_gate_window_ms(analysis.summed_response),
            summed_ripple_db=analysis.summed_ripple_db,
            glitch_detected=bool(analysis.glitch_detected),
            curves=self._banked_curves(PHASE_ENTRY_BASELINE, analysis),
        )
        # The TAKE id and not the store's record id: ``artifact_ref`` is read
        # back out of the bundle by
        # :func:`~.crossover_v2.position_cycle.read_entry_baseline_take`, which
        # answers a banked take's ``take_id`` under this name. One vocabulary,
        # so an in-session baseline and a re-read one name the same thing.
        artifact_ref = (
            str(metadata["take_id"])
            if self._seams.bank_take(result, metadata) else ""
        )
        from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

        self._measure_entry_baseline = EntryBaseline.from_measurement(
            measured,
            graph_fingerprint=str(metadata["graph_fingerprint"]),
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
                reference_mark=_REFERENCE_MARK_DESIGN_AXIS,
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
                # Which epoch that ordinal counts in, from the same durable
                # read — a republish restarts the sequence, and the pair says
                # so where the ordinal alone cannot.
                round_ordinal_epoch=position.ordinal_epoch,
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

    def _consume_verify(
        self,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
        *,
        phase: str,
    ) -> PhaseVerdict:
        # ``phase`` is REQUIRED rather than defaulted: the dispatch table in
        # ``consume_capture`` binds it, and a hardcoded ``verify`` here would
        # let a differently-phased caller bank its capture durably as
        # post-apply tracking evidence — a mislabel written into a write-once
        # record rather than a verdict.
        verdict = self._consume_unprompted(
            phase, index, attempt, analysis, result,
            self._verify_verdict(analysis), self._log_verify_diag,
        )
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

        Both rulings (#2291 5b-ii) are stated inline at their arms below, and
        every act sits beside them: building the record, the
        ``record_model_error`` seam call with the guard that decides whether
        to make it, its ``except`` arms, all of its log lines, the identity
        conflict it can report, the decision payload the household reads, and
        the history append. "How many times can that write fire" is answered
        by reading this method alone.

        **Exactly-once survives a failed write, and that is why the seam call
        catches broadly** (#2386). The rung that stops a second write is the
        already-recorded skip above, and it can only see a repeat
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

        # The identity is the APPLIED candidate's, most specific first: the
        # tuning attempt id the durable state carries, then the built
        # candidate's own fingerprint (read only when no id is in hand — on
        # the stage that grades a round the id is the only rung populated,
        # and the flow makes no candidate read there), then a session-scoped
        # fallback unique per capture. The order is the point: two captures
        # of one applied candidate must land on one id, or the dedup below
        # cannot see a repeat — and an empty fingerprint is as absent as no
        # candidate, which keeps an unidentifiable capture out of another
        # attempt's identity.
        attempt_id = self._tuning_attempt_id
        if not attempt_id and self._candidate is not None:
            attempt_id = str(getattr(self._candidate, "fingerprint", "") or "")
        if not attempt_id:
            attempt_id = f"{self.session_id}:{capture_attempt}"
        if any(item.attempt_id == attempt_id for item in self._attempt_history):
            # The applied candidate is already in accepted history: a repeated
            # successful re-verify is not a new tuning attempt, and this skip
            # is the one rung between a replayed capture and a second durable
            # observation of one candidate identity.
            return

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
                # new and asks the seam a SECOND time.
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
        # The arm ORDER is a ruling (#2033): evidence refusal outranks grading
        # preconditions. A capture that failed its integrity checks is
        # answered as a capture problem even on a speaker with no adopted
        # floor — answering no-floor first would mask a rejected recording
        # behind a configuration sentence. And the LAST arm is the one that
        # makes a claim about the speaker, so any future arm that cannot
        # decide must degrade toward the evidence refusal, never toward
        # ``decide_next``.
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
            # Usable capture, no adopted claim floor: the attempt is recorded
            # ungraded rather than graded against nothing.
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

        NaN reaches it: the caller gates on the integration claim, whose own
        ``max <= tolerance`` is ``False`` for ``nan``, so an unmeasurable grade
        is a ``fail`` rather than the pass an earlier reading of the raw number
        gave it (#3487).

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
        gate_record = _gate_record(
            analysis.summed_response,
            declared_first_bounce_s=_declared_first_bounce_s(MARK_DISTANCE_M),
        )
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
        # Gated on the CLAIM just recorded, not on a second reading of the same
        # number: R18's vocabulary is three-valued and ``not_evaluated`` is
        # first-class, so a claim nobody could grade must not read as one that
        # failed (#3487). A republished candidate has no measure round behind
        # it — ``handle_v2_republish`` clears ``verify_priors`` on purpose and
        # states the consequence: such a VERIFY grades INDETERMINATE, which a
        # refusal is not.
        if self._verify_claims["integration"]["status"] == CLAIM_FAIL:
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
    #
    # The axes, the classifier call and the journal line belong to
    # :mod:`jasper.active_speaker.crossover_v2.delta_probe_run`; what stays here
    # is the session state it reads and the one attribute its verdict lands on.

    def _run_delta_probe(self) -> DeltaProbeMap | None:
        """Classify what the speaker actually did against what was commanded.

        Runs twice per full session and once per express one: at VERIFY, on
        the at-the-mark map alone, and again at the post-apply group's close,
        where the spatial arm becomes measurable. The second call can only
        ever ADD evidence — VERIFY has already refused the session if the mark
        arm did not match — so the later verdict supersedes the earlier.

        Returns what
        :func:`~jasper.active_speaker.crossover_v2.delta_probe_run.run_delta_probe`
        graded, and stamps it. A run that graded nothing returns ``None`` and
        leaves the earlier verdict standing rather than erasing it.
        """
        probe = _delta_probe_run.run_delta_probe(
            logger,
            session_id=self.session_id,
            tracked=self._verify_tracking_curve,
            commanded=self._measure_commanded_delta,
            band_hz=self._verify_trusted_band_hz,
            declared=self._measure_declared_transfer,
            entry_baseline=self._measure_entry_baseline,
            measure_band_spread=self._group_band_spread.get(
                PHASE_CLOUD_MEASURE, (),
            ),
            verify_band_spread=self._group_band_spread.get(
                PHASE_CLOUD_VERIFY, (),
            ),
            trust_ceiling=self._mic_trust_ceiling_hz,
            applied_offset_seam=self._seams.applied_offset_db,
            program_for_phase=self.program_for_phase,
        )
        if probe is not None:
            self._delta_probe = probe
        return probe

    # --- diagnostic logging (Part 1) ------------------------------------------
    #
    # The fields and the emitters belong to
    # :mod:`jasper.active_speaker.crossover_v2.diagnostics`; what stays here is
    # the session state each line reads. These three keep their method form
    # because they ARE the seam: ``_consume_unprompted`` takes the bound method
    # as its ``log_diag`` argument, so an instance-level patch is what the
    # wrapped call resolves.

    def _log_check_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        _diagnostics._log_check_diag(
            logger, analysis, verdict,
            session_id=self.session_id,
            woofer_role=self._woofer.role,
            tweeter_role=self._tweeter_role,
        )

    def _log_measure_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        _diagnostics._log_measure_diag(
            logger, analysis, verdict,
            session_id=self.session_id,
            roles=self._role_names,
            sample_rate_hz=self.program_for_phase(PHASE_MEASURE).sample_rate_hz,
            gate_window_ms=self._measure_gate(analysis),
            gate_floor_source=self._measure_gate_floor_source(analysis),
            guard=self._last_measure_guard,
        )

    def _log_verify_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        _diagnostics._log_verify_diag(
            logger, analysis, verdict,
            session_id=self.session_id,
            verify_frame=self._verify_frame,
            verify_claims=self._verify_claims,
            verify_pilot_transfer_step_db=self._verify_pilot_transfer_step_db,
            measure_gate_window_ms=self._measure_gate_window_ms,
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

        The second is scoped to a session that OWES a measured pair candidate:
        a 1-way main's analysis carries none by design.

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
        roles = self._role_names
        if (self._measurement_protection_sections_by_role is not None
                and not analysis.configured_path_composed):
            raise ValueError("protected-neutral capture reached the fitter uncomposed")
        if analysis.candidate is None and len(roles) > 1:
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
            roles=roles,
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
            roles=self._role_names,
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
# production playback seams (binds W2's play_program to the real DSP boundary)
# --------------------------------------------------------------------------- #


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.priors`, which owns
#: it beside the priors that are its heaviest readers (#2291 Phase 5a-iii).
_role_transfers = _priors.role_transfers


# ``confirm_graph_is_live`` and ``bind_program_playback_seams`` moved whole to
# :mod:`jasper.active_speaker.crossover_v2.composition` (band AE of this
# file's dissolution map) — the engine's own composing module, importable
# without this file.


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


__all__ = [
    "CrossoverV2Session",
    "CrossoverV2FlowError",
    "INTEGRITY_CHECK_SWEEP_HEARD",
    "build_v2_capture_plan",
    "build_v2_session_spec",
    "build_v2_verify_capture_plan",
    "build_v2_verify_session_spec",
    "derive_session_volume_db",
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
    "express_cloud_measure_positions",
    "normalize_tier",
    "resolve_plan_shape",
    "tier_display_info",
    "capture_progress_label",
    "REVERIFY_NO_REWALK_HEADLINE",
    "stage1_base_entries",
    "stage1_plan_max_attempts",
    "LATERAL_POSE_PROMPTS",
    "CLOUD_VERIFY_POSE_PROMPTS",
    "verify_pose_table",
    "position_geometry",
    "LATERAL_EVIDENCE_BAND_HZ",
    "LATERAL_EVIDENCE_POINTS_PER_OCTAVE",
    "LateralPose",
    "lateral_evidence_grid_hz",
    "lateral_pose_curve",
    "STAGE1_INCLUDES_ENTRY_BASELINE",
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
    "verify_absolute_tolerance_db",
    "LINEARIZATION_TRIM_SANITY_MARGIN_DB",
    "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
    "spec_report_for_predicted_sum",
]
