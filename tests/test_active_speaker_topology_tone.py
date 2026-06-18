from __future__ import annotations

from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.playback_route import active_playback_route_capability
from jasper.active_speaker.playback import tone_backend_status
from jasper.active_speaker.readiness import build_playback_readiness
from jasper.active_speaker.topology_tone import build_topology_tone_plan
from jasper.audio_hardware import dac
from jasper.camilla_config_contract import ACTIVE_OUTPUTD_PLAYBACK_DEVICE
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


HIFIBERRY_PHYSICAL_OUTPUTS = 8
DUAL_APPLE_ACTIVE_ROUTE_CHANNELS = dac.active_outputd_lane_channels_for(
    dac.DUAL_APPLE_USB_C_DAC_4CH_ID
)


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
            "card_id": "DAC8",
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
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
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
    assert plan["channel_map"] == {
        "layout": "output_topology",
        "output_count": HIFIBERRY_PHYSICAL_OUTPUTS,
    }
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


def test_topology_tone_plan_can_target_high_physical_output_without_active_lane() -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = (
        HIFIBERRY_PHYSICAL_OUTPUTS - 1
    )
    topology = OutputTopology.from_mapping(raw)

    plan = build_topology_tone_plan(
        topology,
        speaker_group_id="left",
        role="tweeter",
        readiness_report=build_playback_readiness(
            topology,
            speaker_group_id="left",
            role="tweeter",
            environment_report=_environment(),
            safe_session=_session(),
            calibration_level=calibration_level_payload(),
            startup_load_state=_startup_load(),
            tone_backend=tone_backend_status({
                "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
                "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
            }),
        ),
    )

    assert plan["status"] == "ready"
    assert plan["channel_map"]["output_count"] == HIFIBERRY_PHYSICAL_OUTPUTS
    assert plan["target"]["output_index"] == HIFIBERRY_PHYSICAL_OUTPUTS - 1


def test_active_playback_route_capability_resolves_dac8x_active_lane() -> None:
    topology = _topology()

    capability = active_playback_route_capability(topology)

    assert topology.hardware.physical_output_count == 8
    # Stage 2: the DAC8x declares an active outputd lane, so the capability
    # reads that lane (not a direct-DAC route) at the full transport width.
    assert capability.playback_device == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert capability.playback_device_source == "outputd_active_lane"
    assert capability.transport_channel_count == 8
    assert capability.required_active_output_count == 2
    assert capability.fits_required_outputs is True
    assert capability.ready is True
    assert capability.issues == ()


def test_active_playback_route_capability_counts_subwoofer_output_lane() -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"].append({
        "id": "sub",
        "label": "Subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        "channels": [
            {
                "role": "subwoofer",
                "physical_output_index": 2,
                "identity_verified": True,
            },
        ],
    })
    raw["routing"]["subwoofer_group_ids"] = ["sub"]
    topology = OutputTopology.from_mapping(raw)

    capability = active_playback_route_capability(topology)

    assert capability.playback_device_source == "outputd_active_lane"
    assert capability.transport_channel_count == HIFIBERRY_PHYSICAL_OUTPUTS
    assert capability.required_active_output_count == 3
    assert capability.subwoofer_group_count == 1
    assert capability.subwoofer_supported is True
    assert capability.ready is True


def test_active_playback_route_capability_counts_highest_assigned_subwoofer_lane() -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"].append({
        "id": "sub",
        "label": "Subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        "channels": [
            {
                "role": "subwoofer",
                "physical_output_index": HIFIBERRY_PHYSICAL_OUTPUTS - 1,
                "identity_verified": True,
            },
        ],
    })
    raw["routing"]["subwoofer_group_ids"] = ["sub"]
    topology = OutputTopology.from_mapping(raw)

    capability = active_playback_route_capability(topology)

    assert capability.required_active_output_count == HIFIBERRY_PHYSICAL_OUTPUTS
    assert capability.fits_required_outputs is True
    assert capability.ready is True


def test_active_playback_route_capability_uses_actual_outputd_active_lane() -> None:
    raw = _topology().to_dict()
    raw["hardware"] = {
        "device_id": "dual_apple_usb_c_dac_4ch",
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
    }
    topology = OutputTopology.from_mapping(raw)

    capability = active_playback_route_capability(topology)

    assert capability.transport_channel_count == DUAL_APPLE_ACTIVE_ROUTE_CHANNELS
    assert capability.ready is True


def test_active_playback_route_capability_accepts_four_lane_route_when_layout_fits() -> None:
    raw = _topology().to_dict()
    raw["hardware"] = {
        "device_id": "dual_apple_usb_c_dac_4ch",
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
    }
    raw["speaker_groups"] = [
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
        },
        {
            "id": "right",
            "label": "Right speaker",
            "kind": "right",
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": 2,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "physical_output_index": 3,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "present",
                },
            ],
        },
    ]
    raw["routing"] = {"main_left_group_id": "left", "main_right_group_id": "right"}
    topology = OutputTopology.from_mapping(raw)

    capability = active_playback_route_capability(topology)

    assert topology.hardware.physical_output_count == 4
    assert capability.transport_channel_count == DUAL_APPLE_ACTIVE_ROUTE_CHANNELS
    assert capability.required_active_output_count == 4
    assert capability.fits_required_outputs is True
    assert capability.ready is True


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


def test_topology_tone_plan_can_prepare_artifact_without_safe_session() -> None:
    plan = build_topology_tone_plan(
        _topology(),
        speaker_group_id="left",
        role="woofer",
        safe_session={"status": "idle"},
        startup_load_state={"status": "idle"},
        playback_allowed=False,
        tone_playback_implemented=False,
    )

    assert plan["status"] == "ready"
    assert plan["would_play"] is False
    assert plan["playback_allowed"] is False
    assert "safe_session_not_armed" not in {
        issue["code"] for issue in plan["issues"]
    }
    assert "protected_startup_config_not_loaded" not in {
        issue["code"] for issue in plan["issues"]
    }


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
            "JASPER_AUDIO_LAB_TONE_BACKEND": "aplay",
            "JASPER_AUDIO_LAB_TEST_PCM": "hw:Active",
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
