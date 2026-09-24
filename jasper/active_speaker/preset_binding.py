# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile and bind speaker presets from crossover previews and output topology."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from jasper.output_topology import (
    OutputTopology,
    SpeakerChannel,
    SpeakerGroup,
    main_speaker_groups,
    subwoofer_speaker_groups,
)
from jasper.speaker_layout import ADJACENT_PAIRS_BY_MAIN_MODE, WAY_COUNT_BY_MAIN_MODE

from ._common import gate as _gate, issue as _issue
from .crossover_preview import CROSSOVER_PREVIEW_KIND
from .declaration_vocabulary import (
    _normalise_filter_type,
    _slope_to_lr_order,
    supported_declaration_filter_types,
    supported_declaration_slopes_db_per_octave,
)
from .driver_protection import declared_protection_highpass_floor_hz
from .profile import (
    DEFAULT_SUB_CROSSOVER_HZ,
    ActiveChannelMap,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    DriverSpec,
    LocalSubwoofer,
    OutputChannel,
    SafetyEnvelope,
    required_driver_roles,
)

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _safe_stem(value: str) -> str:
    token = _SAFE_STEM_RE.sub("_", str(value or "").strip()).strip("_")
    return token[:80] or "active_speaker"


def _role_pair_key(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    lower, upper = raw
    if not isinstance(lower, str) or not isinstance(upper, str):
        return None
    return lower, upper


def _driver_spec_from_preview(role: str, raw: Any) -> DriverSpec:
    driver = raw if isinstance(raw, dict) else {}
    model = str(driver.get("model") or role).strip() or role
    manufacturer = str(driver.get("manufacturer") or "Operator research").strip()
    try:
        # #1665: DriverSpec.sensitivity_db labels the staged card with the raw
        # datasheet value. level_trim.declared_driver_gains() folds pad
        # attenuation into the actual trims.
        sensitivity = driver.get("sensitivity_db_2v83_1m")
        sensitivity_db = float(sensitivity) if sensitivity is not None else None
    except (TypeError, ValueError):
        sensitivity_db = None
    return DriverSpec(
        role=role,
        manufacturer=manufacturer or "Operator research",
        model=model,
        sensitivity_db=sensitivity_db,
        # #2491: the preview driver payload already carries the confirmed
        # ``required_protection_filters``. This is the ONE point that reads it
        # into the preset, so the emitted graph and every verifier of that
        # graph clamp the derived tweeter protection to the same declared floor.
        protection_highpass_floor_hz=declared_protection_highpass_floor_hz(driver),
    )


def _active_groups_for_preset(
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
) -> tuple[list[SpeakerGroup], list[dict[str, str]], list[dict[str, Any]]]:
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []
    expected_mode = f"active_{preset.way_count}_way"
    active_groups = [
        group for group in topology.speaker_groups if group.mode == expected_mode
    ]
    if preset.channel_map.layout == "mono":
        groups = [group for group in active_groups if group.kind == "mono"]
        ok = len(groups) == 1
        gates.append(_gate(
            "active_layout_groups",
            label="Saved topology has one mono active speaker group",
            passed=ok,
            message=(
                "Mono active speaker group is present"
                if ok
                else f"Create one mono {expected_mode.replace('_', ' ')} output setup first"
            ),
        ))
        if not ok:
            issues.append(_issue(
                "blocker",
                "mono_active_group_required",
                f"stage protected config requires one mono {expected_mode} speaker group",
            ))
        return groups[:1], issues, gates

    if preset.channel_map.layout == "stereo":
        by_kind = {
            group.kind: group
            for group in active_groups
            if group.kind in {"left", "right"}
        }
        ok = set(by_kind) == {"left", "right"} and len(active_groups) == 2
        gates.append(_gate(
            "active_layout_groups",
            label="Saved topology has left and right active speaker groups",
            passed=ok,
            message=(
                "Left and right active speaker groups are present"
                if ok
                else f"Create left and right {expected_mode.replace('_', ' ')} speaker groups first"
            ),
        ))
        if not ok:
            issues.append(_issue(
                "blocker",
                "stereo_active_groups_required",
                f"stage protected config requires left and right {expected_mode} speaker groups",
            ))
        return [by_kind[kind] for kind in ("left", "right") if kind in by_kind], issues, gates

    issues.append(_issue(
        "blocker",
        "unsupported_active_layout",
        f"protected staging does not support {preset.channel_map.layout} layout",
    ))
    gates.append(_gate(
        "active_layout_groups",
        label="Saved active-speaker layout is supported",
        passed=False,
        message=f"Unsupported layout {preset.channel_map.layout}",
    ))
    return [], issues, gates


def _channels_by_side_role(
    groups: list[SpeakerGroup],
) -> dict[tuple[str, str, str], SpeakerChannel]:
    channels: dict[tuple[str, str, str], SpeakerChannel] = {}
    for group in groups:
        side = group.kind if group.kind in {"left", "right"} else "mono"
        for channel in group.channels:
            channels[(side, channel.role, channel.output_variant)] = channel
    return channels


def _local_subwoofer_from_topology(
    topology: OutputTopology,
    *,
    main_output_count: int,
) -> tuple[LocalSubwoofer | None, list[dict[str, str]]]:
    """Derive the local-subwoofer lane intent from a routed subwoofer group.

    Returns ``(LocalSubwoofer, [])`` when exactly one subwoofer group routes to a
    single assigned ``subwoofer`` channel pinned to the next contiguous output
    after the mains, or ``(None, issues)`` so the caller blocks fail-closed. A sub
    that cannot be resolved to a safe, contiguously-pinned output never reaches
    the emitter — a sub output must never carry a full-range / unbounded feed.

    The crossover corner is read from the resolved subwoofer channel's
    user-settable ``crossover_fc_hz`` (the ``/sound`` subwoofer card writes it
    onto the topology); it falls back to the shared bass-management default
    (:data:`DEFAULT_SUB_CROSSOVER_HZ`, 80 Hz) only when the channel leaves it
    unset. An out-of-range corner is already a fail-loud topology blocker
    (``subwoofer_crossover_out_of_range``), so a value that reaches here is
    in-range; ``LocalSubwoofer.validate`` re-checks it as defense in depth.
    """
    sub_groups = subwoofer_speaker_groups(topology)
    if not sub_groups:
        return None, []
    issues: list[dict[str, str]] = []
    if len(sub_groups) != 1:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_single_group_required",
            "active profile supports exactly one local subwoofer group",
        ))
        return None, issues
    group = sub_groups[0]
    sub_channels = [
        channel for channel in group.channels if channel.role == "subwoofer"
    ]
    if len(sub_channels) != 1:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_channel_unresolved",
            f"{group.label} must have exactly one subwoofer channel",
        ))
        return None, issues
    sub_channel = sub_channels[0]
    output_index = sub_channel.physical_output_index
    crossover_fc_hz = (
        sub_channel.crossover_fc_hz
        if sub_channel.crossover_fc_hz is not None
        else DEFAULT_SUB_CROSSOVER_HZ
    )
    if output_index is None:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_output_unassigned",
            f"{group.label} subwoofer is not assigned to a DAC output",
        ))
        return None, issues
    # The sub output MUST be the next contiguous channel after the mains: a misrouted
    # sub index would mute the wrong output (or leave the sub channel un-band-limited).
    if output_index != main_output_count:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_output_not_contiguous",
            (
                f"{group.label} subwoofer must be on DAC output "
                f"{main_output_count + 1} (the next channel after the mains)"
            ),
        ))
        return None, issues
    try:
        sub = LocalSubwoofer(
            physical_output_index=output_index,
            label=group.label or "subwoofer",
            crossover_fc_hz=crossover_fc_hz,
        )
        sub.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_invalid",
            f"could not resolve a safe local subwoofer lane: {exc}",
        ))
        return None, issues
    return sub, issues


def _preset_from_crossover_preview(
    topology: OutputTopology,
    preview: dict[str, Any],
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []

    preview_ready = (
        preview.get("kind") == CROSSOVER_PREVIEW_KIND
        and preview.get("status") == "ready_for_protected_staging"
    )
    gates.append(_gate(
        "crossover_preview_ready",
        label="Crossover preview is ready for protected staging",
        passed=preview_ready,
        message=(
            "Crossover preview can feed protected staging"
            if preview_ready
            else "Complete the crossover declaration before staging"
        ),
    ))
    if not preview_ready:
        issues.append(_issue(
            "blocker",
            "crossover_preview_not_ready",
            "stage protected config requires a ready crossover preview",
        ))
        return None, issues, gates

    source = preview.get("source") if isinstance(preview.get("source"), dict) else {}
    topology_matches = source.get("topology_id") == topology.topology_id  # type: ignore[union-attr]
    gates.append(_gate(
        "crossover_preview_topology_matches",
        label="Crossover preview matches the saved output topology",
        passed=topology_matches,
        message=(
            "Preview topology matches the saved output setup"
            if topology_matches
            else "Complete the crossover declaration for this output setup"
        ),
    ))
    if not topology_matches:
        issues.append(_issue(
            "blocker",
            "crossover_preview_topology_mismatch",
            "crossover declaration is for a different output topology",
        ))
        return None, issues, gates

    preview_groups = [
        group for group in preview.get("groups", []) if isinstance(group, dict)
    ]
    active_modes = {
        str(group.get("mode"))
        for group in preview_groups
        if ADJACENT_PAIRS_BY_MAIN_MODE.get(str(group.get("mode")))
    }
    if len(active_modes) != 1:
        issues.append(_issue(
            "blocker",
            "crossover_preview_single_active_mode_required",
            "protected staging requires one active speaker mode per config",
        ))
        return None, issues, gates
    mode = next(iter(active_modes))
    way_count = WAY_COUNT_BY_MAIN_MODE[mode]

    kinds = {str(group.get("kind")) for group in preview_groups}
    if kinds == {"mono"} and len(preview_groups) == 1:
        layout = "mono"
    elif kinds == {"left", "right"} and len(preview_groups) == 2:
        layout = "stereo"
    else:
        issues.append(_issue(
            "blocker",
            "crossover_preview_layout_unsupported",
            "protected staging supports one mono speaker or a left/right stereo pair",
        ))
        return None, issues, gates

    roles = required_driver_roles(way_count)
    topology_groups = {
        group.id: group
        for group in topology.speaker_groups
        if group.mode == mode and group.kind in {"mono", "left", "right"}
    }
    outputs: list[OutputChannel] = []
    for preview_group in sorted(
        preview_groups,
        key=lambda item: {"mono": 0, "left": 0, "right": 1}.get(str(item.get("kind")), 9),
    ):
        group_id = str(preview_group.get("group_id") or "")
        group = topology_groups.get(group_id)
        if group is None:
            issues.append(_issue(
                "blocker",
                "crossover_preview_group_missing",
                f"preview group {group_id or '<unknown>'} is not in saved topology",
            ))
            continue
        side = group.kind if group.kind in {"left", "right"} else "mono"
        for channel in group.channels:
            role = channel.role
            if channel.physical_output_index is None:
                issues.append(_issue(
                    "blocker",
                    "crossover_preview_channel_unassigned",
                    f"{group.label} {role} is not assigned to a DAC output",
                ))
                continue
            outputs.append(OutputChannel(
                index=channel.physical_output_index,
                side=side,
                driver_role=role,
                output_variant=channel.output_variant,
                label=(
                    channel.human_output_label
                    or f"DAC output {channel.physical_output_index + 1}"
                ),
                startup_muted=True,
            ))

    crossover_values: dict[tuple[str, str], dict[str, Any]] = {}
    for preview_group in preview_groups:
        for crossover in preview_group.get("crossovers", []):
            if not isinstance(crossover, dict):
                continue
            key = _role_pair_key(crossover.get("between_roles"))
            if key is None:
                continue
            frequency = crossover.get("proposed_frequency_hz")
            filters = [
                item for item in crossover.get("filters", [])
                if isinstance(item, dict)
            ]
            filter_type = filters[0].get("filter_type") if filters else None
            slope = filters[0].get("slope_db_per_octave") if filters else None
            current = {
                "frequency_hz": frequency,
                "filter_type": filter_type,
                "slope_db_per_octave": slope,
                # Persisted working-crossover values (Slice 0): a per-side
                # mismatch here trips the SAME stereo-consistency blocker below
                # as a frequency/slope mismatch — a preview only stages when
                # both sides agree.
                "lower_polarity": crossover.get("lower_polarity"),
                "upper_polarity": crossover.get("upper_polarity"),
                "delay_ms": crossover.get("delay_ms"),
                "delay_target_role": crossover.get("delay_target_role"),
            }
            previous = crossover_values.setdefault(key, current)
            if previous != current:
                issues.append(_issue(
                    "blocker",
                    "crossover_preview_stereo_values_differ",
                    f"preview crossover values differ for {key[0]}/{key[1]}",
                ))

    regions: list[CrossoverRegion] = []
    for lower_role, upper_role in ADJACENT_PAIRS_BY_MAIN_MODE[mode]:
        value = crossover_values.get((lower_role, upper_role))
        if value is None:
            issues.append(_issue(
                "blocker",
                "crossover_preview_pair_missing",
                f"preview is missing {lower_role}/{upper_role} crossover",
            ))
            continue
        try:
            frequency = float(value.get("frequency_hz"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            frequency = 0.0
        filter_type = _normalise_filter_type(value.get("filter_type"))
        order = _slope_to_lr_order(value.get("slope_db_per_octave"))
        if frequency <= 0 or filter_type is None or order is None:
            # The vocabulary is READ, not spelled: this message names the same
            # sets the entry gate offers and refuses against, so 4.1 widens one
            # place and no reader is told the old answer here.
            issues.append(_issue(
                "blocker",
                "crossover_preview_filter_unsupported",
                f"preview filter for {lower_role}/{upper_role} is not one of: "
                + ", ".join(supported_declaration_filter_types())
                + " at "
                + ", ".join(
                    f"{slope:g}"
                    for slope in supported_declaration_slopes_db_per_octave()
                )
                + " dB/octave",
            ))
            continue
        regions.append(CrossoverRegion(
            id=f"{lower_role}_{upper_role}_{int(round(frequency))}hz",
            lower_driver=lower_role,
            upper_driver=upper_role,
            fc_hz=frequency,
            target_type=filter_type,
            order=order,
            lower_polarity=value.get("lower_polarity") or "non-inverted",
            upper_polarity=value.get("upper_polarity") or "non-inverted",
            delay_ms=value.get("delay_ms"),
            delay_target_driver=value.get("delay_target_role"),
        ))

    # A routed local subwoofer is the lower half of the bass-management crossover;
    # the mains' lowest driver carries the complementary high-pass. Resolve it here
    # (fail-closed) so both the candidate compile and protected staging emit the SAME
    # sub-bearing graph through the one multi-output emitter. The sub output pins to
    # the next contiguous channel after the mains (validated against main_output_count).
    local_subwoofer, sub_issues = _local_subwoofer_from_topology(
        topology, main_output_count=len(outputs)
    )
    issues.extend(sub_issues)

    if issues:
        return None, issues, gates

    drivers_raw = preview.get("drivers") if isinstance(preview.get("drivers"), dict) else {}
    try:
        preset = ActiveSpeakerPreset(
            preset_id=f"preview-{_safe_stem(topology.topology_id)}-{way_count}way",
            name=f"{topology.name} preview-derived active {way_count}-way",
            way_count=way_count,
            channel_map=ActiveChannelMap(layout=layout, outputs=tuple(sorted(outputs, key=lambda item: item.index))),
            drivers={
                role: _driver_spec_from_preview(role, drivers_raw.get(role))  # type: ignore[union-attr]
                for role in roles
            },
            crossover_regions=tuple(regions),
            local_subwoofer=local_subwoofer,
            safety=SafetyEnvelope(),
            notes="Derived from jts_active_speaker_crossover_preview; review before load.",
        )
        preset.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "crossover_preview_preset_invalid",
            f"could not turn crossover preview into an active preset: {exc}",
        ))
        return None, issues, gates

    gates.append(_gate(
        "crossover_preview_compiled",
        label="Crossover preview compiled to protected startup intent",
        passed=True,
        message="Preview-derived crossover can be staged through the protected emitter",
    ))
    return preset, issues, gates


def compile_preset_from_crossover_preview(
    topology: OutputTopology,
    preview: dict[str, Any],
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    """Compile a crossover preview into active-speaker preset intent.

    This is the shared no-side-effect bridge used by protected startup staging
    and final baseline candidate compilation. It does not write YAML, load
    CamillaDSP, or authorize playback.
    """

    return _preset_from_crossover_preview(topology, preview)


# Passive mains (full_range_passive) carry NO inter-driver crossover, so they
# produce no active crossover preview — the preview-driven compile path above has
# nothing to feed it. Their 1-way preset is built directly from the saved
# topology below, NOT from a preview.
_PASSIVE_MAIN_ROLE = "full_range"


def build_passive_mains_preset(
    topology: OutputTopology,
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    """Build the 1-way (passive full-range mains, optional local sub) preset.

    The passive analogue of :func:`_preset_from_crossover_preview`: a passive
    speaker has no active crossover preview to compile, so the mains — and the
    sub lane when the topology routes one — resolve directly from the saved
    topology. Fail-closed where a sub IS declared (an unresolvable one returns
    ``(None, issues, gates)``, never a mains-only graph that leaves the sub
    un-band-limited). A subless topology yields ``crossover_regions=()`` and
    ``local_subwoofer=None``. It does not write YAML, load CamillaDSP, or
    authorize playback.
    """
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []

    mains = main_speaker_groups(topology)
    by_kind = {group.kind: group for group in mains}
    if set(by_kind) == {"mono"} and len(mains) == 1:
        layout = "mono"
        ordered = [by_kind["mono"]]
    elif set(by_kind) == {"left", "right"} and len(mains) == 2:
        layout = "stereo"
        ordered = [by_kind["left"], by_kind["right"]]
    else:
        issues.append(_issue(
            "blocker",
            "passive_mains_layout_unsupported",
            "passive mains must be one mono speaker or a left/right pair",
        ))
        gates.append(_gate(
            "passive_mains_layout",
            label="Passive mains layout is supported",
            passed=False,
            message="Passive mains must be one mono speaker or a left/right pair",
        ))
        return None, issues, gates
    gates.append(_gate(
        "passive_mains_layout",
        label="Passive mains layout is supported",
        passed=True,
        message=f"Passive {layout} mains can be routed through the roleful emitter",
    ))

    outputs: list[OutputChannel] = []
    for group in ordered:
        side = group.kind if group.kind in {"left", "right"} else "mono"
        channel = next(
            (c for c in group.channels if c.role == _PASSIVE_MAIN_ROLE), None
        )
        if channel is None or channel.physical_output_index is None:
            issues.append(_issue(
                "blocker",
                "passive_main_output_unassigned",
                f"{group.label} full-range driver is not assigned to a DAC output",
            ))
            continue
        outputs.append(OutputChannel(
            index=channel.physical_output_index,
            side=side,
            driver_role=_PASSIVE_MAIN_ROLE,
            label=(
                channel.human_output_label
                or f"DAC output {channel.physical_output_index + 1}"
            ),
            startup_muted=True,
        ))

    local_subwoofer = None
    if subwoofer_speaker_groups(topology):
        # The sub pins to the next contiguous channel after the mains (validated
        # against main_output_count) and carries the user-settable bass-mgmt corner.
        local_subwoofer, sub_issues = _local_subwoofer_from_topology(
            topology, main_output_count=len(outputs)
        )
        issues.extend(sub_issues)
        if local_subwoofer is None:
            # The topology DECLARES a sub, so a None here is the fail-closed
            # resolution rejecting it. Never emit a mains-only graph that drops a
            # declared sub — that leaves it un-band-limited or full-range.
            if not any(i.get("severity") == "blocker" for i in issues):
                issues.append(_issue(
                    "blocker",
                    "passive_sub_unresolved",
                    "routed subwoofer could not be resolved for the passive mains",
                ))
            return None, issues, gates

    if any(i.get("severity") == "blocker" for i in issues):
        return None, issues, gates

    try:
        preset = ActiveSpeakerPreset(
            # The with-sub id keeps its historical spelling: it is banked on
            # candidates already measured, and a rename would read as a preset
            # mismatch at apply time.
            preset_id=(
                f"passive-sub-{_safe_stem(topology.topology_id)}"
                if local_subwoofer is not None
                else f"passive-{_safe_stem(topology.topology_id)}"
            ),
            name=(
                f"{topology.name} passive full-range"
                + (" + local sub" if local_subwoofer is not None else "")
            ),
            way_count=1,
            channel_map=ActiveChannelMap(
                layout=layout,
                outputs=tuple(sorted(outputs, key=lambda item: item.index)),
            ),
            drivers={
                _PASSIVE_MAIN_ROLE: DriverSpec(
                    role=_PASSIVE_MAIN_ROLE,
                    manufacturer="Operator research",
                    model=_PASSIVE_MAIN_ROLE,
                ),
            },
            crossover_regions=(),
            local_subwoofer=local_subwoofer,
            safety=SafetyEnvelope(),
            notes="Derived from a passive-mains topology; no inter-driver crossover.",
        )
        preset.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "passive_mains_preset_invalid",
            f"could not build a passive-mains preset: {exc}",
        ))
        return None, issues, gates

    gates.append(_gate(
        "passive_mains_compiled",
        label="Passive mains compiled to a routable intent",
        passed=True,
        message="Passive preset can be staged through the roleful emitter",
    ))
    return preset, issues, gates


def _bind_preset_to_topology(
    preset: ActiveSpeakerPreset,
    topology: OutputTopology,
) -> tuple[
    ActiveSpeakerPreset | None,
    list[dict[str, str]],
    list[dict[str, Any]],
    list[SpeakerGroup],
]:
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []
    active_groups, group_issues, group_gates = _active_groups_for_preset(topology, preset)
    issues.extend(group_issues)
    gates.extend(group_gates)

    topology_blockers = topology.evaluation().get("blockers", [])
    topology_valid = not topology_blockers
    gates.append(_gate(
        "topology_valid",
        label="Saved output setup has no staging blockers",
        passed=topology_valid,
        message=(
            "Saved output setup can be staged for no-load review"
            if topology_valid
            else "Resolve saved output setup blockers before staging active DSP"
        ),
    ))
    for issue in topology_blockers:
        if isinstance(issue, dict):
            issues.append({
                "severity": str(issue.get("severity", "blocker")),
                "code": str(issue.get("code", "topology_blocker")),
                "message": str(issue.get("message", "output topology is blocked")),
            })

    preset_shape_ok = (
        preset.way_count in {2, 3}
        and preset.channel_map.layout in {"mono", "stereo"}
    )
    gates.append(_gate(
        "preset_shape",
        label="Preset shape is supported for protected staging",
        passed=preset_shape_ok,
        message=(
            f"Preset matches {preset.channel_map.layout} active {preset.way_count}-way staging"
            if preset_shape_ok
            else "Protected staging supports mono/stereo active 2-way or 3-way presets"
        ),
    ))
    if not preset_shape_ok:
        issues.append(_issue(
            "blocker",
            "unsupported_active_preset_shape",
            "protected staging supports mono/stereo active 2-way or 3-way presets",
        ))

    outputs: list[OutputChannel] = []
    roles = required_driver_roles(preset.way_count) if preset_shape_ok else ()
    channels_by_slot = _channels_by_side_role(active_groups)
    sides = ("mono",) if preset.channel_map.layout == "mono" else ("left", "right")
    required_slots = [(side, role, "primary") for side in sides for role in roles]
    required_slots.extend(slot for slot in channels_by_slot if slot[2] != "primary")
    missing_roles = [
        f"{side}/{role}"
        for side, role, variant in required_slots
        if (side, role, variant) not in channels_by_slot
    ]
    if missing_roles:
        issues.append(_issue(
            "blocker",
            "required_driver_role_missing",
            f"saved topology is missing driver roles: {', '.join(missing_roles)}",
        ))
    assigned_roles = [
        f"{side}/{role}"
        for side, role, variant in required_slots
        if (
            (side, role, variant) in channels_by_slot
            and channels_by_slot[(side, role, variant)].physical_output_index is not None
        )
    ]
    gates.append(_gate(
        "physical_outputs_assigned",
        label="Required active-driver outputs are assigned",
        passed=bool(required_slots) and len(assigned_roles) == len(required_slots),
        message=(
            "Required driver outputs are assigned"
            if bool(required_slots) and len(assigned_roles) == len(required_slots)
            else "Assign every active driver channel to a physical DAC output"
        ),
    ))

    physical_indexes: list[int] = []
    for side, role, variant in required_slots:
        channel = channels_by_slot.get((side, role, variant))
        if channel is None or channel.physical_output_index is None:
            continue
        physical_indexes.append(channel.physical_output_index)
        outputs.append(OutputChannel(
            index=channel.physical_output_index,
            side=side,
            driver_role=role,
            output_variant=variant,
            label=channel.human_output_label or f"DAC output {channel.physical_output_index + 1}",
            startup_muted=True,
        ))
    expected_count = len(required_slots)
    contiguous = sorted(physical_indexes) == list(range(expected_count))
    gates.append(_gate(
        "contiguous_low_outputs",
        label="Assigned outputs are contiguous from DAC output 1",
        passed=bool(required_slots) and len(physical_indexes) == expected_count and contiguous,
        message=(
            "Assigned outputs map directly to the first active playback channels"
            if bool(required_slots) and len(physical_indexes) == expected_count and contiguous
            else (
                "This staging slice requires the active drivers on a contiguous "
                "block starting at DAC output 1"
            )
        ),
    ))
    if bool(required_slots) and (len(physical_indexes) != expected_count or not contiguous):
        issues.append(_issue(
            "blocker",
            "active_outputs_must_be_contiguous",
            "protected staging requires active outputs to be contiguous from DAC output 1",
        ))
    if issues:
        blocker_count = sum(
            1 for issue in issues if issue.get("severity") == "blocker"
        )
        if blocker_count:
            return None, issues, gates, active_groups

    try:
        bound = replace(
            preset,
            channel_map=ActiveChannelMap(
                layout=preset.channel_map.layout,
                outputs=tuple(sorted(outputs, key=lambda item: item.index)),
            ),
        )
        bound.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "bound_active_preset_invalid",
            f"saved topology could not bind to protected DSP preset: {exc}",
        ))
        return None, issues, gates, active_groups

    return bound, issues, gates, active_groups
