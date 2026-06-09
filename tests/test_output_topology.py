from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.audio_hardware.dac import DUAL_APPLE_USB_C_DAC_4CH_ID
from jasper.output_topology import (
    CHANNEL_IDENTITY_REPORT_KIND,
    CLOCK_DOMAIN_REPORT_KIND,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    channel_identity_report,
    clock_domain_report,
    hardware_from_output_hardware_state,
    hardware_from_env,
    load_output_topology,
    new_topology_draft,
    save_output_topology,
    set_channel_identity_verified,
    set_channel_protection_status,
)


def _base_hardware() -> dict:
    return {
        "device_id": "hifiberry_dac8x",
        "device_label": "HiFiBerry DAC8x",
        "physical_output_count": 8,
    }


def _topology(*, groups: list[dict], routing: dict | None = None) -> OutputTopology:
    raw = {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": _base_hardware(),
        "speaker_groups": groups,
        "routing": routing or {},
    }
    return OutputTopology.from_mapping(raw)


def test_hardware_from_env_reports_known_output_counts() -> None:
    dac8x = hardware_from_env({
        "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
        "JASPER_AUDIO_DAC_CARD": "sndrpihifiberry",
        "JASPER_OUTPUT_DAC_ROUTE": "stereo:5,6",
    })
    apple = hardware_from_env({
        "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
        "JASPER_AUDIO_DAC_CARD": "A",
    })
    dual_apple = hardware_from_env({
        "JASPER_AUDIO_DAC_ID": DUAL_APPLE_USB_C_DAC_4CH_ID,
    })
    unknown = hardware_from_env({})

    assert dac8x.physical_output_count == 8
    assert dac8x.device_label == "HiFiBerry DAC8x / Studio DAC8x"
    assert dac8x.outputs[4].human_label == "DAC output 5"
    assert dac8x.route == "stereo:5,6"
    assert dac8x.clock_domain_id == "alsa:sndrpihifiberry"
    assert apple.physical_output_count == 2
    assert apple.clock_domain_label == "Single Apple USB audio device clock"
    assert dual_apple.physical_output_count == 4
    assert dual_apple.device_label == "Dual Apple USB-C audio adapters"
    assert dual_apple.clock_domain_label == (
        "Dual Apple USB-C adapter independent clocks"
    )
    assert unknown.physical_output_count == 0
    assert unknown.outputs == ()


def test_hardware_from_output_hardware_state_uses_ready_observed_shape() -> None:
    hardware = hardware_from_output_hardware_state({
        "artifact_schema_version": 1,
        "kind": "jts_output_hardware_state",
        "observed": {
            "profile_id": DUAL_APPLE_USB_C_DAC_4CH_ID,
            "status": "ready",
            "physical_output_count": 4,
        },
        "child_devices": [],
    })

    assert hardware is not None
    assert hardware.device_id == DUAL_APPLE_USB_C_DAC_4CH_ID
    assert hardware.physical_output_count == 4
    assert hardware.outputs[0].terminal_label == "A-L"


def test_hardware_from_output_hardware_state_ignores_blocked_observed_shape() -> None:
    hardware = hardware_from_output_hardware_state({
        "artifact_schema_version": 1,
        "kind": "jts_output_hardware_state",
        "observed": {
            "profile_id": DUAL_APPLE_USB_C_DAC_4CH_ID,
            "status": "blocked",
            "physical_output_count": 4,
        },
    })

    assert hardware is None


def test_empty_topology_draft_is_honest_and_no_audio_allowed() -> None:
    topology = new_topology_draft(
        hardware=hardware_from_env({"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"})
    )
    payload = topology.to_dict(include_evaluation=True)

    assert payload["status"] == "draft"
    assert payload["hardware"]["physical_output_count"] == 8
    assert payload["safety"]["sound_tests_allowed"] is False
    assert payload["evaluation"]["warnings"][0]["code"] == "no_speaker_groups"


def test_passive_stereo_topology_can_be_valid_before_identity_verified() -> None:
    topology = _topology(
        groups=[
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "position": {"x": -0.5, "y": 1.0},
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "full_range_passive",
                "position": {"x": 0.5, "y": 1.0},
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
        ],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
    )
    evaluation = topology.evaluation()
    payload = topology.to_dict()

    assert evaluation["status"] == "valid"
    assert evaluation["assigned_output_count"] == 2
    assert evaluation["unused_output_count"] == 6
    assert {issue["code"] for issue in evaluation["warnings"]} == {
        "identity_unverified"
    }
    assert "Verify physical output identity" in evaluation["safety"]["next_step"]
    assert payload["speaker_groups"][0]["channels"][0]["human_output_label"] == (
        "DAC output 1"
    )
    assert payload["safety"]["sound_tests_allowed"] is False


def test_channel_identity_report_tracks_assigned_verification_progress() -> None:
    topology = _topology(
        groups=[
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{
                    "role": "full_range",
                    "physical_output_index": 1,
                    "identity_verified": True,
                }],
            },
        ],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
    )

    report = channel_identity_report(topology)

    assert report["kind"] == CHANNEL_IDENTITY_REPORT_KIND
    assert report["status"] == "needs_verification"
    assert report["assigned_channel_count"] == 2
    assert report["verified_channel_count"] == 1
    assert report["unverified_channel_count"] == 1
    assert report["sound_tests_allowed"] is False


def test_tweeter_protection_status_can_be_marked_present() -> None:
    topology = _topology(
        groups=[
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "required_missing",
                    },
                ],
            }
        ],
        routing={"mono_group_id": "mono"},
    )

    blocked = topology.evaluation()
    updated = set_channel_protection_status(
        topology,
        speaker_group_id="mono",
        role="tweeter",
        protection_status="present",
    )
    report = channel_identity_report(updated)

    assert "tweeter_protection_unverified" in {
        issue["code"] for issue in blocked["blockers"]
    }
    tweeter = updated.speaker_groups[0].channels[1]
    assert tweeter.protection_required is True
    assert tweeter.protection_status == "present"
    assert tweeter.startup_muted is True
    assert "tweeter_protection_unverified" not in {
        code
        for target in report["targets"]
        for code in target["sound_test_blockers"]
    }
    targets = {target["id"]: target for target in report["targets"]}
    assert targets["mono:woofer"]["sound_test_blockers"] == ["identity_unverified"]
    assert targets["mono:tweeter"]["sound_test_blockers"] == ["identity_unverified"]


def test_software_guard_request_remains_blocked_for_sound_tests() -> None:
    topology = _topology(
        groups=[
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        routing={"mono_group_id": "mono"},
    )

    evaluation = topology.evaluation()
    report = channel_identity_report(topology)
    tweeter = next(target for target in report["targets"] if target["role"] == "tweeter")

    assert evaluation["status"] == "blocked"
    assert "tweeter_software_guard_requested" in {
        issue["code"] for issue in evaluation["blockers"]
    }
    assert "tweeter_software_guard_requested" in tweeter["sound_test_blockers"]
    assert report["sound_tests_allowed"] is False


def test_clock_domain_report_records_single_device_boundary() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 0},
                {"role": "tweeter", "physical_output_index": 1},
            ],
        }
    ])

    report = clock_domain_report(topology)

    assert report["kind"] == CLOCK_DOMAIN_REPORT_KIND
    assert report["status"] == "single_device_clock"
    assert report["clock_domain_count"] == 1
    assert report["coherent_physical_output_count"] == 8
    assert report["profile_known"] is True
    assert report["profile_kind"] == "single"
    assert report["profile_is_composite_output"] is False
    assert report["aggregate_output_runtime_enabled"] is False
    assert report["multi_device_aggregate_supported"] is False
    assert report["sound_tests_allowed"] is False
    assert "one coherent multi-output DAC" in report["recommendation"]


def test_clock_domain_report_flags_unknown_output_clocking() -> None:
    topology = new_topology_draft(
        hardware=hardware_from_env({
            "JASPER_AUDIO_DAC_ID": "mystery_usb_audio",
            "JASPER_AUDIO_DAC_CARD": "Mystery",
        })
    )

    report = clock_domain_report(topology)

    assert report["status"] == "unknown_device_clock"
    assert report["profile_known"] is False
    assert report["profile_kind"] == "unknown"
    assert report["coherent_physical_output_count"] == 0
    assert report["issues"][0]["code"] == "unknown_clock_domain"


def test_clock_domain_report_flags_known_independent_composite_clocking() -> None:
    topology = new_topology_draft(
        hardware=hardware_from_env({
            "JASPER_AUDIO_DAC_ID": DUAL_APPLE_USB_C_DAC_4CH_ID,
        })
    )

    report = clock_domain_report(topology)

    assert report["status"] == "known_independent_clocks"
    assert report["clock_domain_count"] == 2
    assert report["coherent_physical_output_count"] == 0
    assert report["profile_known"] is True
    assert report["profile_kind"] == "composite"
    assert report["profile_is_composite_output"] is True
    assert report["aggregate_output_runtime_enabled"] is False
    assert report["multi_device_aggregate_supported"] is False
    assert report["issues"][0]["code"] == "independent_output_clocks"
    assert "runtime validation" in " ".join(report["notes"])


def test_set_channel_identity_verified_updates_one_channel_only() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 0},
                {
                    "role": "tweeter",
                    "physical_output_index": 1,
                    "protection_required": True,
                    "protection_status": "required_missing",
                },
            ],
        }
    ])

    updated = set_channel_identity_verified(
        topology,
        speaker_group_id="left",
        role="woofer",
        identity_verified=True,
    )
    group = updated.to_dict()["speaker_groups"][0]

    assert group["channels"][0]["identity_verified"] is True
    assert group["channels"][1]["identity_verified"] is False
    assert updated.evaluation()["status"] == "blocked"
    assert "tweeter_protection_unverified" in {
        issue["code"] for issue in updated.evaluation()["blockers"]
    }


def test_posted_human_output_label_is_rederived_from_hardware() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [
                {
                    "role": "full_range",
                    "physical_output_index": 0,
                    "human_output_label": "DAC output 8",
                }
            ],
        }
    ])

    channel = topology.to_dict()["speaker_groups"][0]["channels"][0]

    assert channel["physical_output_index"] == 0
    assert channel["human_output_label"] == "DAC output 1"


def test_active_two_way_topology_requires_tweeter_protection() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left active speaker",
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
                    "protection_status": "unknown",
                },
            ],
        }
    ])

    evaluation = topology.evaluation()

    assert evaluation["status"] == "blocked"
    assert evaluation["safety"]["requires_tweeter_protection"] is True
    assert "tweeter_protection_unverified" in {
        issue["code"] for issue in evaluation["blockers"]
    }


def test_active_two_way_topology_can_be_verified_when_all_guards_pass() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left active speaker",
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
    ])

    evaluation = topology.evaluation()

    assert evaluation["status"] == "verified"
    assert evaluation["blockers"] == []
    assert evaluation["warnings"] == []
    assert evaluation["safety"]["sound_tests_allowed"] is False
    assert "separate safe session" in evaluation["safety"]["next_step"]


def test_stereo_plus_subwoofer_topology_tracks_sub_routes() -> None:
    topology = _topology(
        groups=[
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [{"role": "subwoofer", "physical_output_index": 4}],
            },
        ],
        routing={
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    )

    payload = topology.to_dict(include_evaluation=True)

    assert payload["routing"]["subwoofer_group_ids"] == ["sub"]
    assert payload["evaluation"]["assigned_output_count"] == 3
    assert payload["evaluation"]["unused_output_count"] == 5


def test_duplicate_physical_output_is_blocked_not_silently_reused() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
        {
            "id": "right",
            "label": "Right speaker",
            "kind": "right",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
    ])

    evaluation = topology.evaluation()

    assert evaluation["status"] == "blocked"
    assert "duplicate_physical_output" in {
        issue["code"] for issue in evaluation["blockers"]
    }


def test_save_and_load_output_topology_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    save_output_topology(topology, path)
    loaded = load_output_topology(path)

    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == (
        OUTPUT_TOPOLOGY_KIND
    )
    assert loaded.topology_id == "living_room"
    assert loaded.to_dict()["speaker_groups"][0]["channels"][0][
        "human_output_label"
    ] == "DAC output 3"


def test_save_output_topology_cleans_temp_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    def fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("jasper.output_topology.os.replace", fail_replace)

    with pytest.raises(OSError):
        save_output_topology(topology, path)

    assert not path.exists()
    assert list(tmp_path.glob(".output_topology.json.*.tmp")) == []


def test_load_output_topology_fails_soft_to_detected_draft(tmp_path: Path) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    loaded = load_output_topology(path)

    assert loaded.status == "draft"
    assert loaded.speaker_groups == ()
