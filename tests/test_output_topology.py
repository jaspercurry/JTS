from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.output_hardware import (
    OutputCardFact,
    classify_output_cards,
    write_state as write_output_hardware_state,
)
from jasper.output_topology import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    CHANNEL_IDENTITY_REPORT_KIND,
    CLOCK_DOMAIN_REPORT_KIND,
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    OutputTopologyError,
    channel_identity_report,
    clock_domain_report,
    hardware_from_env,
    load_output_topology,
    load_output_topology_strict,
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


def _dual_apple_hardware(*, include_evidence: bool = True) -> dict:
    hardware = {
        "device_id": DUAL_APPLE_ACTIVE_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left_dac",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FHL2FN3AC",
                "card_id": "A",
                "usb_path": "usb1/1-2",
                "controller": "xhci-hcd.0",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right_dac",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FLL2FN3A3",
                "card_id": "A_1",
                "usb_path": "usb1/1-1",
                "controller": "xhci-hcd.0",
                "physical_output_indexes": [2, 3],
            },
        ],
    }
    if include_evidence:
        hardware["clock_domain_evidence"] = {
            "evidence_kind": "dual_apple_usb_c_dac_drift_measurement",
            "measurement_id": "scarlett-ticks-900s-repeat-buffered",
            "status": "passed",
            "duration_seconds": 900,
            "sample_rate_hz": 48000,
            "offset_frames": -7,
            "max_offset_delta_frames": 0,
            "drift_ppm": 0,
            "xrun_count": 0,
            "dac_serials": [
                "DWH53530FHL2FN3AC",
                "DWH53530FLL2FN3A3",
            ],
            "artifact_path": (
                "/home/pi/jts/logs/"
                "dual-apple-dac-lab-20260603T120839-0400"
            ),
        }
    return hardware


def _write_dual_apple_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    same_bus: bool = True,
) -> None:
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    write_output_hardware_state(
        classify_output_cards([
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FHL2FN3AC",
                usb_path="usb1/1-2",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
            OutputCardFact(
                card_id="A_1",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FLL2FN3A3",
                usb_path="usb1/1-1" if same_bus else "usb3/3-1",
                busnum="1" if same_bus else "3",
                controller="xhci-hcd.0" if same_bus else "xhci-hcd.1",
                endpoint_sync="SYNC",
            ),
        ]),
        path=tmp_path / "output_hardware.json",
    )


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
    })
    apple = hardware_from_env({
        "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
        "JASPER_AUDIO_DAC_CARD": "A",
    })
    dual_apple = hardware_from_env({
        "JASPER_AUDIO_DAC_ID": DUAL_APPLE_ACTIVE_DEVICE_ID,
    })
    dac8x_studio = hardware_from_env({
        "JASPER_AUDIO_DAC_ID": HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
        "JASPER_AUDIO_DAC_CARD": "DAC8XStudio",
    })
    unknown = hardware_from_env({})

    assert dac8x.physical_output_count == 8
    assert dac8x.outputs[4].human_label == "DAC output 5"
    assert dac8x.clock_domain_id == "alsa:sndrpihifiberry"
    assert apple.physical_output_count == 2
    assert apple.clock_domain_label == "Single Apple USB audio device clock"
    assert dual_apple.physical_output_count == 4
    assert dual_apple.clock_domain_id == "profile:dual-apple-usb-c-dac-4ch"
    assert dual_apple.device_label == "Dual Apple USB-C DAC 4-channel pair"
    assert dac8x_studio.physical_output_count == 8
    assert dac8x_studio.device_label == "HiFiBerry DAC8x Studio"
    assert dac8x_studio.clock_domain_id == "alsa:DAC8XStudio"
    assert unknown.physical_output_count == 0
    assert unknown.outputs == ()


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


def test_software_guard_request_is_warning_not_topology_blocker() -> None:
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

    assert evaluation["status"] == "valid"
    assert "tweeter_software_guard_requested" in {
        issue["code"] for issue in evaluation["warnings"]
    }
    assert "tweeter_software_guard_requested" not in tweeter["sound_test_blockers"]
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
    assert report["multi_device_aggregate_supported"] is False
    assert report["sound_tests_allowed"] is False
    assert "one coherent multi-output DAC" in report["recommendation"]


def test_clock_domain_report_accepts_measured_dual_apple_composite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_dual_apple_observation(monkeypatch, tmp_path)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology)
    hardware_payload = topology.to_dict()["hardware"]

    assert report["kind"] == CLOCK_DOMAIN_REPORT_KIND
    assert report["status"] == "dual_apple_composite_clock"
    assert report["clock_domain_count"] == 2
    assert report["coherent_physical_output_count"] == 4
    assert report["multi_device_aggregate_supported"] is False
    assert report["measured_composite_supported"] is True
    assert report["issues"] == []
    assert report["evidence"]["measurement_id"] == (
        "scarlett-ticks-900s-repeat-buffered"
    )
    assert hardware_payload["child_devices"][0]["serial"] == "DWH53530FHL2FN3AC"
    assert hardware_payload["clock_domain_evidence"]["drift_ppm"] == 0


def test_clock_domain_report_warns_on_missing_dual_apple_clock_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_dual_apple_observation(monkeypatch, tmp_path)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": _dual_apple_hardware(include_evidence=False),
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology)

    assert report["status"] == "dual_apple_composite_clock"
    assert report["multi_device_aggregate_supported"] is False
    assert report["composite_clock_supported"] is True
    assert report["measured_composite_supported"] is False
    assert report["coherent_physical_output_count"] == 4
    assert "clock_evidence_missing" in {
        issue["code"] for issue in report["issues"]
    }


def test_clock_domain_report_blocks_wrong_observed_dual_apple_usb_bus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_dual_apple_observation(monkeypatch, tmp_path, same_bus=False)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology)

    assert report["status"] == "dual_apple_composite_clock_blocked"
    assert report["composite_clock_supported"] is False
    assert "dual_apple_usb_topology_mismatch" in {
        issue["code"] for issue in report["issues"]
    }


def test_dual_apple_hardware_requires_exact_four_physical_outputs() -> None:
    hardware = _dual_apple_hardware()
    hardware["physical_output_count"] = 5
    hardware["outputs"] = [
        {"index": index, "human_label": f"Output {index + 1}"}
        for index in range(5)
    ]

    with pytest.raises(ValueError, match="exactly 4 physical outputs"):
        OutputTopology.from_mapping({
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "dual_apple",
            "name": "Dual Apple active pair",
            "hardware": hardware,
            "speaker_groups": [],
            "routing": {},
        })


@pytest.mark.parametrize(
    ("dac_serials", "expected_code"),
    [
        (None, "clock_evidence_serials_required"),
        (["DWH53530FHL2FN3AC"], "clock_evidence_serials_required"),
        (
            ["DWH53530FHL2FN3AC", "DWH53530FHL2FN3AC"],
            "clock_evidence_serials_not_unique",
        ),
        (
            ["DWH53530FHL2FN3AC", "DWH53530F00000000"],
            "clock_evidence_serial_mismatch",
        ),
    ],
)
def test_clock_domain_report_requires_exact_dual_apple_evidence_serials(
    dac_serials: list[str] | None,
    expected_code: str,
) -> None:
    hardware = _dual_apple_hardware()
    evidence = hardware["clock_domain_evidence"]
    if dac_serials is None:
        evidence.pop("dac_serials")
    else:
        evidence["dac_serials"] = dac_serials
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": hardware,
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology)

    assert report["status"] == "dual_apple_composite_clock_blocked"
    assert report["measured_composite_supported"] is False
    assert report["coherent_physical_output_count"] == 0
    assert expected_code in {issue["code"] for issue in report["issues"]}


def test_clock_domain_report_requires_unique_pinned_dual_apple_child_serials() -> None:
    hardware = _dual_apple_hardware()
    hardware["child_devices"][1]["serial"] = hardware["child_devices"][0]["serial"]
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": hardware,
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology)

    assert report["status"] == "dual_apple_composite_clock_blocked"
    assert "dual_apple_child_serials_not_unique" in {
        issue["code"] for issue in report["issues"]
    }


def test_clock_domain_report_flags_unknown_output_clocking() -> None:
    topology = new_topology_draft(
        hardware=hardware_from_env({
            "JASPER_AUDIO_DAC_ID": "mystery_usb_audio",
            "JASPER_AUDIO_DAC_CARD": "Mystery",
        })
    )

    report = clock_domain_report(topology)

    assert report["status"] == "unknown_device_clock"
    assert report["coherent_physical_output_count"] == 0
    assert report["issues"][0]["code"] == "unknown_clock_domain"


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


def test_load_output_topology_strict_rejects_corrupt_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(OutputTopologyError, match="not valid JSON"):
        load_output_topology_strict(path)


def test_load_output_topology_strict_allows_missing_as_unconfigured(
    tmp_path: Path,
) -> None:
    loaded = load_output_topology_strict(tmp_path / "missing.json")

    assert loaded.status == "draft"
    assert loaded.speaker_groups == ()
