# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One bounded, normalized audio-health snapshot for management surfaces.

The existing AirPlay collector already owns the expensive monitoring cadence
(fan-in STATUS, shairport/Camilla journals, MPRIS, and Camilla status).  This
module composes it with one cheap local outputd STATUS read and a slow route
claim read.  Production starts only :class:`AudioHealthSampler`'s thread; the
AirPlay collector is sampled inline, so the broader dashboard adds no daemon or
second resident loop.

The contract deliberately separates continuity from timing.  A USB host-clock
``l2_fallback`` keeps audio playing safely, so it degrades the latency axis but
does not claim the signal path failed.  Likewise ``l0_locked`` is live clocking
state, not proof of end-to-end latency; only the route artifact can verify that
claim.
"""
from __future__ import annotations

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
from .airplay_health import AirPlayHealthSampler, SAMPLE_INTERVAL_SEC

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ROUTE_INTERVAL_SEC = 60.0
OUTPUTD_SOCKET = "/run/jasper-outputd/control.sock"
LOCAL_STATUS_TIMEOUT_SEC = 1.0
MAX_STATUS_BYTES = 256 * 1024
FANIN_STALE_MS = 5000
OUTPUTD_STALE_MS = 3000
ISSUE_RING_SIZE = 20
ISSUE_COALESCE_SEC = 60.0

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


class IssueTracker:
    """Bounded ongoing/recovered lifecycle keyed by stable issue names."""

    def __init__(
        self,
        *,
        ring_size: int = ISSUE_RING_SIZE,
        coalesce_sec: float = ISSUE_COALESCE_SEC,
    ) -> None:
        self._ring_size = max(1, ring_size)
        # Only completed records live in the bounded deque. Ongoing records are
        # retained separately so a burst of point events can never evict the
        # very issue the user most needs to see.
        self._recovered: deque[dict[str, Any]] = deque(maxlen=self._ring_size)
        self._active: dict[str, dict[str, Any]] = {}
        self._coalesce_sec = max(0.0, coalesce_sec)

    def update(self, candidates: list[dict[str, Any]], now: float) -> None:
        seen = {str(candidate["key"]) for candidate in candidates}
        for candidate in candidates:
            key = str(candidate["key"])
            active = self._active.get(key)
            if active is not None:
                active["last_seen_at"] = now
                active.update(candidate)
                continue
            recent = next(
                (
                    item
                    for item in reversed(self._recovered)
                    if item.get("key") == key
                    and item.get("status") == "recovered"
                    and now - float(item.get("last_seen_at") or now)
                    <= self._coalesce_sec
                ),
                None,
            )
            if recent is not None:
                self._recovered.remove(recent)
                recent.update(candidate)
                recent["status"] = "ongoing"
                recent["last_seen_at"] = now
                recent["recovered_at"] = None
                recent["count"] = _as_int(recent.get("count"), 1) + 1
                self._active[key] = recent
                continue
            record = {
                **candidate,
                "status": "ongoing",
                "started_at": now,
                "last_seen_at": now,
                "recovered_at": None,
                "count": 1,
            }
            self._active[key] = record

        for key in tuple(self._active):
            if key in seen:
                continue
            record = self._active.pop(key)
            record["status"] = "recovered"
            record["recovered_at"] = now
            self._recovered.append(record)

    def record_point(
        self,
        candidate: dict[str, Any],
        when: float,
        *,
        count: int = 1,
    ) -> None:
        """Record a recovery event as a completed blip, coalescing bursts."""
        key = str(candidate["key"])
        active = self._active.get(key)
        if active is not None:
            active.update(candidate)
            active["last_seen_at"] = when
            active["count"] = _as_int(active.get("count"), 1) + max(1, count)
            return
        recent = next(
            (
                item
                for item in reversed(self._recovered)
                if item.get("key") == key
                and item.get("status") == "recovered"
                and when - float(item.get("last_seen_at") or when)
                <= self._coalesce_sec
            ),
            None,
        )
        if recent is not None:
            recent.update(candidate)
            recent["last_seen_at"] = when
            recent["recovered_at"] = when
            recent["count"] = _as_int(recent.get("count"), 1) + max(1, count)
            return
        self._recovered.append({
            **candidate,
            "status": "recovered",
            "started_at": when,
            "last_seen_at": when,
            "recovered_at": when,
            "count": max(1, count),
        })

    def snapshot(self) -> list[dict[str, Any]]:
        active = sorted(
            self._active.values(),
            key=lambda item: float(item.get("started_at") or 0.0),
            reverse=True,
        )
        recovered_slots = max(0, self._ring_size - len(active))
        recovered = list(reversed(self._recovered))[:recovered_slots]
        return [copy.deepcopy(item) for item in (*active, *recovered)]


def _active_source(airplay: Mapping[str, Any]) -> str | None:
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    selected = fanin.get("selected_input")
    if isinstance(selected, str):
        normalized = selected.strip().lower()
        if normalized in _SOURCE_LABELS:
            return normalized
        if normalized in _LABEL_TO_SOURCE:
            return _LABEL_TO_SOURCE[normalized]
    # Before the first fan-in selection sample, AirPlay's already-cached MPRIS
    # truth is a useful narrow fallback. Do not infer activity from free-running
    # lane frames.
    mpris = _mapping(current.get("mpris"))
    return Source.AIRPLAY.value if mpris.get("playing") is True else None


def _signal_path(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
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
    active = _active_source(airplay)
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
    elif verification["status"] == "target_missed":
        status = "warn"
        headline = "USB latency target not met"
        if "p95_exceeds_40ms" in verification["issues"]:
            detail = "Measured typical latency exceeds the route's 40 ms budget."
        else:
            detail = "Measured tail latency exceeds the route's 42 ms budget."
    elif verification["status"] == "unverified":
        status = "warn"
        headline = "Low-latency route active · verification needed"
        detail = "Live clocking state alone cannot verify end-to-end latency."
    elif verification["status"] == "partial":
        status = "warn"
        headline = "Typical latency verified · tail check incomplete"
        detail = "The p95 budget passed; the p99 promotion check is incomplete."
    else:
        status = "ok"
        headline = "Low latency verified"
        if active and raw_mode == "l0_locked":
            detail = "USB is in its lowest-latency host-clock mode."
        else:
            detail = "The measured route matches the current audio configuration."
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
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    warmup = bool(airplay.get("warmup_active"))
    current = _mapping(airplay.get("current"))
    fanin = current.get("fanin")
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
        verification = _mapping(latency.get("verification"))
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
        if verification.get("status") == "target_missed":
            issues.append(_issue(
                "usbsink.latency_target_missed",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB latency target not met",
                detail=str(latency.get("detail") or "Measured latency exceeded its budget."),
            ))
        elif verification.get("status") == "unverified":
            issues.append(_issue(
                "usbsink.latency_unverified",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB latency verification needed",
                detail="The stored measurement is missing, stale, or does not match this route.",
            ))
    for source_id, units in _SOURCE_HEALTH_UNITS.items():
        for unit in units:
            failure = _service_failure(unit, _mapping(service_states).get(unit))
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
) -> tuple[str, str, str] | None:
    """Return ``(state, headline, detail)`` from cached systemd truth."""
    states = _mapping(service_states)
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
        service_summary = _source_service_summary(source_id, service_states)
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


def compose_audio_health(
    *,
    airplay: Mapping[str, Any] | None,
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any] | None,
    issues: list[dict[str, Any]],
    sampled_at: float,
    previous_overall: Mapping[str, Any] | None = None,
    service_states: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the public, presentation-ready audio-health contract."""
    ap = _mapping(airplay)
    route_state = _mapping(route)
    active_source = _active_source(ap)
    signal_path = _signal_path(ap, outputd)
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
    )
    unavailable_sources = [
        str(source.get("label") or source.get("id"))
        for source in source_cards
        if source.get("status") == "issue"
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
    return {
        "schema_version": SCHEMA_VERSION,
        "sampled_at": sampled_at,
        "overall": overall,
        "signal_path": signal_path,
        "latency": latency,
        "sources": source_cards,
        "issues": copy.deepcopy(issues),
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
                "content": copy.deepcopy(_mapping(outputd).get("content")),
                "dac": copy.deepcopy(_mapping(outputd).get("dac")),
                "tts": copy.deepcopy(_mapping(outputd).get("tts")),
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
        route_probe: Callable[[], dict[str, Any]] | None = None,
        service_probe: Callable[[], dict[str, dict[str, Any]]] | None = None,
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
        self._route_probe = route_probe or read_route_claim
        self._service_probe = service_probe
        self._issues = IssueTracker()
        self._outputd: dict[str, Any] | None = None
        self._route: dict[str, Any] | None = None
        self._service_states: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._last_route_sample_at = 0.0
        self._previous_input_xruns: dict[str, int] | None = None
        self._previous_fanin_pings_skipped: int | None = None
        self._previous_outputd_xruns: dict[str, int] | None = None
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
            issues = list(snapshot.get("issues") or [])
            issues.insert(0, {
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
            })
            snapshot["issues"] = issues
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

        self._record_raw_events(airplay)
        self._record_counter_events(airplay, outputd, now)
        active_source = _active_source(airplay)
        signal_path = _signal_path(airplay, outputd)
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        host_clock = _mapping(fanin.get("host_clock")) or None
        if active_source == Source.USBSINK.value:
            latency = _usb_timing(_mapping(self._route), host_clock, active=True)
        else:
            latency = _not_applicable_timing()
        self._issues.update(
            _state_issues(
                airplay,
                outputd,
                signal_path,
                latency,
                active_source,
                self._service_states,
            ),
            now,
        )
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
            )

    def _record_raw_events(self, airplay: Mapping[str, Any]) -> None:
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
            self._issues.record_point(
                candidate,
                float(raw.get("ts") or self._time()),
                count=_as_int(raw.get("count"), 1),
            )

    def _record_counter_events(
        self,
        airplay: Mapping[str, Any],
        outputd: Mapping[str, Any] | None,
        now: float,
    ) -> None:
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        watchdog = _mapping(fanin.get("watchdog"))
        pings_skipped = _as_int(watchdog.get("pings_skipped"))
        if self._previous_fanin_pings_skipped is not None:
            skipped_delta = pings_skipped - self._previous_fanin_pings_skipped
            if skipped_delta > 0:
                self._issues.record_point(
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
                if source_id == Source.AIRPLAY.value:
                    continue  # the AirPlay collector already records this lane
                previous = self._previous_input_xruns.get(source_id, count)
                delta = count - previous
                if delta > 0:
                    self._issues.record_point(
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
                    )
        self._previous_input_xruns = input_counts

        if outputd is None:
            self._previous_outputd_xruns = None
            return
        outputd_map = _mapping(outputd)
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
                    self._issues.record_point(
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
                    )
        self._previous_outputd_xruns = outputd_counts
