from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    hardware_from_env,
    load_output_topology,
    new_topology_draft,
    save_output_topology,
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
    unknown = hardware_from_env({})

    assert dac8x.physical_output_count == 8
    assert dac8x.outputs[4].human_label == "DAC output 5"
    assert dac8x.route == "stereo:5,6"
    assert apple.physical_output_count == 2
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
