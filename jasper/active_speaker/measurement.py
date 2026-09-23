# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The physical driver targets a measurement addresses, and their fingerprints."""

from __future__ import annotations

import json
import hashlib
from typing import Any, Mapping, Sequence

from jasper.output_topology import (
    OutputTopology,
    main_speaker_groups,
    topology_is_subless_passive_mains,
)


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _crossover_groups(topology: OutputTopology) -> list[Any]:
    """Return active two-way and three-way speaker groups."""
    return [
        group for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"}
    ]


def measured_speaker_groups(topology: OutputTopology) -> list[Any]:
    """The groups whose drivers need per-driver measurement evidence.

    Every crossover group, PLUS a subless passive main, whose one full-range
    driver a recommissioning session measures with one routed solo and so needs
    the same target, safety limits and ceilings. Passive mains WITH a sub are
    bass management, not this session.
    """
    groups = _crossover_groups(topology)
    if topology_is_subless_passive_mains(topology):
        groups = groups + main_speaker_groups(topology)
    return groups


def _hardware_payload(topology: OutputTopology) -> Mapping[str, Any]:
    return topology.hardware.to_dict()


def _target_fingerprint(
    topology: OutputTopology,
    target: Mapping[str, Any],
) -> str:
    """Fingerprint the physical output target that measurement evidence proves."""

    return _fingerprint({
        "topology_id": topology.topology_id,
        "hardware": _hardware_payload(topology),
        "speaker_group_id": target.get("speaker_group_id"),
        "speaker_group_kind": target.get("speaker_group_kind"),
        "speaker_group_mode": target.get("speaker_group_mode"),
        "role": target.get("role"),
        **({"output_variant": target["output_variant"]} if target.get("output_variant", "primary") != "primary" else {}),
        "output_index": target.get("output_index"),
    })


def physical_driver_target(
    topology: OutputTopology,
    group: Any,
    channel: Any,
) -> dict[str, Any]:
    """Describe and fingerprint one physical driver channel.

    Eligibility remains the owning workflow's decision: measurement calls this
    only for active groups, while driver research may also describe a passive
    full-range component. Keeping construction here gives both workflows one
    target-identity contract without widening measurement eligibility.
    """

    target = {
        "target_id": channel.target_id(group.id),
        **({"output_variant": channel.output_variant} if channel.output_variant != "primary" else {}),
        "speaker_group_id": group.id,
        "speaker_group_label": group.label,
        "speaker_group_kind": group.kind,
        "speaker_group_mode": group.mode,
        "role": channel.role,
        "output_index": channel.physical_output_index,
        "output_label": (
            channel.human_output_label
            or (
                f"DAC output {channel.physical_output_index + 1}"
                if channel.physical_output_index is not None
                else None
            )
        ),
    }
    target["target_fingerprint"] = _target_fingerprint(topology, target)
    return target


def _driver_targets_for(
    topology: OutputTopology, groups: Sequence[Any],
) -> list[dict[str, Any]]:
    """Every channel of ``groups`` as a fingerprinted driver target.

    Shared so the two eligibility filters above are the only difference.
    """
    return [
        physical_driver_target(topology, group, channel)
        for group in groups
        for channel in group.channels
    ]


def active_driver_targets(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return the driver targets that need measurement evidence.

    Eligibility is :func:`measured_speaker_groups`, WIDER than "has a crossover".
    """

    return _driver_targets_for(topology, measured_speaker_groups(topology))


def _summed_fingerprint(
    topology: OutputTopology,
    group: Any,
    driver_targets: list[dict[str, Any]],
) -> str:
    return _fingerprint({
        "topology_id": topology.topology_id,
        "hardware": _hardware_payload(topology),
        "speaker_group_id": group.id,
        "speaker_group_kind": group.kind,
        "speaker_group_mode": group.mode,
        "driver_target_fingerprints": [
            target["target_fingerprint"]
            for target in driver_targets
            if target["speaker_group_id"] == group.id
        ],
    })


def active_summed_targets(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return crossover group targets with roles and fingerprints."""

    crossover_groups = _crossover_groups(topology)
    # NOT ``active_driver_targets``: that set is WIDER, and the fingerprint
    # below only ever consumed the crossover groups' own targets.
    driver_targets = _driver_targets_for(topology, crossover_groups)
    return [
        {
            "speaker_group_id": group.id,
            "speaker_group_label": group.label,
            "mode": group.mode,
            "roles": [channel.role for channel in group.channels],
            "group_fingerprint": _summed_fingerprint(
                topology,
                group,
                driver_targets,
            ),
        }
        for group in crossover_groups
    ]


def empty_driver_check_summary(topology: OutputTopology) -> dict[str, Any]:
    """What the retired driver-check record summarised for ``topology`` with no records.

    Applied profiles hash it into ``source.measurement_summary_fingerprint``, so
    it is rebuilt key for key: any other value re-fingerprints every applied
    profile. REMOVAL CONDITION: drop it, with the source's
    ``measurements_updated_at`` and ``measurement_summary_fingerprint`` keys, in
    the next change that re-fingerprints the profile source anyway.
    """
    targets = active_driver_targets(topology)
    return {
        "required_driver_count": len(targets),
        "captured_driver_count": 0,
        "missing_driver_targets": targets,
        "driver_measurements_complete": False,
        "required_driver_check_count": len(targets),
        "captured_driver_check_count": 0,
        "missing_driver_check_targets": targets,
        "driver_checks_complete": False,
        "latest_driver_measurements": {},
        "latest_driver_checks": {},
        "latest_reference_axis_driver_measurements": {},
        "latest_driver_confirmations": {},
        "stale_driver_record_count": 0,
    }
