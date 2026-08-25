# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persisted USB input-latency presets and their fan-in mapping."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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


class LatencyApplyError(RuntimeError):
    """The preference was saved, but the live fan-in did not apply it."""


def _state_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_USB_LATENCY_FILE", DEFAULT_STATE_PATH)
    )


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


def _runtime_resampler(airplay_health: Any) -> dict[str, Any]:
    current = _mapping(_mapping(airplay_health).get("current"))
    fanin = _mapping(current.get("fanin"))
    inputs = _mapping(fanin.get("inputs"))
    return _mapping(_mapping(inputs.get("usbsink")).get("resampler"))


def applied_mode_from_resampler(resampler: dict[str, Any]) -> str | None:
    decay = _mapping(resampler.get("decay"))
    enabled = decay.get("enabled")
    if enabled is False:
        return "high"
    if enabled is not True:
        return None
    floor = decay.get("floor_frames")
    try:
        floor_frames = int(floor)
    except (TypeError, ValueError):
        return None
    for mode in ("low", "medium"):
        if PRESETS[mode].floor_frames == floor_frames:
            return mode
    return None


def read_state(
    airplay_health: Any = None,
    *,
    state_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    error: str | None = None
    try:
        selected = read_requested_mode(state_path)
    except (OSError, UnicodeError, ValueError) as exc:
        selected = DEFAULT_MODE
        error = f"USB latency preference is invalid: {exc}"
    resampler = _runtime_resampler(airplay_health)
    applied = applied_mode_from_resampler(resampler)
    held_raw = resampler.get("held_target_frames")
    try:
        held_frames = int(held_raw)
    except (TypeError, ValueError):
        held_frames = None
    selected_preset = PRESETS[selected]
    state = "unavailable"
    detail = "Waiting for live USB fan-in state."
    if applied is not None and applied != selected:
        state = "error"
        error = (
            f"{selected_preset.label} is selected, but fan-in is running "
            f"{PRESETS[applied].label}."
        )
        detail = error
    elif applied is not None:
        state = "applied"
        detail = f"{PRESETS[applied].label} is active."
        if (
            applied != "high"
            and held_frames is not None
            and held_frames > selected_preset.floor_frames
        ):
            state = "recovery"
            detail = "Recovery buffer active; latency will fall after timing stabilizes."
    return {
        "selected_mode": selected,
        "applied_mode": applied,
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
    "PRESETS",
    "STATE_ENV_KEY",
    "VALID_MODES",
    "applied_mode_from_resampler",
    "apply_requested_mode",
    "normalize_mode",
    "options",
    "preset_for",
    "read_requested_mode",
    "read_state",
    "write_requested_mode",
]
