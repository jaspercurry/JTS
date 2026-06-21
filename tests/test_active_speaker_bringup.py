# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.active_speaker.bringup import (
    BRINGUP_PREFLIGHT_KIND,
    build_bringup_preflight,
)
from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology(*, protection_status: str = "software_guard_requested") -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": protection_status,
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


def _environment() -> dict:
    return {
        "status": "pass",
        "load_gate": "ready",
        "ok_to_load_active_config": True,
        "issues": [],
    }


def _blocked_environment() -> dict:
    return {
        "status": "blocked",
        "load_gate": "camilla_config_missing",
        "ok_to_load_active_config": False,
        "issues": [
            {
                "severity": "blocker",
                "code": "camilla_config_missing",
                "message": "active-speaker startup config is not loaded",
            }
        ],
    }


def _safe_session(*, armed: bool = False) -> dict:
    return {
        "status": "armed" if armed else "idle",
        "session_id": "safe-1" if armed else None,
        "issues": [],
    }


def _staged_guard(*, staged: bool = True) -> dict:
    return {
        "status": "staged" if staged else "not_staged",
        "software_guard": {
            "passed": staged,
            "no_load": staged,
            "no_playback": staged,
            "checks": {
                "startup_muted": staged,
                "protective_highpass": staged,
                "startup_headroom": staged,
                "startup_limiter": staged,
                "tweeter_pipeline_guarded": staged,
            },
        },
    }


def test_bringup_preflight_allows_manual_software_guard_without_microphone() -> None:
    report = build_bringup_preflight(
        _topology(),
        environment_report=_environment(),
        safe_session=_safe_session(),
        staged_config=_staged_guard(),
        calibration_level=calibration_level_payload(),
    )

    assert report["kind"] == BRINGUP_PREFLIGHT_KIND
    assert report["status"] == "manual_ready"
    assert report["manual_bringup_available"] is True
    assert report["guided_calibration_available"] is False
    assert report["software_guard"]["status"] == "software_guard_ready"
    assert report["microphone"]["status"] == "not_checked"
    assert report["modes"]["manual_guarded_bringup"]["status"] == "ready_to_arm"
    assert "tweeter_software_guard_requested" not in {
        issue["code"] for issue in report["issues"]
    }
    assert "microphone_not_checked" in {issue["code"] for issue in report["issues"]}


def test_bringup_preflight_requires_staged_guard_for_software_only_path() -> None:
    report = build_bringup_preflight(
        _topology(),
        environment_report=_environment(),
        safe_session=_safe_session(),
        staged_config=_staged_guard(staged=False),
        calibration_level=calibration_level_payload(),
    )

    assert report["status"] == "blocked"
    assert report["manual_bringup_available"] is False
    assert report["software_guard"]["status"] == "software_guard_needs_staged_config"
    assert "high_frequency_guard_not_ready" in {
        issue["code"] for issue in report["issues"]
    }


def test_bringup_preflight_requires_environment_gate_before_manual_ready() -> None:
    report = build_bringup_preflight(
        _topology(),
        environment_report=_blocked_environment(),
        safe_session=_safe_session(),
        staged_config=_staged_guard(),
        calibration_level=calibration_level_payload(),
    )
    manual_gates = {
        gate["id"]: gate
        for gate in report["modes"]["manual_guarded_bringup"]["required_gates"]
    }

    assert report["status"] == "blocked"
    assert report["manual_bringup_available"] is False
    assert report["environment"]["ok_to_load_active_config"] is False
    assert manual_gates["active_environment_ready"]["passed"] is False
    assert "camilla_config_missing" in {issue["code"] for issue in report["issues"]}


def test_bringup_preflight_promotes_guided_mode_when_microphone_is_working() -> None:
    report = build_bringup_preflight(
        _topology(),
        environment_report=_environment(),
        safe_session=_safe_session(armed=True),
        staged_config=_staged_guard(),
        calibration_level=calibration_level_payload(),
        microphone_report={
            "capture_works": True,
            "calibrated": True,
            "meter_status": "usable",
        },
    )

    assert report["status"] == "guided_ready"
    assert report["manual_bringup_available"] is True
    assert report["guided_calibration_available"] is True
    assert report["microphone"]["status"] == "calibrated"
    assert report["modes"]["manual_guarded_bringup"]["status"] == "armed"
    assert report["modes"]["guided_calibration"]["status"] == "ready_calibrated"


def test_bringup_preflight_starts_horn_bringup_at_level_floor() -> None:
    report = build_bringup_preflight(
        _topology(),
        environment_report=_environment(),
        safe_session=_safe_session(),
        staged_config=_staged_guard(),
        calibration_level=calibration_level_payload(requested_level_dbfs=-70),
    )

    assert report["status"] == "blocked"
    assert report["manual_bringup_available"] is False
    assert report["calibration_level"]["at_floor"] is False
    assert "calibration_level_not_at_floor" in {
        issue["code"] for issue in report["issues"]
    }
