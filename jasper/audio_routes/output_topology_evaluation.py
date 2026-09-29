# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Evaluate the saved topology's output assignments and protection requirements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jasper.json_fields import issue as _issue
from jasper.speaker_layout import (
    REQUIRED_ROLES_BY_MODE,
    SUB_CROSSOVER_HZ_HI,
    SUB_CROSSOVER_HZ_LO,
    SUPPORTED_OUTPUT_VARIANTS,
)

if TYPE_CHECKING:
    from jasper.audio_routes.output_topology import OutputTopology


# The stable code for "one speaker's drivers are split across two child DACs of
# a composite output device". Shared vocabulary: the /sound/ wizard keys its
# disclosure notice off this exact string. See ``cross_child_group_verdicts``.
CROSS_CHILD_GROUP_CODE = "speaker_group_spans_child_devices"


def cross_child_group_verdicts(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return one verdict per speaker group whose drivers span two child DACs.

    A composite output device (``hardware.child_devices``) is two or more
    physically separate DACs driven from one process. Their clocks are NOT
    corrected against each other: the composite clock contract is
    ``measured_sync_required``, and ``PairedCompositeSink`` detects divergence
    and fails closed rather than resampling it away. A speaker group whose
    woofer sits on one child and whose tweeter on another puts that uncorrected
    seam INSIDE a crossover, where inter-driver drift walks the crossover null.
    The supported shape is one child DAC per speaker.

    This is a FIDELITY verdict, not a hearing-safety one: every lane still
    drives, nothing is at risk of damage, and the household may have a reason.
    So it is reported at ``warning`` severity — it never joins ``blockers`` and
    therefore never refuses the save or moves the topology to ``blocked``.

    ``child_ids`` is sorted so it is comparable regardless of the order the
    group happens to list its channels in.
    """

    children = topology.hardware.child_devices
    if len(children) < 2:
        return []
    # Safe as a flat map: OutputHardware.validate() already refuses a physical
    # output claimed by more than one child.
    owner_by_index: dict[int, str] = {
        index: child.child_id
        for child in children
        for index in child.physical_output_indexes
    }
    verdicts: list[dict[str, Any]] = []
    for group in topology.speaker_groups:
        owners: set[str] = set()
        for channel in group.channels:
            index = channel.physical_output_index
            if index is None:
                continue
            owner = owner_by_index.get(index)
            # An index no child claims is a DIFFERENT defect, owned by the
            # composite's own output-map check.
            if owner is not None:
                owners.add(owner)
        if len(owners) < 2:
            continue
        child_ids = sorted(owners)
        verdicts.append({
            "severity": "warning",
            "code": CROSS_CHILD_GROUP_CODE,
            "message": (
                f"{group.label} is split across DACs {', '.join(child_ids)}; "
                "keep every driver of one speaker on one DAC so its crossover "
                "does not straddle two uncorrected clocks"
            ),
            "group_id": group.id,
            "group_label": group.label,
            "child_ids": child_ids,
        })
    return verdicts


def evaluate_output_topology(topology: OutputTopology) -> dict[str, Any]:
    """Return deterministic safety/validity evidence for a topology."""

    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, Any]] = []
    assigned: dict[int, tuple[str, str]] = {}

    if not topology.speaker_groups:
        warnings.append(
            _issue("warning", "no_speaker_groups", "no speaker groups are configured")
        )

    for group in topology.speaker_groups:
        required_roles = set(REQUIRED_ROLES_BY_MODE[group.mode])
        actual_roles = [channel.role for channel in group.channels if channel.output_variant == "primary"]
        actual_role_set = set(actual_roles)
        slots = [(channel.role, channel.output_variant) for channel in group.channels]
        if (actual_role_set != required_roles or len(slots) != len(set(slots)) or any(
            channel.output_variant not in SUPPORTED_OUTPUT_VARIANTS
            or (channel.output_variant == "rear" and (channel.role != "woofer" or "woofer" not in required_roles))
            for channel in group.channels
        )):
            blockers.append(
                _issue(
                    "blocker",
                    "mode_role_mismatch",
                    f"{group.label} must have exactly {sorted(required_roles)}",
                )
            )
        if group.kind == "subwoofer" and group.mode != "subwoofer":
            blockers.append(
                _issue(
                    "blocker",
                    "subwoofer_mode_mismatch",
                    f"{group.label} is a subwoofer group but mode is {group.mode}",
                )
            )
        if group.kind != "subwoofer" and group.mode == "subwoofer":
            blockers.append(
                _issue(
                    "blocker",
                    "subwoofer_group_required",
                    f"{group.label} uses subwoofer mode but is not a subwoofer group",
                )
            )
        for channel in group.channels:
            if channel.output_variant == "rear" and not channel.startup_muted:
                blockers.append(_issue("blocker", "rear_must_start_muted", f"{group.label} rear woofer must start muted"))
            fc = channel.crossover_fc_hz
            if fc is not None and not (
                SUB_CROSSOVER_HZ_LO <= fc <= SUB_CROSSOVER_HZ_HI
            ):
                # Fail LOUD: an out-of-range bass-management corner would emit
                # an unsafe (or non-band-limiting) crossover, so it is a
                # blocker, never a silent clamp.
                blockers.append(
                    _issue(
                        "blocker",
                        "subwoofer_crossover_out_of_range",
                        (
                            f"{group.label} {channel.role} crossover {fc:g} Hz "
                            f"must be between {SUB_CROSSOVER_HZ_LO:g} and "
                            f"{SUB_CROSSOVER_HZ_HI:g} Hz"
                        ),
                    )
                )
        for channel in group.channels:
            output_index = channel.physical_output_index
            if output_index is None:
                blockers.append(
                    _issue(
                        "blocker",
                        "physical_output_unassigned",
                        f"{group.label} {channel.role} is not assigned to a DAC output",
                    )
                )
                continue
            previous = assigned.get(output_index)
            if previous:
                blockers.append(
                    _issue(
                        "blocker",
                        "duplicate_physical_output",
                        f"DAC output {output_index + 1} is assigned to both "
                        f"{previous[0]}/{previous[1]} and {group.id}/{channel.role}",
                    )
                )
            else:
                assigned[output_index] = (group.id, channel.role)
            if channel.role == "tweeter":
                if not channel.startup_muted:
                    blockers.append(
                        _issue(
                            "blocker",
                            "tweeter_must_start_muted",
                            f"{group.label} tweeter must start muted",
                        )
                    )
                if not channel.protection_required:
                    blockers.append(
                        _issue(
                            "blocker",
                            "tweeter_protection_not_required",
                            f"{group.label} tweeter must require protection",
                        )
                    )

    warnings.extend(cross_child_group_verdicts(topology))

    group_ids = {group.id for group in topology.speaker_groups}
    if topology.routing.main_left_group_id and topology.routing.main_left_group_id not in group_ids:
        blockers.append(_issue("blocker", "left_group_missing", "left routing group is missing"))
    if topology.routing.main_right_group_id and topology.routing.main_right_group_id not in group_ids:
        blockers.append(_issue("blocker", "right_group_missing", "right routing group is missing"))
    for sub_id in topology.routing.subwoofer_group_ids:
        group = next((item for item in topology.speaker_groups if item.id == sub_id), None)  # type: ignore[assignment]
        if group and group.kind != "subwoofer":
            blockers.append(
                _issue(
                    "blocker",
                    "subwoofer_route_kind_mismatch",
                    f"routing subwoofer {sub_id} is not a subwoofer group",
                )
            )

    status = "blocked" if blockers else "valid"
    if not topology.speaker_groups:
        status = "draft"
    if status == "draft":
        next_step = "Create speaker groups and assign physical outputs."
    elif blockers:
        next_step = "Resolve blockers before any sound test can be prepared."
    else:
        next_step = "Topology is saved; sound tests still require a separate safe session."

    return {
        "status": status,
        "assigned_output_count": len(assigned),
        "unused_output_count": max(
            0,
            topology.hardware.physical_output_count - len(assigned),
        ),
        "blockers": blockers,
        "warnings": warnings,
        "safety": {
            "sound_tests_allowed": False,
            "requires_tweeter_protection": any(
                channel.role == "tweeter"
                for group in topology.speaker_groups
                for channel in group.channels
            ),
            "blockers": blockers,
            "warnings": warnings,
            "next_step": next_step,
        },
    }
