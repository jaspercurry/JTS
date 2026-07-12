# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.active_speaker import topology_tone
from jasper.active_speaker.playback_route import active_playback_route_capability
from jasper.active_speaker.tone_plan import MAX_TONE_DURATION_MS
from jasper.active_speaker.topology_tone import build_summed_topology_tone_plan
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
            "physical_output_count": HIFIBERRY_PHYSICAL_OUTPUTS,
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


def _issue_codes(plan: dict) -> set[str]:
    return {str(issue["code"]) for issue in plan["issues"]}


def test_active_playback_route_capability_resolves_dac8x_active_lane() -> None:
    topology = _topology()

    capability = active_playback_route_capability(topology)

    assert capability.playback_device == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert capability.playback_device_source == "outputd_active_lane"
    assert capability.transport_channel_count == HIFIBERRY_PHYSICAL_OUTPUTS
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
                "physical_output_index": HIFIBERRY_PHYSICAL_OUTPUTS - 1,
                "identity_verified": True,
            },
        ],
    })
    raw["routing"]["subwoofer_group_ids"] = ["sub"]

    capability = active_playback_route_capability(
        OutputTopology.from_mapping(raw)
    )

    assert capability.required_active_output_count == HIFIBERRY_PHYSICAL_OUTPUTS
    assert capability.subwoofer_group_count == 1
    assert capability.subwoofer_supported is True
    assert capability.fits_required_outputs is True
    assert capability.ready is True


def test_active_playback_route_capability_uses_actual_outputd_active_lane() -> None:
    raw = _topology().to_dict()
    raw["hardware"] = {
        "device_id": "dual_apple_usb_c_dac_4ch",
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
    }

    capability = active_playback_route_capability(
        OutputTopology.from_mapping(raw)
    )

    assert capability.transport_channel_count == DUAL_APPLE_ACTIVE_ROUTE_CHANNELS
    assert capability.ready is True


def test_active_playback_route_accepts_four_lane_layout() -> None:
    raw = _topology().to_dict()
    raw["hardware"] = {
        "device_id": "dual_apple_usb_c_dac_4ch",
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
    }
    right = dict(raw["speaker_groups"][0])
    right["id"] = "right"
    right["label"] = "Right speaker"
    right["kind"] = "right"
    right["channels"] = [
        dict(right["channels"][0], physical_output_index=2),
        dict(right["channels"][1], physical_output_index=3),
    ]
    raw["speaker_groups"].append(right)
    raw["routing"]["main_right_group_id"] = "right"

    capability = active_playback_route_capability(
        OutputTopology.from_mapping(raw)
    )

    assert capability.transport_channel_count == DUAL_APPLE_ACTIVE_ROUTE_CHANNELS
    assert capability.required_active_output_count == 4
    assert capability.fits_required_outputs is True
    assert capability.ready is True


def test_summed_topology_tone_plan_targets_every_assigned_driver() -> None:
    plan = build_summed_topology_tone_plan(
        _topology(),
        speaker_group_id="left",
        playback_allowed=True,
        safe_session_id="safe-1",
        protected_startup_loaded=True,
    )

    assert plan["status"] == "ready"
    assert plan["would_play"] is True
    assert plan["playback_allowed"] is True
    assert [target["driver_role"] for target in plan["targets"]] == [
        "woofer",
        "tweeter",
    ]
    assert [target["output_index"] for target in plan["targets"]] == [0, 1]
    assert plan["safety"]["safe_session_id"] == "safe-1"
    assert plan["safety"]["protected_startup_loaded"] is True


def test_summed_topology_tone_plan_blocks_missing_or_nonactive_group() -> None:
    missing = build_summed_topology_tone_plan(
        _topology(),
        speaker_group_id="missing",
        playback_allowed=True,
    )
    assert missing["would_play"] is False
    assert "summed_target_not_found" in _issue_codes(missing)

    raw = _topology().to_dict()
    raw["speaker_groups"][0]["mode"] = "full_range_passive"
    raw["speaker_groups"][0]["channels"] = [
        {
            "role": "full_range",
            "physical_output_index": 0,
            "identity_verified": True,
        }
    ]
    passive = build_summed_topology_tone_plan(
        OutputTopology.from_mapping(raw),
        speaker_group_id="left",
        playback_allowed=True,
    )
    assert passive["would_play"] is False
    assert "summed_target_not_active" in _issue_codes(passive)


def test_summed_topology_tone_plan_blocks_missing_or_unverified_output() -> None:
    missing_raw = _topology().to_dict()
    missing_raw["speaker_groups"][0]["channels"][0][
        "physical_output_index"
    ] = None
    missing = build_summed_topology_tone_plan(
        OutputTopology.from_mapping(missing_raw),
        speaker_group_id="left",
        playback_allowed=True,
    )
    assert missing["would_play"] is False
    assert "summed_target_output_missing" in _issue_codes(missing)

    unverified_raw = _topology().to_dict()
    unverified_raw["speaker_groups"][0]["channels"][1][
        "identity_verified"
    ] = False
    unverified = build_summed_topology_tone_plan(
        OutputTopology.from_mapping(unverified_raw),
        speaker_group_id="left",
        playback_allowed=True,
    )
    assert unverified["would_play"] is False
    assert "summed_target_identity_unverified" in _issue_codes(unverified)


def test_summed_topology_tone_plan_blocks_output_outside_active_lane(
    monkeypatch,
) -> None:
    monkeypatch.setattr(topology_tone, "_tone_output_count", lambda _topology: 1)

    plan = build_summed_topology_tone_plan(
        _topology(),
        speaker_group_id="left",
        playback_allowed=True,
    )

    assert plan["would_play"] is False
    assert "summed_target_output_outside_active_playback_lane" in _issue_codes(plan)


def test_summed_topology_tone_plan_bounds_requested_tone() -> None:
    plan = build_summed_topology_tone_plan(
        _topology(),
        speaker_group_id="left",
        requested_frequency_hz=float("nan"),
        requested_level_dbfs=99,
        requested_duration_ms=9999,
        playback_allowed=False,
    )

    assert plan["status"] == "ready"
    assert plan["would_play"] is False
    assert plan["playback_allowed"] is False
    assert plan["tone"]["frequency_hz"] == 1000.0
    assert plan["tone"]["level_dbfs"] == 0.0
    assert plan["tone"]["duration_ms"] == MAX_TONE_DURATION_MS
