# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from jasper.atomic_io import atomic_write_text
from jasper.dsp_control.camilla_config_contract import DEFAULT_SAMPLE_RATE as SAMPLE_RATE
from jasper.fanin.status import USBSINK_INPUT_LABEL
from jasper.json_fields import as_mapping
from jasper.paths import resolve_state_path

STATE_ENV_KEY = "JASPER_USB_LATENCY_MODE"
DEFAULT_MODE = "low"
DEFAULT_STATE_PATH = "/var/lib/jasper/usb_latency.env"


MODE_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}
VALID_MODES = tuple(MODE_LABELS)

LatencyPhase = Literal[
    "unavailable", "idle", "starting", "checking", "clock_adjusting",
    "buffer_adjusting", "buffer_held", "stable", "fallback",
]


@dataclass(frozen=True)
class LatencyRuntime:
    """One interpretation of the live fan-in latency telemetry."""

    phase: LatencyPhase
    applied_mode: str | None
    effective_mode: str | None
    held_frames: int | None
    floor_frames: int | None
    ladder: str | None
    fallback_reason: str | None
    buffer_above_floor: bool


def normalize_mode(raw: str | None) -> str:
    mode = (raw or "").strip().lower()
    if mode not in MODE_LABELS:
        raise ValueError(
            f"unsupported USB latency mode {raw!r}; expected "
            f"{', '.join(VALID_MODES)}"
        )
    return mode


def read_requested_mode(
    path: str | os.PathLike[str] | None = None,
) -> str:
    try:
        text = resolve_state_path(path, None, DEFAULT_STATE_PATH).read_text(
            encoding="utf-8"
        )
    except FileNotFoundError:
        return DEFAULT_MODE
    found: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == STATE_ENV_KEY:
            found = value.strip().strip('"').strip("'")
    return DEFAULT_MODE if found is None else normalize_mode(found)


def write_requested_mode(
    mode: str,
    path: str | os.PathLike[str] | None = None,
) -> str:
    canonical = normalize_mode(mode)
    dst = resolve_state_path(path, None, DEFAULT_STATE_PATH)
    atomic_write_text(
        dst,
        "# Written by JTS /system USB latency control.\n"
        f"{STATE_ENV_KEY}={canonical}\n",
    )
    return canonical


def options() -> list[dict[str, Any]]:
    return [{"mode": mode, "label": label} for mode, label in MODE_LABELS.items()]


def _reported_mode(value: Any) -> str | None:
    return value if isinstance(value, str) and value in MODE_LABELS else None


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runtime_resampler(airplay_health: Any) -> Mapping[str, Any]:
    current = as_mapping(as_mapping(airplay_health).get("current"))
    fanin = as_mapping(current.get("fanin"))
    inputs = as_mapping(fanin.get("inputs"))
    return as_mapping(as_mapping(inputs.get(USBSINK_INPUT_LABEL)).get("resampler"))


def _runtime_host_clock(airplay_health: Any) -> Mapping[str, Any]:
    current = as_mapping(as_mapping(airplay_health).get("current"))
    return as_mapping(as_mapping(current.get("fanin")).get("host_clock"))


def _usb_session_active(
    resampler: Mapping[str, Any], host_clock: Mapping[str, Any]
) -> bool:
    if resampler.get("locked") is True:
        return True
    ladder = host_clock.get("ladder")
    if ladder in {"l0_locked", "l1_warn", "l2_fallback"}:
        return True
    probe = as_mapping(host_clock.get("probe"))
    return ladder == "probing" and probe.get("waiting_for_lock") is True


def classify_runtime(
    resampler: Mapping[str, Any],
    host_clock: Mapping[str, Any] | None = None,
) -> LatencyRuntime:
    """Classify fan-in facts without adding control or presentation state."""
    clock = host_clock or {}
    decay = as_mapping(resampler.get("decay"))
    applied = _reported_mode(decay.get("mode"))
    held_frames = _integer(resampler.get("held_target_frames"))
    floor_frames = _integer(decay.get("floor_frames"))
    locked = resampler.get("locked") is True
    session_active = _usb_session_active(resampler, clock)
    effective = _reported_mode(decay.get("effective_mode")) if locked else None
    buffer_above_floor = (
        applied != "high"
        and held_frames is not None
        and floor_frames is not None
        and held_frames > floor_frames
    )
    raw_ladder = clock.get("ladder")
    ladder = str(raw_ladder) if raw_ladder is not None else None

    if ladder == "l2_fallback" and applied != "high":
        phase: LatencyPhase = "fallback"
    elif ladder == "probing" and session_active:
        phase = "checking"
    elif ladder == "l1_warn":
        phase = "clock_adjusting"
    elif applied is None:
        phase = "stable" if ladder == "l0_locked" else "unavailable"
    elif not session_active:
        phase = "idle"
    elif not locked:
        phase = "starting"
    elif decay.get("frozen_reason") == "backoff":
        phase = "buffer_held"
    elif buffer_above_floor:
        phase = "buffer_adjusting"
    else:
        phase = "stable"

    fallback_reason = clock.get("fallback_reason")
    return LatencyRuntime(
        phase=phase,
        applied_mode=applied,
        effective_mode=effective,
        held_frames=held_frames,
        floor_frames=floor_frames,
        ladder=ladder,
        fallback_reason=(
            str(fallback_reason) if fallback_reason is not None else None
        ),
        buffer_above_floor=buffer_above_floor,
    )


def read_state(
    airplay_health: Any = None,
    *,
    state_path: str | os.PathLike[str] | None = None,
    applying_mode: str | None = None,
) -> dict[str, Any]:
    error: str | None = None
    try:
        selected = read_requested_mode(state_path)
    except (OSError, UnicodeError, ValueError) as exc:
        selected = DEFAULT_MODE
        error = f"USB latency preference is invalid: {exc}"
    resampler = _runtime_resampler(airplay_health)
    host_clock = _runtime_host_clock(airplay_health)
    runtime = classify_runtime(resampler, host_clock)
    applied = runtime.applied_mode
    held_frames = runtime.held_frames
    effective = runtime.effective_mode
    selected_label = MODE_LABELS[selected]
    state = "unavailable"
    detail = "Waiting for live USB fan-in state."
    applying = applying_mode == selected and applied != selected
    if applying:
        state = "applying"
        active = MODE_LABELS[effective] if effective is not None else "current buffer"
        detail = (
            f"Applying {selected_label}; {active} remains active while "
            "fan-in restarts."
        )
    elif applied is not None and applied != selected:
        state = "error"
        error = (
            f"{selected_label} is preferred, but fan-in is configured for "
            f"{MODE_LABELS[applied]}."
        )
        detail = error
    elif applied is not None and runtime.phase == "idle":
        state = "idle"
        detail = (
            f"{selected_label} is preferred. It will be used when USB "
            "audio starts."
        )
    elif applied is not None and runtime.phase in {"starting", "checking"}:
        state = "starting"
        if effective is not None and resampler.get("locked") is True:
            state = "applied"
            detail = f"{MODE_LABELS[effective]} is active. Checking USB timing in the background."
        elif runtime.phase == "checking" and as_mapping(resampler.get("decay")).get("active") is True:
            state = "recovery"
            detail = "Reducing input delay while checking USB timing."
        else:
            detail = "Checking USB timing." if runtime.phase == "checking" else "USB audio is starting."
    elif applied is not None:
        state = "applied"
        detail = f"{MODE_LABELS[applied]} is active."
        if runtime.phase == "fallback" and held_frames is not None:
            state = "fallback"
            live_ms = held_frames * 1000 / SAMPLE_RATE
            if (resampler.get("decay") or {}).get("refilling") is True:
                detail = (
                    f"Input buffer is increasing toward High ({live_ms:.1f} ms now). "
                    f"{selected_label} remains your choice."
                )
            elif runtime.fallback_reason == "actuator_unavailable":
                detail = (
                    f"High ({live_ms:.1f} ms) is active because USB timing "
                    "control is temporarily unavailable. JTS will retry "
                    "automatically."
                )
            elif runtime.fallback_reason == "lost_authority":
                detail = (
                    f"{selected_label} is preferred, but host timing "
                    f"became unstable. This USB session is using High "
                    f"({live_ms:.1f} ms). {selected_label} will be "
                    "tried again when the next USB session starts."
                )
            elif runtime.fallback_reason == "probe_noncompliant":
                detail = (
                    f"{selected_label} is preferred, but the host "
                    f"timing check failed. This USB session is using High "
                    f"({live_ms:.1f} ms). {selected_label} will be "
                    "tried again when the next USB session starts."
                )
            else:
                detail = f"This USB session is using High ({live_ms:.1f} ms)."
        elif runtime.phase == "buffer_held" and held_frames is not None:
            state = "held"
            detail = f"Keeping {held_frames * 1000 / SAMPLE_RATE:.1f} ms to prevent audio gaps. {selected_label} remains your choice."
        elif runtime.buffer_above_floor and held_frames is not None:
            state = "recovery"
            active = (
                MODE_LABELS[effective]
                if effective is not None
                else f"{held_frames * 1000 / SAMPLE_RATE:.1f} ms"
            )
            detail = (
                f"{active} is active while timing stabilizes; JTS will "
                f"reduce toward {selected_label} automatically."
            )
    return {
        "selected_mode": selected,
        "applied_mode": applied,
        "effective_mode": effective,
        "state": state,
        "detail": detail,
        "error": error,
        "live_buffer_frames": held_frames,
        "live_buffer_ms": (
            round(held_frames * 1000 / SAMPLE_RATE, 1)
            if held_frames is not None else None
        ),
        "options": options(),
    }


__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_STATE_PATH",
    "MODE_LABELS",
    "STATE_ENV_KEY",
    "classify_runtime",
    "normalize_mode",
    "options",
    "read_requested_mode",
    "read_state",
    "write_requested_mode",
]
