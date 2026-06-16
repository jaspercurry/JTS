"""No-audio playback-readiness checklist for active-speaker commissioning.

This module owns the deterministic safety evidence that must pass before a
future sound-emitting tone backend may target a physical driver. It deliberately
does not generate samples, open ALSA, reload CamillaDSP, or mutate state.
"""

from __future__ import annotations

import math
from typing import Any

from jasper.output_topology import (
    OutputTopology,
    SpeakerChannel,
    SpeakerGroup,
    channel_identity_report,
    clock_domain_report,
)

from ._common import gate as _gate, issue as _issue
from .audible_policy import (
    audible_policy_payload,
    audible_role_allowed,
    audible_role_block_code,
    audible_role_block_message,
)
from .driver_protection import (
    auto_level_decision,
    driver_protection_payload,
    driver_protection_profile,
)
from .safe_playback import floor_audio_confirmed_for_target

SCHEMA_VERSION = 1
PLAYBACK_READINESS_KIND = "jts_active_speaker_playback_readiness"
HIGH_FREQUENCY_FLOOR_TEST_PREVIEW_KIND = (
    "jts_active_speaker_high_frequency_floor_test_preview"
)
HIGH_FREQUENCY_FLOOR_TEST_RAMP_MS = 20


def _target(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
) -> tuple[SpeakerGroup, SpeakerChannel] | None:
    for group in topology.speaker_groups:
        if group.id != speaker_group_id:
            continue
        for channel in group.channels:
            if channel.role == role:
                return group, channel
    return None


def _bounded_level(calibration_level: dict[str, Any]) -> bool:
    test_signal = calibration_level.get("test_signal") or {}
    safety = calibration_level.get("safety") or {}
    try:
        requested = float(test_signal.get("requested_level_dbfs"))
        minimum = float(test_signal.get("min_level_dbfs"))
        maximum = float(test_signal.get("max_level_dbfs"))
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(requested)
        and math.isfinite(minimum)
        and math.isfinite(maximum)
        and minimum <= requested <= maximum
        and bool(test_signal.get("normal_system_volume_untouched"))
        and bool(safety.get("jts_enforces_bounds"))
        and bool(safety.get("operator_controls_level"))
        and bool(safety.get("requires_stop_control"))
    )


def _level_at_floor(calibration_level: dict[str, Any]) -> bool:
    test_signal = calibration_level.get("test_signal") or {}
    try:
        requested = float(test_signal.get("requested_level_dbfs"))
        minimum = float(test_signal.get("min_level_dbfs"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(requested) and math.isfinite(minimum) and (
        requested <= minimum + 1e-6
    )


def _floor_level_dbfs(calibration_level: dict[str, Any]) -> float:
    test_signal = calibration_level.get("test_signal") or {}
    try:
        level = float(test_signal.get("min_level_dbfs"))
    except (TypeError, ValueError):
        return -80.0
    return level if math.isfinite(level) else -80.0


def _clock_readiness(clock: dict[str, Any]) -> tuple[bool, str]:
    status = str(clock.get("status") or "")
    if status == "single_device_clock" and not bool(
        clock.get("multi_device_aggregate_supported")
    ):
        return True, "Single-device output clock is in use"
    if status == "dual_apple_composite_clock" and bool(
        clock.get("composite_clock_supported")
    ):
        return True, "Dual Apple composite output profile is in use"
    return False, "Use one coherent output clock or measured composite output owner"


def _mic_guidance(calibration_level: dict[str, Any]) -> dict[str, Any]:
    meter = calibration_level.get("mic_meter")
    if not isinstance(meter, dict):
        meter = {}
    status = str(meter.get("status") or "unmeasured")
    observed = meter.get("observed_dbfs")
    blocked = status in {"clipping", "too_loud"}
    guided = status in {"low", "usable"} and not blocked
    return {
        "status": status,
        "observed_dbfs": observed,
        "recommendation": meter.get("recommendation"),
        "blocked": blocked,
        "guided_level_available": guided,
        "message": (
            "Mic observation is usable for relative level guidance"
            if guided
            else "Lower or stop before continuing; mic level is unsafe"
            if blocked
            else "Record a mic observation before guided high-frequency bring-up"
        ),
    }


def _startup_load_payload(
    startup_load_state: dict[str, Any] | None,
    environment_report: dict[str, Any],
) -> dict[str, Any]:
    state = startup_load_state if isinstance(startup_load_state, dict) else {}
    camilla = (
        environment_report.get("camilla_config")
        if isinstance(environment_report.get("camilla_config"), dict)
        else {}
    )
    active_path = str(state.get("active_config_path") or "")
    current_path = str(camilla.get("path") or "")
    loaded = state.get("status") == "loaded" and bool(active_path)
    rollback_available = bool(state.get("rollback_available"))
    current_matches = bool(current_path and active_path and current_path == active_path)
    if loaded and rollback_available and current_matches:
        status = "loaded"
        message = "Protected startup DSP is loaded"
    elif loaded and not rollback_available:
        status = "rollback_missing"
        message = "Protected startup DSP has no rollback anchor"
    elif loaded and not current_path:
        status = "current_config_unknown"
        message = "CamillaDSP current config path is unavailable"
    elif loaded and not current_matches:
        status = "mismatch"
        message = "CamillaDSP is no longer running the loaded startup config"
    else:
        status = "not_loaded"
        message = "Load the protected startup DSP before any audible test"
    return {
        "status": status,
        "loaded": loaded,
        "rollback_available": rollback_available,
        "active_config_path": active_path or None,
        "current_config_path": current_path or None,
        "current_config_matches_loaded": current_matches,
        "ready_for_playback": loaded and rollback_available and current_matches,
        "message": message,
    }


def _high_frequency_driver_readiness(
    *,
    group: SpeakerGroup | None,
    channel: SpeakerChannel | None,
    assigned: bool,
    identity_verified: bool,
    clock: dict[str, Any],
    environment_report: dict[str, Any],
    startup_load: dict[str, Any],
    safe_session: dict[str, Any],
    calibration_level: dict[str, Any],
    stop_control_available: bool,
) -> dict[str, Any] | None:
    """Return readiness for a tweeter/high-frequency target."""

    if not channel or channel.role != "tweeter":
        return None
    protection_status = channel.protection_status
    profile = driver_protection_profile(
        channel.role,
        driver_style=channel.driver_style,
    )
    driver_protection = driver_protection_payload(
        channel.role,
        driver_style=channel.driver_style,
        protection_status=protection_status,
        band_limit={
            "type": "highpass",
            "highpass_hz": profile.min_highpass_hz,
        },
    )
    protection_accepted = (
        channel.startup_muted
        and channel.protection_required
        and protection_status in {"present", "software_guard_requested"}
    )
    clock_ready, clock_message = _clock_readiness(clock)
    mic = _mic_guidance(calibration_level)
    level_floor = _level_at_floor(calibration_level)
    gates = [
        _gate(
            "target_assigned",
            label="High-frequency target is assigned to one physical output",
            passed=assigned,
            message=(
                "High-frequency target has a physical output"
                if assigned
                else "Assign the high-frequency target to a physical output"
            ),
        ),
        _gate(
            "identity_verified",
            label="High-frequency output identity is verified",
            passed=identity_verified,
            message=(
                "High-frequency output identity is verified"
                if identity_verified
                else "Verify the physical output before connecting the high-frequency driver"
            ),
        ),
        _gate(
            "single_clock_domain",
            label="Output clock is coherent or measured composite",
            passed=clock_ready,
            message=clock_message,
        ),
        _gate(
            "protection_accepted",
            label="High-frequency protection path is accepted",
            passed=protection_accepted,
            message=(
                "Software-guarded bring-up is accepted for this high-frequency driver"
                if protection_status == "software_guard_requested"
                else "Physical protection is marked present"
                if protection_status == "present"
                else "Choose software-guarded bring-up or provide protection evidence"
            ),
        ),
        _gate(
            "protected_startup_loaded",
            label="Protected startup DSP is loaded",
            passed=bool(startup_load.get("ready_for_playback")),
            message=str(startup_load.get("message") or "Load protected startup DSP"),
        ),
        _gate(
            "safe_session_armed",
            label="Safe session is armed",
            passed=safe_session.get("status") == "armed",
            message=(
                "Safe session is armed"
                if safe_session.get("status") == "armed"
                else "Arm a safe session before high-frequency bring-up"
            ),
        ),
        _gate(
            "calibration_level_at_floor",
            label="Calibration level starts at the floor",
            passed=level_floor,
            message=(
                "Calibration level is at the quiet floor"
                if level_floor
                else "Reset calibration level to the floor before high-frequency bring-up"
            ),
        ),
        _gate(
            "mic_not_too_loud",
            label="Mic observation is not clipping or too loud",
            passed=not mic["blocked"],
            message=mic["message"],
        ),
        _gate(
            "stop_control_available",
            label="Stop control is available",
            passed=stop_control_available,
            message=(
                "Stop control is available"
                if stop_control_available
                else "Stop control must be available before high-frequency bring-up"
            ),
        ),
        _gate(
            "active_environment_ready",
            label="Active-speaker environment is ready",
            passed=bool(environment_report.get("ok_to_load_active_config")),
            message=(
                "Active-speaker environment is ready"
                if environment_report.get("ok_to_load_active_config")
                else "Active-speaker environment is blocked"
            ),
        ),
    ]
    manual_passed = all(gate["passed"] for gate in gates)
    guided_passed = manual_passed and bool(mic["guided_level_available"])
    target = {
        "speaker_group_id": group.id if group else None,
        "role": channel.role,
        "output_index": channel.physical_output_index,
    }
    floor_confirmed = floor_audio_confirmed_for_target(safe_session, target)
    auto_level = auto_level_decision(
        calibration_level,
        role=channel.role,
        driver_style=channel.driver_style,
        protection_status=protection_status,
        band_limit={
            "type": "highpass",
            "highpass_hz": profile.min_highpass_hz,
        },
        floor_audio_confirmed=floor_confirmed,
        stop_control_available=stop_control_available,
    )
    status = (
        "guided_ready" if guided_passed
        else "manual_ready" if manual_passed
        else "blocked"
    )
    issues = [
        _issue("blocker", gate["id"], gate["message"])
        for gate in gates
        if not gate["passed"]
    ]
    floor_level = _floor_level_dbfs(calibration_level)
    preview_status = "preview_ready" if manual_passed else "blocked"
    floor_test_preview = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": HIGH_FREQUENCY_FLOOR_TEST_PREVIEW_KIND,
        "status": preview_status,
        "would_play": False,
        "audio_allowed": False,
        "target": {
            "speaker_group_id": group.id if group else None,
            "speaker_label": group.label if group else None,
            "role": channel.role,
            "physical_output_index": channel.physical_output_index,
        },
        "tone": {
            "waveform": "sine",
            "frequency_hz": profile.floor_test_frequency_hz,
            "level_dbfs": floor_level,
            "duration_ms": profile.floor_test_duration_ms,
            "ramp_ms": HIGH_FREQUENCY_FLOOR_TEST_RAMP_MS,
            "band_limit": {
                "type": "highpass",
                "highpass_hz": profile.min_highpass_hz,
            },
        },
        "safety": {
            "requires_stop_control": True,
            "requires_level_floor": True,
            "requires_protected_startup_loaded": True,
            "requires_operator_target_identity": True,
            "requires_mic_not_too_loud": True,
        },
        "issues": issues,
        "next_step": (
            "Diagnostic only. Audible high-frequency playback still requires the normal playback endpoint gates."
            if manual_passed
            else "Resolve high-frequency readiness blockers before generating this floor-test diagnostic."
        ),
    }
    return {
        "applies": True,
        "status": status,
        "audio_allowed": manual_passed and bool(driver_protection["audio_allowed"]),
        "manual_floor_test_candidate": manual_passed,
        "guided_floor_test_candidate": guided_passed,
        "protection_mode": (
            "software_guarded"
            if protection_status == "software_guard_requested"
            else "physical_protection"
            if protection_status == "present"
            else "missing"
        ),
        "target": {
            "speaker_group_id": group.id if group else None,
            "speaker_label": group.label if group else None,
            "role": channel.role,
            "driver_style": channel.driver_style,
            "physical_output_index": channel.physical_output_index,
        },
        "driver_protection": driver_protection,
        "auto_level": auto_level,
        "microphone": mic,
        "floor_test_preview": floor_test_preview,
        "required_gates": gates,
        "issues": issues,
        "next_step": (
            "High-frequency evidence is ready for the guarded floor-level playback path."
            if guided_passed
            else "Manual high-frequency guard evidence is ready; record a usable mic observation before guided level work."
            if manual_passed
            else "Resolve the high-frequency readiness blockers before enabling this output."
        ),
    }


def build_playback_readiness(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    environment_report: dict[str, Any],
    safe_session: dict[str, Any],
    calibration_level: dict[str, Any],
    startup_load_state: dict[str, Any] | None = None,
    tone_backend: dict[str, Any] | None = None,
    stop_control_available: bool = True,
) -> dict[str, Any]:
    """Return a versioned readiness checklist for one physical output target."""

    speaker_group_id = str(speaker_group_id or "")
    role = str(role or "")
    evaluation = topology.evaluation()
    identity = channel_identity_report(topology)
    clock = clock_domain_report(topology)
    match = _target(topology, speaker_group_id=speaker_group_id, role=role)
    group = match[0] if match else None
    channel = match[1] if match else None
    assigned = bool(channel and channel.physical_output_index is not None)
    target_identity_verified = bool(
        assigned and channel and channel.identity_verified
    )
    tweeter_protection_ok = True
    driver_style = channel.driver_style if channel else None
    protection_status = channel.protection_status if channel else None
    driver_protection_band_limit = None
    if channel and channel.role == "tweeter":
        profile = driver_protection_profile(
            channel.role,
            driver_style=driver_style,
        )
        driver_protection_band_limit = {
            "type": "highpass",
            "highpass_hz": profile.min_highpass_hz,
        }
    driver_protection = driver_protection_payload(
        channel.role if channel else role,
        driver_style=driver_style,
        protection_status=protection_status,
        band_limit=driver_protection_band_limit,
    )
    if channel and channel.role == "tweeter":
        tweeter_protection_ok = (
            channel.startup_muted
            and channel.protection_required
            and channel.protection_status in {"present", "software_guard_requested"}
        )
    topology_blockers = [
        issue for issue in evaluation.get("blockers", [])
        if isinstance(issue, dict)
    ]
    clock_issues = [
        issue for issue in clock.get("issues", [])
        if isinstance(issue, dict)
    ]
    clock_blockers = [
        issue for issue in clock_issues
        if issue.get("severity") == "blocker"
    ]
    clock_ready, clock_message = _clock_readiness(clock)
    environment_issues = [
        issue for issue in environment_report.get("issues", [])
        if isinstance(issue, dict)
    ]
    startup_load = _startup_load_payload(startup_load_state, environment_report)
    session_issues = [
        issue for issue in safe_session.get("issues", [])
        if isinstance(issue, dict)
    ]
    calibration_bounded = _bounded_level(calibration_level)
    backend = tone_backend if isinstance(tone_backend, dict) else {}
    gates = [
        _gate(
            "topology_valid",
            label="Saved output topology has no blockers",
            passed=bool(topology.speaker_groups) and not topology_blockers,
            message=(
                "Output topology is valid"
                if bool(topology.speaker_groups) and not topology_blockers
                else "Save a valid output topology before testing a driver"
            ),
        ),
        _gate(
            "target_found",
            label="Selected speaker driver exists in the saved topology",
            passed=match is not None,
            message=(
                "Selected target is present"
                if match is not None
                else "Select a saved speaker group and driver role"
            ),
        ),
        _gate(
            "physical_output_assigned",
            label="Selected driver is assigned to a physical DAC output",
            passed=assigned,
            message=(
                "Selected driver has a physical output"
                if assigned
                else "Assign the selected driver to a physical output"
            ),
        ),
        _gate(
            "physical_identity_verified",
            label="Physical output identity has been verified by the operator",
            passed=target_identity_verified,
            message=(
                "Physical channel identity is verified"
                if target_identity_verified
                else "Verify the selected physical output identity first"
            ),
        ),
        _gate(
            "single_clock_domain",
            label="Outputs use one coherent or measured composite clock",
            passed=clock_ready,
            message=clock_message,
        ),
        _gate(
            "tweeter_protection",
            label="High-frequency driver protection is satisfied",
            passed=tweeter_protection_ok,
            message=(
                "Selected target does not need extra high-frequency protection"
                if channel and channel.role != "tweeter"
                else (
                    "High-frequency protection path is accepted"
                    if tweeter_protection_ok
                    else "Confirm high-frequency protection before any high-frequency tone"
                )
            ),
        ),
        _gate(
            "active_config_load_gate",
            label="Active-speaker DSP load gate is ready",
            passed=bool(environment_report.get("ok_to_load_active_config")),
            message=(
                "Active-speaker environment is ready"
                if environment_report.get("ok_to_load_active_config")
                else "Active-speaker environment is blocked"
            ),
        ),
        _gate(
            "protected_startup_config_loaded",
            label="Protected startup DSP is loaded",
            passed=bool(startup_load.get("ready_for_playback")),
            message=str(startup_load.get("message") or "Load protected startup DSP"),
        ),
        _gate(
            "safe_session_armed",
            label="Safe playback session is armed and unexpired",
            passed=safe_session.get("status") == "armed",
            message=(
                "Safe session is armed"
                if safe_session.get("status") == "armed"
                else "Arm a safe session before preparing any tone"
            ),
        ),
        _gate(
            "calibration_level_bounded",
            label="Test-signal level is bounded and separate from volume",
            passed=calibration_bounded,
            message=(
                "Calibration level is inside the enforced envelope"
                if calibration_bounded
                else "Use a bounded calibration test level"
            ),
        ),
        _gate(
            "stop_control_available",
            label="Stop control is available before playback",
            passed=stop_control_available,
            message=(
                "Stop control is available"
                if stop_control_available
                else "Stop control must be available before playback"
            ),
        ),
    ]
    issues: list[dict[str, str]] = []
    issues.extend(topology_blockers)
    issues.extend(clock_issues)
    issues.extend(environment_issues)
    issues.extend(session_issues)
    known_issue_codes = {
        str(issue.get("code")) for issue in issues if isinstance(issue, dict)
    }
    for gate in gates:
        if gate["passed"] or gate["id"] in known_issue_codes:
            continue
        if gate["id"] in {"topology_valid", "tweeter_protection"} and topology_blockers:
            continue
        if gate["id"] == "single_clock_domain" and clock_blockers:
            continue
        if gate["id"] == "active_config_load_gate" and environment_issues:
            continue
        if gate["id"] == "safe_session_armed" and session_issues:
            continue
        issues.append(_issue("blocker", gate["id"], gate["message"]))
        known_issue_codes.add(gate["id"])
    preconditions_passed = all(gate["passed"] for gate in gates)
    backend_issues = [
        issue for issue in backend.get("issues", [])
        if isinstance(issue, dict)
    ]
    audio_backend_enabled = bool(backend.get("audio_enabled"))
    target_role = channel.role if channel else role
    target_role_allowed = bool(channel) and audible_role_allowed(
        target_role,
        driver_protection=driver_protection,
    )
    playback_allowed = (
        preconditions_passed
        and audio_backend_enabled
        and target_role_allowed
        and not backend_issues
    )
    if preconditions_passed and audio_backend_enabled and not target_role_allowed:
        issues.append(
            _issue(
                "blocker",
                audible_role_block_code(target_role),
                audible_role_block_message(target_role),
            )
        )
    high_frequency_driver = _high_frequency_driver_readiness(
        group=group,
        channel=channel,
        assigned=assigned,
        identity_verified=target_identity_verified,
        clock=clock,
        environment_report=environment_report,
        startup_load=startup_load,
        safe_session=safe_session,
        calibration_level=calibration_level,
        stop_control_available=stop_control_available,
    )
    target_label = None
    if group and channel:
        output_label = channel.human_output_label or (
            f"Output {channel.physical_output_index + 1}"
            if channel.physical_output_index is not None else None
        )
        target_label = " ".join(
            item for item in (group.label, channel.role, output_label) if item
        )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": PLAYBACK_READINESS_KIND,
        "status": "preconditions_passed" if preconditions_passed else "blocked",
        "preconditions_passed": preconditions_passed,
        "playback_allowed": playback_allowed,
        "would_play": playback_allowed,
        "tone_playback_implemented": bool(backend.get("tone_playback_implemented")),
        "audio_emitted": False,
        "target": {
            "speaker_group_id": speaker_group_id,
            "speaker_label": group.label if group else None,
            "speaker_kind": group.kind if group else None,
            "speaker_mode": group.mode if group else None,
            "role": channel.role if channel else role,
            "driver_style": channel.driver_style if channel else None,
            "physical_output_index": (
                channel.physical_output_index if channel else None
            ),
            "human_output_label": channel.human_output_label if channel else None,
            "label": target_label,
        },
        "topology": {
            "status": evaluation.get("status"),
            "topology_id": topology.topology_id,
            "name": topology.name,
        },
        "channel_identity": {
            "status": identity.get("status"),
            "verified_channel_count": identity.get("verified_channel_count"),
            "assigned_channel_count": identity.get("assigned_channel_count"),
        },
        "clock_domain": {
            "status": clock.get("status"),
            "clock_domain_id": clock.get("clock_domain_id"),
            "multi_device_aggregate_supported": bool(
                clock.get("multi_device_aggregate_supported")
            ),
            "measured_composite_supported": bool(
                clock.get("measured_composite_supported")
            ),
            "composite_clock_supported": bool(
                clock.get("composite_clock_supported")
            ),
            "coherent_physical_output_count": clock.get(
                "coherent_physical_output_count"
            ),
        },
        "environment": {
            "status": environment_report.get("status"),
            "load_gate": environment_report.get("load_gate"),
            "ok_to_load_active_config": bool(
                environment_report.get("ok_to_load_active_config")
            ),
        },
        "startup_load": {
            "status": startup_load.get("status"),
            "loaded": bool(startup_load.get("loaded")),
            "rollback_available": bool(startup_load.get("rollback_available")),
            "active_config_path": startup_load.get("active_config_path"),
            "current_config_path": startup_load.get("current_config_path"),
            "current_config_matches_loaded": bool(
                startup_load.get("current_config_matches_loaded")
            ),
        },
        "safe_session": {
            "status": safe_session.get("status"),
            "session_id": safe_session.get("session_id"),
            "expires_at": safe_session.get("expires_at"),
        },
        "calibration_level": calibration_level,
        "driver_protection": driver_protection,
        "tone_backend": {
            "status": backend.get("status") or "artifact_only",
            "backend": backend.get("backend"),
            "audio_enabled": audio_backend_enabled,
            "test_pcm": backend.get("test_pcm"),
            "issues": backend_issues,
        },
        "audible_test": audible_policy_payload(
            target_role,
            driver_protection=driver_protection,
        ),
        "high_frequency_driver": high_frequency_driver,
        "required_gates": gates,
        "issues": issues,
        "next_step": (
            "Preconditions and audio backend are ready for a short low-level test."
            if playback_allowed
            else "Preconditions are satisfied. Artifact verification is available; "
            "audible playback remains disabled or not applicable for this target."
            if preconditions_passed
            else "Resolve the blocking readiness items before any audible test tone."
        ),
    }
