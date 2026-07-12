# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Summed-crossover tone plans for active-speaker channel tests.

This module bridges the saved physical output topology to the existing
bounded combined-driver playback seam. It prepares intent only; hardware
playback remains owned by the commissioning host and its safety gates.
"""

from __future__ import annotations

import math
from typing import Any

from jasper.output_topology import OutputTopology, SpeakerChannel, SpeakerGroup

from ._common import issue as _issue
from .playback_route import active_playback_route_capability
from .audible_policy import audible_policy_payload
from .calibration_level import calibration_level_payload, clamp_test_level_dbfs
from .tone_plan import (
    DEFAULT_TONE_DURATION_MS,
    MIN_TONE_DURATION_MS,
    MAX_TONE_DURATION_MS,
    DEFAULT_TONE_RAMP_MS,
    TONE_PLAN_KIND,
)

SCHEMA_VERSION = 1


def _clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def _group(topology: OutputTopology, *, speaker_group_id: str) -> SpeakerGroup | None:
    for group in topology.speaker_groups:
        if group.id == speaker_group_id:
            return group
    return None


def _finite_level(value: Any) -> float:
    level = clamp_test_level_dbfs(value)
    return level if math.isfinite(level) else clamp_test_level_dbfs(None)


def _tone_output_count(topology: OutputTopology) -> int:
    capability = active_playback_route_capability(topology)
    if capability.transport_channel_count > 0:
        return capability.transport_channel_count
    return max(0, int(topology.hardware.physical_output_count or 0))


def build_summed_topology_tone_plan(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    requested_frequency_hz: Any = None,
    requested_level_dbfs: Any = None,
    requested_duration_ms: Any = DEFAULT_TONE_DURATION_MS,
    playback_allowed: bool = False,
    safe_session_id: str | None = None,
    protected_startup_loaded: bool = False,
) -> dict[str, Any]:
    """Build a bounded combined-driver plan for one active speaker group."""

    group_id = str(speaker_group_id or "")
    group = _group(topology, speaker_group_id=group_id)
    issues: list[dict[str, str]] = []
    if group is None:
        issues.append(_issue(
            "blocker",
            "summed_target_not_found",
            "speaker group not found",
        ))
        channels: list[SpeakerChannel] = []
    elif group.mode not in {"active_2_way", "active_3_way"}:
        issues.append(_issue(
            "blocker",
            "summed_target_not_active",
            "combined crossover tests require an active speaker group",
        ))
        channels = []
    else:
        channels = list(group.channels)

    targets: list[dict[str, Any]] = []
    for channel in channels:
        if channel.physical_output_index is None:
            issues.append(_issue(
                "blocker",
                "summed_target_output_missing",
                f"{channel.role} has no assigned DAC output",
            ))
            continue
        if not channel.identity_verified:
            issues.append(_issue(
                "blocker",
                "summed_target_identity_unverified",
                f"confirm the {channel.role} DAC output before testing the pair",
            ))
        output_label = channel.human_output_label or (
            f"DAC output {channel.physical_output_index + 1}"
        )
        targets.append({
            "speaker_group_id": group_id,
            "speaker_label": group.label if group else None,
            "role": channel.role,
            "driver_role": channel.role,
            "driver_style": channel.driver_style,
            "output_index": channel.physical_output_index,
            "label": " ".join(
                item for item in (
                    group.label if group else None,
                    channel.role,
                    output_label,
                )
                if item
            ),
        })

    if len(targets) < 2:
        issues.append(_issue(
            "blocker",
            "summed_target_driver_count_too_low",
            "combined crossover tests require at least two assigned drivers",
        ))

    frequency = _finite_frequency(
        requested_frequency_hz,
        default=1000.0,
    )
    level = calibration_level_payload(requested_level_dbfs=requested_level_dbfs)
    level_dbfs = _finite_level(requested_level_dbfs)
    duration_ms = _clamp_int(
        requested_duration_ms,
        default=DEFAULT_TONE_DURATION_MS,
        lo=MIN_TONE_DURATION_MS,
        hi=MAX_TONE_DURATION_MS,
    )
    route_capability = active_playback_route_capability(topology)
    output_count = _tone_output_count(topology)
    for target in targets:
        output_index = target.get("output_index")
        if (
            isinstance(output_index, int)
            and output_count > 0
            and output_index >= output_count
        ):
            issues.append(_issue(
                "blocker",
                "summed_target_output_outside_active_playback_lane",
                (
                    f"DAC output {output_index + 1} is outside the "
                    "active-speaker test playback lane"
                ),
            ))
    allowed = bool(playback_allowed) and not issues

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAN_KIND,
        "source": "output_topology_summed",
        "status": "blocked" if issues else "ready",
        "would_play": allowed,
        "playback_allowed": allowed,
        "tone_playback_implemented": allowed,
        "topology": {
            "topology_id": topology.topology_id,
            "name": topology.name,
        },
        "channel_map": {
            "layout": "output_topology_summed",
            "output_count": output_count,
        },
        "active_playback_route": route_capability.to_dict(),
        "target": {
            "speaker_group_id": group_id,
            "speaker_label": group.label if group else None,
            "speaker_kind": group.kind if group else None,
            "speaker_mode": group.mode if group else None,
            "role": "summed",
            "driver_role": "summed",
            "label": (
                f"{group.label} combined crossover" if group and group.label
                else "Combined crossover"
            ),
        },
        "targets": targets,
        "tone": {
            "waveform": "sine",
            "frequency_hz": frequency,
            "level_dbfs": level_dbfs,
            "duration_ms": duration_ms,
            "ramp_ms": min(DEFAULT_TONE_RAMP_MS, duration_ms // 4),
            "band_limit": {
                "type": "summed_crossover_region",
                "center_hz": frequency,
            },
        },
        "calibration_level": level,
        "driver_protection": {
            "role": "summed",
            "status": "bounded_by_driver_guards",
            "audio_allowed": True,
            "max_auto_level_dbfs": level_dbfs,
            "issues": [],
        },
        "clamps": {
            "min_duration_ms": MIN_TONE_DURATION_MS,
            "max_duration_ms": MAX_TONE_DURATION_MS,
        },
        "safety": {
            "safe_session_id": safe_session_id,
            "protected_startup_loaded": bool(protected_startup_loaded),
            "requires_emergency_stop": True,
            "artifact_verification_available": True,
            "audible_playback_allowed": allowed,
            "audible_test": audible_policy_payload("summed"),
        },
        "issues": issues,
        "next_step": (
            "Ready for a looped spoken combined-driver test."
            if allowed else
            "Resolve the setup items before running the combined-driver test."
            if issues else
            "Combined-driver artifact can be prepared; audible playback is still gated."
        ),
    }


def _finite_frequency(value: Any, *, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) and out > 0 else default
