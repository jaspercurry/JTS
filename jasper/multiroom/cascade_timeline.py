# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded multiroom restart-cascade timeline for ``/state``.

The journal already has the durable truth via structured ``event=`` lines.
This sampler keeps the last few multiroom/restart supervisor decisions in
memory so an operator can reconstruct "what kicked what" from ``/state``
without SSHing into journald first. It is intentionally small: no log bundle,
no raw journal retention, no unbounded history.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

JOURNAL_INTERVAL_SEC = 15.0
JOURNAL_LOOKBACK_SEC = 15 * 60.0
EVENT_RING_SIZE = 40
SUBPROCESS_TIMEOUT_SEC = 2.0
# Belt-and-suspenders memory cap on a 1 GB Pi: bound the journalctl read so a
# burst inside one window can't load an unbounded stdout into RAM before
# filtering. The ring only keeps EVENT_RING_SIZE; the per-scan window is short
# (JOURNAL_INTERVAL_SEC), so the most-recent N entries is plenty of headroom.
JOURNAL_SCAN_LINE_CAP = 1000

JOURNAL_UNITS = (
    "jasper-control",
    "jasper-grouping-reconcile",
)

EVENT_PREFIXES = (
    "multiroom.reconcile.",
    "restart_broker.",
    "grouping_supervisor.",
)

_ISSUE_TOKENS = (
    "failed", "error", "crash", "unavailable", "denied", "nonzero",
    "rate_limited",
)
_ACTION_TOKENS = ("request", "restarted", "starved_detected", "repaired")


def _parse_logfmt_event(line: str) -> tuple[str, dict[str, str]] | None:
    idx = line.find("event=")
    if idx < 0:
        return None
    try:
        parts = shlex.split(line[idx:])
    except ValueError:
        return None
    event = ""
    fields: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key == "event":
            event = value
        else:
            fields[key] = value
    if not event:
        return None
    return event, fields


def _event_severity(event: str) -> str:
    if any(token in event for token in _ISSUE_TOKENS):
        return "issue"
    if any(token in event for token in _ACTION_TOKENS):
        return "action"
    return "info"


def _event_detail(event: str, fields: dict[str, str]) -> str:
    if event == "restart_broker.request":
        units = fields.get("units") or "(none)"
        verb = fields.get("verb") or "manage"
        reason = fields.get("reason") or "-"
        return f"{verb} {units} reason={reason}"
    if event.startswith("multiroom.reconcile.unit"):
        unit = fields.get("unit") or fields.get("units") or "(unit unknown)"
        action = fields.get("action") or fields.get("desired") or event.rsplit(".", 1)[-1]
        reason = fields.get("reason") or "-"
        return f"{action} {unit} reason={reason}"
    if event == "grouping_supervisor.starved_detected":
        action = fields.get("action") or "kick_reconcile"
        count = fields.get("count") or "?"
        return f"starvation threshold reached; {action} count={count}"
    if event == "grouping_supervisor.kick_rate_limited":
        return (
            "reconciler kick rate-limited after "
            f"{fields.get('since_last_kick') or '?'}"
        )
    if event == "grouping_supervisor.binding_repaired":
        return (
            "snapcast binding repair "
            f"fixed={fields.get('fixed') or '0'} failed={fields.get('failed') or '0'}"
        )
    if fields:
        return " ".join(f"{k}={v}" for k, v in list(fields.items())[:4])
    return event


def classify_journal_line(
    unit: str, line: str, *, observed_at: float, occurred_at: float | None = None,
) -> dict[str, Any] | None:
    """Classify one journal line into the cascade ring shape. PURE."""
    parsed = _parse_logfmt_event(line)
    if parsed is None:
        return None
    event, fields = parsed
    if not event.startswith(EVENT_PREFIXES):
        return None
    return {
        "occurred_at": occurred_at if occurred_at is not None else observed_at,
        "observed_at": observed_at,
        "unit": unit,
        "event": event,
        "severity": _event_severity(event),
        "detail": _event_detail(event, fields),
        "fields": dict(fields),
    }


JournalRecord = tuple[float, str]
JournalReader = Callable[[str, float, float], list[JournalRecord]]


class CascadeTimelineSampler:
    """Journal-driven bounded event ring for multiroom restart cascades."""

    def __init__(
        self,
        *,
        journal_interval_sec: float = JOURNAL_INTERVAL_SEC,
        journal_lookback_sec: float = JOURNAL_LOOKBACK_SEC,
        ring_size: int = EVENT_RING_SIZE,
        journal_reader: JournalReader | None = None,
        time_func: Callable[[], float] = time.time,
    ) -> None:
        self._journal_interval = journal_interval_sec
        self._journal_lookback = max(0.0, journal_lookback_sec)
        self._time = time_func
        self._journal_reader = journal_reader or self._read_journal_lines
        now = self._time()
        start = max(0.0, now - self._journal_lookback)
        self._journal_since = {unit: start for unit in JOURNAL_UNITS}
        self._last_scan_at: float | None = None
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="jasper-multiroom-cascade-timeline",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        """For tests; production runs the daemon thread to process exit."""
        self._stopped = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "journal_interval_sec": self._journal_interval,
                "journal_lookback_sec": self._journal_lookback,
                "last_scan_at": self._last_scan_at,
                "events": [dict(event) for event in self._events],
            }

    def _run(self) -> None:
        while not self._stopped:
            started = self._time()
            try:
                self._tick()
            except (OSError, RuntimeError, TypeError, ValueError):
                logger.debug("multiroom cascade timeline tick failed", exc_info=True)
            elapsed = self._time() - started
            time.sleep(max(0.1, self._journal_interval - elapsed))

    def _tick(self) -> None:
        now = self._time()
        for unit in JOURNAL_UNITS:
            since = self._journal_since.get(unit, now)
            try:
                records = self._journal_reader(unit, since, now)
            except (OSError, RuntimeError, subprocess.SubprocessError, TypeError, ValueError):
                logger.debug("cascade journal scan failed for %s", unit, exc_info=True)
                records = []
            for occurred_at, line in records:
                event = classify_journal_line(
                    unit, line,
                    observed_at=now,
                    occurred_at=occurred_at,
                )
                if event is not None:
                    with self._lock:
                        self._events.append(event)
            self._journal_since[unit] = now
        with self._lock:
            self._last_scan_at = now

    @staticmethod
    def _read_journal_lines(unit: str, since: float, now: float) -> list[JournalRecord]:
        try:
            proc = subprocess.run(
                [
                    "journalctl",
                    "-u", unit,
                    "--since", f"@{since:.3f}",
                    "--until", f"@{now:.3f}",
                    "-n", str(JOURNAL_SCAN_LINE_CAP),
                    "--no-pager",
                    "-o", "json",
                ],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SEC,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return []
        if proc.returncode not in (0, 1):
            return []
        records: list[JournalRecord] = []
        for raw in proc.stdout.splitlines():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            message = payload.get("MESSAGE")
            if not isinstance(message, str) or not message:
                continue
            occurred_at = _journal_realtime_seconds(
                payload.get("__REALTIME_TIMESTAMP"), now,
            )
            records.append((occurred_at, message))
        return records


def _journal_realtime_seconds(value: Any, fallback: float) -> float:
    try:
        micros = int(value)
    except (TypeError, ValueError):
        return fallback
    if micros <= 0:
        return fallback
    return micros / 1_000_000.0


_sampler: CascadeTimelineSampler | None = None


def start_sampler() -> CascadeTimelineSampler:
    """Start the singleton sampler used by jasper-control."""
    global _sampler
    if _sampler is None:
        _sampler = CascadeTimelineSampler()
        _sampler.start()
    return _sampler


def snapshot() -> dict[str, Any]:
    """Read-only state for ``/state``."""
    if _sampler is None:
        return {"enabled": False, "events": []}
    return _sampler.snapshot()
