# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One bounded, normalized audio-health snapshot for management surfaces.

The existing AirPlay collector already owns the expensive monitoring cadence
(fan-in STATUS, shairport/Camilla journals, MPRIS, and Camilla status).  This
module composes it with cheap local outputd and mux STATUS reads plus a slow
route-claim read.  Mux owns the canonical per-source ``playing`` predicates;
the dashboard does not duplicate them.  Production starts only
:class:`AudioHealthSampler`'s thread; the AirPlay collector is sampled inline,
so the broader dashboard adds no daemon or second resident loop.

The contract deliberately separates continuity from timing.  A USB host-clock
``l2_fallback`` keeps audio playing safely, so it degrades the latency axis but
does not claim the signal path failed.  Likewise ``l0_locked`` is live clocking
state, not proof of end-to-end latency; only the route artifact can verify that
claim.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from ..local_sources.registry import local_source_lifecycles
from ..music_sources import MUSIC_SOURCE_SPECS, Source
from ..source_intent import read_source_intents
from .airplay_health import AirPlayHealthSampler, SAMPLE_INTERVAL_SEC
from .audio_incidents import IncidentStore, IssueTracker, SessionRollup
from .uds import MUX_CONTROL_SOCKET_PATH, _mux_socket_command

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ROUTE_INTERVAL_SEC = 60.0
OUTPUTD_SOCKET = "/run/jasper-outputd/control.sock"
LOCAL_STATUS_TIMEOUT_SEC = 1.0
MAX_STATUS_BYTES = 256 * 1024
FANIN_STALE_MS = 5000
OUTPUTD_STALE_MS = 3000

# Expected failures at optional/cached observability boundaries. Programming
# errors outside this set should not be hidden; a dead sampler is surfaced as
# stale by snapshot() instead of silently retrying a broken implementation.
_MONITOR_ERRORS = (
    AttributeError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)

_LABEL_TO_SOURCE = {
    spec.fanin_label: spec.id.value for spec in MUSIC_SOURCE_SPECS
}
_SOURCE_LABELS = {
    spec.id.value: spec.display_name for spec in MUSIC_SOURCE_SPECS
}
_SOURCE_HEALTH_UNITS = {
    lifecycle.source.value: lifecycle.health_units
    for lifecycle in local_source_lifecycles()
}
_SOURCE_OFF_DRIFT_UNITS = {
    lifecycle.source.value: lifecycle.park_units
    for lifecycle in local_source_lifecycles()
}
_SOURCE_PRIMARY_UNITS = {
    lifecycle.source.value: (
        lifecycle.intent_unit
        or (lifecycle.runtime_units[0] if lifecycle.runtime_units else None)
    )
    for lifecycle in local_source_lifecycles()
}


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_local_status(
    socket_path: str = OUTPUTD_SOCKET,
    timeout_sec: float = LOCAL_STATUS_TIMEOUT_SEC,
    max_bytes: int = MAX_STATUS_BYTES,
) -> dict[str, Any] | None:
    """Read one local daemon STATUS response, byte/time bounded and fail-soft."""
    try:
        deadline = time.monotonic() + timeout_sec
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_sec)
            sock.connect(socket_path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(min(8192, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        return None
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_mux_status(
    socket_path: str = MUX_CONTROL_SOCKET_PATH,
    timeout_sec: float = LOCAL_STATUS_TIMEOUT_SEC,
) -> dict[str, Any] | None:
    """Read mux's already-normalized source activity over its local UDS."""
    try:
        return asyncio.run(
            _mux_socket_command(
                "STATUS",
                socket_path=socket_path,
                timeout=timeout_sec,
            )
        )
    except _MONITOR_ERRORS:
        logger.debug("audio health mux STATUS probe failed", exc_info=True)
        return None


def read_route_claim() -> dict[str, Any]:
    """Read the declared route plus its measured latency artifact.

    This is file/config work rather than a live audio probe and therefore runs
    on the slow cadence.  The artifact assessment is shared with ``/state`` via
    :func:`jasper.control.state_aggregate.route_latency_artifact_state`.
    """
    try:
        from ..audio_runtime_plan import build_audio_runtime_plan_from_system
        from .state_aggregate import route_latency_artifact_state

        plan = build_audio_runtime_plan_from_system()
        profile = plan.route_profile
        return {
            "status": "available",
            "route_id": profile.route_id,
            "source_id": profile.source_id,
            "fixed_sample_rate": profile.fixed_sample_rate,
            "low_latency_claim": profile.low_latency_claim,
            "route_config_hash": plan.route_config_hash,
            "p95_budget_ms": profile.p95_budget_ms,
            "p99_budget_ms": profile.p99_budget_ms,
            "artifact": route_latency_artifact_state(plan),
        }
    except _MONITOR_ERRORS as exc:
        logger.debug("audio route claim read failed", exc_info=True)
        return {
            "status": "unavailable",
            "route_id": None,
            "source_id": None,
            "fixed_sample_rate": None,
            "low_latency_claim": False,
            "route_config_hash": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "artifact": {"status": "fail", "reason": str(exc)},
        }


def _issue(
    key: str,
    *,
    scope: str,
    impact: str,
    severity: str,
    title: str,
    detail: str,
    source_id: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "scope": scope,
        "source_id": source_id,
        "impact": impact,
        "severity": severity,
        "title": title,
        "detail": detail,
    }


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return value

def _selected_source(airplay: Mapping[str, Any]) -> str | None:
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    selected = fanin.get("selected_input")
    if not isinstance(selected, str):
        return None
    normalized = selected.strip().lower()
    if normalized in _LABEL_TO_SOURCE:
        normalized = _LABEL_TO_SOURCE[normalized]
    return normalized if normalized in _SOURCE_LABELS else None


def _source_playing(
    mux_status: Mapping[str, Any] | None,
    source_id: str | None,
) -> bool | None:
    """Project mux's canonical per-source activity without inventing fallback."""
    if source_id is None or not isinstance(mux_status, Mapping):
        return None
    source = _mapping(_mapping(mux_status.get("sources")).get(source_id))
    playing = source.get("playing")
    return playing if isinstance(playing, bool) else None


def _active_source(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> str | None:
    selected = _selected_source(airplay)
    return selected if _source_playing(mux_status, selected) is True else None


def _activity_truth_unknown(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> bool:
    """Whether mux cannot authoritatively classify the selected lane."""
    if not isinstance(mux_status, Mapping) or not isinstance(
        mux_status.get("sources"),
        Mapping,
    ):
        return True
    selected = _selected_source(airplay)
    return selected is not None and _source_playing(mux_status, selected) is None


def _activity_unavailable_signal() -> dict[str, str]:
    return {
        "status": "unknown",
        "headline": "Playback activity unavailable",
        "detail": "JTS could not read the mux's canonical source state.",
    }


def _signal_path(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
) -> dict[str, Any]:
    current = _mapping(airplay.get("current"))
    fanin_raw = current.get("fanin")
    warmup = bool(airplay.get("warmup_active"))
    if not isinstance(fanin_raw, Mapping):
        if warmup:
            return {
                "status": "idle",
                "headline": "Audio is starting",
                "detail": "Waiting for the shared audio path.",
            }
        return {
            "status": "unknown",
            "headline": "Audio path unavailable",
            "detail": "Fan-in is not reporting health.",
        }
    if outputd is None:
        if warmup:
            return {
                "status": "idle",
                "headline": "Audio is starting",
                "detail": "Waiting for the final output stage.",
            }
        return {
            "status": "issue",
            "headline": "Final output unavailable",
            "detail": "The shared path is running, but outputd is not reporting.",
        }

    outputd_map = _mapping(outputd)
    backend = outputd_map.get("backend")
    if backend is not None and backend != "alsa":
        return {
            "status": "issue",
            "headline": "Final audio output is not active",
            "detail": f"Outputd reports backend {backend!r} instead of ALSA.",
        }
    outputd_watchdog = _mapping(outputd_map.get("watchdog"))
    outputd_progress_age = _as_int(
        outputd_watchdog.get("last_progress_age_ms"),
    )
    if outputd_watchdog and outputd_progress_age > OUTPUTD_STALE_MS:
        return {
            "status": "issue",
            "headline": "Final audio output has stopped progressing",
            "detail": "Outputd's work-loop watchdog is stale.",
        }

    fanin = _mapping(fanin_raw)
    watchdog = _mapping(fanin.get("watchdog"))
    if _as_int(watchdog.get("last_progress_age_ms")) > FANIN_STALE_MS:
        return {
            "status": "issue",
            "headline": "Audio path has stopped progressing",
            "detail": "Fan-in's watchdog is stale.",
        }
    active = active_source
    inputs = _mapping(fanin.get("inputs"))
    active_input = _mapping(inputs.get(active)) if active else {}
    if active and active_input.get("present") is False:
        return {
            "status": "issue",
            "headline": "Active audio input is unavailable",
            "detail": f"{_SOURCE_LABELS.get(active, 'The source')} has no fan-in lane.",
        }
    if active_input.get("health") == "broken":
        return {
            "status": "issue",
            "headline": "Active audio input is unavailable",
            "detail": f"{_SOURCE_LABELS.get(active or '', 'The source')} stopped capturing.",
        }
    frames_per_sec = active_input.get("frames_per_sec")
    if (
        active
        and isinstance(frames_per_sec, (int, float))
        and not isinstance(frames_per_sec, bool)
        and frames_per_sec < 1000.0
    ):
        return {
            "status": "issue",
            "headline": "Active audio input is not flowing",
            "detail": (
                f"{_SOURCE_LABELS.get(active, active)} is selected, but its "
                "fan-in lane has stopped advancing."
            ),
        }
    tts = _mapping(outputd_map.get("tts"))
    pending_frames = _as_int(tts.get("pending_frames"))
    budget_frames = _as_int(tts.get("budget_frames"))
    if (
        tts.get("enabled") is True
        and budget_frames > 0
        and pending_frames >= budget_frames
    ):
        return {
            "status": "warn",
            "headline": "Voice audio is delayed",
            "detail": "The final voice-output queue is at its pending budget.",
        }
    return {
        "status": "ok",
        "headline": "Signal path clean",
        "detail": "Fan-in and the final output stage are responding.",
    }


def _verification(route: Mapping[str, Any]) -> dict[str, Any]:
    if not bool(route.get("low_latency_claim")):
        return {
            "status": "not_applicable",
            "validated_at": None,
            "p95_ms": None,
            "p99_ms": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "issues": [],
        }
    artifact = _mapping(route.get("artifact"))
    artifact_status = artifact.get("status")
    issues = [
        str(issue)
        for issue in artifact.get("issues") or []
        if isinstance(issue, str)
    ]
    target_missed = any(
        issue in {"p95_exceeds_40ms", "p99_exceeds_42ms"}
        for issue in issues
    )
    if target_missed:
        status = "target_missed"
    else:
        status = {
            "pass": "verified",
            "warn": "partial",
        }.get(str(artifact_status), "unverified")
    return {
        "status": status,
        "validated_at": artifact.get("validated_at"),
        "p95_ms": artifact.get("p95_ms"),
        "p99_ms": artifact.get("p99_ms"),
        "p95_budget_ms": route.get("p95_budget_ms"),
        "p99_budget_ms": route.get("p99_budget_ms"),
        "issues": issues,
    }


def _usb_timing(
    route: Mapping[str, Any],
    host_clock: Mapping[str, Any] | None,
    *,
    active: bool,
) -> dict[str, Any]:
    claimed = bool(route.get("low_latency_claim"))
    verification = _verification(route)
    raw_mode = host_clock.get("ladder") if host_clock is not None else None
    mode = {
        "l0_locked": "lowest_latency",
        "l1_warn": "tracking_warn",
        "l2_fallback": "fallback",
        "probing": "checking",
        "disabled": "standard",
    }.get(str(raw_mode), "unknown")
    runtime = {"mode": mode, "raw_mode": raw_mode}

    if route.get("status") != "available":
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": "unknown",
            "headline": "USB latency state unavailable",
            "detail": "The route plan could not be read; playback health is checked separately.",
            "route_id": route.get("route_id"),
            "verification": verification,
            "runtime": runtime,
        }
    if not claimed:
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": "idle",
            "headline": "Standard buffered route",
            "detail": "This route does not make a measured low-latency claim.",
            "route_id": route.get("route_id"),
            "verification": verification,
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
        detail = "Playback is safe while the host-clock check completes."
    elif active and raw_mode not in {"l0_locked", "l1_warn", "l2_fallback"}:
        status = "warn"
        headline = "USB low-latency clock mode unavailable"
        detail = (
            "Playback continuity is monitored, but live host-clock control "
            "is disabled or not reporting."
        )
    else:
        status = "ok"
        headline = "Low latency · stable"
        if active and raw_mode == "l0_locked":
            detail = "USB is in its lowest-latency host-clock mode."
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
        "verification": verification,
        "runtime": runtime,
    }


def _airplay_timing(airplay: Mapping[str, Any], *, active: bool) -> dict[str, Any]:
    if not active:
        status = "idle"
        headline = "AirPlay idle"
        detail = "Sync timing is checked while AirPlay is playing."
    else:
        recent = _mapping(airplay.get("summary_5m"))
        sync_events = (
            _as_int(recent.get("shairport_packet_drops"))
            + _as_int(recent.get("shairport_sync_errors"))
            + _as_int(recent.get("shairport_underruns"))
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
        "verification": {
            "status": "not_applicable",
            "validated_at": None,
            "p95_ms": None,
            "p99_ms": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "issues": [],
        },
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
        "verification": {
            "status": "not_applicable",
            "validated_at": None,
            "p95_ms": None,
            "p99_ms": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "issues": [],
        },
        "runtime": {"mode": "standard", "raw_mode": None},
    }


def _state_issues(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    signal_path: Mapping[str, Any],
    latency: Mapping[str, Any],
    active_source: str | None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    warmup = bool(airplay.get("warmup_active"))
    current = _mapping(airplay.get("current"))
    fanin = current.get("fanin")
    if signal_path.get("headline") == "Playback activity unavailable":
        issues.append(_issue(
            "monitor.mux_status_unavailable",
            scope="monitor",
            impact="observability",
            severity="issue",
            title="Playback activity unavailable",
            detail="JTS could not read the mux's canonical source state.",
        ))
    if not warmup and not isinstance(fanin, Mapping):
        issues.append(_issue(
            "path.fanin_unavailable",
            scope="path",
            impact="continuity",
            severity="issue",
            title="Shared audio path unavailable",
            detail="Fan-in is not reporting health.",
        ))
    if not warmup and outputd is None:
        issues.append(_issue(
            "path.outputd_unavailable",
            scope="path",
            impact="continuity",
            severity="issue",
            title="Final audio output unavailable",
            detail="Outputd is not reporting health.",
        ))
    if signal_path.get("headline") == "Audio path has stopped progressing":
        issues.append(_issue(
            "path.fanin_watchdog_stale",
            scope="path",
            impact="continuity",
            severity="issue",
            title="Audio path stopped progressing",
            detail="Fan-in's watchdog is stale.",
        ))
    if signal_path.get("headline") == "Final audio output has stopped progressing":
        issues.append(_issue(
            "path.outputd_watchdog_stale",
            scope="path",
            impact="continuity",
            severity="issue",
            title="Final audio output stopped progressing",
            detail="Outputd's work-loop watchdog is stale.",
        ))
    if signal_path.get("headline") == "Final audio output is not active":
        issues.append(_issue(
            "path.outputd_backend_inactive",
            scope="path",
            impact="continuity",
            severity="issue",
            title="Final audio output is not active",
            detail=str(signal_path.get("detail") or "Outputd is not using ALSA."),
        ))
    if signal_path.get("headline") == "Voice audio is delayed":
        issues.append(_issue(
            "path.tts_queue_full",
            scope="path",
            impact="continuity",
            severity="warn",
            title="Voice audio queue is full",
            detail="Assistant audio may be delayed until the output queue drains.",
        ))
    if signal_path.get("headline") in {
        "Active audio input is unavailable",
        "Active audio input is not flowing",
    }:
        source_id = active_source
        issues.append(_issue(
            f"{source_id or 'source'}.input_unavailable",
            scope="source",
            source_id=source_id,
            impact="continuity",
            severity="issue",
            title="Active audio input is unavailable",
            detail=str(signal_path.get("detail") or "The active source stopped capturing."),
        ))
    if active_source == Source.USBSINK.value:
        raw_mode = _mapping(latency.get("runtime")).get("raw_mode")
        if raw_mode == "l2_fallback":
            issues.append(_issue(
                "usbsink.latency_fallback",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB switched to stable latency fallback",
                detail="Playback continues safely with more buffering.",
            ))
        elif raw_mode == "l1_warn":
            issues.append(_issue(
                "usbsink.clock_tracking_warn",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB clock tracking is under strain",
                detail="Low-latency playback remains locked.",
            ))
        if latency.get("status") == "unknown":
            issues.append(_issue(
                "usbsink.latency_state_unavailable",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB latency state unavailable",
                detail="The route plan could not be read.",
            ))
        elif (
            _mapping(latency.get("runtime")).get("raw_mode")
            not in {"l0_locked", "l1_warn", "l2_fallback", "probing"}
        ):
            issues.append(_issue(
                "usbsink.host_clock_unavailable",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB low-latency clock mode unavailable",
                detail="Host-clock control is disabled or not reporting.",
            ))
    for source_id, health_units in _SOURCE_HEALTH_UNITS.items():
        desired = _mapping(source_intents).get(source_id)
        units = (
            _SOURCE_OFF_DRIFT_UNITS.get(source_id, ())
            if desired is False
            else health_units
        )
        for unit in units:
            unit_state = _mapping(service_states).get(unit)
            if desired is False:
                if _mapping(unit_state).get("active_state") == "active":
                    issues.append(_issue(
                        f"{source_id}.service.{unit}.off_drift",
                        scope="source",
                        source_id=source_id,
                        impact="availability",
                        severity="issue",
                        title=(
                            f"{_SOURCE_LABELS.get(source_id, source_id)} "
                            "is running while Off"
                        ),
                        detail=(
                            f"{unit} is active despite the saved Music sources "
                            "choice. Run the source lifecycle reconciler."
                        ),
                    ))
                continue
            failure = _service_failure(unit, unit_state)
            if failure is None:
                continue
            issues.append(_issue(
                f"{source_id}.service.{unit}",
                scope="source",
                source_id=source_id,
                impact="availability",
                severity="issue",
                title=f"{_SOURCE_LABELS.get(source_id, source_id)} renderer unavailable",
                detail=failure,
            ))
    return issues


def _service_failure(unit: str, raw_state: Any) -> str | None:
    state = _mapping(raw_state)
    active_state = str(state.get("active_state") or "")
    result = str(state.get("result") or "")
    load_state = str(state.get("load_state") or "")
    if not (
        active_state == "failed"
        or load_state in {"error", "not-found"}
        or (result not in {"", "success"} and active_state != "active")
    ):
        return None
    observed = active_state or load_state or result or "failure"
    return f"{unit} reports {observed}."


def _source_service_summary(
    source_id: str,
    service_states: Mapping[str, Any] | None,
    source_intents: Mapping[str, bool] | None = None,
) -> tuple[str, str, str] | None:
    """Return ``(state, headline, detail)`` from cached systemd truth."""
    states = _mapping(service_states)
    desired = _mapping(source_intents).get(source_id)
    if desired is False:
        active_units = [
            unit for unit in _SOURCE_OFF_DRIFT_UNITS.get(source_id, ())
            if _mapping(states.get(unit)).get("active_state") == "active"
        ]
        if active_units:
            return (
                "unavailable",
                f"{_SOURCE_LABELS.get(source_id, source_id)} is running while Off",
                "Unexpected active services: " + ", ".join(active_units) + ".",
            )
        return "off", "Off", "Turned off in Music sources."
    if not states:
        return None
    for unit in _SOURCE_HEALTH_UNITS.get(source_id, ()):
        failure = _service_failure(unit, states.get(unit))
        if failure is not None:
            return (
                "unavailable",
                f"{_SOURCE_LABELS.get(source_id, source_id)} unavailable",
                failure,
            )
    primary = _SOURCE_PRIMARY_UNITS.get(source_id)
    primary_state = _mapping(states.get(primary)) if primary else {}
    if primary_state.get("active_state") == "active":
        return "ready", "Ready", "Waiting for a stream."
    if primary_state.get("active_state") == "inactive":
        return "not_running", "Not running", "No active renderer process."
    return None


def _source_cards(
    airplay: Mapping[str, Any],
    signal_path: Mapping[str, Any],
    route: Mapping[str, Any],
    active_source: str | None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    host_clock = _mapping(fanin.get("host_clock")) or None
    cards: list[dict[str, Any]] = []
    for spec in MUSIC_SOURCE_SPECS:
        source_id = spec.id.value
        active = active_source == source_id
        status = "ok" if active else "idle"
        headline = "Playing" if active else "Idle"
        detail = (
            "Using the shared audio path."
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
            timing = _usb_timing(route, host_clock, active=active)
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


def _detail(label: str, value: Any) -> dict[str, str]:
    return {"label": label, "value": str(value)}


def _duration_label(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 1.0:
        return f"{round(seconds * 1000):d} ms"
    if seconds < 60.0:
        return f"{round(seconds):d} sec"
    minutes = int(seconds // 60)
    remainder = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s" if remainder else f"{minutes} min"
    hours = int(minutes // 60)
    return f"{hours}h {minutes % 60}m"


def _fresh_dac_delay_ms(dac: Mapping[str, Any]) -> float | None:
    delay = _finite_number(dac.get("snd_pcm_delay_ms"))
    age = _finite_number(dac.get("snd_pcm_delay_sample_age_ms"))
    if (
        delay is None
        or age is None
        or float(delay) < 0.0
        or float(age) < 0.0
        or float(age) > OUTPUTD_STALE_MS
    ):
        return None
    return float(delay)


def _incident_context(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
) -> dict[str, Any]:
    """Capture only the evidence rendered on a persisted incident."""
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    source_input = (
        _mapping(_mapping(fanin.get("inputs")).get(active_source))
        if active_source is not None else {}
    )
    output = _mapping(_mapping(outputd).get("dac"))
    return {
        "clock_mode": _mapping(fanin.get("host_clock")).get("ladder"),
        "input": {"rms_dbfs": source_input.get("rms_dbfs")},
        "output": {"snd_pcm_delay_ms": _fresh_dac_delay_ms(output)},
    }


def _receiver_latency(
    active_source: str,
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any],
    timing: Mapping[str, Any],
) -> dict[str, Any]:
    """Present a lower bound from the already-sampled JTS queues.

    This deliberately is neither a whole receiver-path estimate nor an
    end-to-end claim: unreported stages, source transport, sender buffering,
    and acoustic propagation are outside the sampled telemetry.
    """
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    output = _mapping(fanin.get("output"))
    source_input = _mapping(_mapping(fanin.get("inputs")).get(active_source))
    resampler = _mapping(source_input.get("resampler"))
    camilla = _mapping(current.get("camilla"))
    dac = _mapping(_mapping(outputd).get("dac"))
    rate = (
        _as_int(output.get("sample_rate"))
        or _as_int(route.get("fixed_sample_rate"))
        or _as_int(dac.get("sample_rate"))
    )
    components: list[tuple[str, float]] = []
    if rate > 0 and active_source == Source.USBSINK.value:
        fill = _finite_number(resampler.get("fill_frames"))
        if fill is not None and float(fill) >= 0.0:
            components.append(("USB input queue", float(fill) * 1000.0 / rate))
    fanin_delay = _finite_number(output.get("snd_pcm_delay_ms"))
    if fanin_delay is not None and float(fanin_delay) >= 0.0:
        components.append(("Fan-in output queue", float(fanin_delay)))
    capture_rate = _as_int(camilla.get("capture_rate")) or rate
    camilla_frames = _finite_number(camilla.get("buffer_level"))
    if (
        capture_rate > 0
        and camilla_frames is not None
        and float(camilla_frames) >= 0.0
    ):
        components.append((
            "DSP queue",
            float(camilla_frames) * 1000.0 / capture_rate,
        ))
    dac_delay = _fresh_dac_delay_ms(dac)
    if dac_delay is not None:
        components.append(("DAC presentation queue", float(dac_delay)))

    mode = str(_mapping(timing.get("runtime")).get("raw_mode") or "")
    mode_label = {
        "l0_locked": "low latency stable",
        "l1_warn": "clock adjusting",
        "l2_fallback": "stable fallback",
        "probing": "timing check in progress",
    }.get(mode)
    details = [
        _detail(label, f"{value:.1f} ms")
        for label, value in components
    ]
    estimate: dict[str, float] | None = None
    if components:
        total = sum(value for _label, value in components)
        lower = int(max(0.0, total) * 10.0) / 10.0
        estimate = {"lower_ms": lower}
        summary = f"At least {lower:g} ms in observed JTS queues"
    else:
        summary = "Live queue timing unavailable"
    if active_source == Source.USBSINK.value and mode_label:
        summary = f"{summary} · {mode_label}"
    details.append(_detail("Scope", "Observed JTS queues only"))
    return {
        "summary": summary,
        "detail": (
            "A lower bound from the queues JTS can observe; excludes USB gadget "
            "dwell, unreported processing, sender transport, and acoustic delay."
        ),
        "details": details,
        "estimate": estimate,
        "mode": mode or None,
    }


def _current_stream(
    *,
    active_source: str | None,
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any],
    timing: Mapping[str, Any],
    sampled_at: float,
    session: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if active_source is None:
        return None
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    source_input = _mapping(_mapping(fanin.get("inputs")).get(active_source))
    resampler = _mapping(source_input.get("resampler"))
    camilla = _mapping(current.get("camilla"))
    dac = _mapping(_mapping(outputd).get("dac"))
    session_state = _mapping(session)
    session_start = session_state.get("started_at") or sampled_at
    stream: dict[str, Any] = {
        "source_id": active_source,
        "label": _SOURCE_LABELS.get(active_source, active_source),
        "started_at": session_start,
    }
    if resampler or camilla:
        stream["processing"] = {
            "summary": (
                "Adaptive resampling · shared DSP"
                if resampler else "Shared DSP path"
            ),
            "detail": "Configured processing route for this stream.",
            "details": [
                _detail("DSP rate", f"{_as_int(camilla.get('capture_rate')):,} Hz")
            ] if _as_int(camilla.get("capture_rate")) else [],
        }
    if session_state:
        stream["session"] = dict(session_state)
    if active_source == Source.USBSINK.value:
        stream["latency"] = _receiver_latency(
            active_source,
            airplay,
            outputd,
            route,
            timing,
        )
    elif active_source == Source.AIRPLAY.value:
        airplay_timing = _airplay_timing(airplay, active=True)
        stream["latency"] = {
            "summary": airplay_timing["headline"],
            "detail": airplay_timing["detail"],
            "details": [],
        }
    if active_source == Source.USBSINK.value:
        rate = _as_int(route.get("fixed_sample_rate"))
        if rate:
            stream["media"] = {
                "summary": f"{rate / 1000:g} kHz · Stereo PCM",
                "detail": "The format advertised by JTS to the connected USB host.",
                "details": [],
            }
    output_rate = _as_int(dac.get("sample_rate"))
    output_details: list[dict[str, str]] = []
    dac_delay = _fresh_dac_delay_ms(dac)
    if dac_delay is not None:
        output_details.append(_detail(
            "DAC queue",
            f"{dac_delay:.1f} ms",
        ))
    if outputd is not None and _mapping(outputd).get("backend") == "alsa" and dac:
        stream["output"] = {
            "summary": (
                f"{output_rate / 1000:g} kHz final output"
                if output_rate else "Final output reporting"
            ),
            "detail": "Post-DSP audio at the physical output stage.",
            "details": output_details,
        }
    rms = _finite_number(source_input.get("rms_dbfs"))
    if rms is not None:
        stream["signal"] = {
            "summary": f"{float(rms):.1f} dBFS recent signal level",
            "detail": "The most recent level observed at the active source lane.",
            "details": [],
        }
    return stream


def _incident_impact(issue: Mapping[str, Any]) -> str:
    return {
        "continuity": "Audio may have briefly interrupted.",
        "latency": "Audio continued with higher latency.",
        "sync": "Playback may have briefly lost synchronization.",
        "quality": "Audio may have briefly distorted.",
        "availability": "This source may not be available.",
        "observability": "JTS could not confirm current audio health.",
    }.get(str(issue.get("impact")), "Audio quality may have been affected.")


def _likely_area(issue: Mapping[str, Any]) -> str:
    key = str(issue.get("key") or "")
    if key.startswith("path.outputd"):
        return "Final output stage"
    if key.startswith("path.fanin") or key.startswith("path.camilla"):
        return "Shared processing path"
    if key.startswith("airplay"):
        return "AirPlay transport and synchronization"
    if key.startswith("usbsink.latency") or key.startswith("usbsink.clock"):
        return "USB host timing"
    source_id = issue.get("source_id")
    if isinstance(source_id, str):
        return f"{_SOURCE_LABELS.get(source_id, source_id)} source"
    return "Audio monitoring"


def _incident_evidence(issue: Mapping[str, Any]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    context = _mapping(_mapping(issue.get("context")).get("started"))
    if context.get("clock_mode"):
        evidence.append(_detail("Clock mode", context["clock_mode"]))
    input_context = _mapping(context.get("input"))
    if _finite_number(input_context.get("rms_dbfs")) is not None:
        evidence.append(_detail(
            "Input level",
            f"{float(input_context['rms_dbfs']):.1f} dBFS",
        ))
    output_context = _mapping(context.get("output"))
    if _finite_number(output_context.get("snd_pcm_delay_ms")) is not None:
        evidence.append(_detail(
            "DAC queue",
            f"{float(output_context['snd_pcm_delay_ms']):.1f} ms",
        ))
    return evidence


def _timestamp(value: Any, default: float) -> float:
    number = _finite_number(value)
    return float(number) if number is not None else default


def _incident_duration(issue: Mapping[str, Any], now: float) -> float:
    observed = _finite_number(issue.get("observed_seconds"))
    if observed is not None:
        return max(0.0, float(observed))
    started = _timestamp(issue.get("started_at"), now)
    end = _timestamp(issue.get("recovered_at"), now)
    return max(0.0, end - started)


def _present_incident(
    issue: Mapping[str, Any],
    now: float,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    started = _timestamp(issue.get("started_at"), now)
    duration = _incident_duration(issue, now)
    cutoff = now - 1800.0
    matching = [
        item for item in history
        if item.get("key") == issue.get("key")
        and _timestamp(
            item.get("last_occurrence_at")
            or item.get("last_seen_at")
            or item.get("started_at"),
            0.0,
        ) >= cutoff
    ]
    recurrence: dict[str, Any] | None = None
    if matching:
        # A coalesced record retains first/last occurrence plus total count,
        # not every timestamp. If it straddles the window boundary, only its
        # last occurrence is provably inside, so expose a lower bound.
        count = sum(
            max(1, _as_int(item.get("count"), 1))
            if _timestamp(
                item.get("first_occurrence_at") or item.get("started_at"),
                0.0,
            ) >= cutoff
            else 1
            for item in matching
        )
        known_firsts = []
        for item in matching:
            item_first = _timestamp(
                item.get("first_occurrence_at") or item.get("started_at"),
                now,
            )
            item_last = _timestamp(
                item.get("last_occurrence_at")
                or item.get("last_seen_at")
                or item.get("started_at"),
                now,
            )
            known_firsts.append(item_first if item_first >= cutoff else item_last)
        first_at = min(known_firsts)
        last_at = max(
            _timestamp(
                item.get("last_occurrence_at")
                or item.get("last_seen_at")
                or item.get("started_at"),
                now,
            )
            for item in matching
        )
        recurrence = {
            "count": count,
            "first_at": first_at,
            "last_at": last_at,
            "window_seconds": 1800.0,
            "count_is_lower_bound": True,
            "summary": (
                f"At least {count} occurrence"
                f"{'s' if count != 1 else ''} observed in 30 min"
            ),
        }
    key = str(issue.get("key") or "audio.issue")
    presented = {
        "id": f"{key}:{started:.3f}",
        "key": key,
        "status": issue.get("status"),
        "severity": issue.get("severity"),
        "title": issue.get("title"),
        "detail": issue.get("detail"),
        "source_id": issue.get("source_id"),
        "started_at": started,
        "last_seen_at": issue.get("last_seen_at"),
        "recovered_at": issue.get("recovered_at"),
        "count": max(1, _as_int(issue.get("count"), 1)),
        "impact": _incident_impact(issue),
        "observed": str(issue.get("detail") or "JTS observed an audio-path change."),
        "likely_area": _likely_area(issue),
        "evidence": _incident_evidence(issue),
    }
    if recurrence is not None and recurrence["count"] > 1:
        presented["recurrence"] = recurrence
    if issue.get("status") == "recovered" and duration > 0.0:
        presented["duration_seconds"] = round(duration, 1)
        presented["duration_label"] = _duration_label(duration)
    return presented


def _incident_priority(
    issue: Mapping[str, Any],
    active_source: str | None,
) -> tuple[int, int, float]:
    relevant = _incident_is_relevant(issue, active_source)
    return (
        1 if relevant else 0,
        1 if issue.get("severity") == "issue" else 0,
        _timestamp(issue.get("last_seen_at"), 0.0),
    )


def _incident_is_relevant(
    issue: Mapping[str, Any],
    active_source: str | None,
) -> bool:
    source_id = issue.get("source_id")
    return (
        issue.get("scope") in {"path", "monitor"}
        or source_id is None
        or (active_source is not None and source_id == active_source)
    )


def compose_audio_health(
    *,
    airplay: Mapping[str, Any] | None,
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any] | None,
    issues: list[dict[str, Any]],
    sampled_at: float,
    previous_overall: Mapping[str, Any] | None = None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
    session: Mapping[str, Any] | None = None,
    mux_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the public, presentation-ready audio-health contract."""
    ap = _mapping(airplay)
    route_state = _mapping(route)
    mux = mux_status if mux_status is not None else _mapping(ap.get("mux_status"))
    active_source = _active_source(ap, mux)
    activity_unknown = _activity_truth_unknown(ap, mux)
    signal_path = _signal_path(ap, outputd, active_source)
    if activity_unknown:
        signal_path = _activity_unavailable_signal()
    current = _mapping(ap.get("current"))
    fanin = _mapping(current.get("fanin"))
    host_clock = _mapping(fanin.get("host_clock")) or None
    if active_source == Source.USBSINK.value:
        latency = _usb_timing(route_state, host_clock, active=True)
    else:
        latency = _not_applicable_timing()
    source_cards = _source_cards(
        ap,
        signal_path,
        route_state,
        active_source,
        service_states,
        source_intents,
    )
    unavailable_sources = [
        str(source.get("label") or source.get("id"))
        for source in source_cards
        if source.get("status") == "issue"
        and source.get("id") == active_source
    ]

    path_status = str(signal_path.get("status") or "unknown")
    if path_status in {"issue", "unknown"}:
        overall_status = path_status
        headline = str(signal_path.get("headline"))
        detail = str(signal_path.get("detail"))
    elif unavailable_sources:
        overall_status = "warn"
        headline = "A playback source needs attention"
        detail = f"Unavailable: {', '.join(unavailable_sources)}."
    elif path_status == "warn":
        overall_status = "warn"
        headline = "Audio is playing" if active_source else str(signal_path.get("headline"))
        detail = str(signal_path.get("headline") if active_source else signal_path.get("detail"))
    elif path_status == "idle":
        overall_status = "idle"
        headline = str(signal_path.get("headline"))
        detail = str(signal_path.get("detail"))
    elif active_source is None:
        overall_status = "idle"
        headline = "Audio is ready"
        detail = "No source is playing."
    elif latency.get("status") in {"warn", "unknown"}:
        overall_status = "warn"
        headline = "Audio is playing"
        detail = str(latency.get("headline"))
    else:
        overall_status = "ok"
        headline = "Audio is playing"
        detail = (
            f"{_SOURCE_LABELS.get(active_source, active_source)} · signal path clean."
        )

    previous = _mapping(previous_overall)
    same_overall = (
        previous.get("status") == overall_status
        and previous.get("headline") == headline
        and previous.get("active_source") == active_source
    )
    since = previous.get("since") if same_overall else sampled_at
    overall = {
        "status": overall_status,
        "headline": headline,
        "detail": detail,
        "active_source": active_source,
        "since": since,
    }
    ongoing_issues = [
        issue for issue in issues
        if issue.get("status") == "ongoing"
        and _incident_is_relevant(issue, active_source)
    ]
    ongoing = max(
        ongoing_issues,
        key=lambda issue: _incident_priority(issue, active_source),
        default=None,
    )
    current_incident = (
        _present_incident(ongoing, sampled_at, issues)
        if ongoing is not None else None
    )
    secondary_ongoing = sorted(
        (issue for issue in ongoing_issues if issue is not ongoing),
        key=lambda issue: _incident_priority(issue, active_source),
        reverse=True,
    )
    recovered = [
        issue for issue in issues if issue.get("status") == "recovered"
    ]
    recent_incidents = [
        _present_incident(issue, sampled_at, issues)
        for issue in (*secondary_ongoing, *recovered)
    ][:5]
    current_stream = _current_stream(
        active_source=active_source,
        airplay=ap,
        outputd=outputd,
        route=route_state,
        timing=latency,
        sampled_at=sampled_at,
        session=session,
    )
    if activity_unknown:
        selected = _selected_source(ap)
        current_stream = {
            "source_id": selected,
            "label": _SOURCE_LABELS.get(selected or "", "Audio activity"),
            "signal": {
                "summary": "Playback state unavailable",
                "detail": "Waiting for a fresh source state from the mux.",
                "details": [],
            },
        }
        if session is not None:
            current_stream["session"] = dict(session)
    return {
        "schema_version": SCHEMA_VERSION,
        "sampled_at": sampled_at,
        "overall": overall,
        "signal_path": signal_path,
        "latency": latency,
        "sources": source_cards,
        "issues": copy.deepcopy(issues),
        "current_stream": current_stream,
        "current_incident": current_incident,
        "recent_incidents": recent_incidents,
        "technical": {
            "sampler": {
                "last_sample_at": ap.get("last_sample_at"),
                "warmup_active": bool(ap.get("warmup_active")),
                "suppressed_reason": ap.get("suppressed_reason"),
            },
            "fanin": {
                "available": bool(fanin.get("available")),
                "input_buffer_frames": fanin.get("input_buffer_frames"),
                "output_buffer_frames": fanin.get("output_buffer_frames"),
                "inputs": copy.deepcopy(fanin.get("inputs")),
                "host_clock": copy.deepcopy(fanin.get("host_clock")),
                "watchdog": copy.deepcopy(fanin.get("watchdog")),
            },
            "outputd": {
                "available": outputd is not None,
                "mix": copy.deepcopy(_mapping(outputd).get("mix")),
                "content": copy.deepcopy(_mapping(outputd).get("content")),
                "dac": copy.deepcopy(_mapping(outputd).get("dac")),
                "tts": copy.deepcopy(_mapping(outputd).get("tts")),
            },
            "route_verification": {
                "route_id": route_state.get("route_id"),
                "route_config_hash": route_state.get("route_config_hash"),
                **_verification(route_state),
            },
            "airplay": {
                "status": ap.get("status"),
                "reason": ap.get("reason"),
                "mpris": copy.deepcopy(current.get("mpris")),
                "camilla": copy.deepcopy(current.get("camilla")),
                "summary_5m": copy.deepcopy(ap.get("summary_5m")),
                "summary_30m": copy.deepcopy(ap.get("summary_30m")),
                "storm": copy.deepcopy(ap.get("storm")),
            },
        },
    }


class AudioHealthSampler:
    """The one production audio-health loop, with bounded in-memory history."""

    def __init__(
        self,
        *,
        sample_interval_sec: float = SAMPLE_INTERVAL_SEC,
        route_interval_sec: float = ROUTE_INTERVAL_SEC,
        airplay_sampler: AirPlayHealthSampler | Any | None = None,
        outputd_probe: Callable[[], dict[str, Any] | None] | None = None,
        mux_probe: Callable[[], dict[str, Any] | None] | None = None,
        route_probe: Callable[[], dict[str, Any]] | None = None,
        service_probe: Callable[[], dict[str, dict[str, Any]]] | None = None,
        incident_store: IncidentStore | None = None,
        time_fn: Callable[[], float] = time.time,
        camilla_host: str = "127.0.0.1",
        camilla_port: int = 1234,
    ) -> None:
        self._sample_interval = sample_interval_sec
        self._route_interval = route_interval_sec
        self._time = time_fn
        self._airplay = airplay_sampler or AirPlayHealthSampler(
            sample_interval_sec=sample_interval_sec,
            camilla_host=camilla_host,
            camilla_port=camilla_port,
            time_fn=time_fn,
        )
        self._outputd_probe = outputd_probe or _read_local_status
        self._mux_probe = mux_probe or _read_mux_status
        self._route_probe = route_probe or read_route_claim
        self._service_probe = service_probe
        observation_gap = max(15.0, sample_interval_sec * 3.0)
        self._issues = IssueTracker(
            store=incident_store,
            max_observation_gap_sec=observation_gap,
        )
        self._session = SessionRollup(
            max_observation_gap_sec=observation_gap,
        )
        self._outputd: dict[str, Any] | None = None
        self._route: dict[str, Any] | None = None
        self._service_states: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._last_route_sample_at = 0.0
        self._previous_input_xruns: dict[str, int] | None = None
        self._previous_fanin_pings_skipped: int | None = None
        self._previous_outputd_xruns: dict[str, int] | None = None
        self._previous_outputd_clipped: int | None = None
        self._seen_raw_events: deque[tuple[Any, ...]] = deque(maxlen=40)
        self._seen_raw_event_set: set[tuple[Any, ...]] = set()
        self._lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="jasper-audio-health-sampler",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stopped = True

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        if snapshot is None:
            return None
        sampled_at = snapshot.get("sampled_at")
        stale_after = max(15.0, self._sample_interval * 3.0)
        if (
            isinstance(sampled_at, (int, float))
            and self._time() - float(sampled_at) > stale_after
        ):
            stale_since = float(sampled_at) + stale_after
            snapshot["overall"] = {
                "status": "unknown",
                "headline": "Audio monitor is stale",
                "detail": "The last health sample is no longer current.",
                "active_source": _mapping(snapshot.get("overall")).get(
                    "active_source",
                ),
                "since": stale_since,
            }
            snapshot["signal_path"] = {
                "status": "unknown",
                "headline": "Audio health unavailable",
                "detail": "The monitor has not completed a fresh sample.",
            }
            stale_issue = {
                "key": "monitor.sample_stale",
                "scope": "monitor",
                "source_id": None,
                "impact": "observability",
                "severity": "issue",
                "title": "Audio monitor is stale",
                "detail": "Current audio health cannot be confirmed.",
                "status": "ongoing",
                "started_at": stale_since,
                "last_seen_at": self._time(),
                "recovered_at": None,
                "count": 1,
                "first_occurrence_at": stale_since,
                "last_occurrence_at": stale_since,
            }
            issues = list(snapshot.get("issues") or [])
            issues.insert(0, stale_issue)
            snapshot["issues"] = issues
            previous_stream = _mapping(snapshot.get("current_stream"))
            source_id = previous_stream.get("source_id") or _mapping(
                snapshot.get("overall")
            ).get("active_source")
            snapshot["current_stream"] = {
                "source_id": source_id,
                "label": previous_stream.get("label") or "Audio",
                "started_at": stale_since,
                "signal": {
                    "summary": "Current stream details unavailable",
                    "detail": "The audio monitor has not completed a fresh sample.",
                    "details": [],
                },
            }
            snapshot["current_incident"] = _present_incident(
                stale_issue,
                self._time(),
                issues,
            )
        return snapshot

    def airplay_snapshot(self) -> dict[str, Any]:
        """Compatibility surface for the existing ``airplay_health`` payload."""
        return self._airplay.snapshot()

    def outputd_snapshot(self) -> dict[str, Any] | None:
        """Reuse the cached outputd observation in ``/system/snapshot``."""
        with self._lock:
            return copy.deepcopy(self._outputd)

    def _run(self) -> None:
        while not self._stopped:
            started = time.monotonic()
            try:
                self._tick()
            except _MONITOR_ERRORS:
                logger.exception("audio health sampler tick failed")
            elapsed = time.monotonic() - started
            time.sleep(max(0.1, self._sample_interval - elapsed))

    def _tick(self) -> None:
        now = self._time()
        self._airplay.sample_once()
        airplay = self._airplay.snapshot()
        try:
            outputd = self._outputd_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health outputd probe failed", exc_info=True)
            outputd = None
        try:
            mux_status = self._mux_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health mux STATUS probe failed", exc_info=True)
            mux_status = None
        if mux_status is None and isinstance(airplay.get("mux_status"), Mapping):
            # Explicit fixture/injected observation seam; production AirPlay
            # snapshots do not carry mux state and therefore still fail closed.
            mux_status = dict(airplay["mux_status"])
        if self._service_probe is not None:
            try:
                service_states = self._service_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health service-state probe failed", exc_info=True)
            else:
                if isinstance(service_states, dict):
                    self._service_states = service_states
        if (
            self._route is None
            or now - self._last_route_sample_at >= self._route_interval
        ):
            try:
                route = self._route_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health route probe failed", exc_info=True)
                route = {"status": "unavailable", "low_latency_claim": False}
            self._route = route if isinstance(route, dict) else None
            self._last_route_sample_at = now

        active_source = _active_source(airplay, mux_status)
        activity_unknown = _activity_truth_unknown(airplay, mux_status)
        selected_source = _selected_source(airplay)
        if activity_unknown:
            if (
                self._session.source_id is not None
                and selected_source != self._session.source_id
            ):
                self._session.reset(None, now)
        elif active_source != self._session.source_id:
            self._session.reset(active_source, now)
        context = _incident_context(airplay, outputd, active_source)
        try:
            intents = {
                source.value: enabled
                for source, enabled in read_source_intents().items()
            }
        except RuntimeError:
            logger.debug("audio health source-intent probe failed", exc_info=True)
            intents = None
        signal_path = _signal_path(airplay, outputd, active_source)
        if activity_unknown:
            signal_path = _activity_unavailable_signal()
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        host_clock = _mapping(fanin.get("host_clock")) or None
        if active_source == Source.USBSINK.value:
            latency = _usb_timing(_mapping(self._route), host_clock, active=True)
        else:
            latency = _not_applicable_timing()
        state_issues = _state_issues(
            airplay,
            outputd,
            signal_path,
            latency,
            active_source,
            self._service_states,
            intents,
        )
        tracked_state_issues = [
            issue for issue in state_issues
            if not (
                issue.get("impact") == "availability"
                and issue.get("source_id") != active_source
            )
        ]
        with self._issues.batch(now):
            self._record_raw_events(airplay, now=now)
            clipping_issue = self._record_counter_events(
                airplay,
                outputd,
                now,
                context=context,
            )
            if clipping_issue is not None:
                tracked_state_issues.append(clipping_issue)
            self._issues.update(
                tracked_state_issues,
                now,
                context=context,
            )
        self._session.observe_state(tracked_state_issues, now)
        with self._lock:
            previous_overall = (
                self._snapshot.get("overall")
                if isinstance(self._snapshot, dict)
                else None
            )
            self._outputd = copy.deepcopy(outputd)
            self._snapshot = compose_audio_health(
                airplay=airplay,
                outputd=outputd,
                route=self._route,
                issues=self._issues.snapshot(),
                sampled_at=now,
                previous_overall=previous_overall,
                service_states=self._service_states,
                source_intents=intents,
                session=self._session.snapshot(now),
                mux_status=mux_status,
            )

    def _record_point(
        self,
        candidate: dict[str, Any],
        when: float,
        *,
        count: int,
        context: Mapping[str, Any] | None,
        observed_at: float | None = None,
    ) -> None:
        self._issues.record_point(
            candidate,
            when,
            count=count,
            context=context,
            observed_at=observed_at,
        )
        self._session.record_point(candidate, when, count=count)

    def _record_raw_events(
        self,
        airplay: Mapping[str, Any],
        *,
        now: float,
    ) -> None:
        for raw in airplay.get("events") or []:
            if not isinstance(raw, Mapping):
                continue
            fingerprint = (
                raw.get("ts"), raw.get("type"), raw.get("count"), raw.get("detail")
            )
            if fingerprint in self._seen_raw_event_set:
                continue
            if len(self._seen_raw_events) == self._seen_raw_events.maxlen:
                oldest = self._seen_raw_events.popleft()
                self._seen_raw_event_set.discard(oldest)
            self._seen_raw_events.append(fingerprint)
            self._seen_raw_event_set.add(fingerprint)
            event_type = str(raw.get("type") or "")
            if event_type == "camilla_short_read":
                # Documented inaudible recovered partials are technical evidence,
                # not a household issue. A playback underrun is surfaced below.
                continue
            if event_type in {"fanin_output_xrun", "camilla_playback_underrun"}:
                candidate = _issue(
                    f"path.{event_type}",
                    scope="path",
                    impact="continuity",
                    severity="issue",
                    title=str(raw.get("title") or "Audio path recovered"),
                    detail=str(raw.get("detail") or "The shared path recovered."),
                )
            elif event_type.startswith("shairport_") or event_type == "fanin_airplay_xrun":
                impact = "sync" if event_type in {
                    "shairport_packet_drop",
                    "shairport_oos",
                    "shairport_sync_positive",
                    "shairport_sync_negative",
                    "shairport_offset_too_short",
                } else "continuity"
                candidate = _issue(
                    f"airplay.{event_type}",
                    scope="source",
                    source_id=Source.AIRPLAY.value,
                    impact=impact,
                    severity=(
                        "issue" if raw.get("severity") == "issue" else "warn"
                    ),
                    title=str(raw.get("title") or "AirPlay recovered"),
                    detail=str(raw.get("detail") or "AirPlay recovered."),
                )
            else:
                continue
            event_time = _finite_number(raw.get("ts"))
            when = float(event_time) if event_time is not None else now
            self._record_point(
                candidate,
                when,
                count=_as_int(raw.get("count"), 1),
                context=None,
                observed_at=now,
            )

    def _record_counter_events(
        self,
        airplay: Mapping[str, Any],
        outputd: Mapping[str, Any] | None,
        now: float,
        *,
        context: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        watchdog = _mapping(fanin.get("watchdog"))
        pings_skipped = _as_int(watchdog.get("pings_skipped"))
        if self._previous_fanin_pings_skipped is not None:
            skipped_delta = pings_skipped - self._previous_fanin_pings_skipped
            if skipped_delta > 0:
                self._record_point(
                    _issue(
                        "path.fanin_watchdog_recovered",
                        scope="path",
                        impact="continuity",
                        severity="issue",
                        title="Audio path watchdog recovered",
                        detail=(
                            "Fan-in resumed after skipping "
                            f"{skipped_delta} watchdog ping(s)."
                        ),
                    ),
                    now,
                    count=skipped_delta,
                    context=context,
                )
        self._previous_fanin_pings_skipped = pings_skipped
        inputs = _mapping(fanin.get("inputs"))
        input_counts = {
            source_id: _as_int(_mapping(observation).get("xrun_count"))
            for source_id, observation in inputs.items()
            if isinstance(source_id, str)
            and bool(_mapping(observation).get("present"))
        }
        if self._previous_input_xruns is not None:
            for source_id, count in input_counts.items():
                if (
                    source_id == Source.AIRPLAY.value
                    or source_id != self._session.source_id
                ):
                    continue  # AirPlay has its own events; idle lanes are noise.
                previous = self._previous_input_xruns.get(source_id, count)
                delta = count - previous
                if delta > 0:
                    self._record_point(
                        _issue(
                            f"{source_id}.input_xrun",
                            scope="source",
                            source_id=source_id,
                            impact="continuity",
                            severity="issue",
                            title=f"{_SOURCE_LABELS.get(source_id, source_id)} input recovered",
                            detail=f"The input recovered {delta} interruption(s).",
                        ),
                        now,
                        count=delta,
                        context=context,
                    )
        self._previous_input_xruns = input_counts

        if outputd is None:
            self._previous_outputd_xruns = None
            self._previous_outputd_clipped = None
            return None
        outputd_map = _mapping(outputd)
        clipping_issue: dict[str, Any] | None = None
        clipped_samples = _as_int(
            _mapping(outputd_map.get("mix")).get("clipped_samples"),
        )
        if self._previous_outputd_clipped is not None:
            clipped_delta = clipped_samples - self._previous_outputd_clipped
            if clipped_delta > 0:
                clipping_issue = _issue(
                    "path.outputd_clipping",
                    scope="path",
                    impact="quality",
                    severity="issue",
                    title="Audio clipping detected",
                    detail=(
                        f"JTS observed {clipped_delta} clipped sample(s) "
                        "in the latest output interval."
                    ),
                )
        self._previous_outputd_clipped = clipped_samples
        outputd_counts = {
            "content": _as_int(_mapping(outputd_map.get("content")).get("xrun_count")),
            "dac": _as_int(_mapping(outputd_map.get("dac")).get("xrun_count")),
        }
        if self._previous_outputd_xruns is not None:
            for stage, count in outputd_counts.items():
                previous = self._previous_outputd_xruns.get(stage, count)
                delta = count - previous
                if delta > 0:
                    title = (
                        "Final output recovered"
                        if stage == "dac" else "Audio program path recovered"
                    )
                    self._record_point(
                        _issue(
                            f"path.outputd_{stage}_xrun",
                            scope="path",
                            impact="continuity",
                            severity="issue",
                            title=title,
                            detail=f"Outputd recovered {delta} interruption(s).",
                        ),
                        now,
                        count=delta,
                        context=context,
                    )
        self._previous_outputd_xruns = outputd_counts
        return clipping_issue
