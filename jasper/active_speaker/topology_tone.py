"""Topology-target tone plans for active-speaker channel tests.

This module bridges the saved physical output topology to the existing
bounded tone playback seam. It prepares intent only; hardware playback remains
owned by ``jasper.active_speaker.playback`` and its explicit safety gates.
"""

from __future__ import annotations

import math
from typing import Any

from jasper.output_topology import OutputTopology, SpeakerChannel, SpeakerGroup

from .playback_route import active_playback_route_capability
from .audible_policy import audible_policy_payload
from .calibration_level import calibration_level_payload, clamp_test_level_dbfs
from .driver_protection import driver_protection_payload, driver_protection_profile
from .tone_plan import (
    DEFAULT_TONE_DURATION_MS,
    MIN_TONE_DURATION_MS,
    MAX_TONE_DURATION_MS,
    DEFAULT_TONE_RAMP_MS,
    TONE_PLAN_KIND,
)

SCHEMA_VERSION = 1


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def _target(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
) -> tuple[SpeakerGroup, SpeakerChannel] | None:
    for group in topology.speaker_groups:
        if group.id != speaker_group_id:
            continue
        for channel in group.channels:
            if channel.role == role:
                return group, channel
    return None


def _group(topology: OutputTopology, *, speaker_group_id: str) -> SpeakerGroup | None:
    for group in topology.speaker_groups:
        if group.id == speaker_group_id:
            return group
    return None


def _role_tone(role: str, *, driver_style: Any = None) -> tuple[float, dict[str, Any]]:
    profile = driver_protection_profile(role, driver_style=driver_style)
    if profile.role_class == "high_frequency":
        highpass = profile.min_highpass_hz or 5000.0
        return profile.floor_test_frequency_hz, {
            "type": "highpass",
            "highpass_hz": highpass,
        }
    if role == "subwoofer":
        return 50.0, {"type": "lowpass", "lowpass_hz": 120.0}
    if role == "woofer":
        return 120.0, {"type": "lowpass", "lowpass_hz": 300.0}
    if role == "mid":
        return 800.0, {
            "type": "bandpass",
            "highpass_hz": 250.0,
            "lowpass_hz": 3000.0,
        }
    return 500.0, {"type": "role_band_limited"}


def _finite_level(value: Any) -> float:
    level = clamp_test_level_dbfs(value)
    return level if math.isfinite(level) else clamp_test_level_dbfs(None)


def _target_label(
    readiness: dict[str, Any] | None,
    group: SpeakerGroup | None,
    channel: SpeakerChannel | None,
) -> str | None:
    ready_target = readiness.get("target") if isinstance(readiness, dict) else {}
    if isinstance(ready_target, dict) and ready_target.get("label"):
        return str(ready_target["label"])
    if not group or not channel:
        return None
    output_label = channel.human_output_label or (
        f"Output {channel.physical_output_index + 1}"
        if channel.physical_output_index is not None else None
    )
    return " ".join(item for item in (group.label, channel.role, output_label) if item)


def _tone_output_count(topology: OutputTopology) -> int:
    capability = active_playback_route_capability(topology)
    if capability.transport_channel_count > 0:
        return capability.transport_channel_count
    return max(0, int(topology.hardware.physical_output_count or 0))


def build_topology_tone_plan(
    topology: OutputTopology,
    *,
    readiness_report: dict[str, Any] | None = None,
    speaker_group_id: str,
    role: str,
    requested_level_dbfs: Any = None,
    requested_duration_ms: Any = DEFAULT_TONE_DURATION_MS,
    safe_session: dict[str, Any] | None = None,
    startup_load_state: dict[str, Any] | None = None,
    playback_allowed: bool = False,
    tone_playback_implemented: bool = False,
    requires_protected_startup: bool = True,
) -> dict[str, Any]:
    """Build a bounded plan for one saved topology channel."""

    readiness = readiness_report if isinstance(readiness_report, dict) else None
    group_id = str(speaker_group_id or "")
    role_id = str(role or "")
    match = _target(topology, speaker_group_id=group_id, role=role_id)
    group = match[0] if match else None
    channel = match[1] if match else None
    ready_target = (
        readiness.get("target")
        if readiness and isinstance(readiness.get("target"), dict)
        else {}
    )
    startup_load = (
        readiness.get("startup_load")
        if readiness and isinstance(readiness.get("startup_load"), dict)
        else startup_load_state if isinstance(startup_load_state, dict) else {}
    )
    session = (
        readiness.get("safe_session")
        if readiness and isinstance(readiness.get("safe_session"), dict)
        else safe_session if isinstance(safe_session, dict) else {}
    )
    issues: list[dict[str, str]] = []
    if readiness and (
        str(ready_target.get("speaker_group_id") or "") != group_id
        or str(ready_target.get("role") or "") != role_id
    ):
        issues.append(
            _issue(
                "blocker",
                "readiness_target_mismatch",
                "readiness report does not match the requested speaker channel",
            )
        )
    if match is None:
        issues.append(_issue("blocker", "target_not_found", "speaker channel not found"))
    elif channel and channel.physical_output_index is None:
        issues.append(
            _issue(
                "blocker",
                "target_output_missing",
                "speaker channel has no assigned physical output",
            )
        )
    elif channel and not channel.identity_verified:
        issues.append(
            _issue(
                "blocker",
                "target_identity_unverified",
                "confirm the DAC output for this driver before testing",
            )
        )
    if readiness and not readiness.get("preconditions_passed"):
        issues.append(
            _issue(
                "blocker",
                "readiness_blocked",
                "playback readiness preconditions have not passed",
            )
        )
    requested_playback_allowed = (
        bool(readiness.get("playback_allowed")) if readiness else bool(playback_allowed)
    )
    if not readiness and requested_playback_allowed and session.get("status") != "armed":
        issues.append(
            _issue(
                "blocker",
                "safe_session_not_armed",
                "safe test controls are not open for this driver",
            )
        )

    driver_style = channel.driver_style if channel else None
    protection_status = channel.protection_status if channel else None
    frequency_hz, band_limit = _role_tone(role_id, driver_style=driver_style)
    driver_protection = driver_protection_payload(
        role_id,
        driver_style=driver_style,
        protection_status=protection_status,
        band_limit=band_limit,
    )
    level = calibration_level_payload(requested_level_dbfs=requested_level_dbfs)
    level_dbfs = _finite_level(requested_level_dbfs)
    duration_ms = _clamp_int(
        requested_duration_ms,
        default=DEFAULT_TONE_DURATION_MS,
        lo=MIN_TONE_DURATION_MS,
        hi=MAX_TONE_DURATION_MS,
    )
    current_matches_loaded = startup_load.get("current_config_matches_loaded")
    protected_startup_loaded = bool(
        startup_load.get("loaded")
        and startup_load.get("rollback_available")
        and current_matches_loaded is not False
    )
    if (
        not readiness
        and requested_playback_allowed
        and requires_protected_startup
        and not protected_startup_loaded
    ):
        issues.append(
            _issue(
                "blocker",
                "protected_startup_config_not_loaded",
                "JTS needs to finish opening the safe test setup for this driver",
            )
        )
    playback_allowed = (
        requested_playback_allowed
        and (protected_startup_loaded or not requires_protected_startup)
        and session.get("status") == "armed"
        and not issues
    )
    label = _target_label(readiness, group, channel)
    route_capability = active_playback_route_capability(topology)
    output_count = _tone_output_count(topology)
    if (
        channel
        and channel.physical_output_index is not None
        and output_count > 0
        and channel.physical_output_index >= output_count
    ):
        issues.append(_issue(
            "blocker",
            "target_output_outside_active_playback_lane",
            (
                f"DAC output {channel.physical_output_index + 1} is outside "
                "the active-speaker test playback lane"
            ),
        ))
        playback_allowed = False

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAN_KIND,
        "source": "output_topology",
        "status": "blocked" if issues else "ready",
        "would_play": playback_allowed,
        "playback_allowed": playback_allowed,
        "tone_playback_implemented": bool(
            readiness.get("tone_playback_implemented") if readiness else tone_playback_implemented
        ),
        "topology": {
            "topology_id": topology.topology_id,
            "name": topology.name,
        },
        "channel_map": {
            "layout": "output_topology",
            "output_count": output_count,
        },
        "active_playback_route": route_capability.to_dict(),
        "target": {
            "speaker_group_id": group_id,
            "speaker_label": group.label if group else None,
            "speaker_kind": group.kind if group else None,
            "speaker_mode": group.mode if group else None,
            "side": group.kind if group and group.kind in {"left", "right", "mono"} else None,
            "role": channel.role if channel else role_id,
            "driver_role": channel.role if channel else role_id,
            "driver_style": driver_style,
            "output_index": channel.physical_output_index if channel else None,
            "label": label,
        },
        "tone": {
            "waveform": "sine",
            "frequency_hz": frequency_hz,
            "level_dbfs": level_dbfs,
            "duration_ms": duration_ms,
            "ramp_ms": min(DEFAULT_TONE_RAMP_MS, duration_ms // 4),
            "band_limit": band_limit,
        },
        "calibration_level": level,
        "driver_protection": driver_protection,
        "clamps": {
            "min_duration_ms": MIN_TONE_DURATION_MS,
            "max_duration_ms": MAX_TONE_DURATION_MS,
        },
        "safety": {
            "safe_session_id": (
                session.get("session_id")
            ),
            "readiness_status": (
                readiness.get("status")
                if readiness else "preconditions_passed" if not issues else "blocked"
            ),
            "protected_startup_loaded": protected_startup_loaded,
            "requires_protected_startup": requires_protected_startup,
            "startup_load_status": startup_load.get("status"),
            "requires_emergency_stop": True,
            "artifact_verification_available": True,
            "audible_playback_allowed": playback_allowed,
            "audible_test": audible_policy_payload(
                role_id,
                driver_protection=driver_protection,
            ),
        },
        "issues": issues,
        "next_step": (
            "Ready for a short low-level audible channel test."
            if playback_allowed
            else "Ready for artifact verification; audible playback is still gated."
            if not issues
            else "Resolve readiness blockers before generating a channel test."
        ),
    }


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
            "Ready for a short combined-driver test."
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
