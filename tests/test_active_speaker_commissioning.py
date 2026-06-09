from __future__ import annotations

from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.commissioning import (
    COMMISSIONING_REHEARSAL_KIND,
    build_commissioning_rehearsal,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "valid",
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
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


def _gate(gate_id: str, passed: bool) -> dict:
    blocked_messages = {
        "physical_identity_verified": "Verify assigned DAC outputs before continuing",
    }
    return {
        "id": gate_id,
        "label": gate_id.replace("_", " "),
        "passed": passed,
        "message": "passed" if passed else blocked_messages.get(
            gate_id,
            f"{gate_id} blocked",
        ),
    }


def _bringup(*, identity: bool = True, staged: bool = True) -> dict:
    return {
        "status": "manual_ready" if identity and staged else "blocked",
        "software_guard": {"status": "software_guard_ready" if staged else "missing"},
        "modes": {
            "manual_guarded_bringup": {
                "required_gates": [
                    _gate("output_topology_present", True),
                    _gate("topology_has_no_unhandled_blockers", True),
                    _gate("physical_identity_verified", identity),
                    _gate("protected_startup_config_staged", staged),
                    _gate("compression_driver_guard_accepted", staged),
                ],
            }
        },
        "issues": [],
    }


def _startup(*, path: bool = True, loaded: bool = True) -> dict:
    return {
        "state": {
            "status": "loaded" if loaded else "idle",
            "loaded": loaded,
            "rollback_available": loaded,
            "current_config_matches_loaded": loaded,
        },
        "preflight": {
            "status": "ready" if path else "blocked",
            "path_safety": {"load_gate": "ready" if path else "missing"},
            "required_gates": [
                _gate("path_safety_ready", path),
                _gate("path_safety_matches_current_startup_load", path),
            ],
            "issues": [],
        },
    }


def test_commissioning_rehearsal_reports_ready_for_target_check() -> None:
    report = build_commissioning_rehearsal(
        _topology(),
        bringup_preflight=_bringup(),
        startup_load=_startup(),
        safe_session={
            "status": "armed",
            "session_id": "safe-1",
            "expires_at": "2026-06-09T12:00:00Z",
        },
        calibration_level=calibration_level_payload(),
    )

    assert report["kind"] == COMMISSIONING_REHEARSAL_KIND
    assert report["status"] == "ready_for_target_check"
    assert report["no_audio"] is True
    assert report["durable_steps_ready"] is True
    assert report["steps"][0]["id"] == "output_map_saved"
    assert report["steps"][7]["id"] == "target_readiness_checked"
    assert report["steps"][7]["status"] == "next"
    assert report["steps"][8]["status"] == "blocked"


def test_commissioning_rehearsal_blocks_before_verified_outputs() -> None:
    report = build_commissioning_rehearsal(
        _topology(),
        bringup_preflight=_bringup(identity=False),
        startup_load=_startup(path=False, loaded=False),
        safe_session={"status": "idle"},
        calibration_level=calibration_level_payload(),
    )
    steps = {step["id"]: step for step in report["steps"]}

    assert report["status"] == "blocked"
    assert report["durable_steps_ready"] is False
    assert steps["channel_identity_verified"]["status"] == "next"
    assert steps["protected_config_staged"]["status"] == "done"
    assert steps["protected_path_checked"]["status"] == "pending"
    assert "Verify" in steps["channel_identity_verified"]["message"]


def test_commissioning_rehearsal_requires_level_floor_before_target_check() -> None:
    report = build_commissioning_rehearsal(
        _topology(),
        bringup_preflight=_bringup(),
        startup_load=_startup(),
        safe_session={"status": "armed", "session_id": "safe-1"},
        calibration_level=calibration_level_payload(requested_level_dbfs=-70.0),
    )
    steps = {step["id"]: step for step in report["steps"]}

    assert report["status"] == "blocked"
    assert steps["level_floor_ready"]["status"] == "next"
    assert steps["target_readiness_checked"]["status"] == "blocked"
