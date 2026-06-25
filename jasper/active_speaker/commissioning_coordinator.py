# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Backend view model for active-speaker commissioning.

This module is intentionally read-only. It composes the durable state files that
the setup flow already owns and turns them into product actions/messages for the
web UI. Sound-producing transitions still live in the existing action modules;
the coordinator is the single place that decides what the next obvious action
is and how failure evidence should be presented to a household.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from jasper.output_topology import OutputTopology, channel_identity_report

from .measurement import active_summed_targets

COORDINATOR_KIND = "jts_active_speaker_commissioning_view"


def issue_codes(issues: Any) -> set[str]:
    if not isinstance(issues, list):
        return set()
    return {
        str(issue.get("code") or "")
        for issue in issues
        if isinstance(issue, Mapping) and issue.get("code")
    }


def has_blocker(issues: Any) -> bool:
    return any(
        isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        for issue in (issues if isinstance(issues, list) else [])
    )


def summed_test_failure_message(issues: Any) -> str:
    """Return the one user-facing reason for a failed combined test."""

    codes = issue_codes(issues)
    if "tone_backend_failed" in codes:
        return (
            "JTS could not prepare the combined test audio. Retry after the setup "
            "finishes; if it fails again, open System status."
        )
    if "summed_commission_load_failed" in codes:
        return (
            "JTS could not open the quiet combined-test path. Press Play combined "
            "test to retry."
        )
    if "safe_session_not_armed" in codes:
        return (
            "JTS could not open the quiet combined-test session. Press Play "
            "combined test to retry."
        )
    if "summed_test_artifact_missing" in codes or "summed_test_playback_incomplete" in codes:
        return "The combined test did not finish. Press Play combined test to retry."
    if "summed_test_output_mismatch" in codes:
        return (
            "The last combined test did not match the saved speaker outputs. "
            "Re-check Confirm outputs before retrying."
        )
    if codes:
        return "The last combined test did not play. Press Play combined test to retry."
    return ""


def _step(step_id: str, label: str, status: str, message: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "status": status,
        "message": message,
    }


def _action(
    action_id: str,
    label: str,
    *,
    enabled: bool,
    endpoint: str | None = None,
    method: str = "POST",
    body: Mapping[str, Any] | None = None,
    message: str = "",
) -> dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "enabled": bool(enabled),
        "endpoint": endpoint,
        "method": method,
        "body": dict(body or {}),
        "message": message,
    }


def _latest(mapping: Any, key: str) -> Mapping[str, Any]:
    if isinstance(mapping, Mapping):
        value = mapping.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _combined_test_level(
    calibration_level: Mapping[str, Any] | None,
    latest_test: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    test_signal = (
        calibration_level.get("test_signal")
        if isinstance(calibration_level, Mapping)
        and isinstance(calibration_level.get("test_signal"), Mapping)
        else {}
    )
    software_guard = (
        calibration_level.get("software_gain_guard")
        if isinstance(calibration_level, Mapping)
        and isinstance(calibration_level.get("software_gain_guard"), Mapping)
        else {}
    )
    requested = test_signal.get("requested_level_dbfs", -80.0)
    latest_tone = (
        latest_test.get("tone")
        if isinstance(latest_test, Mapping)
        and isinstance(latest_test.get("tone"), Mapping)
        else {}
    )
    latest_level = _finite_float(latest_tone.get("level_dbfs"))
    if (
        latest_level is not None
        and isinstance(latest_test, Mapping)
        and latest_test.get("captured") is True
        and latest_test.get("audio_emitted") is True
        and not has_blocker(latest_test.get("issues"))
    ):
        requested = latest_level
    return {
        "requested_level_dbfs": requested,
        "min_level_dbfs": test_signal.get("min_level_dbfs", -80.0),
        "max_level_dbfs": test_signal.get("max_level_dbfs", 0.0),
        "step_db": test_signal.get("step_db", 1.0),
        "upward_step_limit_db": software_guard.get("upward_step_limit_db", 6.0),
    }


def _combined_group_view(
    target: Mapping[str, Any],
    *,
    summary: Mapping[str, Any],
    calibration_level: Mapping[str, Any] | None,
) -> dict[str, Any]:
    group_id = str(target.get("speaker_group_id") or "")
    label = str(target.get("speaker_group_label") or group_id or "Speaker")
    latest_tests = summary.get("latest_summed_tests")
    latest_validations = summary.get("latest_summed_validations")
    latest_test = _latest(latest_tests, group_id)
    latest_validation = _latest(latest_validations, group_id)
    test_level = _combined_test_level(calibration_level, latest_test)
    driver_checks_complete = bool(
        summary.get("driver_checks_complete")
        or summary.get("driver_measurements_complete")
    )
    validated = latest_validation.get("validated") is True
    has_audible_test = (
        latest_test.get("captured") is True
        and latest_test.get("audio_emitted") is True
        and not has_blocker(latest_test.get("issues"))
    )
    failure_message = (
        ""
        if has_audible_test or not latest_test
        else summed_test_failure_message(latest_test.get("issues"))
    )

    if validated:
        status = "validated"
        status_label = "validated"
        message = "Combined crossover check is saved."
    elif has_audible_test:
        status = "ready_to_record"
        status_label = "ready to record"
        message = "Combined speaker test played. Record what you heard."
    elif driver_checks_complete:
        status = "ready_to_test" if not failure_message else "test_failed"
        status_label = "next" if not failure_message else "not tested"
        message = failure_message or (
            "Run the combined speaker test. It uses the prepared crossover setup "
            "and starts at the quiet test level."
        )
    else:
        status = "blocked"
        status_label = "after driver checks"
        message = "Test each driver first, then test the combined speaker."

    latest_test_id = str(
        latest_test.get("summed_test_id") or latest_test.get("playback_id") or ""
    )
    latest_validation_test_id = str(
        latest_validation.get("summed_test_id")
        or latest_validation.get("playback_id")
        or ""
    )
    latest_test_validated = bool(
        validated
        and latest_test_id
        and latest_validation_test_id == latest_test_id
    )
    actions = {
        "start_combined_test": _action(
            "start_combined_test",
            "Play combined test",
            enabled=driver_checks_complete,
            endpoint="./active-speaker/summed-test",
            body={
                "speaker_group_id": group_id,
                "audio": True,
                "stimulus": "speech",
                "duration_ms": 12000,
                "level_dbfs": test_level.get("requested_level_dbfs"),
            },
        ),
        "record_combined_result": _action(
            "record_combined_result",
            "Record combined check",
            enabled=has_audible_test and not latest_test_validated,
            endpoint="./active-speaker/summed-validation",
            body={
                "speaker_group_id": group_id,
                "summed_test_id": latest_test_id,
                "operator_listening_check": True,
            },
        ),
    }
    return {
        "group_id": group_id,
        "label": label,
        "mode": target.get("mode"),
        "roles": list(target.get("roles") or []),
        "status": status,
        "status_label": status_label,
        "message": message,
        "failure_message": failure_message,
        "latest_test_id": latest_test_id,
        "has_audible_test": has_audible_test,
        "validated": validated,
        "test_level": test_level,
        "actions": actions,
    }


def _first_enabled_action(groups: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for group in groups:
        actions = group.get("actions")
        if not isinstance(actions, Mapping):
            continue
        for action_id in ("record_combined_result", "start_combined_test"):
            action = actions.get(action_id)
            if isinstance(action, Mapping) and action.get("enabled") is True:
                return {
                    **dict(action),
                    "speaker_group_id": group.get("group_id"),
                    "group_label": group.get("label"),
                }
    return None


def build_commissioning_view(
    topology: OutputTopology,
    *,
    measurements: Mapping[str, Any] | None = None,
    commission: Mapping[str, Any] | None = None,
    startup_load: Mapping[str, Any] | None = None,
    baseline_profile: Mapping[str, Any] | None = None,
    calibration_level: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose active-speaker setup state into one UI-facing view model."""

    measurements = measurements if isinstance(measurements, Mapping) else {}
    summary = (
        measurements.get("summary")
        if isinstance(measurements.get("summary"), Mapping)
        else {}
    )
    identity = channel_identity_report(topology)
    assigned_count = int(identity.get("assigned_channel_count") or 0)
    unverified_count = int(identity.get("unverified_channel_count") or 0)
    output_identity_complete = assigned_count > 0 and unverified_count == 0
    driver_checks_complete = bool(
        summary.get("driver_checks_complete")
        or summary.get("driver_measurements_complete")
    )
    summed_complete = bool(summary.get("summed_validation_complete"))
    profile_status = str((baseline_profile or {}).get("status") or "")
    profile_applied = profile_status == "applied"
    active_targets = active_summed_targets(topology)
    has_layout = bool(topology.speaker_groups)
    steps = [
        _step(
            "layout",
            "Choose speaker layout",
            "done" if has_layout else "active",
            "Speaker layout is saved." if has_layout else "Choose what is wired.",
        ),
        _step(
            "outputs",
            "Confirm outputs",
            "done" if output_identity_complete else ("active" if has_layout else "todo"),
            (
                "All assigned outputs are confirmed."
                if output_identity_complete
                else "Confirm each DAC output before any driver test can count."
            ),
        ),
        _step(
            "drivers",
            "Test each driver",
            "done" if driver_checks_complete else (
                "active" if output_identity_complete else "todo"
            ),
            (
                "Driver checks are saved."
                if driver_checks_complete
                else "Start with one quiet driver test at a time."
            ),
        ),
        _step(
            "combined",
            "Check the crossover blend",
            "done" if summed_complete else ("active" if driver_checks_complete else "todo"),
            (
                "Combined crossover check is saved."
                if summed_complete
                else "Run one quiet combined test and record what you heard."
            ),
        ),
        _step(
            "profile",
            "Save and apply",
            "done" if profile_applied else ("active" if summed_complete else "todo"),
            (
                "This is now the active speaker profile."
                if profile_applied
                else "Save the active speaker profile after the combined check."
            ),
        ),
    ]
    combined_groups = [
        _combined_group_view(
            target,
            summary=summary,
            calibration_level=calibration_level,
        )
        for target in active_targets
    ]
    next_action = None if summed_complete else _first_enabled_action(combined_groups)
    if next_action is None and not output_identity_complete:
        next_action = _action(
            "confirm_outputs",
            "Confirm outputs",
            enabled=has_layout,
            method="GET",
            message="Confirm each assigned output before testing drivers.",
        )
    elif next_action is None and not driver_checks_complete:
        next_action = _action(
            "start_driver_test",
            "Start driver test",
            enabled=output_identity_complete,
            message="Choose the first unchecked driver.",
        )
    elif next_action is None and summed_complete and not profile_applied:
        next_action = _action(
            "save_profile",
            "Save active profile",
            enabled=True,
            endpoint="./active-speaker/baseline-profile",
        )

    status = (
        "applied" if profile_applied else
        "ready_to_save_profile" if summed_complete else
        "needs_combined_check" if driver_checks_complete else
        "needs_driver_checks" if output_identity_complete else
        "needs_output_confirmation" if has_layout else
        "needs_layout"
    )
    return {
        "artifact_schema_version": 1,
        "kind": COORDINATOR_KIND,
        "status": status,
        "steps": steps,
        "combined_groups": combined_groups,
        "next_action": dict(next_action or {}),
        "output_identity": {
            "assigned_channel_count": assigned_count,
            "unverified_channel_count": unverified_count,
            "complete": output_identity_complete,
        },
        "driver_checks": {
            "complete": driver_checks_complete,
            "captured": int(
                summary.get("captured_driver_check_count")
                or summary.get("captured_driver_count")
                or 0
            ),
            "required": int(
                summary.get("required_driver_check_count")
                or summary.get("required_driver_count")
                or 0
            ),
        },
        "summed_validation": {
            "complete": summed_complete,
            "validated": int(summary.get("validated_summed_group_count") or 0),
            "required": int(summary.get("required_summed_group_count") or 0),
        },
        "test_level": (
            dict(combined_groups[0]["test_level"])
            if combined_groups else _combined_test_level(calibration_level)
        ),
        "runtime": {
            "commission": dict(commission or {}),
            "startup_load": dict(startup_load or {}),
        },
    }
