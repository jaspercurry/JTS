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

SCHEMA_VERSION = 1
PLAYBACK_READINESS_KIND = "jts_active_speaker_playback_readiness"


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _gate(
    gate_id: str,
    *,
    label: str,
    passed: bool,
    message: str,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "passed": bool(passed),
        "message": message,
    }


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
    allow_tweeter_playback: bool = False,
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
    if channel and channel.role == "tweeter":
        tweeter_protection_ok = (
            channel.startup_muted
            and channel.protection_required
            and channel.protection_status == "present"
        )
    topology_blockers = [
        issue for issue in evaluation.get("blockers", [])
        if isinstance(issue, dict)
    ]
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
            label="Outputs use one coherent device clock",
            passed=clock.get("status") == "single_device_clock"
            and not clock.get("multi_device_aggregate_supported"),
            message=(
                "Single-device output clock is in use"
                if clock.get("status") == "single_device_clock"
                else "Use one coherent multi-output DAC for product playback"
            ),
        ),
        _gate(
            "tweeter_protection",
            label="Tweeter/compression-driver protection is satisfied",
            passed=tweeter_protection_ok,
            message=(
                "Selected target does not need extra tweeter protection"
                if channel and channel.role != "tweeter"
                else (
                    "Tweeter protection is present"
                    if tweeter_protection_ok
                    else "Confirm tweeter protection before any tweeter tone"
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
    target_is_tweeter = bool(channel and channel.role == "tweeter")
    target_role_allowed = not target_is_tweeter or allow_tweeter_playback
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
                "tweeter_audio_not_enabled",
                "tweeter/compression-driver playback is disabled for this slice",
            )
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
        "tone_backend": {
            "status": backend.get("status") or "artifact_only",
            "backend": backend.get("backend"),
            "audio_enabled": audio_backend_enabled,
            "test_pcm": backend.get("test_pcm"),
            "issues": backend_issues,
        },
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
