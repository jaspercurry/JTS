# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read current facts for preflight. Live admission remains with resource owners."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.program import KIND_PILOT
from jasper.audio_measurement.wired_capture import WiredCaptureError, require_wired_mic

from .angle_capture import BASE_CANDIDATE, AngleCaptureRequest, candidate_identity
from . import candidate_bank
from .baseline_profile import load_applied_baseline_profile_state
from .candidate_parts import candidate_from_applied_profile
from .commission_wiring import commissioning_spl_ceiling_db
from .crossover_v2.conductor_context import conductor_status, resolve_conductor_context
from .crossover_v2.measure_spec import branch_channels_for
from .crossover_v2.programs import SessionExcitation, compose_summed_program
from .crossover_v2.refusal_copy import CrossoverV2Refused
from .excitation_safety_plan import declared_minimum_cooldown_s
from .measured_crossover_candidate import MeasuredCrossoverCandidate
from .preflight import PreflightFacts, PreflightIssue
from .run_levels import prepare_level_captures
from .seat_level_reference import AnchorFacts, load_seat_level_reference


def read_preflight_facts(
    plan: AngleCaptureRequest, *, context: Any = None, device: Any = None,
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
    applied_bass_extension = None
    if context is not None:
        try:
            stop = commissioning_spl_ceiling_db(context.topology, preset=context.preset)
        except ValueError:
            pass
        try:
            applied = candidate_from_applied_profile(context.topology, load_applied_baseline_profile_state() or {})
            applied_bass_extension = applied.bass_extension
        except (OSError, RuntimeError, ValueError, LookupError):
            pass
    candidates: dict[str, MeasuredCrossoverCandidate | PreflightIssue] = {}
    for name in dict.fromkeys(candidate_identity(stop.candidate_id) for stop in plan.stops):
        if name == BASE_CANDIDATE:
            continue
        try:
            candidates[name] = candidate_bank.find_banked_candidate(name).candidate
        except candidate_bank.CandidateBankRefusal as exc:
            candidates[name] = PreflightIssue.from_code(exc.code, f"{name}: {exc.detail}")
    anchor = AnchorFacts(load_seat_level_reference() or {},
                         resolved_household_sensitivity(device) if device is not None else None)
    pilot_band = None
    if context is not None and anchor.record.get("ambient_report") and any(pose.plays_summed for pose in plan.stops):
        program = SessionExcitation(
            roles=context.roles_bands, caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db, fc_hz=context.fc_hz,
            sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
        ).verify_program()
        pilot = next(segment for segment in program.stimulus_segments() if segment.kind == KIND_PILOT)
        if pilot.f1_hz is not None and pilot.f2_hz is not None:
            pilot_band = (pilot.f1_hz, pilot.f2_hz)
    def program_ids(request: AngleCaptureRequest) -> tuple[str, ...]:
        if context is None or request.level.volume_db is None or not hasattr(context, "roles_bands"):
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
        role_targets = getattr(context, "role_targets", {})
        cooldown_s = (
            declared_minimum_cooldown_s(safety_profile, role_targets)
            if any(c.spec.graph_scope == "candidate_branches" for c in captures) else 0.0
        )
        programs = []
        for capture in captures:
            program = compose_summed_program(excitation, capture.spec,
                safety_profile=safety_profile, role_targets=role_targets)
            if capture.spec.graph_scope == "candidate_branches":
                program = build_branch_program(
                    program, branch_channels_for(capture.spec), cooldown_s=cooldown_s)
            programs.append(program.program_id)
        return tuple(programs)

    return PreflightFacts(
        candidates=candidates, mic_present=device is not None,
        mic_identified=bool(device is not None and device.model_key),
        anchor=anchor, summed_pilot_band_hz=pilot_band,
        commissioning_stop_db_spl=stop, mover=plan.mover, issues=tuple(issues),
        applied_bass_extension=applied_bass_extension,
        program_ids_for=program_ids,
    )
