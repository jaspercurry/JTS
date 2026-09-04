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

``docs/historical/crossover-measurement-productization-design.md`` §5
replaces the legacy per-driver distributed transaction with this shape: the
Pi compiles one excitation program per phase, plays it as one continuous
stream, and analyzes
``(program, capture) → analysis`` as a pure function. The session owns the
phase state machine that drives the capture session. At the shipped defaults a
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
journey spans TWO capture sessions since the two-stage split (work order D1/D2,
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
    # ``round_evidence`` is imported lazily at its runtime use site
    # (``_consume_entry_baseline``): eagerly it drags ``flat_spec`` in.
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

from jasper.active_speaker.crossover_v2.intervention import (
    LINEARIZATION_TRIM_SANITY_MARGIN_DB,
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

# --- phase vocabulary (owned by crossover_v2.journey) ----------------------

ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED = (
    _contracts.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED
)

SUMMED_SWEEP_PHASES = _programs.SUMMED_SWEEP_PHASES

ENTRY_GRAPH_FINGERPRINT_UNKNOWN = _ENTRY_GRAPH_FINGERPRINT_UNKNOWN

MAX_EXTRA_ATTEMPTS_PER_POSITION = _admission.MAX_EXTRA_ATTEMPTS_PER_POSITION
ATTEMPT_INITIATOR_HOUSEHOLD = _admission.ATTEMPT_INITIATOR_HOUSEHOLD
ATTEMPT_INITIATOR_SPEAKER = _admission.ATTEMPT_INITIATOR_SPEAKER

# Corner admissibility (plan §4.2 / #1894 / #1675). READ-ONLY doors: patching a
# name here rebinds this module only; production resolves via ``fc_sweep``.

from jasper.active_speaker.crossover_v2.fc_sweep import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND as FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR as FC_REJECT_BELOW_DECLARED_FLOOR,
    _fc_rejection as _fc_rejection,
)

# Two more READ-ONLY doors, on the block above's terms.
from jasper.active_speaker.branch_chain import (
    sections_by_role as sections_by_role,
)
from jasper.active_speaker.crossover_v2.intervention import (
    LINEARIZATION_MIN_PAIRED_OCCURRENCES as LINEARIZATION_MIN_PAIRED_OCCURRENCES,
)


# --- the walk this session will do (see crossover_v2.capture_plan) ---------
# READ-ONLY doors: patching a name here rebinds this module only.
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


# Substituting one of these names here binds only for readers inside this module.

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
    """Which of a candidate's driver branches are PRESCRIBED rather than fitted."""
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

DECLARED_GEOMETRY_PATH = _measurement_geometry.DEFAULT_PATH


def _declared_first_bounce_s(distance_m: float | None) -> float | None:
    """The operator-declared rig's first bounce at ONE capture's distance.

    Read FRESH per capture — the file is wizard-owned and a cached value would
    outlive a correction. ``None``, journaled rather than raised, when unreadable.
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


# --- tuning constants -----------------------------------------------------

GAIN_CAP_BACKOFF_DB = _programs.GAIN_CAP_BACKOFF_DB
# Per gain-adjusted clip retry, drop the offending program's level by this much.
CLIP_RETRY_BACKOFF_DB = 3.0
PILOT_LEVEL_DELTA_DB = _programs.PILOT_LEVEL_DELTA_DB
LOCATE_MIN_CONFIDENCE = _dispatch.LOCATE_MIN_CONFIDENCE
VERIFY_TOLERANCE_DB = _contracts.VERIFY_TOLERANCE_DB


verify_absolute_tolerance_db = _verification.verify_absolute_tolerance_db


# The prescribed on-axis mic distance the parallax correction assumes (§5.2).
MEASUREMENT_DISTANCE_M = 1.0
# GCC-seed/capture confidence floor. A DISCLOSURE trigger, not a gate: below it
# the capture is ACCEPTED and the confidence is banked as a reservation.
ALIGNMENT_CONFIDENCE_TRUST_FLOOR = 0.6
# ms, added on BOTH sides of the crossover region's declared ``delay_range_ms``
# (a SEARCH bound, not a physical limit) before a measured delay is rejected:
# GCC can return a confidently wrong lag that still clears the floor above.
ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS = 0.1

# Measurement-honesty disclosure G1, dB. A DISCLOSURE trigger, not a gate (owner
# ruling 2026-08-03, #2087): above it the capture is ACCEPTED with a reservation.
# Calibrated on the 2026-07-22 corpus — 13 clean captures at 4.387-9.031 dB
# against one corrupt at 27.316. THE FRAME: the summed branch sum at zero delay
# residual and at the polarity the candidate ships.
MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB = 15.0

# Measurement-honesty gate G3, dB. VERIFY replays the IDENTICAL program through
# the IDENTICAL graph, so its leading pilot pair's transfer must not move between
# attempts. Measured 2026-07-22: 0.75-0.82 dB across a dishonest sequence,
# ≤0.05 dB across the clean multi-attempt session.
VERIFY_PILOT_TRANSFER_STEP_CEILING_DB = 0.35

# dB. How close two consecutive graded VERIFY attempts must land before the
# mismatch is called DETERMINISTIC rather than transient (#1873). MEASURED:
# ``captures/repeat-floor-20260731/README.md`` puts the consecutive-pair repeat
# floor of ``max_db_notch_excluded`` over 1000-4000 Hz at 0.085 dB p95, and
# ``attempts_loop.CLAIM_FLOOR_P95_MULTIPLE`` (2.0) owns the doubling — 0.17016,
# of which 0.2 is that README's conservative display rounding. Do NOT tighten
# toward 0.17016: a SMALLER floor narrows the agreement window, and this is a
# fixed-mic number a hand-held phone exceeds.
VERIFY_REPEAT_FLOOR_DB = 0.2

#: ``terminal_outcome`` for the verdict above: the captures agreed, and the
#: agreement ends the set.
VERIFY_TERMINAL_OUTCOME_DETERMINISTIC = "verify_result_is_deterministic"

# Nothing here reads this name; it survives as a door pinned by
# ``test_crossover_v2_programs`` — do not delete it on an importer grep alone.
courtesy_prelude_for_phase = _programs.courtesy_prelude_for_phase


CrossoverV2FlowError = _contracts.CrossoverV2FlowError


# --- pure helpers (fixture-testable in isolation) --------------------------


back_off_gain = _programs.back_off_gain


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
    """Flatness-search magnitude bounds from the preset's declaration."""
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
    """True when ``|delay_us|`` is inside the preset's declared ``delay_range_ms``."""
    if delay_us is None:
        return True
    declared = _declared_alignment_delay_range_ms(source_preset)
    if declared is None:
        return True
    _region, lo_ms, hi_ms = declared
    delay_ms = abs(float(delay_us)) / 1000.0
    return (lo_ms - margin_ms) <= delay_ms <= (hi_ms + margin_ms)


_analysis_json = _planning.analysis_json


_stimulus_locate_ok = _dispatch._stimulus_locate_ok


def _any_sweep_clipped(analysis: ProgramAnalysis) -> bool:
    return any(
        loc.clipped for loc in analysis.locations if loc.kind in STIMULUS_KINDS
    )


_band_edge = _verification._band_edge

_flatness_tilt_log_field = _verification._flatness_tilt_log_field
_per_band_flatness_log_field = _verification._per_band_flatness_log_field


def _capture_wav_sha256(result: Any) -> str | None:
    """SHA-256 of a capture's WAV bytes, or ``None`` when there are none."""

    wav = getattr(result, "wav", None)
    if not isinstance(wav, (bytes, bytearray)):
        return None
    return hashlib.sha256(bytes(wav)).hexdigest()


CLAIM_PASS = _contracts.CLAIM_PASS
CLAIM_FAIL = _contracts.CLAIM_FAIL
CLAIM_NOT_EVALUATED = _contracts.CLAIM_NOT_EVALUATED
CLAIM_NO_PER_BRANCH_CAPTURE = _verification.CLAIM_NO_PER_BRANCH_CAPTURE

_verify_evidence_from_tracking = _verification._verify_evidence_from_tracking
_verify_graded_band_from_tracking = (
    _verification._verify_graded_band_from_tracking
)
_verify_claims = _verification._verify_claims
_verify_frame_from_tracking = _verification._verify_frame_from_tracking


PILOT_SNR_UNUSABLE_DB = _diagnostics.PILOT_SNR_UNUSABLE_DB
_worst_pilot_snr_db = _diagnostics._worst_pilot_snr_db


# --- seams + snapshot -----------------------------------------------------

# Injected seams: the web host binds production, tests inject fakes.
PlayProgram = Callable[[str, ExcitationProgram], None]


class AnalyzeCapture(Protocol):
    """analyze(program, capture_result, priors, geometry, *, phase) → ProgramAnalysis.

    ``phase`` is the SESSION's flow phase, never ``program.phase``, which is always
    "verify" for every cloud position (#1855). Required and keyword-only.
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
# Reads whether an apply hit a TERMINAL failure: the reason code, or "" while
# pending. Distinct from ``apply_complete``, which is success only.
ApplyFailureGate = Callable[[], str]
# Banks one accepted capture: ``(capture_result, record)`` -> the store id, or
# ``""`` when nothing was stored.
BankTake = Callable[[Any, Mapping[str, Any]], str]


def _no_bank_take(_result: Any, _record: Mapping[str, Any]) -> str:
    """The unbound record seam: nothing is stored, and that is not an error."""
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
    # Called once per ACCEPTED capture of every retained kind. Fail-soft.
    bank_take: BankTake = _no_bank_take
    # Cloud honesty bundle publisher, once per CLOSED group: ``(phase, result)``.
    publish_cloud: Callable[[str, Mapping[str, Any]], None] | None = None
    # #1866: the banked level-frame disagreement, at most once per session and
    # AFTER ``publish_candidate``, so the artifact it cites already exists.
    publish_findings: Callable[[Mapping[str, Any]], None] | None = None
    # PR-L5: undo the applied correction; True when the previous profile was
    # restored. Absent, the session still classifies and refuses.
    rollback: Callable[[str], bool] | None = None
    # #1811: the whole-band level move the APPLY made and did not command, read at
    # probe time. ``None`` is "nothing known", which the probe reports honestly.
    applied_offset_db: Callable[[], float] | None = None
    # #2611: the Layer-A profile the speaker is playing right now. Its absence is
    # NOT a fallback to the raw-crossover axis — the probe reports ``unavailable``.
    applied_profile: Callable[[], Mapping[str, Any] | None] | None = None
    # S3: once per newly accepted applied-candidate VERIFY.
    record_model_error: RecordModelError | None = None
    # #2291: which DSP graph the entry baseline was measured through, read at accept
    # time. A raise is caught: a fingerprint is provenance, never a gate.
    entry_graph_fingerprint: Callable[[], str] | None = None
    # #2291: is a prior candidate recorded to restore TO? Absence reads as "cannot
    # confirm", never as "there is one".
    rollback_available: Callable[[], bool] | None = None
    # #2291/#2318: does the APPLIED graph put energy in? Absence answers "boosted".
    applied_boosts: Callable[[], bool] | None = None
    # #2291: publish the round receipt, returning its artifact fingerprint. A raise
    # or a ``None`` is "no receipt written"; a receipt is never a gate.
    publish_round_receipt: Callable[[Mapping[str, Any]], str] | None = None


V2ConductorSnapshot = _durable_state.V2ConductorSnapshot
attempt_history_from_state = _durable_state.attempt_history_from_state
attempt_record_from_verify = _durable_state.attempt_record_from_verify
_attempt_optional_float = _durable_state._attempt_optional_float


# One prompted position's attempt ledger (owner ruling #2086). Kept importable
# from the flow: the endpoints and capture-sequence suites name it here.
SlotAttempts = _admission.SlotAttempts


_CloudPosition = _spatial._CloudPosition


# R16's lateral evidence types (plan §4.4). Kept importable from the flow —
# ``_primary_sweep_bands`` included — because the R16/R17 suites name them here.
LATERAL_EVIDENCE_BAND_HZ = _spatial.LATERAL_EVIDENCE_BAND_HZ
LATERAL_EVIDENCE_POINTS_PER_OCTAVE = _spatial.LATERAL_EVIDENCE_POINTS_PER_OCTAVE
LateralPose = _spatial.LateralPose
lateral_evidence_grid_hz = _spatial.lateral_evidence_grid_hz
lateral_pose_curve = _spatial.lateral_pose_curve
_primary_sweep_bands = _spatial._primary_sweep_bands


cloud_position_capture = _spatial.cloud_position_capture
_geometry_verdict_from_combined = _spatial._geometry_verdict_from_combined


def combine_cloud_positions(positions: Sequence[_CloudPosition]) -> Any:
    """Combine a closed group, and journal a combiner failure."""
    result = _spatial.combine_cloud_positions(positions)
    _diagnostics._emit_cloud_combine_diagnostics(logger, result.diagnostics)
    return result.combined


def cloud_geometry_verdict(positions: Sequence[_CloudPosition]) -> dict[str, Any]:
    """Combine, read ``.geometry``, and journal a combiner failure."""
    result = _spatial.cloud_geometry_verdict(positions)
    _diagnostics._emit_cloud_combine_diagnostics(logger, result.diagnostics)
    return result.verdict


# --- cloud group bands + honesty pipeline (the emitting halves) ------------
cloud_validity_floor_hz = _spatial.cloud_validity_floor_hz
cloud_trusted_floor_hz = _spatial.cloud_trusted_floor_hz


def _derive_cloud_echo_band_hz(
    signal_band_hz: tuple[float, float],
    tweeter_measurement_band_hz: tuple[float, float] | None,
) -> _spatial._CloudEchoBand:
    """Derive the echo band, and journal whichever clamp produced it."""
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
    """Assemble the closed group's result, and journal a pipeline failure."""
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


# --- what one candidate build produced (see crossover_v2.candidates) -------
_CloudFitEvidence = _candidates.CloudFitEvidence
_LinearizationState = _candidates.LinearizationState
_SpeculativeClose = _candidates.SpeculativeClose


committed_crossover_region_hz = _verification.committed_crossover_region_hz


def spec_report_for_predicted_sum(predicted_sum: Any) -> Any:
    """Grade the PREDICTED post-apply response against the flat spec.

    **``None`` means "unknown", never "passed".** Decimating before the smooth is
    load-bearing: a raw 512k-point grid costs ~11 s on a laptop, worse on a Pi 5.
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
        # A malformed or degenerate prediction is a diagnostic gap, not a crash: it
        # becomes an honest "no report".
        log_event(
            logger, "correction.crossover_v2_predicted_spec_failed",
            level=logging.WARNING, error=str(exc),
        )
        return None


def _commanded_delta(previous_predicted_sum: Any, predicted_sum: Any) -> Any:
    """``(freqs_hz, delta_db)`` the applied correction COMMANDS, or ``None``."""
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
    """How many points a ``(freqs, values)`` pair carries, or ``None``."""
    try:
        return int(np.asarray(curve[1], dtype=float).size)
    except (ValueError, TypeError, IndexError, AttributeError):
        return None


# --- the session ----------------------------------------------------------


class CrossoverV2Session:
    """One measurement session: its state, its seams, and its host contract.

    Hand :meth:`authorize_begin`, :meth:`on_armed` and :meth:`consume_capture` to
    :func:`jasper.web.correction_crossover_v2_wired.build_v2_wired_run_and_consume`;
    :meth:`snapshot` / :meth:`hydrate` carry phase persistence. Three things
    belong here: this session's mutable state, the reads of it the web host
    needs, and the acts that cannot be undone or repeated, each behind a seam.
    No RULE belongs here.
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
        # Empty = unknown (a caller that never declared one), never silently TIER_FULL.
        self._tier = normalize_tier(tier) if tier else ""
        # #2879: are this walk's begins HELD until the mic is reported in place? ORed
        # with the tier's own answer so no caller can drop the arm's gate.
        self._positions_gated = (
            tier_is_externally_positioned(self._tier) or bool(positions_gated)
        )
        self._preset = source_preset
        self._roles = roles
        # Lowest role first. ``_tweeter`` is ``None`` on a 1-way main, never aliased.
        self._role_names = tuple(band.role for band in roles)
        self._woofer = roles[0]
        self._tweeter: RoleBand | None = roles[1] if len(roles) == 2 else None
        self._tweeter_role = None if self._tweeter is None else self._tweeter.role
        # Why this session evaluates no driver PAIR, or ``None`` when it does.
        self._pair_reason = None if len(roles) > 1 else MEASURE_PAIR_SINGLE_DRIVER
        self._fc_hz = None if fc_hz is None else float(fc_hz)
        # #2662. Already validated by the request boundary; never re-judged here.
        self._alignment_prescription = alignment_prescription
        # The topology twin, already applied: the boundary opened this session AT the
        # pinned corner, so ``_fc_hz`` and ``_preset`` above are the pin.
        self._topology_prescription = topology_prescription
        # A9. The blend twin. Named for what it IS: ``_blend_prescription`` is already
        # the METHOD that ranks sources, and a field of that name would shadow it.
        self._prescribed_blend = blend_prescription
        self._prescribed_blend_sha256 = str(blend_prescription_sha256 or "")
        # A9/PR-B. Per-ROLE rather than per-region: its door is the candidate's
        # ``linearization`` map, merged where the fit is final.
        self._prescribed_driver = driver_prescription
        # PR-4: computed once so every group-close event uses the SAME bands.
        self._cloud_signal_band_hz = _programs.measurement_band_hz(roles)
        # Band AND provenance as one value (#1763): the payload cannot publish a band
        # without the disclosure of how it was derived.
        self._cloud_echo_band = _derive_cloud_echo_band_hz(
            self._cloud_signal_band_hz, tweeter_measurement_band_hz,
        )
        self._caps = dict(driver_caps_dbfs)
        # Per-role longest admissible ONE sweep (#2921); an absent role composes at its
        # nominal duration.
        self._sweep_duration_limits_s = dict(driver_sweep_duration_limits_s or {})
        self._session_volume_db = float(session_volume_db)
        self._seams = seams
        # True once ``authorize_begin`` has refused: the capture writes its own
        # capture_refused into a last-write-wins slot the terminal rider
        # must not clobber.
        self.capture_published_refusal = False
        self._measurement_protection_sections_by_role = None
        if measurement_protection_sections_by_role is not None:
            self._measurement_protection_sections_by_role = {
                str(role): tuple(sections)
                for role, sections in measurement_protection_sections_by_role.items()
            }
        # Attempts belong to the commissioning journey, not to this capture session.
        self._attempt_history = list(attempt_history)[
            -AttemptBudget().hard_cap_attempts:
        ]
        # #2602's series memory, resolved by the host from durable state on BOTH stages
        # since #2698, because the two readers run on different ones.
        self._series_position = series_position
        self._attempt_floor = attempt_floor
        self._last_attempt_decision = (
            dict(last_attempt_decision)
            if isinstance(last_attempt_decision, Mapping) else None
        )
        self._speaker_id = str(speaker_id or "unknown")
        self._tuning_attempt_id = str(tuning_attempt_id or "")
        # Layer-1a per-role driver class (#1668 PR-C); empty matches
        # ``compose_envelope``'s own "unknown".
        self._driver_class_by_role = (
            dict(driver_class_by_role) if driver_class_by_role else {}
        )
        # #1675: declared effective radiating diameter per role, provenance only. Empty
        # means UNDECLARED — there is no conservative default diameter.
        self._radiating_diameter_mm_by_role = (
            dict(radiating_diameter_mm_by_role) if radiating_diameter_mm_by_role else {}
        )
        self._geometry = MeasurementGeometry(
            driver_spacing_m=float(driver_spacing_m),
            mic_distance_m=MEASUREMENT_DISTANCE_M,
        )
        # Where this round is, and the walk it is in (#2291 Phase 4). ONE aggregate:
        # six correlated fields here could disagree.
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
        # CHECK's measured room floor, held until ``_measure_priors`` reads it (#1830).
        # In-memory only: §5.6 invalidates CHECK/MEASURE evidence across sessions.
        self._check_ambient_report: dict[str, Any] | None = None
        # Retained per-position evidence in capture order, keyed by group phase.
        self._group_positions: dict[str, list[_CloudPosition]] = {
            phase: [] for phase in self._journey.plan.group_indexes
        }
        # The lateral walk keeps its OWN retention: a pose is per-driver evidence and a
        # cloud position is one summed curve, which one list cannot combine.
        self._lateral_poses: list[LateralPose] = []
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
        # #3498: what each pose was measured UNDER, in prompt order. Empty on every
        # shipped walk.
        self._lateral_claims: tuple[_spatial.TakeClaim, ...] = tuple(lateral_claims)
        # Resolved through the resolver the plan builder uses, so the session and the
        # plan cannot read different pose tables.
        self._verify_prompts: tuple[CloudPositionPrompt, ...] = verify_pose_table(
            verify_prompts
        )
        # WO-1: per-position evidence metadata by position id. Not pruned on removal;
        # the serializer joins on ``combined.position_ids``, so an orphan is never read.
        self._group_position_meta: dict[str, dict[str, dict[str, Any]]] = {
            phase: {} for phase in self._journey.plan.group_indexes
        }
        # Geometry-locked retakes already spent, per group.
        self._geometry_retries_used: dict[str, int] = {
            phase: 0 for phase in self._journey.plan.group_indexes
        }
        # The group's closing geometry verdict; ``None`` until the group closes.
        self._group_geometry: dict[str, dict[str, Any]] = {}
        # PR-4: the group's closing honesty pipeline result. ``None`` until the group
        # closes — never confuse not-yet-run with a clean verdict.
        self._group_cloud_result: dict[str, dict[str, Any]] = {}
        # #2291: the LIVE ``FlatSpecReport`` behind the serialized ``spec`` key. Not
        # persisted; the dict beside it is the durable copy.
        self._group_graded_spec: dict[str, Any] = {}
        # #1872: which phases' evidence artifact has already been PUBLISHED. Only the
        # durable write skips a repeat, and only on a SUCCESSFUL publish.
        self._group_cloud_published: set[str] = set()
        # #2609 SF5 / §4.2, per closed group, read only by ``_grade_round_once``: the
        # frame the spec bands were graded in, and one residual per position.
        self._group_trusted_floor_hz: dict[str, float | None] = {}
        self._group_position_residuals: dict[str, tuple[Mapping[str, Any], ...]] = {}
        # The group's most recent COMBINE, held until the household confirms past it
        # (§2.6). Held rather than recomputed: a combine is 2.7-6 s of operator time.
        self._group_combined: dict[str, Any] = {}

        # Frozen together so a subset cannot drift.
        self._excitation = _programs.SessionExcitation(
            roles=self._roles,
            caps_dbfs=self._caps,
            session_volume_db=self._session_volume_db,
            fc_hz=self._fc_hz,
            sweep_duration_limits_s=self._sweep_duration_limits_s,
        )
        # Composed ONCE and held: ``program_for_phase`` answers by OBJECT IDENTITY, and
        # #2291's before→after comparability depends on it.
        self._check_program = self._excitation.check_program()
        self._measure_program: ExcitationProgram | None = (
            self._excitation.measure_program(self._gain_plan_db)
            if self._gain_plan_db is not None
            else None
        )
        self._verify_program = self._excitation.verify_program()
        # The position groups' twin: same sweep, same clamp, no courtesy prelude.
        self._cloud_program = self._excitation.cloud_program()

        # Per-SLOT attempt bookkeeping: the phase for a single-capture phase,
        # ``phase:index`` inside a group. ONE meter per slot (owner ruling #2086).
        self._slot_attempts: dict[str, SlotAttempts] = {}
        self._last_reason: dict[str, str] = {}
        # The capture evidence paired with each slot's last rejection; exhaustion reads
        # this rather than the global pair, which can belong to a different position.
        self._last_pilot_evidence: dict[
            str, tuple[str, bool | None, bool | None]
        ] = {}
        # Positions the flow GAVE UP on, so the group closes with what it has instead
        # of the session dying at the mic.
        self._group_unresolved: dict[str, dict[int, str]] = {
            phase: {} for phase in self._journey.plan.group_indexes
        }
        self._armed_index: int | None = None
        # The most recent authorized (index, attempt): the host addresses the terminal
        # ``capture_result`` to it at a play-seam failure (§5.10 / W6.1).
        self._armed_capture: tuple[int, int] | None = None
        # MEASURE→VERIFY handoff evidence; a verify-only re-arm rehydrates it (§5.2).
        self._measure_predicted_sum: Any = measure_predicted_sum
        # D4's spec verdict for the curve above, graded ONCE against the full-resolution
        # tuple and held serialized — the rehydration route can only hand back JSON.
        self._measure_predicted_spec_report: dict[str, Any] | None = (
            dict(measure_predicted_spec_report)
            if isinstance(measure_predicted_spec_report, Mapping)
            else None
        )
        # PR-L5: what the applied correction COMMANDS on the summed response.
        self._measure_commanded_delta: Any = measure_commanded_delta
        # #2614: the applied graph's OWN transfer against the uncorrected crossover —
        # the STATE axis beside the CHANGE axis above.
        self._measure_declared_transfer: Any = measure_declared_transfer
        # What ``_previous_graph_predicted_sum``'s INFO line last disclosed.
        self._previous_graph_disclosed: tuple[Any, ...] | None = None
        # #2291's "before": WRITTEN by stage 1's entry-baseline capture, PASSED IN on
        # stage 2, which never captures one.
        self._measure_entry_baseline: "EntryBaseline | None" = measure_entry_baseline
        # #2392's proposal identity, written by the stage that COMMITS and passed in to
        # the stage that GRADES. It travels as the fingerprint, not the ingredients.
        self._measure_proposal_fingerprint: str = str(measure_proposal_fingerprint or "")
        # WHICH commitment produced the committed delay (#2662). ``""`` is "no candidate
        # committed yet" — a third answer from "committed" and "not committed".
        self._measure_alignment_objective: str = str(measure_alignment_objective or "")
        # The proposal itself, for THIS session only; the fingerprint above is the
        # durable identity.
        self._intervention_proposal: Any = None
        # #2291's round grading and its fire-once guard: the round is graded at two
        # different moments on the two tiers.
        self._round_evaluated = False
        self._round_evaluation: Any = None
        # Where this round's receipt landed. ``None`` when writing failed — an
        # identity for a receipt that does not exist would be worse than none.
        self._round_receipt_identity: dict[str, Any] | None = None
        # Which arm of ``correction_rollback_failed`` this is: ``True`` a restore
        # failed against a real anchor, ``False`` there was never one, ``None``
        # not established.
        self._last_failure_rollback_anchor: bool | None = None
        # The post-apply VERIFY analysis, retained for the Full tier's later grading.
        self._verify_analysis: ProgramAnalysis | None = None
        # ``None`` until VERIFY is consumed.
        self._delta_probe: DeltaProbeMap | None = None
        # VERIFY's measured-vs-predicted pair and the band its own gate says it can be
        # judged over — the gate disclosure's, never derived here (#2521).
        self._verify_tracking_curve: Any = None
        self._verify_trusted_band_hz: tuple[float, float] | None = None
        # Absent for a group that never closed; empty for one with fewer than two
        # positions.
        self._group_band_spread: dict[str, tuple[Any, ...]] = {}
        self._measure_gate_window_ms: float | None = measure_gate_window_ms
        # The accepted MEASURE analysis, held from MEASURE's accept until the
        # CLOUD_MEASURE group closes and the fit consumes it, then released. Its
        # size scales with capture length: on the S0 corpus's 524,289-bin grid
        # (2026-07-27) one two-occurrence ``DriverResponse`` is 33.6 MB of ndarray
        # payload. That is not a production MEASURE's grid, and is quoted only for
        # the ORDER — tens of megabytes on a 1 GB Pi that also retains every cloud
        # position's response for the combine.
        self._measure_analysis: Any = None
        self._candidate: Any = None
        # HAS THE HOUSEHOLD CONFIRMED? — the held-set predicate, deliberately separate
        # from the ``_candidate`` fire-once guard.
        self._group_confirmed = False
        # A group close that already ran speculatively, parked until the household
        # confirms. ``None`` = no eager fit is banked.
        self._speculative_close: _SpeculativeClose | None = None
        # Serializes the group close against the eager fit, the one part of this
        # session that runs off the capture thread. Three entry points take it and
        # none nests — the confirm path reaches ``_close_measure_cloud_candidate``,
        # never the lock-taking ``_close_cloud_group`` — so this non-reentrant
        # ``Lock`` is correct AND enforces that: an edit that makes one call another
        # deadlocks the capture thread. It covers ``_group_combined`` and
        # ``_group_cloud_result``, not ``_measure_analysis``, which is safe by phase
        # ordering. The combine and the speculative stash are written together under
        # it, which is why no generation counter tells a stale bank from a current one.
        self._close_lock = threading.Lock()
        # Set the instant the set-completion signal is admitted, so the combine + fit
        # are a NAMED state. Never cleared.
        self._group_close_running = False
        self._verify_outcome: str | None = None  # pass | fail | inconclusive
        # WHICH VERDICT produced that outcome (#1974), written with it and never apart.
        # ``failure.code`` cannot answer it: that is the last rejection of ANY phase.
        self._verify_code: str | None = None
        # VERIFY's own gate, reduced to what the screens need (#1974). Written only by
        # ``_set_verify_outcome``, so it always describes the same capture.
        self._verify_gate: dict[str, Any] | None = None
        # The verify_fail expert-disclosure numbers (#1605). Set only once the tolerance
        # comparison is reached, so no half-empty disclosure renders.
        self._verify_evidence: dict[str, Any] | None = None
        # The span that comparison graded (#1868), surfaced on EVERY outcome: a pass is
        # exactly when an unstated band overclaims.
        self._verify_graded_band_hz: list[float] | None = None
        # The FRAME that comparison spanned — one offset, one tilt (rung P1). Same
        # lifecycle and every-outcome rule as the band above.
        self._verify_frame: dict[str, Any] | None = None
        # The plan §7 claim record, on EVERY outcome that reached a grade (R18, #1868):
        # "Verified." over no claim list reads as "everything was checked".
        self._verify_claims: dict[str, Any] | None = None
        self._last_failure_code: str | None = None
        # The pilot evidence belonging to ``_last_failure_code`` (#2085), ALWAYS written
        # with it. ``None`` is "no pilot evidence for this failure".
        self._last_failure_pilot_heard: bool | None = None
        # G3's reference, SESSION-SCOPED since #1927: the first usable VERIFY attempt of
        # THIS session records it and every later attempt is compared against it. A
        # rehydrated one conflated within-session chain consistency with cross-day
        # setup identity — the 2026-07-30 bench measured 0.775 dB of ordinary mic
        # replacement.
        self._verify_pilot_baseline: dict[str, float] | None = None
        # #1873's discriminator: the PREVIOUS VERIFY attempt's out-of-tolerance
        # ``max_db_notch_excluded``, in this session only. A MISMATCH, not a
        # grade — an attempt inside tolerance clears it. SESSION-SCOPED because
        # ``VERIFY_REPEAT_FLOOR_DB`` is a fixed-mic number and a re-arm is a fresh
        # sitting.
        self._verify_last_mismatch_max_db: float | None = None
        # WHEN this session set the reference above (epoch float), stamped in the same
        # statement so the two cannot disagree.
        self._verify_pilot_baseline_at: float | None = None
        # The PREVIOUS session's reference, as dated HISTORY, never a comparator. An
        # undated record cannot be shown as history without inventing a date (#1942).
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
                # Values AND a date, together or not at all.
                if values:
                    self._verify_pilot_prior = values
                    self._verify_pilot_prior_at = float(prior_at)
        # Set once by the attempt that establishes this session's reference, and only
        # past the gate's own ceiling — one threshold, not a second definition.
        self._verify_level_reference_reset: dict[str, float] | None = None
        # Transient, recomputed on every VERIFY attempt; ``None`` when there is nothing
        # to compare.
        self._verify_pilot_transfer_step_db: float | None = None
        # Which measurement-honesty check produced the LAST MEASURE verdict, reset at
        # the top of every ``_measure_verdict``. NOT every value is a refusal: G1 writes
        # ``ripple_disclosure`` on a capture it ACCEPTS, so pair it with ``accepted=``.
        self._last_measure_guard: str = ""
        # G1's banked reservation (#2087). Reset at the top of every
        # ``_measure_verdict``, so it describes THE ACCEPTED CAPTURE and no other.
        self._measure_ripple_reservation: dict[str, Any] | None = None
        self._measure_alignment_reservation: dict[str, Any] | None = None
        # Audit gauntlet 5a: ``True`` when the accepted MEASURE had no resolved mic
        # calibration, ``None`` otherwise. Same reset lifecycle as the two above.
        self._measure_calibration_reservation: bool | None = None

    # --- program composition -------------------------------------------------

    def _compose_measure_program(
        self, gain_plan_db: Mapping[str, float], *, extra_backoff_db: float = 0.0,
    ) -> ExcitationProgram:
        """MEASURE's program at the solved gains, the one with a LIFECYCLE."""
        return self._excitation.measure_program(
            gain_plan_db, extra_backoff_db=extra_backoff_db,
        )

    # --- priors per phase ----------------------------------------------------

    def _check_priors(self) -> MeasurementPriors:
        return _priors.check_priors(fc_hz=self._fc_hz)

    def _measure_priors(self) -> MeasurementPriors:
        return _priors.measure_priors(
            fc_hz=self._fc_hz,
            source_preset=self._preset,
            protection_sections_by_role=self._measurement_protection_sections_by_role,
            ambient_report=self._check_ambient_report,
            # Derived here: its producer is shared with the plausibility gate.
            alignment_delay_bounds_us=alignment_delay_search_bounds_us(self._preset),
            applied_alignment=self._applied_alignment(),
            explicit_alignment_delay_us=(
                None if self._alignment_prescription is None
                else self._alignment_prescription.delay_us
            ),
            # Translated by the record's own ``polarity_sign``, never here.
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
        """The blend correction the post-apply capture rode, or ``None``."""
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

        Three sources, in order: a BLEND prescription staged for THIS round (A9), which
        supersedes for exactly one round and cannot persist past it; the series'
        instruction (``SeriesPosition.previous_blend_correction``); then what the
        speaker is already playing, read through the same SSOT and strict reader
        ``_applied_blend_correction`` uses.
        """
        from .crossover_v2.blend_prescription import (
            BLEND_CANDIDATE_FIELD,
            blend_prescription_to_candidate_fields,
        )

        if self._prescribed_blend is not None:
            # Through the route rather than off the object's ``filters``, so the promise
            # that a boost cannot populate this field holds on every path.
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
        """Will this session's correction be MEASURED after it is applied?"""
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
        """The closing geometry verdict for one position group, or ``None``."""
        verdict = self._group_geometry.get(phase)
        return dict(verdict) if verdict is not None else None

    def group_cloud_result(self, phase: str) -> dict[str, Any] | None:
        """The honesty-pipeline result for one closed group, or ``None`` when the
        group has not closed — never a clean verdict.
        """
        result = self._group_cloud_result.get(phase)
        return dict(result) if result is not None else None

    def group_positions(self, phase: str) -> tuple[str, ...]:
        """Accepted position ids in one group, in capture order."""
        return tuple(p.position_id for p in self._group_positions.get(phase, ()))

    def group_position_takes(self, phase: str) -> tuple[dict[str, Any], ...]:
        """The SURVIVING take per position — ``{position_id, index, attempt}``."""
        return tuple(
            {"position_id": p.position_id, "index": p.index, "attempt": p.attempt}
            for p in self._group_positions.get(phase, ())
        )

    @property
    def lateral_poses(self) -> tuple[LateralPose, ...]:
        """The accepted lateral walk, in capture order (plan §4.4)."""
        return tuple(self._lateral_poses)

    def lateral_mark_return_drift_db(self) -> dict[str, float] | None:
        """Per-role worst |Δ dB| between the walk's two AT-MARK poses."""
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
        """The reason code behind :attr:`verify_outcome`, or ``None`` on a pass."""
        return self._verify_code

    @property
    def verify_gate(self) -> dict[str, Any] | None:
        """VERIFY's gate as the screens need it, or ``None`` (#1974)."""
        return dict(self._verify_gate) if self._verify_gate else None

    @property
    def verify_evidence(self) -> dict[str, Any] | None:
        """The verify_fail expert-disclosure numbers (#1605), or None."""
        return dict(self._verify_evidence) if self._verify_evidence else None

    @property
    def verify_graded_band_hz(self) -> list[float] | None:
        """``[lo, hi]`` VERIFY's tracking comparison graded, or None (#1868)."""
        return list(self._verify_graded_band_hz) if self._verify_graded_band_hz else None

    @property
    def verify_frame(self) -> dict[str, Any] | None:
        """The frame VERIFY's comparison spanned, or None (rung P1)."""
        return dict(self._verify_frame) if self._verify_frame else None

    @property
    def verify_claims(self) -> dict[str, Any] | None:
        """The plan §7 claim record, or ``None`` (R18, #1868)."""
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

        ``None`` means it could not be graded — **never that it passed**. Graded
        against the full-resolution tuple; durable state holds a 512-point
        average (#1858).
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
        """The applied graph's own transfer against the raw crossover (#2614)."""
        return self._measure_declared_transfer

    @property
    def measure_proposal_fingerprint(self) -> str:
        """This round's :class:`InterventionProposal` identity, or ``""``."""
        return self._measure_proposal_fingerprint

    @property
    def measure_alignment_objective(self) -> str:
        """Which commitment produced this round's delay, or ``""`` (#2662)."""
        return self._measure_alignment_objective

    @property
    def alignment_prescription_record(self) -> dict[str, Any] | None:
        """This session's delay prescription as the receipt banks it (#2662)."""
        if self._alignment_prescription is None:
            return None
        return self._alignment_prescription.to_dict()

    @property
    def topology_prescription_record(self) -> dict[str, Any] | None:
        """This session's crossover pin as the receipt banks it.

        ``None`` means the automatic path. Exactly ``to_dict()``:
        ``topology_prescription_from_mapping`` refuses an unknown field on rehydration.
        """
        if self._topology_prescription is None:
            return None
        return self._topology_prescription.to_dict()

    @property
    def blend_prescription_record(self) -> dict[str, Any] | None:
        """This session's blend prescription as the receipt banks it (A9).

        ``None`` means the automatic path. Exactly ``to_dict()``:
        ``blend_prescription_from_mapping`` refuses an unknown field on rehydration.
        """
        if self._prescribed_blend is None:
            return None
        return self._prescribed_blend.to_dict()

    @property
    def blend_prescription_sha256(self) -> str:
        """The digest of the document this round's prescription came from (A9)."""
        return self._prescribed_blend_sha256

    @property
    def last_intervention_proposal(self) -> Any:
        """This session's proposal, its refusal, or ``None`` before the commit."""
        return self._intervention_proposal

    @property
    def round_receipt_identity(self) -> dict[str, Any] | None:
        """Where this session's round receipt landed, or ``None`` (#2291)."""
        record = self._round_receipt_identity
        return dict(record) if isinstance(record, Mapping) else None

    @property
    def round_evaluation(self) -> Any:
        """This session's graded round, or ``None`` before it is graded."""
        return self._round_evaluation

    @property
    def measure_entry_baseline(self) -> "EntryBaseline | None":
        """#2291's pre-apply side of this round, or ``None``."""
        return self._measure_entry_baseline

    @property
    def delta_probe(self) -> DeltaProbeMap | None:
        """This session's realized-vs-commanded verdict (PR-L5), or ``None``."""
        return self._delta_probe

    @property
    def verify_tracking_curve(self) -> Any:
        """The VERIFY capture's ``(freqs_hz, measured_db, predicted_db)``, or
        ``None`` (#2522).
        """
        return self._verify_tracking_curve

    @property
    def measure_gate_window_ms(self) -> float | None:
        return self._measure_gate_window_ms

    @property
    def measure_ripple_reservation(self) -> dict[str, Any] | None:
        """G1's banked reservation about the accepted MEASURE, or ``None``."""
        reservation = self._measure_ripple_reservation
        return dict(reservation) if reservation else None

    @property
    def measure_alignment_reservation(self) -> dict[str, Any] | None:
        """The banked reservation about an accepted low-confidence alignment."""
        reservation = self._measure_alignment_reservation
        return dict(reservation) if reservation else None

    @property
    def measure_calibration_reservation(self) -> bool | None:
        """``True`` when the accepted MEASURE ran with no resolved measurement-mic
        calibration; ``None`` when it was calibrated, or never resolved.
        """
        return self._measure_calibration_reservation

    @property
    def verify_pilot_transfer_reference(self) -> Mapping[str, Any] | None:
        """This session's own G3 reference, DATED, for the host to persist.

        ``{"values": {role: dB}, "at": epoch}``. One value rather than two keys: a
        record without its date cannot be shown as history (#1942).
        """
        if self._verify_pilot_baseline is None or self._verify_pilot_baseline_at is None:
            return None
        return {
            "values": dict(self._verify_pilot_baseline),
            "at": self._verify_pilot_baseline_at,
        }

    @property
    def verify_level_reference_reset(self) -> Mapping[str, float] | None:
        """This session's level-reference reset, when it is worth disclosing."""
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

        This getter checks nothing: the pairing that CAN diverge is with the code a
        caller chooses to persist, and ``persist_conductor_state`` makes that check.
        """
        return self._last_failure_pilot_heard if self._last_failure_code else None

    @property
    def last_failure_rollback_anchor(self) -> bool | None:
        """Which ``correction_rollback_failed`` arm this failure is (#2291)."""
        return self._last_failure_rollback_anchor if self._last_failure_code else None

    def _pilot_heard_for(
        self, code: str | None, *, slot: str | None = None,
    ) -> bool | None:
        """The pilot evidence recorded WITH ``code``, else ``None`` (#2085)."""
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
        """The last authorized ``(index, attempt)``: the host addresses the terminal
        ``capture_result`` host event at a play-seam failure to it.
        """
        return self._armed_capture

    def _phase_of_index(self, index: int) -> str:
        phase = self._journey.plan.phase_for_index(index)
        if phase is None:
            raise CrossoverV2FlowError(f"no v2 phase for capture index {index}")
        return phase

    def _slot_of_index(self, index: int) -> str:
        """The retry-budget key for one capture index."""
        phase = self._phase_of_index(index)
        return f"{phase}:{index}" if phase in GROUP_PHASES else phase

    def _cloud_prompt(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt for one group index — the SAME table the plan emitted."""
        offsets = self._journey.plan.group_offsets(phase)
        try:
            position = offsets.index(index)
        except ValueError:
            position = 0
        # Three groups, three tables: the lateral walk and the post-apply walk each
        # have their own; the pre-apply cloud walks the shared table.
        table = (
            self._lateral_prompts if phase == PHASE_LATERAL
            else self._verify_prompts if phase == PHASE_CLOUD_VERIFY
            else CLOUD_POSITION_PROMPTS
        )
        if position < len(table):
            return table[position]
        # A DISTINCT defensive spot, not a table row repeated: 45 cm right is past the
        # table's widest RIGHT offset (40 cm) and inside the geometry rung's.
        return _pose(_LATERAL_POSE, 45.0, POSITION_ROLE_OFFAX, side="RIGHT")

    def _prompt_shown_for(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt the operator ACTUALLY followed for the take in hand.

        Not always the table entry: after a geometry-locked rejection the phone showed a
        wider retry rung, and the sidecar's prompt is the durable statement of where.
        """
        slot = self._slot_of_index(index)
        if self._last_reason.get(slot) == REASON_CLOUD_GEOMETRY_LOCKED:
            used = max(self._geometry_retries_used.get(phase, 1), 1)
            index_ = min(used - 1, len(CLOUD_GEOMETRY_RETRY_PROMPTS) - 1)
            rung = CLOUD_GEOMETRY_RETRY_PROMPTS[index_]
            # Rung 2 is COMPOUND — 75 cm sideways AND 30 cm up — so its rise
            # is stated rather than left to read as mark height.
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
        """The restore-observed host event — disarms the VERIFY hold (#2616)."""
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

        Same session ⇒ resume with its accepted phases and gain plan; a different
        or absent one ⇒ fresh start at CHECK, mic position being unverifiable
        across sessions.
        """
        journey: dict[str, Any] = {}
        if snapshot is not None:
            journey = {
                "attempt_history": snapshot.attempt_history,
                "last_attempt_decision": snapshot.last_attempt_decision,
            }
        # Explicit caller values win for migrations/tests that deliberately replace one
        # journey fact; ordinary production hydration supplies none.
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

    # --- capture callbacks ---------------------------------------------------

    def authorize_begin(self, index: int, attempt: int, entry: Any = None) -> None:
        """Admit (or defer / refuse) one phone ``begin_capture`` (§5.7)."""
        phase = self._phase_of_index(index)
        slot = self._slot_of_index(index)
        # READ, never create: a begin held at the VERIFY anchor must not leave a meter
        # behind for a capture that never started.
        ledger = self._slot_attempts.get(slot)

        def apply_failure_code() -> str:
            """The apply seam's TERMINAL reason, or ``""`` — guarded here."""
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
            # No capture ran, so no pilot evidence pairs with this (#2085). Written so a
            # previous capture's cannot trail in.
            self._last_failure_pilot_heard = None
            spec = REASON_REGISTRY.get(decision.code)
            message = (
                reason_message(decision.code, spec) if spec else decision.code
            )
            self.capture_published_refusal = True
            raise CaptureBeginRefused(decision.code, message)
        if decision.kind == _admission.DEFER_AWAITING_APPLY:
            raise CaptureBeginDeferred("awaiting_apply", VERIFY_ANCHOR_HOLD_MESSAGE)
        if decision.kind == _admission.REFUSE_NON_RETRIABLE:
            spec = REASON_REGISTRY[decision.code]
            self.capture_published_refusal = True
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
            self.capture_published_refusal = True
            raise CaptureBeginRefused(
                # The code the household is told about is the condition actually
                # observed here, never a generic exhaustion code.
                code,
                self._extras_spent_message(
                    ledger,
                    diagnosis=diagnosis,
                    outcome=self._spent_slot_outcome(phase, index),
                ),
            )
        if decision.kind != _admission.ADMIT:
            # One arm per :data:`admission.DECISION_KINDS` member; this fallback is LOUD
            # because falling through would start a capture nobody authorized.
            log_event(
                logger, "correction.crossover_v2_begin_decision_kind_unmapped",
                level=logging.ERROR, session_id=self.session_id,
                phase=phase, index=index, kind=str(decision.kind),
            )
            self.capture_published_refusal = True
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
                # The flow's own error type is what every caller already handles;
                # the ledger is pure and has no business knowing it.
                raise CrossoverV2FlowError(str(exc)) from exc
        ledger.admitted += 1
        self._armed_index = index
        self._armed_capture = (index, attempt)
        log_event(
            logger, "correction.crossover_v2_authorized",
            session_id=self.session_id, phase=phase, index=index, attempt=attempt,
            # The same numbers the household reads (ruling item 2). ``attempt`` alone
            # is the PLAN's running counter and cannot say how many tries this
            # POSITION has had.
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
        """The state after an exhausted slot, derived from session state."""
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
        """The composed program this session plays for ``phase``."""
        try:
            return _programs.program_for_phase(
                phase,
                check=self._check_program,
                measure=self._measure_program,
                verify=self._verify_program,
                cloud=self._cloud_program,
            )
        except _programs.NoProgramForPhaseError as exc:
            # The flow's own error type is what every caller already handles;
            # the selector is pure and has no business knowing it.
            raise CrossoverV2FlowError(str(exc)) from exc

    def consume_capture(
        self, index: int, attempt: int, result: Any,
    ) -> dict[str, Any]:
        """Analyze one uploaded capture and advance (or reject) the phase."""
        phase = self._phase_of_index(index)
        slot = self._slot_of_index(index)
        # ONE table for both "which priors" and "which consumer". Group members are
        # keyed per phase: ``PHASE_LATERAL`` is in ``GROUP_PHASES`` yet reads its own.
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
        # The whole CaptureResult crosses the seam: the binding resolves mic calibration
        # from it. ``phase`` is the flow's own (#1855) — ``program.phase`` is not.
        analysis = self._seams.analyze(
            program, result, priors, self._geometry, phase=phase,
        )
        verdict = consume(index, attempt, analysis, result)
        # THIS capture's pilot evidence, attached at ONE point rather than at each of
        # the three gates that can produce ``locate_failed``.
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
            # attribution fallback, and ``admission.extra_initiator`` next begin.
            self._last_reason[slot] = verdict.code
            self._last_pilot_evidence[slot] = (
                verdict.code,
                verdict.pilot_heard,
                verdict.reflection_measured,
            )
            # SETTLE HERE, not at the next begin (owner ruling #2086 item 3), so the
            # household is never shown a retry screen whose button leads to a pre-play
            # refusal — UNLESS the verdict already ended the set on its own finding.
            if verdict.payload.get("terminal") is not True:
                verdict = self._resolve_spent_slot(phase, index, slot, verdict)
        if verdict.accepted:
            # A group's PHASE is accepted only when its last index is in. Both
            # route through ``_note_accepted``, so one place decides "done".
            self._note_accepted(phase, index)
            # A clean acceptance supersedes the slot's rejection; a settled
            # exhaustion stays paired for the defensive replay.
            if not (
                "unresolved" in verdict.payload
                or verdict.payload.get("kept_earlier_take") is True
            ):
                self._last_reason.pop(slot, None)
                self._last_pilot_evidence.pop(slot, None)
            self._last_failure_code = None
            self._last_failure_pilot_heard = None
        elif verdict.code is not None:
            # Re-read off the FINAL verdict: a settled close can substitute a product
            # refusal for the quality rejection that got here.
            self._last_reason[slot] = verdict.code
            self._last_failure_code = verdict.code
            # Set and cleared together with the code above: the envelope renders the
            # persisted failure's sentence from this pair.
            self._last_failure_pilot_heard = verdict.pilot_heard
        # Stamped once here rather than in each ``_consume_*``, so the number the phone
        # renders and the number the journal logs cannot drift (ruling item 2).
        verdict = self._with_attempt_payload(slot, verdict)
        log_event(
            logger, "correction.crossover_v2_result",
            session_id=self.session_id, phase=phase,
            accepted=verdict.accepted, code=verdict.code or "",
            # The discriminator behind the sentence the household just read (#2085):
            # without it four ``code=locate_failed`` lines are indistinguishable.
            pilot_heard=verdict.pilot_heard,
        )
        return verdict.to_capture_dict()

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

        The ladder belongs to ``crossover_v2.admission.settle_spent_slot``; its two
        halves bracket the lock, whose group rungs read facts only true while held.
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
            # The condition rung — nothing was spent, so the copy stays the code's OWN
            # sentence; the exhaustion sentence would be false about a first take.
            _diagnostics._log_condition_settled(
                logger, phase, index, observed, kind, diagnosis,
                session_id=self.session_id,
            )
            return replace(
                verdict,
                payload={
                    **verdict.payload,
                    # Same runner/page contract the spent terminals use: publish this
                    # capture_result, then finish rather than wait for a refused begin.
                    "terminal": True,
                    "terminal_outcome": kind,
                },
            )
        # Past this rung the meter is empty, which is what lets the terminal builder
        # below index ``_slot_attempts`` directly.
        if kind != _admission.SETTLE_GROUP_CLOSE_REQUIRED:
            # One arm per :data:`admission.SETTLE_KINDS` member, and the LOUD fallback:
            # here the dangerous direction is the PERMISSIVE one.
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
            # ``SETTLE_BELOW_POSITION_FLOOR``'s arm and the group half's LOUD fallback:
            # a group that cannot be shown to reach its floor ends honestly.
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
                # Overrides ``to_capture_dict``'s retryable reason; the same observed
                # code still selects the diagnosis.
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

        ``accepted=True`` is the only "this slot is done" signal, so a settled
        position must look accepted on the wire. Caller holds ``_close_lock``.
        """
        if self._journey.plan.is_last_index_of_group(phase, index):
            # A dropped LAST pose must still close the walk, so the journal records
            # that the walk ENDED. Nothing is published either way.
            if phase == PHASE_LATERAL:
                return PhaseVerdict(
                    True, payload={**self._close_lateral_walk(), **payload}
                )
            closing = self._close_cloud_group(phase, None)
            if not closing.accepted:
                # This slot is already spent: a close-time product gate replaced
                # the retryable rejection with its own hard stop. Not
                # ``_terminal_spent_verdict``, whose diagnosis is the earlier one.
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
        # A position the flow gave up on counts as resolved too, or the phase
        # would never close. ``_group_positions`` stays the record of what was
        # MEASURED.
        self._journey.accept(phase, index)

    # --- per-phase verdicts ---------------------------------------------------
    # Each ``_consume_<phase>`` wraps ``_<phase>_verdict``, the only place an
    # accept/reject may be decided, and logs through ``_safe_log_diag``.

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
        """Bank an accepted unprompted capture, journal every one, decide nothing."""
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
        """CHECK's verdict: the ladder's answer, then the accept-side banking."""
        gain_plan = analysis.gain_plan
        kind = _dispatch.check_screens(
            _dispatch.CheckScreens(
                stimulus_located=_stimulus_locate_ok(analysis),
                anchor_ambiguous=analysis.anchor_ambiguous,
                channel_map_ok=analysis.channel_map_ok,
                pilot_snr_ok=analysis.pilot_snr_ok,
                linearity_ok=analysis.linearity_ok,
                gain_plan_present=gain_plan is not None,
                # Read only when a plan exists; ``False`` is the value the ladder
                # ignores in that case, never a claim that an absent solve cleared it.
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
        self._gain_plan_db = dict(gain_plan.gain_db)
        # HOLD the ambient report, don't just publish it (#1830): without it MEASURE's
        # per-driver SNR verdict has no noise floor to grade against.
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
        """Bank one take for a phase with no prompted spot."""
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
        """The four facts every banked take states about its OWN capture."""
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

        Ruling S3: the complex responses land in no file unless they land on the take.
        Guarded because a raise here would cost the CAPTURE; the caught tuple is
        concrete. ``[]`` means the curves were LOST, and the journal is the tell.
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

        It records and decides nothing, and must never acquire a branch that
        could, or the #2087 ruling quietly grows a gate back.
        """
        self._last_measure_guard = "ripple_disclosure"
        self._measure_ripple_reservation = {
            "predicted_ripple_db": float(predicted_ripple_db),
            # The threshold rides WITH the value: the disclosure states what was
            # true when the capture was judged.
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

        Records and decides nothing. Not a hearing-safety mechanism: the analysis still
        runs, and only the absolute-SPL commissioning stop hard-stops on calibration.
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

        ``docs/measurement-loop-doctrine.md`` §4 names confidence heuristics as
        provenance, not a gate. The PHYSICS half still refuses, on its own screen kind.
        """
        self._last_measure_guard = "alignment_confidence_disclosure"
        self._measure_alignment_reservation = {
            "confidence": float(confidence),
            "delay_us": float(delay_us),
            # The floor rides WITH the value, for the ripple disclosure's reason:
            # a rendered "0.41, below 0.6" is a lie once the constant moves.
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
        screen = _dispatch.measure_screens(
            _dispatch.MeasureScreens(
                stimulus_located=_stimulus_locate_ok(analysis),
                pilot_snr_ok=analysis.pilot_snr_ok,
                sweep_locate_confidence_ok=_sweep_locate_confidence_ok(analysis),
                glitch_detected=bool(analysis.glitch_detected),
                # A CALLABLE, and the rung whose eager resolution would be OBSERVABLE:
                # ``program_for_phase`` RAISES when MEASURE has no composed program.
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
                # A callable: the physical backstop is asked ONLY of an estimate
                # that already cleared the rung above.
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
        # Measurement-honesty DISCLOSURE G1 (owner ruling 2026-08-03, #2087). **This
        # does not refuse.** The capture is ACCEPTED and carries a reservation, which
        # changes what the household is TOLD and nothing about what is built.
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
        # Disclose, never block — an explicit ``False`` only, never ``None``, which is
        # "not resolved either way" per ``ProgramAnalysis.mic_calibrated``.
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
            # Fail FAST, at the capture that produced the unusable analysis: a
            # household must not walk the whole cloud for a session that cannot
            # produce a candidate.
            raise CrossoverV2FlowError("MEASURE analysis produced no candidate")
        self._measure_gate_window_ms = self._measure_gate(analysis)
        # **The fit runs at the last capture before the apply.** A session with a
        # CLOUD_MEASURE group (every production one) defers the fit and the publish to
        # that group's close, so the fit consumes the cloud's honesty verdict instead of
        # preceding it by eight captures; a session with no such group builds
        # here. On the deferring branch ONLY the analysis is retained — it is the
        # fit's input and must outlive the cloud walk. Exactly one is ever held.
        if PHASE_CLOUD_MEASURE in self._journey.plan.phases:
            self._measure_analysis = analysis
            return PhaseVerdict(True, payload={
                "measurement_phase": PHASE_MEASURE, **pair_claim,
            })
        # The no-deferral shape. The entry baseline is the "before" the round grades
        # against, not the fit's input, so it defers nothing.
        return PhaseVerdict(
            True,
            payload={
                "measurement_phase": PHASE_MEASURE,
                **pair_claim,
                **self._publish_measure_candidate(analysis, None),
            },
        )

    def _retained_group_indexes(self, phase: str) -> set[int]:
        """Which indexes of one group already hold evidence — one accessor over the
        two retentions, so the settle bookkeeping never branches on which list.
        """
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

        One rule is this method's rather than the ladder's: a rejected pose does NOT
        re-arm MEASURE with a level backoff, or its curve stops being comparable.
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
        # Outside the lock below, unlike the cloud's in-lock retention: this writes
        # nothing any close reads.
        self._retain_lateral_pose(pose, prompt, result)
        # ONE critical section for retain + close: the candidate build reads the whole
        # walk, and a half-landed retain would fit a session that never existed.
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
        """Bank one accepted pose's WAV + sidecar. Fail-soft; never a gate."""
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
        """What the pose at this capture index was measured under."""
        offsets = self._journey.plan.group_offsets(PHASE_LATERAL)
        try:
            position = offsets.index(index)
        except ValueError:
            return _spatial.TakeClaim()
        claims = self._lateral_claims
        return claims[position] if position < len(claims) else _spatial.TakeClaim()

    def _close_lateral_walk(self) -> dict[str, Any]:
        """Record that the walk finished. Publishes nothing."""
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
        """One prompted position: light per-capture QC, then the group check."""
        response = analysis.summed_response
        # All SEVEN screens are stated though this ladder reads three: a fact about the
        # capture is the caller's to state, and two are vacuous for a cloud position.
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
        # ONE critical section for retain + close: ``run_speculative_group_close`` takes
        # the same lock, and a VOLUNTARY retake's discard must be atomic with it.
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

        Idempotent per index: a retaken position REPLACES the earlier take. The hash
        stays inside ``_close_lock`` on purpose — the meta is read by
        :meth:`_run_cloud_pipeline` at a close that can run on a background thread.
        """
        retained = self._group_positions[phase]
        retained[:] = [p for p in retained if p.index != position.index]
        retained.append(position)
        retained.sort(key=lambda p: p.index)
        gating = getattr(position.response, "gating", None) or {}
        # THIS seat's distance, not the rig's: the room floor rises with distance, so
        # the pose's own mark distance is what the declared geometry is evaluated at.
        bounce_s = _declared_first_bounce_s(position.geometry.mark_distance_m)
        gate = _gate_record(position.response, declared_first_bounce_s=bounce_s) or {}
        # The room survives a capture with no gating block, which is the one state
        # ``_gate_record`` reports as no record at all.
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

        ``position`` is ``None`` when the group closes on a SETTLED position with no
        curve, and a settled close never asks for a geometry retake. Combines exactly
        ONCE: a combine measured 3-6 s on the ten-position S0 corpus, worse on a Pi 5.
        """
        positions = self._group_positions[phase]
        combined = combine_cloud_positions(positions)
        # PR-L5's spatial arm reads the across-position level spread of BOTH groups,
        # off the one combine this method already paid for.
        self._group_band_spread[phase] = tuple(
            getattr(combined, "band_spread", None) or ()
        )
        verdict = _geometry_verdict_from_combined(combined, len(positions))
        retries = self._geometry_retries_used[phase]
        # Four conjuncts and a narrowing — see
        # :func:`~jasper.active_speaker.crossover_v2.spatial.geometry_retake`.
        retake = _spatial.geometry_retake(
            locked=verdict.get("locked"),
            thin_evidence=verdict.get("thin_evidence"),
            retries_used=retries,
            budget=GEOMETRY_RETRY_POSITIONS,
            group_already_closed=phase in self._group_geometry,
            have_take_to_replace=position is not None,
        )
        if retake is not None and self._positions_gated:
            # REFUSE rather than prompt (owner ruling: refuse, don't mislead), for
            # EITHER gated shape: a wider rung is a pose an external positioner cannot
            # reach, and the retry re-authorizes the SAME entry with its original
            # bearing. The retry budget is NOT spent and no take is dropped.
            log_event(
                logger,
                "correction.crossover_v2_geometry_retake_unreachable",
                level=logging.WARNING,
                session_id=self.session_id,
                phase=phase,
                tier=self._tier,
                # `tier` cannot carry this: stage 2 is constructed without one. This
                # names the PREDICATE that refused, not WHICH shape.
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
            # Drop the take being replaced FROM THE CLOUD — what the retake lever
            # means. Its artifact stays on disk under its attempt-qualified path.
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
        # #1872: a retake of the group's LAST position can land AFTER the group closed
        # once. Either way this is a REAL close as it stands NOW, so everything below
        # re-runs; only the durable artifact write is a per-phase singleton.
        self._group_geometry[phase] = verdict
        log_event(
            logger, "correction.crossover_v2_cloud_group_complete",
            session_id=self.session_id, phase=phase,
            positions=len(self._group_positions[phase]),
            geometry_locked=bool(verdict.get("locked")),
            geometry_reason=verdict.get("reason") or "",
            thin_evidence=bool(verdict.get("thin_evidence")),
            geometry_retries=retries,
            # Positions the flow gave up on (ruling #2086 item 3), so a support read
            # can tell a degraded cloud from a completed walk.
            unresolved=len(self._group_unresolved.get(phase, {})),
        )
        # The group's accept is decided ABOVE; this pipeline is disclosure on top of it.
        # Scoped claim: a NAMED-family exception cannot cost the accept, and anything
        # outside the six names propagates by design, pinned by
        # ``test_an_unnamed_exception_family_still_propagates_through_the_outer_wrap``.
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
            # The FIT no longer runs here (§2.6): firing it on this acceptance made
            # the final prompted position the one spot a household could not redo.
            # Stash the combine so the confirm does not pay for a second one.
            self._group_combined[phase] = combined
            # …and DROP any eagerly-fitted candidate in the same locked region, which
            # is what makes "a bank matches the current combine" hold with no counter.
            self._speculative_close = None
            payload["awaiting_confirm"] = True
        if phase == PHASE_CLOUD_VERIFY:
            # The delta probe's spatial arm, deliberately OUTSIDE the disclosure wrap:
            # this is a product gate, and a gate that cannot fail a capture is none.
            self._run_delta_probe()
            # **The probe reports; the ROUND decides.** The verdict reaches
            # ``evaluate_round_quality`` and restores go through the one restore owner.
            return self._grade_round_once(PhaseVerdict(True, payload=payload))
        return PhaseVerdict(True, payload=payload)

    def cloud_measure_group_awaiting_confirm(self) -> bool:
        """Whether the pre-apply cloud is walked but not yet confirmed."""
        return (
            PHASE_CLOUD_MEASURE in self._group_combined
            and not self._group_confirmed
        )

    @property
    def cloud_close_state(self) -> str:
        """Where the pre-apply cloud's close has got to."""
        if self._candidate is not None:
            return CLOUD_CLOSE_NONE
        if self._group_close_running:
            return CLOUD_CLOSE_RUNNING
        if self.cloud_measure_group_awaiting_confirm():
            return CLOUD_CLOSE_AWAITING_CONFIRM
        return CLOUD_CLOSE_NONE

    def run_speculative_group_close(self) -> bool:
        """Fit the pre-apply cloud NOW, before the household confirms.

        Returns True when a build was banked; every reason not to run is checked here.
        **Runs OFF the capture thread**, holding ``_close_lock`` for the whole fit, which
        the retake close and the confirm both take. It never closes the retake window,
        and a failure here is dropped — the confirm refits and raises the same thing.
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
                # Deliberately open: this is speculative work whose failure the
                # household has not asked about, and the confirm path will raise the
                # same thing where it can be handled. Not ``BaseException``.
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
        """The household's set-completion signal arrived; the fit is next."""
        self._group_close_running = True

    def confirm_cloud_measure_group(self) -> dict[str, Any] | None:
        """Close out the pre-apply cloud on the household's EXPLICIT confirmation.

        **The group-close seam** (§2.6), called by the host on the phone's
        set-completion signal; a begin *inside* the group is not a confirmation.
        Returns :meth:`_publish_measure_candidate`'s payload, or ``None``. **Nothing
        downstream applies anything.** Fires at most once per session: the guard is
        ``self._candidate``, so a raise leaves it unset and a failure can be retried.
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
        """Fit, build, and publish the candidate the household will review."""
        if self._measure_analysis is None:
            raise CrossoverV2FlowError(
                "cloud-measure group closed with no retained MEASURE analysis"
            )
        # A bank is only ever present for the CURRENT combine — a retake drops it in
        # the same locked region — so consuming it cannot smuggle a stale cloud past.
        banked = self._speculative_close
        if banked is not None:
            self._speculative_close = None
            payload = self._commit_measure_candidate(banked)
        else:
            payload = self._publish_measure_candidate(
                self._measure_analysis, self._cloud_fit_evidence(combined)
            )
        # Released on success. Releasing makes a SECOND call raise instead of
        # rebuilding, which is safe because the sole caller refuses once
        # ``self._candidate`` is set. Left in place on a raise.
        self._measure_analysis = None
        return payload

    def _publish_measure_candidate(
        self, analysis: ProgramAnalysis, cloud: "_CloudFitEvidence | None",
    ) -> dict[str, Any]:
        """Build and publish one candidate for the household to review.

        The single build/publish path; nothing it returns triggers an apply.

        **The accountability seam.** Its two load-bearing measurements NEITHER REFUSE
        (doctrine deviations (c) and (i)). They run AFTER the build and BEFORE
        ``_candidate`` is set, outside the SF2 arm that degrades to trims-only.
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

        Three things make a candidate REAL and none happen here: ``self._candidate`` is
        not written, ``publish_candidate`` does not fire, and the retained MEASURE
        analysis is not released — so a build a retake moots can be dropped.
        """
        if candidate_sections is None and source_preset is None:
            candidate, linearization = self._build_candidate(analysis, cloud)
        else:
            candidate, linearization = self._build_candidate(
                analysis, cloud, candidate_sections=candidate_sections,
                source_preset=source_preset,
            )
        # VERIFY-prediction coherence (#1668 PR-D): when this attempt fitted Layer-1a
        # linearization the persisted prediction must be the LINEARIZED model, the
        # thing the emitted graph carries. Otherwise ``analysis.predicted_sum``.
        predicted_sum = (
            linearization.linearized_predicted_sum
            if linearization.linearized_predicted_sum is not None
            else analysis.predicted_sum
        )
        # PR-L4: the last GRADING before a candidate can be proposed. It refuses
        # nothing; what it returns is the level-frame record the publish banks.
        level_frame_finding = self._assert_accountable(
            predicted_sum, analysis.predicted_sum, linearization=linearization,
            # Read off the CANDIDATE rather than ``self._prescribed_driver``: the
            # bar below is about the graph this apply would emit.
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

        Evaluated on the same measured branch pair against the same alignment anchor as
        the applied side. ``capture_fc_hz`` stays a PARAMETER because the guard checks
        it against the corner the applied profile ran. ``None`` becomes an
        ``unavailable`` probe; there is no fallback to the pre-#2611 axis.
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
        # The DRAFT's declared per-role polarity, which the measured branches carry.
        # The profile records absolute flags, so without this the frames differ.
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
        # INFO, carrying the four numbers the model turned on: a disputed rollback
        # should not need a second session. ONCE per distinct answer, keyed on fields.
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
        """This candidate's commanded axis: applied graph minus previous graph."""
        return _commanded_delta(
            self._previous_graph_predicted_sum(analysis, capture_fc_hz),
            predicted_sum,
        )

    @staticmethod
    def _declared_transfer_for(analysis: Any, predicted_sum: Any) -> Any:
        """This candidate's STATE axis: applied graph minus the RAW crossover."""
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
        linearization: _LinearizationState | None = None,
    ) -> None:
        """The ONE seam through which a planned candidate becomes real (#2291).

        Covers the three state writes, the proposal assembly and the two irreversible
        seam fires; it does NOT cover ``_measure_predicted_spec_report`` or the
        ``candidate_built`` disclosure. Every attribute write completes before
        ``publish_candidate``. Assembly cannot fail this commit (#2392).
        """
        from jasper.active_speaker.crossover_v2.contracts import InterventionProposal
        from jasper.active_speaker.crossover_v2.proposal import (
            plan_intervention_proposal,
        )

        self._candidate = candidate
        self._measure_predicted_sum = predicted_sum
        self._measure_commanded_delta = commanded_delta
        # #2614's STATE axis. Deliberately NOT part of the proposal: the proposal states
        # what the round asks for, this states what the graph declares.
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
            linearization=linearization,
        )
        self._intervention_proposal = planned
        self._measure_proposal_fingerprint = (
            planned.fingerprint
            if isinstance(planned, InterventionProposal)
            else ""
        )
        # #2662. Read off the candidate's own frozen evidence: ``analysis_json`` already
        # puts ``alignment_objective`` there, so this is the fingerprinted answer.
        analysis_evidence = getattr(candidate, "analysis", None)
        self._measure_alignment_objective = str(
            (analysis_evidence or {}).get("alignment_objective") or ""
            if isinstance(analysis_evidence, Mapping)
            else ""
        )
        self._seams.publish_candidate(candidate)
        self._publish_level_frame_finding(level_frame_finding)

    def _commit_measure_candidate(self, built: _SpeculativeClose) -> dict[str, Any]:
        """Make a built candidate REAL: stash it, publish it, disclose it."""
        candidate = built.candidate
        predicted_sum = built.predicted_sum
        analysis = built.analysis
        cloud = built.cloud
        self.commit_intervention_proposal(
            candidate,
            predicted_sum=predicted_sum,
            # The configured walk's branches are composed at the session's own
            # corner, the corner the guard checks the applied profile against.
            commanded_delta=self._commanded_delta_for(
                analysis, predicted_sum, self._fc_hz,
            ),
            declared_transfer=self._declared_transfer_for(analysis, predicted_sum),
            level_frame_finding=built.level_frame_finding,
            # Read off this build's own state (#2392).
            realized_branch_level=_contracts.realized_branch_level(
                built.linearization.realized_branch_level,
                pair_reason=self._pair_reason,
            ),
            linearization=built.linearization,
        )
        log_event(
            logger, "correction.crossover_v2_candidate_built",
            session_id=self.session_id,
            candidate_fingerprint=candidate.fingerprint,
            # Which linearization path this build took, read off the candidate
            # rather than a session field so one value is quoted, not two.
            linearization=candidate.linearization_outcome,
            # Did the cloud's honesty verdict actually reach the envelope?
            cloud_evidence=cloud is not None,
            excluded_bands=len(cloud.excluded_bands_hz) if cloud else 0,
            cloud_positions=cloud.n_positions if cloud else 0,
        )
        return {
            "candidate_fingerprint": candidate.fingerprint,
            # "This correction costs N dB of maximum level" (PR-L5). This is the
            # CONFIRM payload; the household disclosure is persisted by
            # ``_candidate_summary``. Both use ``worst_headroom_cost_db``.
            "headroom_cost_db": self._candidate_headroom_cost_db(),
        }

    def _publish_level_frame_finding(
        self, record: Mapping[str, Any] | None,
    ) -> None:
        """Persist the banked frame disagreement, or say why it was not.

        Called AFTER ``publish_candidate``, inside :meth:`_commit_measure_candidate`,
        which buys three things: once per session behind the ``_candidate`` guard (the
        finding store is write-once), never for a candidate that does not exist, and a
        citation that resolves. Fail-soft: plan §3.4 makes findings optional.
        """

        if record is None or self._seams.publish_findings is None:
            return
        try:
            self._seams.publish_findings(record)
        except (OSError, RuntimeError, TypeError, ValueError):
            # ``…_publish_failed`` and not ``…_finding_failed``, because
            # ``…_level_frame_finding`` is a RETIRED name (#2609).
            log_event(
                logger, "correction.crossover_v2_level_frame_publish_failed",
                level=logging.WARNING, session_id=self.session_id, exc_info=True,
            )

    def _candidate_headroom_cost_db(self) -> float:
        """The applied correction's disclosed max-level cost, dB (PR-L5)."""
        linearization = getattr(self._candidate, "linearization", None)
        if not isinstance(linearization, Mapping):
            return 0.0
        return worst_headroom_cost_db(linearization)

    def _position_residual_rows(
        self, combined: Any, floor_hz: float | None, ceiling_hz: float | None,
    ) -> tuple[Mapping[str, Any], ...]:
        """§4.2: how far each position sat from the combined curve, labelled."""
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

        Read off the envelope module's ``mic_trust_limit``: the first grid bin where the
        allowed depth is 0 dB IS the ceiling, so the probe's and the fit's cannot drift.
        On a ``reference`` mic that is 20 kHz (2026-08-29 horn-droop ruling).
        **The fitter may not command there; the probe may not grade there** (#2649).
        ``None`` in four cases, each of which SAYS so on the journal.
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
            # An unknown tier raises by design in the envelope module. Here that is
            # missing evidence: fall back to no ceiling and grade what the gate trusted.
            unavailable("mic_tier_not_recognised", tier)
            return None
        zeros = np.flatnonzero(allowed <= 0.0)
        if zeros.size == 0:
            unavailable("trust_curve_never_reaches_zero", tier)
            return None
        return float(grid[zeros[0]])

    def _refuse(self, code: str) -> "CaptureBeginRefused":
        """Build the refusal for ``code``, with its household copy, and record it as
        this session's failure code.

        **No production path calls this today**; it is kept because it alone owns the
        stamp. **Stamping ``_last_failure_code`` is the load-bearing half**: the host
        falls back to a capture-timeout sentence when it is unset.
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
        ``crossover_v2.accountability.assess_accountability``.

        What stays here is the stash the host persists and the journal identity. The
        ONE input the gate is TOLD is the prediction threshold, and choosing between its
        two values is THIS method's job (PR-B, owner ruling 2026-08-20): the fitted
        class keeps :data:`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB` (0.5 dB), while a
        prescribed graph requires NON-WORSENING (0.0), because a narrow high-Q filter
        predicts only 0.077-0.152 dB of pooled improvement when it is exactly right.
        **Neither bar stops anything**: it chooses which LEDGER value is banked.
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
        # ``spec_report`` is always written — ``None`` meaning "graded nothing".
        self._measure_predicted_spec_report = decision.spec_report
        for record in decision.journal:
            self._journal_linearization(record)
        return decision.finding

    def _cloud_fit_evidence(self, combined: Any) -> "_CloudFitEvidence | None":
        """This group's honesty verdict, in the shape the fit envelope takes.

        ``None`` — the fit runs with no cloud terms — in two disclosed cases.
        **All-or-nothing on purpose**: the screen cannot see a position-invariant null
        (0 of 5462 bins in 8-16 kHz on S0), so a screen-only mask is worse (#1742).
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
        """Bands BELOW the null registry's floor where this cloud's positions disagree
        about a dip (#1967) — the derivation is in ``crossover_v2.spatial``.
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
        """The honest-instrument pipeline for one closed group.

        ``combined`` is the SAME object ``_close_cloud_group`` derived its verdict from
        — ONE combine per close. ``positions`` supplies the gated validity floor the
        spec bands' lower edges are intersected with (#2551). **Runs on EVERY close,
        including a re-close from a retake** (#1872). Never raises.
        """
        # One reading of the tier's trust ceiling, spent twice below: the spec may not
        # GRADE above where the fitter may not COMMAND (#2649).
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
            # preset's committed regions.
            crossover_region_hz=committed_crossover_region_hz(
                getattr(self._preset, "crossover_regions", ()) or ()
            ),
            # #2291: the round's SPEC verdict needs the live object; the dict
            # below keeps the serialized copy every other surface reads.
            graded_spec_sink=lambda graded: self._group_graded_spec.__setitem__(
                phase, graded
            ),
        )
        self._group_cloud_result[phase] = result
        # #2609 SF5 / §4.2: what the ROUND needs and the serialized result does not
        # carry. Recorded for both phases; only ``PHASE_CLOUD_VERIFY``'s are read.
        floor_hz = cloud_trusted_floor_hz(cloud_validity_floor_hz(positions))
        self._group_trusted_floor_hz[phase] = floor_hz
        self._group_position_residuals[phase] = self._position_residual_rows(
            combined, floor_hz, ceiling_hz,
        )
        # PR-5: the spec verdict a session's journal carries, logged once per group
        # instead of once per capture.
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
            # WHICH FRAME the deviation above is stated against (#1857): the
            # pointer moves under a different reference band.
            flatness_reference_band_lo_hz=_band_edge(
                flatness.get("reference_band_hz"), 0
            ),
            flatness_reference_band_hi_hz=_band_edge(
                flatness.get("reference_band_hz"), 1
            ),
            # EVERY band's own deviation from that reference (#1857): a
            # uniformly-off band drags it and mislabels the largest deviation.
            flatness_bands=_per_band_flatness_log_field(spec.get("bands")),
            # The one figure above that the frame CANNOT move (#1857): the step
            # between two band levels, in which the shared reference cancels.
            flatness_tilt=_flatness_tilt_log_field(flatness),
            flatness_rms_db=flatness.get("rms_db"),
            spec_n_excluded=flatness.get("n_excluded"),
            validity_floor_hz=result.get("validity_floor_hz"),
        )
        # #1872: the PUBLISH is the one per-phase SINGLETON here. The store accepts an
        # identical retry idempotently, so this guard exists to stop a re-close spending
        # an attempt guaranteed to be REFUSED. Marked only on success.
        if phase in self._group_cloud_published:
            # The skip is the one fact nothing else states: the durable artifact now
            # LAGS the recomputed result. INFO — the retake contract as designed.
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
                # Evidence publication is forensics, never a gate, so a full disk
                # or a write-once conflict must not undo the group's own accept.
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
        """#2291's "before" capture: screen it, reduce it, retain it."""
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
        """The screens above, and the reduced side when they all pass."""
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

        **The only retention site that reads the seam's answer**, which decides only
        whether this baseline can CITE a durable artifact. The take carries the reduced
        CURVE, not only its scalars: a take is write-once, the state file is not.
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
        # The TAKE id, not the store's record id: ``read_entry_baseline_take``
        # answers a banked take's ``take_id`` under this name.
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
        """Which graph this capture was measured through, or the unknown word."""
        from jasper.active_speaker.crossover_v2 import coordinator

        return coordinator.entry_graph_fingerprint(
            self._round_ports(), session_id=self.session_id,
        )

    # --- #2291: the round, graded and acted on -------------------------------

    def _round_ports(self) -> "RoundPorts":
        """Narrow this session's seams down to the five a round may call."""
        from jasper.active_speaker.crossover_v2.coordinator import RoundPorts

        return RoundPorts(
            rollback=self._seams.rollback,
            rollback_available=self._seams.rollback_available,
            applied_boosts=self._seams.applied_boosts,
            entry_graph_fingerprint=self._seams.entry_graph_fingerprint,
            publish_round_receipt=self._seams.publish_round_receipt,
        )

    def _applied_candidate_id(self) -> str:
        """The APPLIED candidate's fingerprint, by the one honest chain."""
        return self._tuning_attempt_id or str(
            getattr(self._candidate, "fingerprint", "") or ""
        )

    def _grade_round_once(self, verdict: PhaseVerdict) -> PhaseVerdict:
        """Grade this round and act on the adoption table. Once per session.

        **One owner, two triggers**: express, at the end of :meth:`_consume_verify`;
        full, at the ``PHASE_CLOUD_VERIFY`` close. **Both require an ACCEPTED
        capture** — a retriable rejection would burn this guard on evidence the
        household then replaced, and a write-once receipt would name the wrong capture.
        """
        from jasper.active_speaker.crossover_v2 import coordinator

        if self._round_evaluated:
            return verdict
        self._round_evaluated = True
        # #2602. ``None`` is a host that resolved nothing, and the opening round is the
        # fail-safe reading: it can only offer another round, never suppress a stop.
        position = self._series_position or coordinator.SeriesPosition.first()
        graded_verify = self._group_graded_spec.get(PHASE_CLOUD_VERIFY)
        decision = coordinator.run_round(
            coordinator.RoundEvidence(
                session_id=self.session_id,
                tier=self._tier,
                post_analysis=self._verify_analysis,
                entry_baseline=self._measure_entry_baseline,
                # ``None`` on a tier that walks no cloud, which the evaluator reads
                # as "no report" rather than as a pass (#2160).
                spec_report=(
                    None if graded_verify is None else graded_verify.report
                ),
                # Decision 10's evidence: the SAME evaluation the spec verdict
                # reads, with its curve and merged honesty mask.
                graded_spec=graded_verify,
                applied_blend_correction=self._applied_blend_correction(),
                previous_blend_residual_db=position.previous_blend_residual_db,
                # #2662. Rehydrated from stage 1's durable ``verify_priors``: this
                # stage holds no candidate to derive one from.
                alignment_prescription=self._alignment_prescription,
                # …and whether the machinery COMMITTED it: provenance without its
                # outcome is a receipt that can credit a round it never ran.
                alignment_objective=self._measure_alignment_objective,
                # The crossover pin, on the identical route. It needs no outcome
                # field: the boundary opened both stages at the pinned topology.
                topology_prescription=self._topology_prescription,
                # WHAT THIS ROUND PROPOSED (#2392), preferred over what it applied.
                # The candidate below is a real fallback: a stage-2 re-arm predating
                # #2392, and a commit whose proposal assembly was refused.
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
                # ``None`` when this session never ran one (#2537). Both triggers
                # reach here AFTER :meth:`_run_delta_probe` has stamped it.
                delta_probe=self._delta_probe,
                # Where this round sits in the household's flattening series (#2602),
                # from the durable receipt the previous round banked.
                round_ordinal=position.ordinal,
                # Which epoch that ordinal counts in: a republish restarts the
                # sequence, and the pair says so where the ordinal alone cannot.
                round_ordinal_epoch=position.ordinal_epoch,
                previous_objectives=position.previous_objectives,
                # #2609 SF5: the frame those objectives were graded in. Without the
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
        """Map a coordinator refusal KIND to the code the household reads."""
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
        """Stamp a round-driven refusal the way the delta probe already does."""
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
        # ``phase`` is REQUIRED rather than defaulted: a hardcoded ``verify`` would
        # mislabel another phase's capture into a write-once record.
        verdict = self._consume_unprompted(
            phase, index, attempt, analysis, result,
            self._verify_verdict(analysis), self._log_verify_diag,
        )
        # #2291: the round's post-apply side, retained BEFORE grading, because the Full
        # tier grades the round later from a call that cannot see this capture.
        self._verify_analysis = analysis
        self._grade_verify_attempt(analysis, verdict, capture_attempt=attempt)
        # Grade the round HERE when this ACCEPTED capture is the last post-apply
        # evidence there will be; a Full session grades it at the cloud close. **Only
        # on an accepted verdict**, or the fire-once guard burns on replaced evidence.
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

        A rejected capture is still judged so integrity failures reach STOP_EVIDENCE
        (#2033) but is not appended to accepted history. **Exactly-once survives a
        failed write, which is why the seam call catches broadly** (#2386): the
        repeat guard only sees a repeat once the attempt is appended, at the END
        of this method.
        """

        # The identity is the APPLIED candidate's, most specific first: the tuning
        # attempt id, the built candidate's fingerprint, then a per-capture fallback.
        # Two captures of one candidate must land on one id or the dedup is blind.
        attempt_id = self._tuning_attempt_id
        if not attempt_id and self._candidate is not None:
            attempt_id = str(getattr(self._candidate, "fingerprint", "") or "")
        if not attempt_id:
            attempt_id = f"{self.session_id}:{capture_attempt}"
        if any(item.attempt_id == attempt_id for item in self._attempt_history):
            # Already in accepted history: a repeated successful re-verify is not a
            # new tuning attempt, and this skip is the one rung against a second
            # durable observation of one identity.
            return

        record = attempt_record_from_verify(
            analysis,
            attempt_id=attempt_id,
            # The session that captured THIS sweep — a capture session is the
            # sitting (#2081).
            sitting_id=self.session_id,
        )
        writer = self._seams.record_model_error
        # The store banks PREDICTION error, and its number is the tracking deviation —
        # read off the analysis, not ``record.grade_db``. The two are equal today and
        # that coincidence is the hazard: two owners, two quantities (#2291).
        tracking_deviation_db = _attempt_optional_float(
            (analysis.verify_tracking or {}).get(
                ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED
            )
        )
        if verdict.accepted and writer is not None and tracking_deviation_db is not None:
            try:
                # Claim the durable observation identity before banking the journey
                # projection: a recovery capture can measure a slightly different grade.
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
                # Ordinary persistence outages are forensics failures: they do not
                # reverse a VERIFY the measurement gate already accepted.
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
                # Any OTHER store failure, contained for the arm above's reason — and
                # containing it is what makes the write exactly-once (#2386): an
                # escape also skips the ``_attempt_history`` append below. Not
                # ``BaseException``.
                # ERROR, because the arm above is an outage and this one is a defect.
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
                    # The store already owns this identity with different numbers. Clear
                    # the hydrated decision too, or the done screen calls a prior basis
                    # "the latest applied result".
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
        # preconditions. The LAST arm makes the claim about the speaker, so a future
        # arm that cannot decide must degrade toward the first.
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

        One call, not three assignments (#1974): the done screen reads the code and the
        gate TOGETHER. **The gate is a parameter, not a field this method reads** —
        recomputing it before the early returns let an attempt that early-returned
        overwrite it while leaving the PREVIOUS attempt's outcome and code standing.
        """
        self._verify_outcome = outcome
        self._verify_code = code
        self._verify_gate = gate

    def _note_verify_mismatch(self, max_db: Any) -> str:
        """Which out-of-tolerance code this attempt earns (#1873).

        The single owner of both halves of the discriminator.
        ``verify_deterministic_mismatch`` once an attempt lands within
        :data:`VERIFY_REPEAT_FLOOR_DB` of **its predecessor** — never of a fixed
        first attempt, unlike the G3 reference above — where the instrument cannot
        tell the two apart. **The non-finite guard is load-bearing for NaN**:
        without it, ``nan > floor`` is ``False`` and an unmeasurable capture reads
        as agreement.
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
        # Reset every call: ``_log_verify_diag`` runs unconditionally after this method
        # returns and would misreport a prior attempt's step as fresh.
        self._verify_pilot_transfer_step_db = None
        # Same reset discipline: only a verdict that reaches the tracking comparison
        # carries expert-disclosure evidence (#1605) or a graded band (#1868).
        self._verify_evidence = None
        self._verify_graded_band_hz = None
        self._verify_frame = None
        self._verify_claims = None
        # THIS attempt's gate, as a LOCAL: computed before the early returns because
        # the gate-comparability refusal needs it, but it becomes session state only
        # through ``_set_verify_outcome``. ``verify_gate`` below is a WINDOW in ms.
        gate_record = _gate_record(
            analysis.summed_response,
            declared_first_bounce_s=_declared_first_bounce_s(MARK_DISTANCE_M),
        )
        # The pre-grade ladder belongs to ``capture_dispatch.verify_integrity_screens``
        # and runs ahead of EVERY grade. What stays here is every rung that reads state
        # outliving ONE capture.
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
        # G3: the tracking-max comparison below is exactly what a shifted recording
        # chain invalidates, so check the chain's OWN consistency first. The first
        # usable attempt of the session only records the reference (#1927).
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
        # Every §7 claim, graded BEFORE any of them gates, so a capture that fails one
        # still discloses the others.
        self._verify_claims = _verify_claims(tracking, analysis.verify_absolute)
        # Notch-aware, validity-floor-clamped comparator (W6.7 ruling 1): the
        # NOTCH-EXCLUDED max over this capture's own gate-derived band. Run 7 read
        # 27.83 dB raw against a predicted sum whose own ripple was ~30 dB.
        max_db = tracking.get("max_db_notch_excluded")
        # Gated on the CLAIM just recorded: R18's vocabulary is three-valued, and a
        # claim nobody could grade must not read as one that failed (#3487).
        if self._verify_claims["integration"]["status"] == CLAIM_FAIL:
            code = self._note_verify_mismatch(max_db)
            self._set_verify_outcome("fail", code, gate_record)
            # Its own name: the integrity-screen branch above already binds a
            # ``payload`` in this scope.
            mismatch_payload: dict[str, Any] = {"tracking": dict(tracking)}
            if code == REASON_VERIFY_DETERMINISTIC_MISMATCH:
                # The runner's contract for "no later capture can make this set
                # usable": the session closes on the verdict instead of waiting
                # for a next begin whose only answer would be a refusal.
                mismatch_payload["terminal"] = True
                mismatch_payload["terminal_outcome"] = (
                    VERIFY_TERMINAL_OUTCOME_DETERMINISTIC
                )
            return PhaseVerdict(False, code, payload=mismatch_payload)
        # Graded and inside tolerance: the mismatch did NOT repeat, so the pair #1873's
        # discriminator would draw its claim from is broken.
        self._verify_last_mismatch_max_db = None
        # PR-L5's delta probe, run only once tracking has PASSED. What it adds is the
        # band tracking cannot see: the whole span the correction commands.
        self._verify_tracking_curve = analysis.verify_tracking_curve
        summed = analysis.summed_response
        if summed is not None:
            self._verify_trusted_band_hz = _gate_trusted_band_hz(summed)
        # The probe reports here and the ROUND decides one call later; this CAPTURE
        # passed tracking, which is what this verdict answers.
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
        """Record that this session set its own G3 reference, if that is news."""
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
        # The step across the session boundary, which the WITHIN-session
        # ``pilot_transfer_step_db`` cannot be. INFO: a reset is ordinary.
        log_event(
            logger, "correction.crossover_v2_level_reference_reset",
            level=logging.INFO,
            session_id=self.session_id,
            step_db=round(step, 3),
            prior_age_s=round(time.time() - self._verify_pilot_prior_at, 1),
            ceiling_db=VERIFY_PILOT_TRANSFER_STEP_CEILING_DB,
        )

    # --- delta probe ---------------------------------------------------------

    def _run_delta_probe(self) -> DeltaProbeMap | None:
        """Classify what the speaker actually did against what was commanded.

        Runs at VERIFY on the at-the-mark map, and again at the post-apply group's
        close, which can only ADD evidence. A run that graded nothing is ``None``.
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

    # --- diagnostic logging ---------------------------------------------------
    # These three keep their method form because they ARE the seam that
    # ``_consume_unprompted`` takes as its ``log_diag`` argument.

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
        """The driver response whose gate window BINDS MEASURE — the shortest."""
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
        """Build one candidate — see ``crossover_v2.planning.build_candidate``.

        **The two preconditions stay HERE**: both are facts about this SESSION in this
        module's refusal vocabulary, and the first is ABOVE the SF2 degrade handler,
        which once caught it and degraded to a committable trims-only candidate in the
        wrong polarity convention. The two ports are bound attributes (#2354).
        """
        roles = self._role_names
        if (self._measurement_protection_sections_by_role is not None
                and not analysis.configured_path_composed):
            raise ValueError("protected-neutral capture reached the fitter uncomposed")
        if analysis.candidate is None and len(roles) > 1:
            # Hoisted to the capture that produces the analysis, so reaching it here
            # means a caller that did not walk that path. KEPT: without it the
            # fallback is a bare builtin mapped to ``internal_error``, not
            # ``program_unplayable``, and the organ contracts on this check.
            raise CrossoverV2FlowError("MEASURE analysis produced no candidate")
        return _planning.build_candidate(
            analysis, analysis.candidate, cloud,
            candidate_sections=candidate_sections,
            source_preset=source_preset or self._preset,
            roles=roles,
            plan=self._plan_linearization,
            exclusion_evidence=self._exclusion_evidence_json,
            journal=self._journal_linearization,
            # Decision 10: what the previous round prescribed, or what the
            # speaker is already playing. See ``_blend_prescription``.
            blend_correction=self._blend_prescription(),
            # Handed over RAW: the blend field has three sources to rank, this has
            # none, and merge-by-role IS the precedence, decided where the fit is final.
            driver_prescription=self._prescribed_driver,
        )

    def _exclusion_evidence_json(self, cloud: _CloudFitEvidence) -> dict[str, Any]:
        """The fit's cloud inputs — see ``planning.exclusion_evidence_json``.

        Read HERE, at call time, which is why the build takes this as a PORT: it must
        see ``_group_cloud_result``'s CURRENT value, refreshed on every close (#1872).
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

        Two suites pin these lines to the ``crossover_v2_flow`` logger by name. The
        three producers carry deliberately different record types, and only the build's
        has ``exc_info``. ``record.fields`` is spread as keyword arguments so the
        rendered order matches; a colliding key raises ``TypeError``.
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
        """Assemble ONE candidate's planner request and run the pure planner — see
        ``crossover_v2.planning.plan_for_candidate``.

        :meth:`program_for_phase` is passed rather than called because it can raise
        before the gain solve, and must raise AFTER the section set has been judged.
        **The ``journal_dropped`` notice stays HERE**, or it is lost with the port.
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
            # record's payload cannot also swallow the notice about it.
            log_event(
                logger, "correction.crossover_v2_linearization_journal_dropped",
                level=logging.WARNING, session_id=self.session_id,
                dropped=len(plan.journal_dropped),
                detail="; ".join(plan.journal_dropped),
            )
        return plan


_role_transfers = _priors.role_transfers


# --- session-volume lifecycle (one SessionVolumePlan per session, §5.5) ----


def derive_session_volume_db(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Sequence[str],
    *,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> float:
    """The fixed session measurement volume — the SSOT derivation (§5.5)."""
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
