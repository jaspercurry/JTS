# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-source timing cards and the source-availability roll-up.

Continuity and timing are separate axes (see ``audio_health``'s module
docstring): this leaf answers only "how is this source's stream timed" and
"is this source's renderer even up", never whether the shared path is
carrying sound at all -- that verdict is `signal_path`'s, spliced in by the
caller.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..fanin.latency_mode import PRESETS, classify_runtime
from ..music_sources import MUSIC_SOURCE_SPECS, Source
from ..service_units import unit_failed
from ._health_fields import as_int, mapping
from ._health_sources import (
    SOURCE_OFF_DRIFT_DETAIL,
    SOURCE_UNAVAILABLE_DETAIL,
    _SOURCE_HEALTH_UNITS,
    SOURCE_LABELS,
    _SOURCE_OFF_DRIFT_UNITS,
    _SOURCE_PRIMARY_UNITS,
)


def _usb_timing(
    route: Mapping[str, Any],
    host_clock: Mapping[str, Any] | None,
    usb_input: Mapping[str, Any] | None = None,
    *,
    active: bool,
) -> dict[str, Any]:
    claimed = bool(route.get("low_latency_claim"))
    resampler = mapping(mapping(usb_input).get("resampler"))
    latency_runtime = classify_runtime(resampler, host_clock)
    raw_mode = latency_runtime.ladder
    preset_mode = latency_runtime.applied_mode
    mode = {
        "l0_locked": "lowest_latency",
        "l1_warn": "tracking_warn",
        "l2_fallback": "fallback",
        "probing": "checking",
        "disabled": "standard",
    }.get(str(raw_mode), "unknown")
    runtime: dict[str, Any] = {
        "mode": mode,
        "raw_mode": raw_mode,
        "phase": latency_runtime.phase,
    }
    if preset_mode is not None:
        runtime.update({
            "preset": preset_mode,
            "effective_preset": latency_runtime.effective_mode,
            "held_target_frames": latency_runtime.held_frames,
            "floor_frames": latency_runtime.floor_frames,
        })

    if preset_mode is not None:
        preset = PRESETS[preset_mode]
        current_frames = latency_runtime.held_frames or preset.floor_frames
        current_ms = current_frames * 1000 / 48_000
        if active and latency_runtime.phase == "fallback":
            status = "warn"
            headline = f"Stable fallback · {current_ms:.1f} ms input buffer"
            detail = (
                "Playback is protected by more buffering while JTS retries "
                "USB timing."
                if latency_runtime.fallback_reason == "actuator_unavailable"
                else "Playback is protected by more buffering for this USB session."
            )
        elif active and latency_runtime.phase == "clock_adjusting":
            status = "warn"
            headline = f"{preset.label} latency · clock tracking under strain"
            detail = "Playback remains locked while USB host timing stabilizes."
        elif active and latency_runtime.phase == "buffer_adjusting":
            status = "warn"
            headline = f"Recovery buffer active · {current_ms:.1f} ms input buffer"
            detail = "Latency will fall after USB host timing stabilizes."
        elif active and latency_runtime.phase == "buffer_held":
            status = "warn"
            headline = f"Extra buffer in use · {current_ms:.1f} ms input buffer"
            detail = f"JTS keeps this buffer to prevent audio gaps. {preset.label} remains selected."
        elif active and latency_runtime.phase == "checking":
            status = "idle"
            headline = "Checking USB host timing"
            detail = "Playback is safe while JTS checks USB timing."
        else:
            status = "ok"
            headline = f"{preset.label} latency · {current_ms:.1f} ms input buffer"
            detail = (
                "The larger stable USB buffer is active."
                if preset_mode == "high"
                else "The selected USB input buffer is active."
            )
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": status,
            "headline": headline,
            "detail": detail,
            "route_id": route.get("route_id"),
            "runtime": runtime,
        }

    if route.get("status") != "available":
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": "unknown",
            "headline": "USB latency state unavailable",
            "detail": (
                "JTS cannot check this computer's USB audio delay; playback "
                "health is checked separately."
            ),
            "route_id": route.get("route_id"),
            "runtime": runtime,
        }
    if not claimed:
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": "idle",
            "headline": "Standard buffered route",
            "detail": "This route runs with standard buffering.",
            "route_id": route.get("route_id"),
            "runtime": runtime,
        }
    if active and raw_mode == "l2_fallback":
        status = "warn"
        headline = "Stable fallback · latency increased"
        detail = "Playback is protected by resampling while host timing recovers."
    elif active and raw_mode == "l1_warn":
        status = "warn"
        headline = "Low latency active · clock tracking under strain"
        detail = "The host is following the speaker clock with unusually high demand."
    elif active and raw_mode == "probing":
        status = "idle"
        headline = "Checking USB host timing"
        detail = "Playback is safe while JTS checks USB timing."
    elif active and raw_mode not in {"l0_locked", "l1_warn", "l2_fallback"}:
        status = "warn"
        headline = "USB low-latency clock mode unavailable"
        detail = (
            "Playback continues with standard buffering; JTS is not "
            "fine-tuning USB timing right now."
        )
    else:
        status = "ok"
        headline = "Low latency · stable"
        if active and raw_mode == "l0_locked":
            detail = "USB is running with the smallest safe delay."
        else:
            detail = "The low-latency route is active."
    return {
        "applicable": active or claimed,
        "source_id": Source.USBSINK.value,
        "kind": "route_latency",
        "status": status,
        "headline": headline,
        "detail": detail,
        "route_id": route.get("route_id"),
        "runtime": runtime,
    }


def _airplay_timing(airplay: Mapping[str, Any], *, active: bool) -> dict[str, Any]:
    if not active:
        status = "idle"
        headline = "AirPlay idle"
        detail = "Sync timing is checked while AirPlay is playing."
    else:
        recent = mapping(airplay.get("summary_5m"))
        sync_events = (
            as_int(recent.get("shairport_packet_drops"))
            + as_int(recent.get("shairport_sync_errors"))
            + as_int(recent.get("shairport_underruns"))
        )
        if sync_events:
            status = "warn"
            headline = "AirPlay sync recently recovered"
            detail = "Wireless timing had a recent correction; playback is still monitored."
        else:
            status = "ok"
            headline = "AirPlay sync timing clean"
            detail = "No recent sender or synchronization corrections."
    return {
        "applicable": active,
        "source_id": Source.AIRPLAY.value,
        "kind": "sync",
        "status": status,
        "headline": headline,
        "detail": detail,
        "route_id": None,
        "runtime": {"mode": "standard", "raw_mode": None},
    }


def _not_applicable_timing() -> dict[str, Any]:
    return {
        "applicable": False,
        "source_id": None,
        "kind": "none",
        "status": "idle",
        "headline": "No timing contract for this source",
        "detail": "Timing is shown only where JTS has an honest runtime signal.",
        "route_id": None,
        "runtime": {"mode": "standard", "raw_mode": None},
    }


def _source_service_summary(
    source_id: str,
    service_states: Mapping[str, Any] | None,
    source_intents: Mapping[str, bool] | None = None,
) -> tuple[str, str, str] | None:
    """Return ``(state, headline, detail)`` from cached systemd truth."""
    states = mapping(service_states)
    desired = mapping(source_intents).get(source_id)
    if desired is False:
        if any(
            mapping(states.get(unit)).get("active_state") == "active"
            for unit in _SOURCE_OFF_DRIFT_UNITS.get(source_id, ())
        ):
            return (
                "unavailable",
                f"{SOURCE_LABELS.get(source_id, source_id)} is running while Off",
                SOURCE_OFF_DRIFT_DETAIL,
            )
        return "off", "Off", "Turned off in Playback sources."
    if not states:
        return None
    for unit in _SOURCE_HEALTH_UNITS.get(source_id, ()):
        if unit_failed(mapping(states.get(unit))):
            return (
                "unavailable",
                f"{SOURCE_LABELS.get(source_id, source_id)} unavailable",
                SOURCE_UNAVAILABLE_DETAIL,
            )
    primary = _SOURCE_PRIMARY_UNITS.get(source_id)
    primary_state = mapping(states.get(primary)) if primary else {}
    if primary_state.get("active_state") == "active":
        return "ready", "Ready", "Waiting for a stream."
    if primary_state.get("active_state") == "inactive":
        return "not_running", "Not running", "Nothing is running for this source."
    return None


def _source_cards(
    airplay: Mapping[str, Any],
    signal_path: Mapping[str, Any],
    route: Mapping[str, Any],
    active_source: str | None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    current = mapping(airplay.get("current"))
    fanin = mapping(current.get("fanin"))
    inputs = mapping(fanin.get("inputs"))
    host_clock = mapping(fanin.get("host_clock")) or None
    cards: list[dict[str, Any]] = []
    for spec in MUSIC_SOURCE_SPECS:
        source_id = spec.id.value
        active = active_source == source_id
        status = "ok" if active else "idle"
        headline = "Playing" if active else "Idle"
        detail = (
            "Playing through the speaker."
            if active else "No active stream."
        )
        state = "active" if active else "idle"
        service_summary = _source_service_summary(
            source_id,
            service_states,
            source_intents,
        )
        if service_summary is not None and (
            not active or service_summary[0] == "unavailable"
        ):
            state, headline, detail = service_summary
            if state == "ready":
                status = "ok"
            elif state == "unavailable":
                status = "issue"
        timing: dict[str, Any] | None = None
        if spec.id == Source.AIRPLAY:
            timing = _airplay_timing(airplay, active=active)
            if active and timing["status"] in {"warn", "unknown"}:
                status = "warn"
        elif spec.id == Source.USBSINK:
            timing = _usb_timing(
                route, host_clock, mapping(inputs.get(source_id)), active=active
            )
            if active and timing["status"] in {"warn", "unknown"}:
                status = "warn"
        if active and signal_path.get("status") in {"issue", "unknown"}:
            status = str(signal_path.get("status"))
            headline = str(signal_path.get("headline"))
            detail = str(signal_path.get("detail"))
        cards.append({
            "id": source_id,
            "label": spec.display_name,
            "state": state,
            "status": status,
            "headline": headline,
            "detail": detail,
            "timing": timing,
        })
    return cards
