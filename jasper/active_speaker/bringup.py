# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bring-up preflight contract for active-speaker commissioning.

This module answers a narrow product question before audible driver work:
"Can the operator continue in guided calibration mode, or only in manual
software-guarded bring-up mode?"  It deliberately does not open microphones,
play tones, load CamillaDSP, or mutate state.
"""

from __future__ import annotations

import math
from typing import Any

from jasper.output_topology import OutputTopology, channel_identity_report

from ._common import gate as _gate, issue as _issue
from .calibration_level import MIN_TEST_LEVEL_DBFS

SCHEMA_VERSION = 1
BRINGUP_PREFLIGHT_KIND = "jts_active_speaker_bringup_preflight"


def _requested_level(calibration_level: dict[str, Any]) -> float:
    test_signal = calibration_level.get("test_signal") or {}
    try:
        level = float(test_signal.get("requested_level_dbfs"))
    except (TypeError, ValueError):
        return MIN_TEST_LEVEL_DBFS
    return level if math.isfinite(level) else MIN_TEST_LEVEL_DBFS


def _floor_level(calibration_level: dict[str, Any]) -> float:
    test_signal = calibration_level.get("test_signal") or {}
    try:
        level = float(test_signal.get("min_level_dbfs"))
    except (TypeError, ValueError):
        return MIN_TEST_LEVEL_DBFS
    return level if math.isfinite(level) else MIN_TEST_LEVEL_DBFS


def _level_at_floor(calibration_level: dict[str, Any]) -> bool:
    return _requested_level(calibration_level) <= _floor_level(calibration_level) + 1e-6


def _required_tweeters(topology: OutputTopology) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            if channel.role != "tweeter" or not channel.protection_required:
                continue
            targets.append({
                "speaker_group_id": group.id,
                "speaker_label": group.label,
                "role": channel.role,
                "physical_output_index": channel.physical_output_index,
                "protection_status": channel.protection_status,
                "startup_muted": channel.startup_muted,
            })
    return targets


def _software_guard_staged(staged_config: dict[str, Any]) -> bool:
    software_guard = staged_config.get("software_guard")
    if not isinstance(software_guard, dict):
        return False
    return (
        staged_config.get("status") == "staged"
        and bool(software_guard.get("passed"))
        and bool(software_guard.get("no_load"))
        and bool(software_guard.get("no_playback"))
    )


def _guard_summary(
    topology: OutputTopology,
    *,
    staged_config: dict[str, Any],
) -> dict[str, Any]:
    tweeters = _required_tweeters(topology)
    if not tweeters:
        return {
            "status": "not_required",
            "accepted_for_manual_bringup": True,
            "software_guard_ready": False,
            "required_tweeter_count": 0,
            "targets": [],
            "message": "No high-frequency protection gate is required.",
        }

    present = [
        target for target in tweeters
        if target["protection_status"] == "present"
    ]
    software = [
        target for target in tweeters
        if target["protection_status"] == "software_guard_requested"
    ]
    missing = [
        target for target in tweeters
        if target["protection_status"] not in {"present", "software_guard_requested"}
    ]
    software_ready = bool(software) and _software_guard_staged(staged_config)
    if missing:
        status = "missing"
        accepted = False
        message = "High-frequency protection is not declared for every tweeter."
    elif len(present) == len(tweeters):
        status = "hardware_present"
        accepted = True
        message = "Hardware protection is marked present."
    elif software_ready:
        status = "software_guard_ready"
        accepted = True
        message = "Software-guarded bring-up evidence is staged."
    else:
        status = "software_guard_needs_staged_config"
        accepted = False
        message = "Stage and inspect the protected startup config first."

    return {
        "status": status,
        "accepted_for_manual_bringup": accepted,
        "software_guard_ready": software_ready,
        "required_tweeter_count": len(tweeters),
        "hardware_present_count": len(present),
        "software_guard_requested_count": len(software),
        "targets": tweeters,
        "message": message,
    }


def _microphone_summary(
    calibration_level: dict[str, Any],
    *,
    microphone_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meter = calibration_level.get("mic_meter") or {}
    supplied = microphone_report if isinstance(microphone_report, dict) else {}
    meter_status = str(supplied.get("meter_status") or meter.get("status") or "unmeasured")
    calibrated = bool(
        supplied.get("calibrated")
        or supplied.get("calibration_status") == "calibrated"
    )
    capture_works = bool(supplied.get("capture_works")) or meter_status in {
        "too_quiet",
        "low",
        "usable",
        "too_loud",
    }
    clipping = bool(supplied.get("clipping")) or meter_status == "clipping"
    safe_for_guidance = capture_works and not clipping and meter_status != "too_loud"
    if calibrated and safe_for_guidance:
        status = "calibrated"
        confidence = "absolute"
        message = "Calibrated microphone feedback is available."
    elif safe_for_guidance:
        status = "working_uncalibrated"
        confidence = "relative"
        message = "Microphone works; use relative level guidance only."
    elif clipping:
        status = "clipping"
        confidence = "blocked"
        message = "Microphone clipping blocks guided calibration."
    else:
        status = "not_checked"
        confidence = "none"
        message = "Check microphone capture before guided calibration."
    return {
        "status": status,
        "meter_status": meter_status,
        "capture_works": capture_works,
        "calibrated": calibrated,
        "clipping": clipping,
        "safe_for_guidance": safe_for_guidance,
        "confidence": confidence,
        "message": message,
    }


def _mode(
    mode_id: str,
    *,
    label: str,
    requires_microphone: bool,
    required_gates: list[dict[str, Any]],
    next_step: str,
    status: str | None = None,
) -> dict[str, Any]:
    passed = all(gate["passed"] for gate in required_gates)
    return {
        "id": mode_id,
        "label": label,
        "status": status or ("ready" if passed else "blocked"),
        "available": passed,
        "requires_microphone": requires_microphone,
        "required_gates": required_gates,
        "next_step": next_step,
    }


def build_bringup_preflight(
    topology: OutputTopology,
    *,
    environment_report: dict[str, Any],
    safe_session: dict[str, Any],
    staged_config: dict[str, Any],
    calibration_level: dict[str, Any],
    tone_backend: dict[str, Any] | None = None,
    microphone_report: dict[str, Any] | None = None,
    stop_control_available: bool = True,
) -> dict[str, Any]:
    """Return the read-only bring-up preflight packet."""

    evaluation = topology.evaluation()
    identity = channel_identity_report(topology)
    guard = _guard_summary(topology, staged_config=staged_config)
    microphone = _microphone_summary(
        calibration_level,
        microphone_report=microphone_report,
    )
    raw_blockers = [
        issue for issue in evaluation.get("blockers", [])
        if isinstance(issue, dict)
    ]
    ignored_codes = (
        {"tweeter_software_guard_requested"}
        if guard["status"] == "software_guard_ready"
        else set()
    )
    topology_blockers = [
        issue for issue in raw_blockers
        if str(issue.get("code")) not in ignored_codes
    ]
    assigned = int(identity.get("assigned_channel_count") or 0)
    unverified = int(identity.get("unverified_channel_count") or 0)
    level_at_floor = _level_at_floor(calibration_level)
    protected_config_staged = staged_config.get("status") == "staged"
    environment_ready = bool(environment_report.get("ok_to_load_active_config"))
    environment_load_gate = str(environment_report.get("load_gate") or "unknown")

    common_gates = [
        _gate(
            "output_topology_present",
            label="Speaker output topology is saved",
            passed=bool(topology.speaker_groups),
            message=(
                "Output topology exists"
                if topology.speaker_groups
                else "Create and save the speaker output topology"
            ),
        ),
        _gate(
            "topology_has_no_unhandled_blockers",
            label="Topology has no unhandled blockers",
            passed=not topology_blockers,
            message=(
                "Only the accepted software-guard exception remains"
                if raw_blockers and not topology_blockers
                else (
                    "Topology blockers remain"
                    if topology_blockers else "Topology is usable for bring-up"
                )
            ),
        ),
        _gate(
            "active_environment_ready",
            label="Active-speaker environment load gate is ready",
            passed=environment_ready,
            message=(
                "Active-speaker environment load gate is ready"
                if environment_ready
                else f"Resolve active-speaker environment load gate: {environment_load_gate}"
            ),
        ),
        _gate(
            "physical_identity_verified",
            label="Assigned physical outputs are verified",
            passed=assigned > 0 and unverified == 0,
            message=(
                "Physical output identity is verified"
                if assigned > 0 and unverified == 0
                else "Verify assigned DAC outputs before connecting drivers"
            ),
        ),
        _gate(
            "protected_startup_config_staged",
            label="Protected startup config is staged",
            passed=protected_config_staged,
            message=(
                "Protected startup candidate is staged"
                if protected_config_staged
                else "Stage the protected startup config"
            ),
        ),
        _gate(
            "high_frequency_guard_accepted",
            label="High-frequency guard is accepted",
            passed=bool(guard["accepted_for_manual_bringup"]),
            message=str(guard["message"]),
        ),
        _gate(
            "calibration_level_at_floor",
            label="Calibration level starts at the floor",
            passed=level_at_floor,
            message=(
                "Test level is at the safe floor"
                if level_at_floor
                else "Reset the calibration level before first bring-up"
            ),
        ),
        _gate(
            "stop_control_available",
            label="Stop control is available",
            passed=stop_control_available,
            message=(
                "Stop is available"
                if stop_control_available else "Stop must be available"
            ),
        ),
    ]
    manual_prereqs = all(gate["passed"] for gate in common_gates)
    session_armed = safe_session.get("status") == "armed"
    manual_status = (
        "armed" if manual_prereqs and session_armed
        else "ready_to_arm" if manual_prereqs
        else "blocked"
    )
    manual = _mode(
        "manual_guarded_bringup",
        label="Manual guarded bring-up",
        requires_microphone=False,
        required_gates=common_gates,
        status=manual_status,
        next_step=(
            "Ready for the next guarded bring-up step; keep level at the floor."
            if manual_prereqs and session_armed
            else "Arm the safe session before preparing any tone."
            if manual_prereqs
            else "Resolve the failed preflight gates."
        ),
    )

    mic_gates = [
        *common_gates,
        _gate(
            "microphone_capture_working",
            label="Microphone capture is working",
            passed=bool(microphone["capture_works"]),
            message=str(microphone["message"]),
        ),
        _gate(
            "microphone_safe_for_guidance",
            label="Microphone level is usable for guidance",
            passed=bool(microphone["safe_for_guidance"]),
            message=str(microphone["message"]),
        ),
    ]
    guided_prereqs = all(gate["passed"] for gate in mic_gates)
    guided_status = (
        "ready_calibrated" if guided_prereqs and microphone["calibrated"]
        else "ready_relative" if guided_prereqs
        else "blocked"
    )
    guided = _mode(
        "guided_calibration",
        label="Guided calibration",
        requires_microphone=True,
        required_gates=mic_gates,
        status=guided_status,
        next_step=(
            "Guided calibration can use calibrated microphone feedback."
            if guided_status == "ready_calibrated"
            else "Guided bring-up can use relative microphone feedback; final calibration still needs a calibrated mic."
            if guided_status == "ready_relative"
            else "Check microphone capture before guided calibration."
        ),
    )

    issues: list[dict[str, str]] = []
    issues.extend(topology_blockers)
    issues.extend(
        issue for issue in environment_report.get("issues", [])
        if isinstance(issue, dict)
    )
    if not guard["accepted_for_manual_bringup"]:
        issues.append(_issue(
            "blocker",
            "high_frequency_guard_not_ready",
            str(guard["message"]),
        ))
    if not level_at_floor:
        issues.append(_issue(
            "warning",
            "calibration_level_not_at_floor",
            "reset calibration level before first high-frequency bring-up",
        ))
    if not microphone["capture_works"]:
        issues.append(_issue(
            "warning",
            "microphone_not_checked",
            "guided calibration requires microphone capture",
        ))
    status = (
        "guided_ready" if guided["available"]
        else "manual_ready" if manual["available"]
        else "blocked"
    )
    backend = tone_backend if isinstance(tone_backend, dict) else {}
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BRINGUP_PREFLIGHT_KIND,
        "status": status,
        "manual_bringup_available": bool(manual["available"]),
        "guided_calibration_available": bool(guided["available"]),
        "microphone": microphone,
        "software_guard": guard,
        "modes": {
            "manual_guarded_bringup": manual,
            "guided_calibration": guided,
        },
        "environment": {
            "status": environment_report.get("status"),
            "load_gate": environment_report.get("load_gate"),
            "ok_to_load_active_config": bool(
                environment_report.get("ok_to_load_active_config")
            ),
        },
        "safe_session": {
            "status": safe_session.get("status"),
            "session_id": safe_session.get("session_id"),
            "armed": session_armed,
        },
        "calibration_level": {
            "requested_level_dbfs": _requested_level(calibration_level),
            "floor_level_dbfs": _floor_level(calibration_level),
            "at_floor": level_at_floor,
        },
        "tone_backend": {
            "status": backend.get("status") or "artifact_only",
            "audio_enabled": bool(backend.get("audio_enabled")),
        },
        "issues": issues,
        "next_step": (
            "Guided calibration is ready."
            if guided["available"]
            else "Manual software-guarded bring-up can continue without a mic."
            if manual["available"]
            else "Resolve preflight blockers before high-frequency bring-up."
        ),
    }
