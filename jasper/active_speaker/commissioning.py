"""Read-only commissioning rehearsal for active-speaker bring-up.

The rehearsal packet is a deterministic summary of existing evidence. It does
not persist progress, play audio, reload CamillaDSP, or inspect hardware. Its
job is to make the thin vertical slice legible: what durable gates are already
done, what the next safe action is, and where the flow hands back to an
operator-selected channel.
"""

from __future__ import annotations

from typing import Any

from jasper.output_topology import OutputTopology

from .calibration_level import MIN_TEST_LEVEL_DBFS

SCHEMA_VERSION = 1
COMMISSIONING_REHEARSAL_KIND = "jts_active_speaker_commissioning_rehearsal"


def _gate_map(report: dict[str, Any], mode_id: str) -> dict[str, dict[str, Any]]:
    mode = (report.get("modes") or {}).get(mode_id)
    gates = mode.get("required_gates") if isinstance(mode, dict) else []
    return {
        str(gate.get("id")): gate
        for gate in gates
        if isinstance(gate, dict) and gate.get("id")
    }


def _startup_gate_map(startup_load: dict[str, Any]) -> dict[str, dict[str, Any]]:
    preflight = startup_load.get("preflight") if isinstance(startup_load, dict) else {}
    gates = preflight.get("required_gates") if isinstance(preflight, dict) else []
    return {
        str(gate.get("id")): gate
        for gate in gates
        if isinstance(gate, dict) and gate.get("id")
    }


def _passed(gates: dict[str, dict[str, Any]], gate_id: str) -> bool:
    return bool((gates.get(gate_id) or {}).get("passed"))


def _gate_message(
    gates: dict[str, dict[str, Any]],
    gate_id: str,
    fallback: str,
) -> str:
    message = (gates.get(gate_id) or {}).get("message")
    return str(message or fallback)


def _level_at_floor(calibration_level: dict[str, Any]) -> bool:
    test_signal = calibration_level.get("test_signal") or {}
    try:
        requested = float(test_signal.get("requested_level_dbfs"))
    except (TypeError, ValueError):
        requested = MIN_TEST_LEVEL_DBFS
    try:
        floor = float(test_signal.get("min_level_dbfs"))
    except (TypeError, ValueError):
        floor = MIN_TEST_LEVEL_DBFS
    return requested <= floor + 1e-6


def _step(
    step_id: str,
    *,
    label: str,
    done: bool,
    blocked: bool,
    message: str,
    next_message: str,
    side_effect: str = "none",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = "done" if done else "blocked" if blocked else "pending"
    return {
        "id": step_id,
        "label": label,
        "status": status,
        "message": message if done or blocked else next_message,
        "side_effect": side_effect,
        "evidence": evidence or {},
    }


def _mark_next(steps: list[dict[str, Any]]) -> None:
    for step in steps:
        if step["status"] == "pending":
            step["status"] = "next"
            return


def _normalise_issue(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    if not code:
        return None
    return {
        "severity": str(raw.get("severity") or "warning"),
        "code": str(code),
        "message": str(raw.get("message") or code),
    }


def _loaded_current_state(startup_load: dict[str, Any]) -> bool:
    state = startup_load.get("state") if isinstance(startup_load, dict) else {}
    if not isinstance(state, dict):
        return False
    return (
        state.get("status") == "loaded"
        and bool(state.get("loaded"))
        and bool(state.get("rollback_available"))
        and bool(state.get("current_config_matches_loaded"))
    )


def build_commissioning_rehearsal(
    topology: OutputTopology,
    *,
    bringup_preflight: dict[str, Any],
    startup_load: dict[str, Any],
    safe_session: dict[str, Any],
    calibration_level: dict[str, Any],
) -> dict[str, Any]:
    """Return a versioned no-audio rehearsal packet for the saved flow."""

    manual_gates = _gate_map(bringup_preflight, "manual_guarded_bringup")
    startup_gates = _startup_gate_map(startup_load)
    assigned_count = sum(
        1
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.physical_output_index is not None
    )
    topology_ready = (
        bool(topology.speaker_groups)
        and _passed(manual_gates, "output_topology_present")
        and _passed(manual_gates, "topology_has_no_unhandled_blockers")
    )
    identity_ready = _passed(manual_gates, "physical_identity_verified")
    staged_ready = (
        _passed(manual_gates, "protected_startup_config_staged")
        and _passed(manual_gates, "high_frequency_guard_accepted")
    )
    path_ready = (
        _passed(startup_gates, "path_safety_ready")
        and _passed(startup_gates, "path_safety_matches_current_startup_load")
    )
    startup_loaded = _loaded_current_state(startup_load)
    session_armed = safe_session.get("status") == "armed"
    floor_ready = _level_at_floor(calibration_level)

    steps = [
        _step(
            "output_map_saved",
            label="Save output map",
            done=topology_ready,
            blocked=not topology.speaker_groups,
            message="Speaker layout is saved and usable for guarded bring-up.",
            next_message="Choose and save the speaker layout before safety checks.",
            evidence={
                "speaker_group_count": len(topology.speaker_groups),
                "assigned_channel_count": assigned_count,
            },
        ),
        _step(
            "channel_identity_verified",
            label="Verify physical outputs",
            done=identity_ready,
            blocked=topology_ready is False,
            message="Assigned DAC outputs are verified.",
            next_message=_gate_message(
                manual_gates,
                "physical_identity_verified",
                "Verify every assigned DAC output before connecting drivers.",
            ),
            evidence={"assigned_channel_count": assigned_count},
        ),
        _step(
            "protected_config_staged",
            label="Stage protected startup config",
            done=staged_ready,
            blocked=identity_ready is False,
            message="Protected startup config and compression-driver guard evidence are staged.",
            next_message="Stage the muted/protected startup config.",
            evidence={
                "bringup_status": bringup_preflight.get("status"),
                "software_guard": (bringup_preflight.get("software_guard") or {}).get(
                    "status"
                ),
            },
        ),
        _step(
            "protected_path_checked",
            label="Check protected path",
            done=path_ready,
            blocked=staged_ready is False,
            message="Path-safety evidence matches this startup load.",
            next_message=_gate_message(
                startup_gates,
                "path_safety_ready",
                "Run Check protected path before loading the startup config.",
            ),
            evidence={
                "preflight_status": (startup_load.get("preflight") or {}).get("status"),
                "path_safety": (
                    (startup_load.get("preflight") or {}).get("path_safety") or {}
                ).get("load_gate"),
            },
        ),
        _step(
            "startup_loaded",
            label="Load protected startup config",
            done=startup_loaded,
            blocked=path_ready is False,
            message="Protected startup config is loaded, current, and rollbackable.",
            next_message="Load the protected startup config before arming playback.",
            side_effect="camilla_reload_no_audio",
            evidence={
                "startup_load_status": (startup_load.get("state") or {}).get("status"),
                "rollback_available": bool(
                    (startup_load.get("state") or {}).get("rollback_available")
                ),
            },
        ),
        _step(
            "safe_session_armed",
            label="Arm safe session",
            done=session_armed,
            blocked=startup_loaded is False,
            message="Safe playback session is armed and time-bounded.",
            next_message="Arm a safe session after the protected startup config is loaded.",
            evidence={
                "safe_session_status": safe_session.get("status"),
                "expires_at": safe_session.get("expires_at"),
            },
        ),
        _step(
            "level_floor_ready",
            label="Reset calibration level",
            done=floor_ready,
            blocked=session_armed is False,
            message="Calibration level is at the quiet floor.",
            next_message="Reset the calibration level to the quiet floor.",
            evidence={
                "requested_level_dbfs": (
                    calibration_level.get("test_signal") or {}
                ).get("requested_level_dbfs"),
            },
        ),
        _step(
            "target_readiness_checked",
            label="Check one saved target",
            done=False,
            blocked=floor_ready is False,
            message="Target readiness is checked interactively in the browser.",
            next_message="Choose a saved channel and run Check readiness.",
            evidence={"persisted": False},
        ),
        _step(
            "artifact_verified",
            label="Verify artifact before audio",
            done=False,
            blocked=True,
            message="Artifact verification is target-specific and interactive.",
            next_message="Verify a bounded WAV artifact for the selected target.",
            evidence={"persisted": False},
        ),
        _step(
            "floor_audio_confirmed",
            label="Confirm floor audio",
            done=False,
            blocked=True,
            message="Floor audio confirmation is target/session-specific.",
            next_message="Play one floor-level audible test only after readiness allows it.",
            side_effect="short_floor_audio_when_lab_enabled",
            evidence={"persisted": False},
        ),
    ]
    _mark_next(steps)
    blocking_steps = [step for step in steps if step["status"] == "blocked"]
    completed_steps = [step for step in steps if step["status"] == "done"]
    durable_ready = (
        session_armed
        and floor_ready
        and not any(step["status"] == "blocked" for step in steps[:7])
    )
    issues = [
        issue
        for issue in (
            _normalise_issue(raw)
            for raw in (bringup_preflight.get("issues") or [])
        )
        if issue is not None
    ]
    issues.extend(
        issue
        for issue in (
            _normalise_issue(raw)
            for raw in ((startup_load.get("preflight") or {}).get("issues") or [])
        )
        if issue is not None
    )
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": COMMISSIONING_REHEARSAL_KIND,
        "status": (
            "ready_for_target_check"
            if durable_ready
            else "blocked"
            if blocking_steps
            else "in_progress"
        ),
        "no_audio": True,
        "durable_steps_ready": durable_ready,
        "completed_step_count": len(completed_steps),
        "total_step_count": len(steps),
        "steps": steps,
        "issues": issues[:8],
        "next_step": (
            "Choose a saved channel and run Check readiness. No sound is played by this rehearsal."
            if durable_ready
            else blocking_steps[0]["message"]
            if blocking_steps
            else "Continue the next pending commissioning step."
        ),
    }
