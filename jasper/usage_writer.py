# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded usage persistence and cached household spend for the voice daemon."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
import uuid

from jasper.log_event import log_event
from jasper.usage import (
    AggregateUsageReader, Pricing, UsageStore, _UNRECORDED_SESSION,
    _USAGE_READS, _UsageRow, household_usage_reader,
)

logger = logging.getLogger(__name__)


class VoiceUsageStore(UsageStore):
    """Voice-owned pending ledger plus fixed-size household spend snapshots.

    Disk queries exclude pending IDs; publishing a new snapshot retires saved
    rows atomically. Both sides use UsageStore's pricing and window queries.
    The condition protects memory only, never disk I/O.
    """

    _MAX_PENDING = 128
    _REFRESH_SECONDS = 1.0
    _DRAIN_SECONDS = 1.0

    def __init__(
        self, db_path: str, pricing: Pricing | None = None,
        *, pricing_overrides: dict | None = None,
    ) -> None:
        super().__init__(":memory:", pricing, pricing_overrides=pricing_overrides)
        self._condition = threading.Condition()
        # Open rows keep their slot until their close is saved.
        self._pending: dict[tuple[str, int], _UsageRow] = {}
        self._dirty: dict[tuple[str, int], _UsageRow] = {}
        self._totals: dict[str, float | int] = dict.fromkeys(_USAGE_READS, 0)
        self._published: tuple[dict[str, float | int], list[tuple[str, int]]] | None = None
        self._other_totals: dict[str, float | int] = dict.fromkeys(_USAGE_READS, 0)
        self._refreshed = self._other_refreshed = 0.0
        self._read_error: str | None = None
        self._cleanup_requested = False
        self._write_error: str | None = None
        self._lost = 0
        self._deadline: float | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._write_loop, args=(db_path,), name="usage-writer", daemon=True,
        )

    @classmethod
    async def start(cls, db_path: str, **kwargs) -> VoiceUsageStore:
        store = cls(db_path, **kwargs)
        store._thread.start()
        # Startup may proceed with disclosed-stale accounting. No default-
        # executor task can hold asyncio.run shutdown open on wedged storage.
        try:
            until = time.monotonic() + 0.1
            while not store._ready.is_set() and time.monotonic() < until:
                await asyncio.sleep(0.005)
            return store
        except asyncio.CancelledError:
            await store.aclose()
            raise

    def _new_row_id(self) -> int:
        # Negative IDs leave AUTOINCREMENT available to other writers.
        return -2 - (uuid.uuid4().int & ((1 << 62) - 1))

    def _load_snapshot(self) -> None:
        if self._published is not None:
            self._totals, retired = self._published
            self._published = None
            self._discard_rows(retired)

    def _submit(self, table: str, row_id: int) -> bool:
        row = self._row(table, row_id)
        if row is None:
            return False
        key = (table, row_id)
        if self._deadline is not None or (
            key not in self._pending and len(self._pending) >= self._MAX_PENDING
        ):
            self._lost += 1
            self._discard_rows([key])
            return False
        self._pending[key] = self._dirty[key] = row
        self._condition.notify()
        return True

    def open_session(self, provider: str | None = None) -> int:
        with self._condition:
            self._load_snapshot()
            row_id = super().open_session(provider)
            return row_id if self._submit("sessions", row_id) else _UNRECORDED_SESSION

    def _close_session_with_pricing(self, session_id, *args, **kwargs) -> float:
        with self._condition:
            self._load_snapshot()
            cost = super()._close_session_with_pricing(session_id, *args, **kwargs)
            self._submit("sessions", session_id)
            return cost

    def record_billable_activity_open(self, provider: str, rate_per_hour_usd: float) -> None:
        with self._condition:
            self._load_snapshot()
            super().record_billable_activity_open(provider, rate_per_hour_usd)
            for row_id in self._open_activity_ids():
                self._submit("connection_intervals", row_id)

    def record_billable_activity_close(self) -> None:
        with self._condition:
            self._load_snapshot()
            rows = self._open_activity_ids()
            super().record_billable_activity_close()
            for row_id in rows:
                self._submit("connection_intervals", row_id)

    def close_dangling_intervals(self) -> None:
        with self._condition:
            self._cleanup_requested = True
            self._condition.notify()

    def _read_cached(self, method: str) -> float | int:
        with self._condition:
            self._load_snapshot()
            return self._totals[method] + self._other_totals[method] + getattr(super(), method)()

    def spend_last_24h_usd(self) -> float:
        return float(self._read_cached("spend_last_24h_usd"))

    def spend_month_to_date_usd(self) -> float:
        return float(self._read_cached("spend_month_to_date_usd"))

    def session_count_today_utc(self) -> int:
        return int(self._read_cached("session_count_today_utc"))

    @property
    def write_degraded(self) -> bool:
        return bool(
            super().write_degraded or self._lost or self._write_error or self._read_error
            or self._cleanup_requested
            or (not self._thread.is_alive() and self._deadline is None)
            or time.monotonic() - min(self._refreshed, self._other_refreshed)
            > 3 * self._REFRESH_SECONDS
        )

    async def aclose(self) -> None:
        with self._condition:
            self._deadline = time.monotonic() + self._DRAIN_SECONDS
            self._condition.notify()
        while self._thread.is_alive() and time.monotonic() < self._deadline + 0.1:
            await asyncio.sleep(0.01)
        with self._condition:
            if self._dirty:
                self._lost += len(self._dirty)
                log_event(
                    logger, "usage.drain_incomplete", pending=len(self._dirty),
                    level=logging.WARNING,
                )
        self._conn.close()

    def _publish_snapshot(self, disk: UsageStore) -> None:
        with self._condition:
            completed = {
                key: row for key, row in self._pending.items()
                if key not in self._dirty
                and row.closed
            }
            excluded = [key for key in self._pending if key not in completed]
        totals = disk._snapshot(excluded)
        with self._condition:
            # A close updated during the read must remain wholly in the
            # pending ledger until its latest values are durable.
            if any(self._pending.get(key) is not row for key, row in completed.items()):
                return
            retired = list(completed)
            if self._published is not None:
                retired.extend(self._published[1])
            for key in completed:
                del self._pending[key]
            self._published = totals, retired
            self._refreshed = time.monotonic()

    def _refresh_other(self, reader: AggregateUsageReader) -> None:
        totals = {}
        for name in _USAGE_READS:
            totals[name] = getattr(reader, name)()
            if reader.read_degraded:
                self._read_error = "household ledger unreadable"
                return
        self._other_totals = totals
        self._read_error = None
        self._other_refreshed = time.monotonic()

    def _write_loop(self, db_path: str) -> None:
        disk: UsageStore | None = None
        # The voice ledger is accounted separately, so only companion
        # ledgers contribute to this reader. The household member list stays
        # owned by household_usage_reader.
        other = household_usage_reader(
            db_path, main_store=AggregateUsageReader(), timeout=0.05,
        )
        refreshed = 0.0
        loss_reported = False
        try:
            while True:
                with self._condition:
                    stopping = self._deadline is not None and (
                        (not self._dirty and not self._cleanup_requested)
                        or time.monotonic() >= self._deadline
                    )
                    item = next(iter(self._dirty.items()), None)
                    cleanup = self._cleanup_requested
                if self._lost and not loss_reported:
                    log_event(logger, "usage.queue_overflow", lost=self._lost, level=logging.WARNING)
                    loss_reported = True
                if stopping:
                    return
                try:
                    if disk is None:
                        disk = UsageStore(db_path, timeout=0.05)
                    if cleanup:
                        disk.close_dangling_intervals()
                        with self._condition:
                            self._cleanup_requested = False
                    if item is not None:
                        key, row = item
                        disk._save_row(row)
                        with self._condition:
                            if self._dirty.get(key) is row:
                                del self._dirty[key]
                    self._publish_snapshot(disk)
                    if self._write_error is not None:
                        log_event(logger, "usage.write_recovered")
                    self._write_error = None
                except (sqlite3.Error, OSError) as exc:
                    if self._write_error is None:
                        log_event(logger, "usage.write_degraded", error_type=type(exc).__name__,
                                  level=logging.WARNING)
                    self._write_error = type(exc).__name__
                if time.monotonic() - refreshed >= self._REFRESH_SECONDS:
                    self._refresh_other(other)
                    refreshed = time.monotonic()
                self._ready.set()
                with self._condition:
                    if self._write_error or (not self._dirty and not self._cleanup_requested):
                        self._condition.wait(0.1 if self._write_error else self._REFRESH_SECONDS)
        finally:
            if disk is not None:
                disk._conn.close()
