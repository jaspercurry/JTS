from __future__ import annotations

from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.playback import tone_backend_status
from jasper.active_speaker.readiness import build_playback_readiness
from jasper.active_speaker.topology_tone import build_topology_tone_plan
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
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
                        "protection_status": "present",
                    },
                ],
            }
        ],
        "routing": {"main_left_group_id": "left"},
    })


def _environment() -> dict:
    return {
        "status": "pass",
        "ok_to_load_active_config": True,
        "load_gate": "ready",
        "camilla_config": {
            "path": "/tmp/active.yml",
            "classification": "active_startup_candidate",
        },
        "issues": [],
    }


def _startup_load() -> dict:
    return {
        "status": "loaded",
        "loaded": True,
        "candidate_config_path": "/tmp/active.yml",
        "active_config_path": "/tmp/active.yml",
        "previous_config_path": "/tmp/prior.yml",
        "rollback_available": True,
        "current_config_matches_loaded": True,
    }


def _session() -> dict:
    return {
        "status": "armed",
        "session_id": "safe-1",
        "expires_at": "2026-06-02T12:02:00Z",
        "issues": [],
    }


def test_topology_tone_plan_uses_saved_physical_output_map() -> None:
    topology = _topology()
    readiness = build_playback_readiness(
        topology,
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_session(),
        calibration_level=calibration_level_payload(requested_level_dbfs=-60),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({
            "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
            "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
            "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
        }),
    )

    plan = build_topology_tone_plan(
        topology,
        readiness_report=readiness,
        speaker_group_id="left",
        role="woofer",
        requested_level_dbfs=-60,
        requested_duration_ms=150,
    )

    assert plan["source"] == "output_topology"
    assert plan["status"] == "ready"
    assert plan["would_play"] is True
    assert plan["playback_allowed"] is True
    assert plan["channel_map"] == {"layout": "output_topology", "output_count": 8}
    assert plan["target"]["speaker_group_id"] == "left"
    assert plan["target"]["driver_role"] == "woofer"
    assert plan["target"]["output_index"] == 0
    assert plan["tone"]["frequency_hz"] == 120.0
    assert plan["tone"]["level_dbfs"] == -60.0
    assert plan["safety"]["safe_session_id"] == "safe-1"
    assert plan["safety"]["audible_test"]["target_role_allowed"] is True
    assert plan["safety"]["audible_test"]["policy_version"] == (
        "driver_protection_auto_level_v1"
    )
    assert plan["driver_protection"]["role_class"] == "low_frequency"


def test_topology_tone_plan_can_prepare_artifact_without_audio_authority() -> None:
    topology = _topology()
    readiness = build_playback_readiness(
        topology,
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({}),
    )

    plan = build_topology_tone_plan(
        topology,
        readiness_report=readiness,
        speaker_group_id="left",
        role="woofer",
    )

    assert readiness["preconditions_passed"] is True
    assert readiness["playback_allowed"] is False
    assert plan["status"] == "ready"
    assert plan["would_play"] is False
    assert plan["playback_allowed"] is False
    assert plan["next_step"] == (
        "Ready for artifact verification; audible playback is still gated."
    )


def test_topology_tone_plan_accepts_guarded_frontend_selection_without_readiness_report() -> None:
    topology = _topology()
    startup_load = _startup_load()
    startup_load.pop("current_config_matches_loaded")

    plan = build_topology_tone_plan(
        topology,
        speaker_group_id="left",
        role="woofer",
        safe_session=_session(),
        startup_load_state=startup_load,
        playback_allowed=True,
        tone_playback_implemented=True,
    )

    assert plan["source"] == "output_topology"
    assert plan["status"] == "ready"
    assert plan["would_play"] is True
    assert plan["playback_allowed"] is True
    assert plan["target"]["speaker_group_id"] == "left"
    assert plan["target"]["driver_role"] == "woofer"
    assert plan["target"]["output_index"] == 0
    assert plan["safety"]["safe_session_id"] == "safe-1"
    assert plan["safety"]["readiness_status"] == "preconditions_passed"
    assert plan["safety"]["protected_startup_loaded"] is True


def test_topology_tone_plan_blocks_guarded_frontend_selection_without_loaded_startup() -> None:
    topology = _topology()

    plan = build_topology_tone_plan(
        topology,
        speaker_group_id="left",
        role="woofer",
        safe_session=_session(),
        startup_load_state={"status": "not_loaded"},
        playback_allowed=True,
        tone_playback_implemented=True,
    )

    assert plan["status"] == "blocked"
    assert plan["would_play"] is False
    assert "protected_startup_config_not_loaded" in {
        issue["code"] for issue in plan["issues"]
    }


def test_topology_tone_plan_uses_conservative_high_frequency_floor_tone() -> None:
    topology = _topology()
    readiness = build_playback_readiness(
        topology,
        speaker_group_id="left",
        role="tweeter",
        environment_report=_environment(),
        safe_session=_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({
            "JASPER_ACTIVE_SPEAKER_TONE_BACKEND": "aplay",
            "JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO": "1",
            "JASPER_ACTIVE_SPEAKER_TEST_PCM": "hw:Active",
        }),
    )

    plan = build_topology_tone_plan(
        topology,
        readiness_report=readiness,
        speaker_group_id="left",
        role="tweeter",
    )

    assert plan["status"] == "ready"
    assert plan["playback_allowed"] is True
    assert plan["tone"]["frequency_hz"] == 5000.0
    assert plan["tone"]["band_limit"] == {
        "type": "highpass",
        "highpass_hz": 5000.0,
    }
    assert plan["driver_protection"]["role_class"] == "high_frequency"
    assert plan["driver_protection"]["audio_allowed"] is True


def test_topology_tone_plan_blocks_mismatched_readiness_report() -> None:
    topology = _topology()
    readiness = build_playback_readiness(
        topology,
        speaker_group_id="left",
        role="woofer",
        environment_report=_environment(),
        safe_session=_session(),
        calibration_level=calibration_level_payload(),
        startup_load_state=_startup_load(),
        tone_backend=tone_backend_status({}),
    )

    plan = build_topology_tone_plan(
        topology,
        readiness_report=readiness,
        speaker_group_id="left",
        role="tweeter",
    )

    assert plan["status"] == "blocked"
    assert "readiness_target_mismatch" in {
        issue["code"] for issue in plan["issues"]
    }
