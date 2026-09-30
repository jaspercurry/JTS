# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read current facts for preflight. Live admission remains with resource owners."""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Mapping

from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.band_ladders import NEAR_FIELD_BANDS_HZ
from jasper.audio_measurement.program import KIND_PILOT
from jasper.audio_measurement.wired_capture import WiredCaptureError, require_wired_mic
from jasper.platform.biquad import PeqFilter
from jasper.platform.log_event import log_event
from jasper.platform.control_client import read_output_volume

from .angle_capture import AngleCaptureRequest
from .arm_walk import TurntableMover
from .anchor_provenance import read_graph, read_pose
from .movers import MOVER_ARM
from . import candidate_bank
from .baseline_profile import load_applied_baseline_profile_state
from .candidate_parts import candidate_from_applied_profile
from .capture_schedule import takes_timing
from .commission_wiring import commissioning_spl_ceiling_db
from .crossover_v2.conductor_context import published_driver_caps, resolve_conductor_context
from .crossover_v2.measure_spec import branch_channels_for
from .crossover_v2.programs import SessionExcitation, compose_summed_program
from .crossover_v2.refusal_copy import CrossoverV2Refused
from .measured_crossover_candidate import MeasuredCrossoverCandidate, candidate_room_peqs
from .measurement_emit import load_tuning_declaration, room_layer_charge_db
from .measurement_programs import BASE_CANDIDATE, candidate_identity, near_field_drivers
from .preflight import PreflightFacts, PreflightIssue
from .setup_status import conductor_status
from .run_levels import prepare_level_captures
from .seat_level_reference import AnchorFacts, load_seat_level_reference


def read_preflight_facts(
    plan: AngleCaptureRequest, *, context: Any = None, device: Any = None,
    rig_clear_attested: bool | None = None, mover_available: bool = True,
) -> PreflightFacts:
    issues: list[PreflightIssue] = []
    if context is None:
        try:
            context = resolve_conductor_context(conductor_status())
        except CrossoverV2Refused as exc:
            issues.append(PreflightIssue.from_code(exc.code or "measure_box_not_ready", str(exc)))
    if device is None:
        try:
            device = require_wired_mic()
        except WiredCaptureError:
            pass
    stop = None
    applied_bass_extension: Mapping[str, Any] = {}
    applied_room_peqs: tuple[PeqFilter, ...] | None = ()
    applied_room_charge_db: float | None = None
    if context is not None:
        try:
            stop = commissioning_spl_ceiling_db(context.topology, preset=context.preset)
        except ValueError:
            pass
        state: Mapping[str, Any] | None = None
        try:
            state = load_applied_baseline_profile_state() or {}
            applied = candidate_from_applied_profile(context.topology, state)
            applied_bass_extension, applied_room_peqs = applied.bass_extension, candidate_room_peqs(applied)
            if applied_room_peqs:
                applied_room_charge_db = room_layer_charge_db(load_tuning_declaration(context.topology), applied)
        except (OSError, RuntimeError, ValueError, LookupError):
            # No applied profile has no room layer; one that cannot be read has an unknown one.
            applied_room_peqs = () if state is not None and state.get("status") != "applied" else None
    candidates: dict[str, MeasuredCrossoverCandidate | PreflightIssue] = {}
    for name in dict.fromkeys(candidate_identity(stop.candidate_id) for stop in plan.stops):
        if name == BASE_CANDIDATE:
            continue
        try:
            candidates[name] = candidate_bank.find_banked_candidate(name).candidate
        except candidate_bank.CandidateBankRefusal as exc:
            candidates[name] = PreflightIssue.from_code(exc.code, f"{name}: {exc.detail}")
    anchor = AnchorFacts(load_seat_level_reference() or {},
                         resolved_household_sensitivity(device) if device is not None else None,
                         graph=read_graph(compile_graph=True), pose=read_pose(arm_offset_deg=TurntableMover(timeout_s=5.0).offset_deg() if plan.mover == MOVER_ARM else None))
    pilot_band = None
    if context is not None and anchor.record.get("ambient_report") and (
            takes_timing(plan) or any(pose.plays_summed for pose in plan.stops)):
        program = SessionExcitation(
            roles=context.roles_bands, caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db, fc_hz=context.fc_hz,
            sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
        ).verify_program()
        pilot = next(segment for segment in program.stimulus_segments() if segment.kind == KIND_PILOT)
        if pilot.f1_hz is not None and pilot.f2_hz is not None:
            pilot_band = (pilot.f1_hz, pilot.f2_hz)
    def stimulus_ids(request: AngleCaptureRequest) -> tuple[str, ...]:
        if context is None or request.level.volume_db is None:
            return ()
        excitation = SessionExcitation(
            roles=context.roles_bands, caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=request.level.volume_db, fc_hz=context.fc_hz,
            sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
        )
        captures = prepare_level_captures(replace(request, repeats=1), roles_bands=context.roles_bands)
        if any(capture.spec.graph_scope == "drivers" for capture in captures):
            return ()
        safety_profile = getattr(context, "safety_profile", {})
        programs = []
        for capture in captures:
            program = compose_summed_program(excitation, capture.spec,
                safety_profile=safety_profile, role_targets=context.role_targets)
            if capture.spec.graph_scope == "candidate_branches":
                program = build_branch_program(program, branch_channels_for(capture.spec))
            programs.append(program.stimulus_id)
        return tuple(programs)

    near_field = {stop.pose.driver for stop in plan.stops if stop.pose.near_field}
    output_volume = read_output_volume()
    if output_volume.get("muted"):
        log_event(logging.getLogger(__name__), "active_speaker.measurement_output_muted", fields=output_volume)
    return PreflightFacts(
        rig_clear_attested=rig_clear_attested, mover_available=mover_available,
        output_volume=output_volume,
        candidates=candidates, mic_present=device is not None,
        mic_identified=bool(device is not None and device.model_key),
        anchor=anchor, summed_pilot_band_hz=pilot_band,
        commissioning_stop_db_spl=stop, mover=plan.mover, issues=tuple(issues),
        applied_bass_extension=applied_bass_extension, applied_room_peqs=applied_room_peqs,
        applied_room_charge_db=applied_room_charge_db,
        stimulus_ids_for=stimulus_ids,
        declared_target_ids=tuple(context.role_targets) if context is not None else None,
        # A near-field pose's driver is offered only when its sweep holds the view's top band whole.
        near_field_drivers=(tuple(driver for driver in near_field_drivers(context.topology)
                                  if driver not in near_field
                                  or context.driver_bands[driver].lower_hz <= NEAR_FIELD_BANDS_HZ[-1][0])
                            if context is not None and any(stop.pose.driver for stop in plan.stops) else None),
        roles_bands=context.roles_bands if context is not None else (),
        driver_caps=published_driver_caps(context.safety_profile, context.role_targets) if context is not None else {},
    )
