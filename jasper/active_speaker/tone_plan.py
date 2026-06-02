"""No-audio tone-plan contract for active-speaker commissioning.

This module prepares the bounded intent that a future playback backend must
consume. It deliberately does not generate samples, open ALSA devices, reload
CamillaDSP, or change volume.
"""

from __future__ import annotations

import json
import math
from importlib.resources import files
from pathlib import Path
from typing import Any

from .calibration_level import (
    DEFAULT_TEST_LEVEL_DBFS,
    MAX_TEST_LEVEL_DBFS,
    MIN_TEST_LEVEL_DBFS,
    calibration_level_payload,
    clamp_test_level_dbfs,
)
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, OutputChannel

SCHEMA_VERSION = 1
TONE_PLAN_KIND = "jts_active_speaker_tone_plan"
DEFAULT_PRESET_RESOURCE = "presets/bc_de250_dayton_e150he44_v1.json"
DEFAULT_TONE_LEVEL_DBFS = DEFAULT_TEST_LEVEL_DBFS
MIN_TONE_LEVEL_DBFS = MIN_TEST_LEVEL_DBFS
MAX_TONE_LEVEL_DBFS = MAX_TEST_LEVEL_DBFS
DEFAULT_TONE_DURATION_MS = 300
MIN_TONE_DURATION_MS = 100
MAX_TONE_DURATION_MS = 500
DEFAULT_TONE_RAMP_MS = 20


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def load_active_speaker_preset(
    preset_path: str | Path | None = None,
) -> ActiveSpeakerPreset:
    """Load a preset from an explicit path or the bundled worked example."""

    if preset_path:
        try:
            raw = json.loads(Path(preset_path).read_text(encoding="utf-8"))
        except OSError as e:
            raise ActiveSpeakerConfigError(f"could not read active preset: {e}") from e
        except json.JSONDecodeError as e:
            raise ActiveSpeakerConfigError(f"active preset is not valid JSON: {e}") from e
    else:
        raw = json.loads(
            files("jasper.active_speaker")
            .joinpath(DEFAULT_PRESET_RESOURCE)
            .read_text(encoding="utf-8")
        )
    return ActiveSpeakerPreset.from_mapping(raw)


def tone_targets_payload(preset: ActiveSpeakerPreset) -> dict[str, Any]:
    """Return preset-derived channel targets for the web UI."""

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": "jts_active_speaker_tone_targets",
        "preset_id": preset.preset_id,
        "name": preset.name,
        "layout": preset.channel_map.layout,
        "calibration_level": calibration_level_payload(),
        "targets": [
            {
                "side": output.side,
                "driver_role": output.driver_role,
                "output_index": output.index,
                "label": output.label,
            }
            for output in sorted(preset.channel_map.outputs, key=lambda item: item.index)
        ],
    }


def _target_output(
    preset: ActiveSpeakerPreset,
    *,
    side: str | None,
    driver_role: str | None,
) -> OutputChannel | None:
    if not side or not driver_role:
        return None
    for output in preset.channel_map.outputs:
        if output.side == side and output.driver_role == driver_role:
            return output
    return None


def _crossovers_for_role(preset: ActiveSpeakerPreset, role: str) -> tuple[float | None, float | None]:
    lower_edge: float | None = None
    upper_edge: float | None = None
    for region in preset.crossover_regions:
        if region.upper_driver == role:
            lower_edge = region.fc_hz
        if region.lower_driver == role:
            upper_edge = region.fc_hz
    return lower_edge, upper_edge


def _tone_frequency_hz(
    preset: ActiveSpeakerPreset,
    role: str,
) -> tuple[float, dict[str, Any]]:
    lower_edge, upper_edge = _crossovers_for_role(preset, role)
    if lower_edge and upper_edge:
        frequency = math.sqrt(lower_edge * upper_edge)
        return round(frequency, 1), {
            "type": "bandpass",
            "highpass_hz": lower_edge,
            "lowpass_hz": upper_edge,
        }
    if lower_edge:
        frequency = min(max(lower_edge * 2.0, 2500.0), 6000.0)
        return round(frequency, 1), {
            "type": "highpass",
            "highpass_hz": lower_edge,
        }
    if upper_edge:
        frequency = min(max(upper_edge / 2.0, 80.0), 220.0)
        return round(frequency, 1), {
            "type": "lowpass",
            "lowpass_hz": upper_edge,
        }
    return 500.0, {"type": "role_band_limited"}


def build_safe_tone_plan(
    preset: ActiveSpeakerPreset,
    *,
    safe_session: dict[str, Any],
    environment_report: dict[str, Any],
    side: str | None = None,
    driver_role: str | None = None,
    requested_level_dbfs: Any = DEFAULT_TONE_LEVEL_DBFS,
    requested_duration_ms: Any = DEFAULT_TONE_DURATION_MS,
) -> dict[str, Any]:
    """Build a bounded, no-audio plan for a future channel test."""

    target_required = not side or not driver_role
    target = _target_output(preset, side=side, driver_role=driver_role)
    issues: list[dict[str, str]] = []
    if safe_session.get("status") != "armed":
        issues.append(
            _issue(
                "blocker",
                "safe_session_not_armed",
                "active-speaker safe session must be armed and unexpired",
            )
        )
    if not environment_report.get("ok_to_load_active_config"):
        issues.append(
            _issue(
                "blocker",
                "active_environment_not_ready",
                "active-speaker environment load gate is not ready",
            )
        )
    if (environment_report.get("safe_playback") or {}).get("playback_allowed"):
        issues.append(
            _issue(
                "blocker",
                "unexpected_playback_permission",
                "environment report unexpectedly allowed playback in a no-audio build",
            )
        )
    if target_required:
        issues.append(
            _issue(
                "blocker",
                "target_output_required",
                "explicit side and driver_role are required before preparing a channel test",
            )
        )
    elif target is None:
        issues.append(
            _issue(
                "blocker",
                "target_output_not_found",
                "requested active-speaker output is not present in the preset channel map",
            )
        )

    role = target.driver_role if target else (driver_role or "unknown")
    frequency_hz, band_limit = _tone_frequency_hz(preset, role)
    level = calibration_level_payload(requested_level_dbfs=requested_level_dbfs)
    level_dbfs = clamp_test_level_dbfs(requested_level_dbfs)
    duration_ms = _clamp_int(
        requested_duration_ms,
        default=DEFAULT_TONE_DURATION_MS,
        lo=MIN_TONE_DURATION_MS,
        hi=MAX_TONE_DURATION_MS,
    )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAN_KIND,
        "status": "blocked" if issues else "ready",
        "would_play": False,
        "playback_allowed": False,
        "tone_playback_implemented": False,
        "preset_id": preset.preset_id,
        "preset_name": preset.name,
        "target": {
            "side": target.side if target else side,
            "driver_role": target.driver_role if target else driver_role,
            "output_index": target.index if target else None,
            "label": target.label if target else None,
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
        "clamps": {
            "min_level_dbfs": MIN_TONE_LEVEL_DBFS,
            "max_level_dbfs": MAX_TONE_LEVEL_DBFS,
            "min_duration_ms": MIN_TONE_DURATION_MS,
            "max_duration_ms": MAX_TONE_DURATION_MS,
        },
        "safety": {
            "safe_session_id": safe_session.get("session_id"),
            "safe_session_status": safe_session.get("status"),
            "environment_load_gate": environment_report.get("load_gate"),
            "requires_emergency_stop": True,
            "prepared_only": True,
        },
        "issues": issues,
        "next_step": (
            "Prepared only. A future playback backend must consume this bounded "
            "plan and keep Stop available before emitting any tone."
        ),
    }
