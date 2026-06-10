"""Topology-target tone plans for active-speaker channel tests.

This module bridges the saved physical output topology to the existing
bounded tone playback seam. It prepares intent only; hardware playback remains
owned by ``jasper.active_speaker.playback`` and its explicit safety gates.
"""

from __future__ import annotations

import math
from typing import Any

from jasper.output_topology import OutputTopology, SpeakerChannel, SpeakerGroup

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
    readiness: dict[str, Any],
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


def build_topology_tone_plan(
    topology: OutputTopology,
    *,
    readiness_report: dict[str, Any],
    speaker_group_id: str,
    role: str,
    requested_level_dbfs: Any = None,
    requested_duration_ms: Any = DEFAULT_TONE_DURATION_MS,
) -> dict[str, Any]:
    """Build a bounded plan for one saved topology channel."""

    group_id = str(speaker_group_id or "")
    role_id = str(role or "")
    match = _target(topology, speaker_group_id=group_id, role=role_id)
    group = match[0] if match else None
    channel = match[1] if match else None
    ready_target = (
        readiness_report.get("target")
        if isinstance(readiness_report.get("target"), dict)
        else {}
    )
    startup_load = (
        readiness_report.get("startup_load")
        if isinstance(readiness_report.get("startup_load"), dict)
        else {}
    )
    issues: list[dict[str, str]] = []
    if not isinstance(readiness_report, dict):
        issues.append(_issue("blocker", "readiness_required", "readiness report is required"))
    elif (
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
    if not readiness_report.get("preconditions_passed"):
        issues.append(
            _issue(
                "blocker",
                "readiness_blocked",
                "playback readiness preconditions have not passed",
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
    playback_allowed = bool(readiness_report.get("playback_allowed")) and not issues
    label = _target_label(readiness_report, group, channel)
    output_count = max(0, int(topology.hardware.physical_output_count or 0))

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAN_KIND,
        "source": "output_topology",
        "status": "blocked" if issues else "ready",
        "would_play": playback_allowed,
        "playback_allowed": playback_allowed,
        "tone_playback_implemented": bool(
            readiness_report.get("tone_playback_implemented")
        ),
        "topology": {
            "topology_id": topology.topology_id,
            "name": topology.name,
        },
        "channel_map": {
            "layout": "output_topology",
            "output_count": output_count,
        },
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
                readiness_report.get("safe_session", {}).get("session_id")
                if isinstance(readiness_report.get("safe_session"), dict)
                else None
            ),
            "readiness_status": readiness_report.get("status"),
            "protected_startup_loaded": bool(
                startup_load.get("loaded")
                and startup_load.get("rollback_available")
                and startup_load.get("current_config_matches_loaded")
            ),
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
