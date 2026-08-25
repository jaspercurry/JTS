# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persisted USB input-latency presets and their fan-in mapping."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

STATE_ENV_KEY = "JASPER_USB_LATENCY_MODE"
DEFAULT_MODE = "low"
VALID_MODES = ("low", "medium", "high")
DEFAULT_STATE_PATH = "/var/lib/jasper/usb_latency.env"
SAMPLE_RATE = 48_000


@dataclass(frozen=True)
class LatencyPreset:
    mode: str
    label: str
    floor_frames: int
    decay_enabled: bool

    @property
    def settled_ms(self) -> float:
        return round(self.floor_frames * 1000 / SAMPLE_RATE, 1)


PRESETS = {
    "low": LatencyPreset("low", "Low", 576, True),
    "medium": LatencyPreset("medium", "Medium", 1024, True),
    "high": LatencyPreset("high", "High", 2560, False),
}

LatencyPhase = Literal[
    "unavailable", "idle", "starting", "checking", "clock_adjusting",
    "buffer_adjusting", "stable", "fallback",
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


class LatencyApplyError(RuntimeError):
    """The preference was saved, but the live fan-in did not apply it."""


def _state_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or DEFAULT_STATE_PATH)


def normalize_mode(raw: str | None) -> str:
    mode = (raw or "").strip().lower()
    if mode not in PRESETS:
        raise ValueError(
            f"unsupported USB latency mode {raw!r}; expected "
            f"{', '.join(VALID_MODES)}"
        )
    return mode


def preset_for(raw: str | None) -> LatencyPreset:
    return PRESETS[normalize_mode(raw)]


def read_requested_mode(
    path: str | os.PathLike[str] | None = None,
) -> str:
    try:
        text = _state_path(path).read_text(encoding="utf-8")
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
    dst = _state_path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text(
        "# Written by JTS /system USB latency control.\n"
        f"{STATE_ENV_KEY}={canonical}\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o644)
    os.replace(tmp, dst)
    return canonical


def options() -> list[dict[str, Any]]:
    return [
        {
            "mode": preset.mode,
            "label": preset.label,
            "settled_frames": preset.floor_frames,
            "settled_ms": preset.settled_ms,
        }
        for preset in PRESETS.values()
    ]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runtime_resampler(airplay_health: Any) -> dict[str, Any]:
    current = _mapping(_mapping(airplay_health).get("current"))
    fanin = _mapping(current.get("fanin"))
    inputs = _mapping(fanin.get("inputs"))
    return _mapping(_mapping(inputs.get("usbsink")).get("resampler"))


def _runtime_host_clock(airplay_health: Any) -> dict[str, Any]:
    current = _mapping(_mapping(airplay_health).get("current"))
    return _mapping(_mapping(current.get("fanin")).get("host_clock"))


def _usb_session_active(
    resampler: Mapping[str, Any], host_clock: Mapping[str, Any]
) -> bool:
    if resampler.get("locked") is True:
        return True
    ladder = host_clock.get("ladder")
    if ladder in {"l0_locked", "l1_warn", "l2_fallback"}:
        return True
    probe = _mapping(host_clock.get("probe"))
    return ladder == "probing" and probe.get("waiting_for_lock") is True


def applied_mode_from_resampler(resampler: Mapping[str, Any]) -> str | None:
    decay = _mapping(resampler.get("decay"))
    enabled = decay.get("enabled")
    if enabled is False:
        return "high"
    if enabled is not True:
        return None
    floor_frames = _integer(decay.get("floor_frames"))
    if floor_frames is None:
        return None
    for mode in ("low", "medium"):
        if PRESETS[mode].floor_frames == floor_frames:
            return mode
    return None


def effective_mode_from_buffer(
    held_frames: int | None,
    applied_mode: str | None,
) -> str | None:
    if held_frames is None:
        return None
    for mode, preset in PRESETS.items():
        if held_frames == preset.floor_frames:
            return mode
    applied_preset = PRESETS.get(applied_mode or "")
    if applied_preset is not None and held_frames <= applied_preset.floor_frames:
        return applied_preset.mode
    return None


def classify_runtime(
    resampler: Mapping[str, Any],
    host_clock: Mapping[str, Any] | None = None,
) -> LatencyRuntime:
    """Classify fan-in facts without adding control or presentation state."""
    clock = host_clock or {}
    applied = applied_mode_from_resampler(resampler)
    held_frames = _integer(resampler.get("held_target_frames"))
    floor_frames = _integer(_mapping(resampler.get("decay")).get("floor_frames"))
    locked = resampler.get("locked") is True
    session_active = _usb_session_active(resampler, clock)
    effective = (
        effective_mode_from_buffer(held_frames, applied) if locked else None
    )
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
    selected_preset = PRESETS[selected]
    state = "unavailable"
    detail = "Waiting for live USB fan-in state."
    applying = applying_mode == selected and applied != selected
    if applying:
        state = "applying"
        active = PRESETS[effective].label if effective is not None else "current buffer"
        detail = (
            f"Applying {selected_preset.label}; {active} remains active while "
            "fan-in restarts."
        )
    elif applied is not None and applied != selected:
        state = "error"
        error = (
            f"{selected_preset.label} is preferred, but fan-in is configured for "
            f"{PRESETS[applied].label}."
        )
        detail = error
    elif applied is not None and runtime.phase == "idle":
        state = "idle"
        detail = (
            f"{selected_preset.label} is preferred. It will be used when USB "
            "audio starts."
        )
    elif applied is not None and runtime.phase in {"starting", "checking"}:
        state = "starting"
        detail = (
            "Checking USB host timing; waiting for the live buffer."
            if runtime.phase == "checking"
            else "USB audio is starting; waiting for the live buffer."
        )
    elif applied is not None:
        state = "applied"
        detail = f"{PRESETS[applied].label} is active."
        if runtime.phase == "fallback" and held_frames is not None:
            state = "fallback"
            live_ms = held_frames * 1000 / SAMPLE_RATE
            if runtime.fallback_reason == "actuator_unavailable":
                detail = (
                    f"High ({live_ms:.1f} ms) is active because USB timing "
                    "control is temporarily unavailable. JTS will retry "
                    "automatically."
                )
            elif runtime.fallback_reason == "lost_authority":
                detail = (
                    f"{selected_preset.label} is preferred, but host timing "
                    f"became unstable. This USB session is using High "
                    f"({live_ms:.1f} ms). {selected_preset.label} will be "
                    "tried again when the next USB session starts."
                )
            elif runtime.fallback_reason == "probe_noncompliant":
                detail = (
                    f"{selected_preset.label} is preferred, but the host "
                    f"timing check failed. This USB session is using High "
                    f"({live_ms:.1f} ms). {selected_preset.label} will be "
                    "tried again when the next USB session starts."
                )
            else:
                detail = f"This USB session is using High ({live_ms:.1f} ms)."
        elif runtime.buffer_above_floor and held_frames is not None:
            state = "recovery"
            active = (
                PRESETS[effective].label
                if effective is not None
                else f"{held_frames * 1000 / SAMPLE_RATE:.1f} ms"
            )
            detail = (
                f"{active} is active while timing stabilizes; JTS will "
                f"reduce toward {selected_preset.label} automatically."
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


def apply_requested_mode(
    mode: str,
    *,
    state_path: str | os.PathLike[str] | None = None,
    reconcile: Callable[..., Any] | None = None,
) -> str:
    """Save one preset and run the fan-in env's existing single writer."""
    canonical = write_requested_mode(mode, state_path)
    if reconcile is None:
        from .coupling_reconcile import reconcile_auto

        reconcile = reconcile_auto
    result = reconcile(reason="usb_latency_mode", usb_latency_mode=canonical)
    if not bool(getattr(result, "ok", False)):
        detail = str(getattr(result, "detail", "") or "fan-in reconcile failed")
        raise LatencyApplyError(detail)
    return canonical


__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_STATE_PATH",
    "LatencyApplyError",
    "LatencyPreset",
    "LatencyRuntime",
    "PRESETS",
    "STATE_ENV_KEY",
    "VALID_MODES",
    "applied_mode_from_resampler",
    "apply_requested_mode",
    "classify_runtime",
    "effective_mode_from_buffer",
    "normalize_mode",
    "options",
    "preset_for",
    "read_requested_mode",
    "read_state",
    "write_requested_mode",
]
