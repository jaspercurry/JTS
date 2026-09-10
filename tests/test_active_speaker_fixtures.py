# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the shared active-speaker topology test builder."""

from __future__ import annotations

import pytest

from tests.active_speaker_fixtures import mono_output_topology


def test_mono_output_topology_pins_guarded_two_way_defaults() -> None:
    topology = mono_output_topology()
    raw = topology.to_dict()

    assert raw["topology_id"] == "bench_mono"
    assert raw["name"] == "Bench mono cabinet"
    assert raw["hardware"]["device_id"] == "hifiberry_dac8x"
    assert raw["hardware"]["card_id"] == "DAC8"
    assert raw["routing"]["mono_group_id"] == "mono"
    group = raw["speaker_groups"][0]
    assert group["label"] == "Mono cabinet"
    assert group["mode"] == "active_2_way"
    assert [channel["role"] for channel in group["channels"]] == [
        "woofer",
        "tweeter",
    ]
    tweeter = group["channels"][1]
    assert tweeter["startup_muted"] is True
    assert tweeter["protection_required"] is True
    assert tweeter["protection_status"] == "software_guard_requested"


def test_mono_output_topology_preserves_identity_and_hardware_variants() -> None:
    unverified_channels = (
        mono_output_topology(identity_verified=False).speaker_groups[0].channels
    )
    assert [channel.identity_verified for channel in unverified_channels] == [
        False,
        False,
    ]

    topology = mono_output_topology(
        identity_verified=False,
        tweeter_verified=True,
        tweeter_output=2,
        protection_status="required_missing",
        topology_name="Bench mono",
        group_label="Mono speaker",
        device_id="unregistered_lab_dac",
        device_label="Unregistered lab DAC",
        physical_output_count=4,
        card_id=None,
    )
    raw = topology.to_dict()
    woofer, tweeter = raw["speaker_groups"][0]["channels"]

    assert raw["name"] == "Bench mono"
    assert raw["speaker_groups"][0]["label"] == "Mono speaker"
    assert raw["hardware"]["device_id"] == "unregistered_lab_dac"
    assert raw["hardware"]["physical_output_count"] == 4
    assert "card_id" not in raw["hardware"]
    assert raw["hardware"]["clock_domain_id"] == "device:unregistered_lab_dac"
    assert woofer["identity_verified"] is False
    assert tweeter["identity_verified"] is True
    assert tweeter["physical_output_index"] == 2
    assert tweeter["protection_status"] == "required_missing"


@pytest.mark.parametrize(
    ("mode", "roles", "indexes"),
    (
        ("active_3_way", ["woofer", "mid", "tweeter"], [0, 1, 2]),
        ("full_range_passive", ["full_range"], [0]),
    ),
)
def test_mono_output_topology_preserves_crossover_shape_variants(
    mode: str,
    roles: list[str],
    indexes: list[int],
) -> None:
    topology = mono_output_topology(mode=mode)
    channels = topology.to_dict()["speaker_groups"][0]["channels"]

    assert [channel["role"] for channel in channels] == roles
    assert [channel["physical_output_index"] for channel in channels] == indexes


def test_mono_output_topology_preserves_optional_subwoofer_shape() -> None:
    topology = mono_output_topology(with_subwoofer=True, card_id=None)
    raw = topology.to_dict()
    sub = raw["speaker_groups"][1]

    assert sub["id"] == "sub"
    assert sub["mode"] == "subwoofer"
    assert sub["channels"] == [
        {
            "role": "subwoofer",
            "physical_output_index": 2,
            "identity_verified": True,
            "startup_muted": True,
            "protection_required": False,
            "protection_status": "not_required",
            "human_output_label": "DAC output 3",
        }
    ]
    assert raw["routing"]["subwoofer_group_ids"] == ["sub"]


def test_mono_output_topology_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unsupported mono output topology mode"):
        mono_output_topology(mode="not_a_mode")
