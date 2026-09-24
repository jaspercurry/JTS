# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Speaker setup choices translated into the output topology contract."""

from typing import Any, Mapping

from jasper.output_topology import OutputTopology, OutputTopologyError
from jasper.speaker_layout import MAIN_DRIVER_ROLES_BY_MODE, OUTPUT_VARIANT_SCHEMA_VERSION, PASSIVE_MAIN_MODE, physical_target_id

from .profile import SIDES_BY_LAYOUT, SUPPORTED_LAYOUTS


def layout_choices(topology: OutputTopology) -> dict[str, Any]:
    mains = [group for group in topology.speaker_groups if group.kind != "subwoofer"]
    mode = mains[0].mode if mains else PASSIVE_MAIN_MODE
    rear = any(channel.output_variant == "rear" for group in mains for channel in group.channels)
    return {"layout": "stereo" if len(mains) > 1 else "mono",
            "crossover": "passive" if mode == PASSIVE_MAIN_MODE else "active",
            "channels": 3 if rear or mode == "active_3_way" else 2,
            "cardioid": rear}


def build_speaker_layout(topology: OutputTopology, choices: Mapping[str, Any]) -> OutputTopology:
    layout, crossover = choices.get("layout"), choices.get("crossover")
    channels, cardioid = choices.get("channels", 2), choices.get("cardioid", False)
    if layout not in SUPPORTED_LAYOUTS or crossover not in {"passive", "active"}:
        raise OutputTopologyError("Choose mono or stereo and passive or active crossover.")
    if type(channels) is not int or channels not in {2, 3} or type(cardioid) is not bool:
        raise OutputTopologyError("Choose two or three amplifier channels per speaker.")
    rear = crossover == "active" and channels == 3 and cardioid
    mode = (PASSIVE_MAIN_MODE if crossover == "passive" else
            "active_3_way" if channels == 3 and not rear else "active_2_way")
    roles = MAIN_DRIVER_ROLES_BY_MODE[mode]
    sides = SIDES_BY_LAYOUT[layout]
    subs = [group.to_dict() for group in topology.speaker_groups if group.kind == "subwoofer"]
    required = len(sides) * (len(roles) + int(rear)) + sum(len(group["channels"]) for group in subs)
    if required > topology.hardware.physical_output_count:
        raise OutputTopologyError(f"This layout needs {required} outputs; this device has {topology.hardware.physical_output_count}.")
    previous = {channel.target_id(group.id): channel.to_dict()
                for group in topology.speaker_groups for channel in group.channels}
    groups, used, pending = [], {channel["physical_output_index"] for group in subs for channel in group["channels"]}, []
    for side in sides:
        group_id = side if layout == "stereo" else "main"
        group: dict[str, Any] = {"id": group_id, "kind": side, "mode": mode,
                                 "label": f"{side.title()} speaker" if layout == "stereo" else "Speaker",
                                 "channels": []}
        for role, variant in [(role, "primary") for role in roles] + ([("woofer", "rear")] if rear else []):
            target_id = physical_target_id(group_id, role, variant)
            channel = dict(previous.get(target_id) or {"role": role, "output_variant": variant,
                           "startup_muted": True, "protection_required": role == "tweeter"})
            index = channel.get("physical_output_index")
            if index is None or index in used:
                pending.append(channel)
            else:
                used.add(index)
            group["channels"].append(channel)
        groups.append(group)
    free = iter(index for index in range(topology.hardware.physical_output_count) if index not in used)
    for channel in pending:
        channel["physical_output_index"] = next(free)
    raw = topology.to_dict()
    if rear:
        raw["artifact_schema_version"] = OUTPUT_VARIANT_SCHEMA_VERSION
    routing: dict[str, Any] = {"main_left_group_id": "left", "main_right_group_id": "right"} if layout == "stereo" else {"mono_group_id": "main"}
    routing["subwoofer_group_ids"] = list(topology.routing.subwoofer_group_ids)
    raw.update(speaker_groups=groups + subs, routing=routing)
    return OutputTopology.from_mapping(raw)
