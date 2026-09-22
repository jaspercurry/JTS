# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared declared topology inputs for schema and persistence tests."""

from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _base_hardware() -> dict:
    return {
        "device_id": "hifiberry_dac8x",
        "device_label": "HiFiBerry DAC8x",
        "physical_output_count": 8,
    }


def _topology(
    *,
    groups: list[dict],
    routing: dict | None = None,
    hardware: dict | None = None,
) -> OutputTopology:
    raw = {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": hardware or _base_hardware(),
        "speaker_groups": groups,
        "routing": routing or {},
    }
    return OutputTopology.from_mapping(raw)


def _passive_main(
    group_id: str, kind: str, index: int, *, identity_verified: bool = False
) -> dict:
    return {
        "id": group_id,
        "label": group_id.title(),
        "kind": kind,
        "mode": "full_range_passive",
        "channels": [{
            "role": "full_range",
            "physical_output_index": index,
            "identity_verified": identity_verified,
        }],
    }


def _passive_sub_topology_raw(fc: object) -> dict:
    sub_channel: dict = {"role": "subwoofer", "physical_output_index": 2}
    if fc is not None:
        sub_channel["crossover_fc_hz"] = fc
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench",
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
                "label": "L",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "R",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
            {
                "id": "sub",
                "label": "Sub",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [sub_channel],
            },
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    }


def _fingerprint_topology() -> OutputTopology:
    return _topology(
        groups=[_passive_main("left", "left", 0), _passive_main("right", "right", 1)],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
    )
