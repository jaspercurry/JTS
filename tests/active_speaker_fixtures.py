# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared topology builders for active-speaker tests."""

from __future__ import annotations

from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def mono_output_topology(
    *,
    mode: str = "active_2_way",
    with_subwoofer: bool = False,
    identity_verified: bool = True,
    tweeter_verified: bool | None = None,
    tweeter_output: int | None = None,
    protection_status: str = "software_guard_requested",
    topology_name: str = "Bench mono cabinet",
    group_label: str = "Mono cabinet",
    device_id: str = "hifiberry_dac8x",
    device_label: str = "HiFiBerry DAC8x",
    physical_output_count: int = 8,
    card_id: str | None = "DAC8",
) -> OutputTopology:
    """Build the suite's guarded mono topology with evidence-backed variants."""

    resolved_tweeter_verified = (
        identity_verified if tweeter_verified is None else tweeter_verified
    )
    if mode == "active_2_way":
        channels = [
            {
                "role": "woofer",
                "physical_output_index": 0,
                "identity_verified": identity_verified,
            },
            {
                "role": "tweeter",
                "physical_output_index": 1
                if tweeter_output is None
                else tweeter_output,
                "identity_verified": resolved_tweeter_verified,
                "startup_muted": True,
                "protection_required": True,
                "protection_status": protection_status,
            },
        ]
    elif mode == "active_3_way":
        channels = [
            {
                "role": "woofer",
                "physical_output_index": 0,
                "identity_verified": identity_verified,
            },
            {
                "role": "mid",
                "physical_output_index": 1,
                "identity_verified": identity_verified,
            },
            {
                "role": "tweeter",
                "physical_output_index": 2
                if tweeter_output is None
                else tweeter_output,
                "identity_verified": resolved_tweeter_verified,
                "startup_muted": True,
                "protection_required": True,
                "protection_status": protection_status,
            },
        ]
    elif mode == "full_range_passive":
        channels = [
            {
                "role": "full_range",
                "physical_output_index": 0,
                "identity_verified": identity_verified,
            }
        ]
    else:
        raise ValueError(f"unsupported mono output topology mode: {mode}")

    speaker_groups = [
        {
            "id": "mono",
            "label": group_label,
            "kind": "mono",
            "mode": mode,
            "channels": channels,
        }
    ]
    routing: dict[str, object] = {"mono_group_id": "mono"}
    if with_subwoofer:
        speaker_groups.append(
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [
                    {
                        "role": "subwoofer",
                        "physical_output_index": 3 if mode == "active_3_way" else 2,
                        "identity_verified": True,
                        "startup_muted": True,
                    }
                ],
            }
        )
        routing["subwoofer_group_ids"] = ["sub"]

    hardware = {
        "device_id": device_id,
        "device_label": device_label,
        "physical_output_count": physical_output_count,
    }
    if card_id is not None:
        hardware["card_id"] = card_id

    return OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "bench_mono",
            "name": topology_name,
            "status": "draft",
            "hardware": hardware,
            "speaker_groups": speaker_groups,
            "routing": routing,
        }
    )
