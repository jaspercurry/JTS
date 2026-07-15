# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded audio-incident persistence and current-session accounting.

This module owns the two small state machines behind the audio dashboard:
durable incident lifecycles and process-local playback-session totals. It has
no probes; the session accumulator returns its small presentation-ready rollup,
while the audio-health composer owns incident and source presentation.
"""
from __future__ import annotations

import copy
import json
import logging
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from ..atomic_io import atomic_write_text, read_regular_bytes_nofollow
from ..log_event import log_event

logger = logging.getLogger(__name__)

ISSUE_RING_SIZE = 20
ISSUE_COALESCE_SEC = 60.0
INCIDENT_PERSIST_DEBOUNCE_SEC = 300.0
INCIDENT_HISTORY_PATH = "/var/lib/jasper/audio_health_incidents.json"
INCIDENT_HISTORY_MAX_BYTES = 128 * 1024
INCIDENT_HISTORY_SCHEMA_VERSION = 1

_INCIDENT_STRING_FIELDS = frozenset({
    "scope",
    "source_id",
    "impact",
    "severity",
    "title",
    "detail",
    "status",
})
_INCIDENT_NUMBER_FIELDS = frozenset({
    "started_at",
    "last_seen_at",
    "recovered_at",
    "first_occurrence_at",
    "last_occurrence_at",
    "observed_seconds",
})


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_freeze_frame(raw: Any) -> dict[str, Any]:
    """Keep only the start evidence the dashboard actually renders."""
    context = _mapping(raw)
    out: dict[str, Any] = {}
    clock_mode = context.get("clock_mode")
    if isinstance(clock_mode, str):
        out["clock_mode"] = clock_mode[:160]
    rms = _finite_number(_mapping(context.get("input")).get("rms_dbfs"))
    if rms is not None:
        out["input"] = {"rms_dbfs": rms}
    delay = _finite_number(
        _mapping(context.get("output")).get("snd_pcm_delay_ms"),
    )
    if delay is not None:
        out["output"] = {"snd_pcm_delay_ms": delay}
    return out


def _started_context(raw: Any) -> dict[str, Any]:
    frame = _clean_freeze_frame(raw)
    return {"context": {"started": frame}} if frame else {}


def _clean_incident(raw: Any) -> dict[str, Any] | None:
    """Validate the on-disk shape without coercing corrupt field types."""
    record = _mapping(raw)
    key = record.get("key")
    status = record.get("status")
    if (
        not isinstance(key, str)
        or not key
        or len(key) > 160
        or status not in {"ongoing", "recovered"}
    ):
        return None
    out: dict[str, Any] = {"key": key, "status": status}
    for field in _INCIDENT_STRING_FIELDS - {"status"}:
        value = record.get(field)
        if isinstance(value, str):
            out[field] = value[:1000]
        elif value is None and field == "source_id":
            out[field] = None
    for field in _INCIDENT_NUMBER_FIELDS:
        value = _finite_number(record.get(field))
        if value is not None and (field != "observed_seconds" or value >= 0):
            out[field] = value
        elif record.get(field) is None and field == "recovered_at":
            out[field] = None
    count = record.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
        out["count"] = count
    started = _clean_freeze_frame(
        _mapping(record.get("context")).get("started"),
    )
    if started:
        out["context"] = {"started": started}
    return out


class IncidentStore:
    """Tiny, versioned incident store; persistence never affects audio health."""

    def __init__(
        self,
        path: str = INCIDENT_HISTORY_PATH,
        *,
        max_records: int = ISSUE_RING_SIZE,
        writer: Callable[..., None] = atomic_write_text,
    ) -> None:
        self.path = path
        self._max_records = max(1, max_records)
        self._writer = writer
        self._load_warning_emitted = False
        self._write_warning_emitted = False

    def _warn_load_once(self, reason: str, *, exc_info: bool = False) -> None:
        if self._load_warning_emitted:
            return
        self._load_warning_emitted = True
        log_event(
            logger,
            "audio_incident_store.load_failed",
            level=logging.WARNING,
            exc_info=exc_info,
            path=self.path,
            reason=reason,
        )

    def load(self) -> list[dict[str, Any]]:
        try:
            raw = read_regular_bytes_nofollow(
                self.path,
                max_bytes=INCIDENT_HISTORY_MAX_BYTES,
            )
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._warn_load_once(type(exc).__name__, exc_info=True)
            return []
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != INCIDENT_HISTORY_SCHEMA_VERSION
            or not isinstance(payload.get("incidents"), list)
        ):
            self._warn_load_once("invalid_schema")
            return []
        cleaned: list[dict[str, Any]] = []
        for raw_record in payload["incidents"][:self._max_records]:
            record = _clean_incident(raw_record)
            if record is not None:
                cleaned.append(record)
        return cleaned

    def save(self, incidents: list[dict[str, Any]]) -> bool:
        payload = {
            "schema_version": INCIDENT_HISTORY_SCHEMA_VERSION,
            "incidents": [
                cleaned
                for raw in incidents[:self._max_records]
                if (cleaned := _clean_incident(raw)) is not None
            ],
        }
        try:
            encoded = json.dumps(payload, separators=(",", ":")) + "\n"
            while (
                len(encoded.encode("utf-8")) > INCIDENT_HISTORY_MAX_BYTES
                and len(payload["incidents"]) > 1
            ):
                payload["incidents"].pop()
                encoded = json.dumps(payload, separators=(",", ":")) + "\n"
            if len(encoded.encode("utf-8")) > INCIDENT_HISTORY_MAX_BYTES:
                raise ValueError("incident history exceeds read limit")
            self._writer(
                self.path,
                encoded,
                mode=0o660,
                group_from_parent=True,
            )
            self._write_warning_emitted = False
            return True
        except (OSError, TypeError, ValueError):
            if not self._write_warning_emitted:
                self._write_warning_emitted = True
                log_event(
                    logger,
                    "audio_incident_store.write_failed",
                    level=logging.WARNING,
                    exc_info=True,
                    path=self.path,
                )
            return False


class IssueTracker:
    """Bounded durable incident lifecycle keyed by stable issue names."""

    def __init__(
        self,
        *,
        ring_size: int = ISSUE_RING_SIZE,
        coalesce_sec: float = ISSUE_COALESCE_SEC,
        persist_debounce_sec: float = INCIDENT_PERSIST_DEBOUNCE_SEC,
        max_observation_gap_sec: float = 15.0,
        store: IncidentStore | None = None,
    ) -> None:
        self._ring_size = max(1, ring_size)
        self._recovered: deque[dict[str, Any]] = deque(maxlen=self._ring_size)
        self._active: dict[str, dict[str, Any]] = {}
        self._observed_at: dict[str, float] = {}
        self._coalesce_sec = max(0.0, coalesce_sec)
        self._persist_debounce_sec = max(300.0, persist_debounce_sec)
        self._max_observation_gap_sec = max(0.0, max_observation_gap_sec)
        self._store = store
        self._batch_depth = 0
        self._dirty = False
        self._urgent = False
        self._pending_now: float | None = None
        self._last_persist_at: float | None = None
        self._next_retry_at: float | None = None
        if store is not None:
            for record in store.load():
                if record.get("status") == "ongoing":
                    record.setdefault("observed_seconds", 0.0)
                    self._active[str(record["key"])] = record
                else:
                    # The file is newest-first; the deque is oldest-first.
                    self._recovered.appendleft(record)

    def _persist(self, now: float, *, immediate: bool) -> None:
        self._dirty = True
        self._urgent = self._urgent or immediate
        self._pending_now = max(self._pending_now or now, now)
        if self._batch_depth == 0:
            self._flush(now)

    def _flush(self, now: float | None = None) -> None:
        if now is not None:
            self._pending_now = max(self._pending_now or now, now)
        if not self._dirty:
            return
        if self._store is None:
            self._dirty = False
            self._urgent = False
            return
        write_at = self._pending_now
        if write_at is None:
            return
        if self._next_retry_at is not None and write_at < self._next_retry_at:
            return
        if not self._urgent:
            if self._last_persist_at is None:
                self._last_persist_at = write_at
                return
            if write_at - self._last_persist_at < self._persist_debounce_sec:
                return
        if self._store.save(self.snapshot()) is False:
            self._next_retry_at = write_at + self._persist_debounce_sec
            return
        self._last_persist_at = write_at
        self._next_retry_at = None
        self._dirty = False
        self._urgent = False
        self._pending_now = None

    @contextmanager
    def batch(self, now: float | None = None) -> Iterator[None]:
        """Coalesce one sample's transitions into at most one disk write."""
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self._flush(now)

    def update(
        self,
        candidates: list[dict[str, Any]],
        now: float,
        *,
        context: Mapping[str, Any] | None = None,
        preserve_unseen_keys: Collection[str] = (),
    ) -> None:
        """Observe current conditions; only known absence closes an incident."""
        changed = False
        seen = {str(candidate["key"]) for candidate in candidates}
        preserved = set(preserve_unseen_keys)
        for candidate in candidates:
            key = str(candidate["key"])
            active = self._active.get(key)
            if active is not None:
                active["last_seen_at"] = now
                active.update(candidate)
                previous = self._observed_at.get(key)
                self._observed_at[key] = now
                if previous is not None:
                    gap = now - previous
                    if 0.0 <= gap <= self._max_observation_gap_sec:
                        active["observed_seconds"] = float(
                            active.get("observed_seconds") or 0.0,
                        ) + gap
                continue
            self._active[key] = {
                **candidate,
                "status": "ongoing",
                "started_at": now,
                "last_seen_at": now,
                "recovered_at": None,
                "count": 1,
                "first_occurrence_at": now,
                "last_occurrence_at": now,
                "observed_seconds": 0.0,
                **_started_context(context),
            }
            self._observed_at[key] = now
            changed = True

        for key in tuple(self._active):
            if key in seen:
                continue
            if key in preserved:
                # The condition is temporarily unobservable. Retain its stable
                # identity, but clear the duration anchor so the next valid
                # observation cannot count this gap as time degraded.
                self._observed_at.pop(key, None)
                continue
            record = self._active.pop(key)
            self._observed_at.pop(key, None)
            record["status"] = "recovered"
            record["recovered_at"] = now
            self._recovered.append(record)
            changed = True
        if changed:
            self._persist(now, immediate=True)

    def record_point(
        self,
        candidate: dict[str, Any],
        when: float,
        *,
        count: int = 1,
        context: Mapping[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> None:
        """Record a completed blip, debouncing only repeated count updates."""
        key = str(candidate["key"])
        persist_at = observed_at if observed_at is not None else when
        active = self._active.get(key)
        if active is not None:
            active.update(candidate)
            active["last_seen_at"] = max(
                float(active.get("last_seen_at") or when),
                when,
            )
            active["count"] = int(active.get("count") or 1) + max(1, count)
            active["last_occurrence_at"] = max(
                float(active.get("last_occurrence_at") or when),
                when,
            )
            self._persist(persist_at, immediate=False)
            return
        recent = next(
            (
                item
                for item in reversed(self._recovered)
                if item.get("key") == key
                and item.get("status") == "recovered"
                and 0.0 <= when - float(item.get("last_seen_at") or when)
                <= self._coalesce_sec
            ),
            None,
        )
        if recent is not None:
            recent.update(candidate)
            recent["last_seen_at"] = max(
                float(recent.get("last_seen_at") or when),
                when,
            )
            recent["recovered_at"] = max(
                float(recent.get("recovered_at") or when),
                when,
            )
            recent["count"] = int(recent.get("count") or 1) + max(1, count)
            recent["last_occurrence_at"] = max(
                float(recent.get("last_occurrence_at") or when),
                when,
            )
            self._persist(persist_at, immediate=False)
            return
        self._recovered.append({
            **candidate,
            "status": "recovered",
            "started_at": when,
            "last_seen_at": when,
            "recovered_at": when,
            "count": max(1, count),
            "first_occurrence_at": when,
            "last_occurrence_at": when,
            "observed_seconds": 0.0,
            **_started_context(context),
        })
        self._persist(persist_at, immediate=True)

    def snapshot(self) -> list[dict[str, Any]]:
        active = sorted(
            self._active.values(),
            key=lambda item: float(item.get("started_at") or 0.0),
            reverse=True,
        )[:self._ring_size]
        recovered_slots = max(0, self._ring_size - len(active))
        recovered = list(reversed(self._recovered))[:recovered_slots]
        return [copy.deepcopy(item) for item in (*active, *recovered)]


class SessionRollup:
    """Process-local, monotonic totals for the currently observed source."""

    def __init__(self, *, max_observation_gap_sec: float = 15.0) -> None:
        self._max_observation_gap_sec = max(0.0, max_observation_gap_sec)
        self.source_id: str | None = None
        self.started_at: float | None = None
        self._interruptions = 0
        self._latency_events = 0
        self._sync_events = 0
        self._degraded_seconds = 0.0
        self._last_incident_at: float | None = None
        self._active_degradation: dict[str, float | None] = {}

    def reset(self, source_id: str | None, now: float) -> None:
        self.source_id = source_id
        self.started_at = now if source_id is not None else None
        self._interruptions = 0
        self._latency_events = 0
        self._sync_events = 0
        self._degraded_seconds = 0.0
        self._last_incident_at = None
        self._active_degradation.clear()

    def _relevant(self, issue: Mapping[str, Any]) -> bool:
        return (
            self.source_id is not None
            and issue.get("source_id") in {None, self.source_id}
        )

    def _count(self, issue: Mapping[str, Any], count: int, when: float) -> None:
        impact = issue.get("impact")
        if impact == "continuity":
            self._interruptions += count
        elif impact == "latency":
            self._latency_events += count
        elif impact == "sync":
            self._sync_events += count
        else:
            return
        self._last_incident_at = max(self._last_incident_at or when, when)

    def record_point(
        self,
        issue: Mapping[str, Any],
        when: float,
        *,
        count: int = 1,
    ) -> None:
        if (
            not self._relevant(issue)
            or self.started_at is None
            or when < self.started_at
        ):
            return
        self._count(issue, max(1, count), when)

    def observe_state(
        self,
        candidates: list[dict[str, Any]],
        now: float,
        *,
        preserve_unseen_keys: Collection[str] = (),
    ) -> None:
        preserved = set(preserve_unseen_keys)
        relevant = {
            str(issue["key"]): issue
            for issue in candidates
            if self._relevant(issue)
            and issue.get("impact") in {"continuity", "latency", "sync"}
        }
        for key, issue in relevant.items():
            previous = self._active_degradation.get(key)
            if key not in self._active_degradation:
                self._count(issue, 1, now)
            elif previous is not None:
                gap = now - previous
                if (
                    issue.get("impact") in {"latency", "sync"}
                    and 0.0 <= gap <= self._max_observation_gap_sec
                ):
                    self._degraded_seconds += gap
            self._active_degradation[key] = now
        for key in tuple(self._active_degradation):
            if key not in relevant:
                if key in preserved:
                    self._active_degradation[key] = None
                else:
                    self._active_degradation.pop(key)

    def snapshot(self, now: float) -> dict[str, Any] | None:
        if self.source_id is None or self.started_at is None:
            return None
        adjustments = self._latency_events + self._sync_events
        if self._interruptions:
            summary = (
                f"{self._interruptions} observed interruption"
                f"{'s' if self._interruptions != 1 else ''}"
            )
        elif adjustments:
            summary = (
                "No interruptions observed · "
                f"{adjustments} timing adjustment"
                f"{'s' if adjustments != 1 else ''}"
            )
        else:
            summary = "No interruptions observed"
        details: list[dict[str, str]] = []
        if self._interruptions:
            details.append({
                "label": "Observed interruptions",
                "value": str(self._interruptions),
            })
        if self._latency_events:
            details.append({
                "label": "Latency events",
                "value": str(self._latency_events),
            })
        if self._sync_events:
            details.append({
                "label": "Sync corrections",
                "value": str(self._sync_events),
            })
        if self._degraded_seconds:
            details.append({
                "label": "Time degraded",
                "value": _duration_label(self._degraded_seconds),
            })
        return {
            "summary": summary,
            "detail": "Since JTS observed this source become active.",
            "details": details,
            "started_at": self.started_at,
            "duration_seconds": round(max(0.0, now - self.started_at), 1),
            "interruptions": self._interruptions,
            "latency_events": self._latency_events,
            "sync_events": self._sync_events,
            "degraded_seconds": round(self._degraded_seconds, 1),
            "last_incident_at": self._last_incident_at,
        }


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
