# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import wave
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

logger = logging.getLogger(__name__)


# Mic capture emits 16 kHz mono int16. Five six-second legs retain 960 KB.
SAMPLE_RATE_HZ = 16000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1

CAPTURE_PRE_SEC = 4.0
CAPTURE_POST_SEC = 2.0

MAX_PENDING_WORK = 64
MAX_PENDING_BYTES = 2 * 1024 * 1024

DEFAULT_MAX_AUDIO_BYTES = 128 * 1024 * 1024  # 128 MiB

ROLLED_OFF_SENTINEL = "rolled_off"


_STAGE_TO_COLUMN: dict[str, str] = {
    "late_cancel":      "ts_late_cancel",
    "peer_lost":        "ts_peer_lost",
    "gate_blocked":     "ts_gate_blocked",
    "turn_opened":      "ts_turn_opened",
    "speech_detected":  "ts_speech_detected",
    "response_started": "ts_response_started",
    "tool_called":      "ts_tool_called",
    "tool_completed":   "ts_tool_completed",
    "turn_complete":    "ts_turn_complete",
}

_VALID_OUTCOMES = frozenset({
    "in_progress",   # initial state on begin_event
    "completed",     # turn ran end-to-end naturally
    "late_cancel",   # mic muted / correction window opened mid-wake
    "peer_lost",     # another Pi won arbitration
    "gate_blocked",  # spend cap reached / connection paused
    "no_speech",     # session opened but VAD never saw user speech
    "session_failed",
    "tool_failed",
})


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wake_events (
  event_id            TEXT PRIMARY KEY,
  ts_utc              TEXT NOT NULL,

  trigger_kind        TEXT NOT NULL,
  peak_score_aec_on   REAL,
  peak_score_aec_off  REAL,
  peak_offset_ms_on   INTEGER,
  peak_offset_ms_off  INTEGER,
  threshold           REAL NOT NULL,

  ts_late_cancel      TEXT,
  ts_peer_lost        TEXT,
  ts_gate_blocked     TEXT,
  ts_turn_opened      TEXT,
  ts_speech_detected  TEXT,
  ts_response_started TEXT,
  ts_tool_called      TEXT,
  ts_tool_completed   TEXT,
  ts_turn_complete    TEXT,

  outcome             TEXT NOT NULL,
  outcome_detail      TEXT,
  tool_name           TEXT,

  wake_model          TEXT NOT NULL,
  music_active        INTEGER NOT NULL DEFAULT 0,
  music_renderer      TEXT,
  music_volume_db     REAL,
  condition_class     TEXT,
  voice_provider      TEXT,
  bridge_config_json  TEXT,

  audio_on_path       TEXT,
  audio_off_path      TEXT,
  audio_chip_aec_150_path TEXT,
  audio_chip_aec_210_path TEXT,

  label               TEXT,
  label_notes         TEXT,

  mic_muted           INTEGER,    -- 0/1; null on pre-migration rows
  mic_rms_dbfs_on     REAL,       -- instantaneous RMS at fire-time, AEC ON leg
  mic_rms_dbfs_off    REAL,       -- same for AEC OFF; null in single-stream

  peak_score_chip_aec_150     REAL,
  peak_score_chip_aec_210     REAL,
  peak_offset_ms_chip_aec_150 INTEGER,
  peak_offset_ms_chip_aec_210 INTEGER,
  mic_rms_dbfs_chip_aec_150   REAL,
  mic_rms_dbfs_chip_aec_210   REAL
);

CREATE INDEX IF NOT EXISTS idx_wake_events_ts       ON wake_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_wake_events_outcome  ON wake_events(outcome);
CREATE INDEX IF NOT EXISTS idx_wake_events_trigger  ON wake_events(trigger_kind);
CREATE INDEX IF NOT EXISTS idx_wake_events_label    ON wake_events(label);
"""

_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("mic_muted", "INTEGER"),
    ("mic_rms_dbfs_on", "REAL"),
    ("mic_rms_dbfs_off", "REAL"),
    ("peak_score_dtln_aec", "REAL"),
    ("peak_offset_ms_dtln", "INTEGER"),
    ("mic_rms_dbfs_dtln", "REAL"),
    ("audio_dtln_path", "TEXT"),
    ("audio_chip_aec_150_path", "TEXT"),
    ("audio_chip_aec_210_path", "TEXT"),
    ("fired_legs", "TEXT"),
    ("max_silero_aec", "REAL"),
    ("max_silero_raw", "REAL"),
    ("silero_aec_armed_at_ms", "INTEGER"),
    ("silero_raw_armed_at_ms", "INTEGER"),
    ("endpointer", "TEXT"),
    ("transcript_nonempty", "INTEGER"),
    ("music_playing_at_turn", "INTEGER"),
    ("music_db_at_turn", "REAL"),
    ("music_renderer", "TEXT"),
    ("condition_class", "TEXT"),
    ("peak_score_chip_aec_150", "REAL"),
    ("peak_score_chip_aec_210", "REAL"),
    ("peak_offset_ms_chip_aec_150", "INTEGER"),
    ("peak_offset_ms_chip_aec_210", "INTEGER"),
    ("mic_rms_dbfs_chip_aec_150", "REAL"),
    ("mic_rms_dbfs_chip_aec_210", "REAL"),
]


def make_event_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{now.microsecond:06d}-{uuid4().hex}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_wav(path: Path, pcm: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH_BYTES)
        w.setframerate(SAMPLE_RATE_HZ)
        w.writeframes(pcm)
    os.replace(tmp, path)


def _retained_bytes(value: Any) -> int:
    return sys.getsizeof(value) + (
        sum(_retained_bytes(item) for item in value) if isinstance(value, tuple) else 0
    )


class WakeEventStore:
    """One ordered storage worker. Writes acknowledge admission, reads await completion.

    Pending counts and bytes include the active operation. Producers never wait
    for storage or capacity. Only immutable arguments cross the worker boundary.
    """

    def __init__(
        self,
        base_dir: Path | str,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._max_audio_bytes = int(max_audio_bytes)
        self._db_path = self._base_dir / "wake-events.sqlite3"
        self._conn: sqlite3.Connection | None = None
        self._audio_bytes_estimate: int | None = None
        self._condition = threading.Condition()
        self._queue: deque[tuple[Callable, tuple, Future, int]] = deque()
        self._worker: threading.Thread | None = None
        self._closed: Future[None] = Future()
        self._stopping = False
        self._pending = self._pending_bytes = 0
        self._discarded = self._write_errors = 0
        self._last_error: str | None = None

    def open(self) -> None:
        """Startup only: return once the worker has opened and migrated storage."""
        if self._worker is not None:
            return
        ready: Future[None] = Future()
        self._worker = threading.Thread(
            target=self._run, args=(ready,), name="wake-events", daemon=True,
        )
        self._worker.start()
        ready.result()

    def _open_database(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=0.1,  # Bound contention when draining the 64-operation queue at shutdown.
            isolation_level=None,  # autocommit; we use WAL durability
        )
        self._conn = conn
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA_SQL)
        cur = conn.execute("PRAGMA table_info(wake_events)")
        existing_cols = {row[1] for row in cur.fetchall()}
        added: list[str] = []
        for col, typ in _MIGRATION_COLUMNS:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE wake_events ADD COLUMN {col} {typ}")
                added.append(col)
        if added:
            logger.info(
                "wake_events: schema migration added columns: %s",
                ", ".join(added),
            )
        logger.info(
            "wake_events: opened %s (max_audio_bytes=%d MB)",
            self._db_path, self._max_audio_bytes // (1024 * 1024),
        )

    def _run(self, ready: Future[None]) -> None:
        try:
            try:
                self._open_database()
            except Exception as exc:  # noqa: BLE001
                ready.set_exception(exc)
                return
            ready.set_result(None)
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._queue or self._stopping)
                    if not self._queue:
                        break
                    operation, args, result, size = self._queue.popleft()
                try:
                    value = operation(*args)
                except Exception as exc:  # noqa: BLE001
                    with self._condition:
                        self._write_errors += 1
                        self._last_error = type(exc).__name__
                    logger.warning("event=wake_events.write_failed error=%s", type(exc).__name__)
                    result.set_exception(exc)
                else:
                    result.set_result(value)
                    del value
                finally:
                    with self._condition:
                        self._pending -= 1
                        self._pending_bytes -= size
                    # Do not retain a completed PCM payload while waiting for work.
                    del operation, args, result
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._closed.set_result(None)

    def _stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify()

    def close(self) -> None:
        """Stop admission, finish accepted work, then close on the owning thread."""
        self._stop()
        if self._worker is not None:
            self._worker.join()

    async def aclose(self) -> None:
        self._stop()
        if self._worker is None:
            return
        done = asyncio.wrap_future(self._closed)
        cancelled = False
        while not done.done():
            try:
                await asyncio.shield(done)
            except asyncio.CancelledError:
                cancelled = True
        done.result()
        if cancelled:
            raise asyncio.CancelledError

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "accepting": self._worker is not None and not self._stopping and not self._closed.done(),
                "pending_work": self._pending,
                "pending_bytes": self._pending_bytes,
                "max_pending_work": MAX_PENDING_WORK,
                "max_pending_bytes": MAX_PENDING_BYTES,
                "discarded_work": self._discarded,
                "write_errors": self._write_errors,
                "last_error": self._last_error,
            }

    def _require_open(self) -> None:
        if self._worker is None or self._stopping or self._closed.done():
            raise RuntimeError("WakeEventStore.open() must precede reads/writes; storage is closed")

    def _enqueue(self, operation: Callable, *args: Any) -> Future | None:
        size = _retained_bytes(args)
        with self._condition:
            self._require_open()
            if self._pending >= MAX_PENDING_WORK or self._pending_bytes + size > MAX_PENDING_BYTES:
                self._discarded += 1
                return None
            result: Future = Future()
            result.set_running_or_notify_cancel()
            self._queue.append((operation, args, result, size))
            self._pending += 1
            self._pending_bytes += size
            self._condition.notify()
            return result

    async def _result(self, operation: Callable, *args: Any) -> Any:
        result = self._enqueue(operation, *args)
        if result is None:
            raise RuntimeError("wake-event storage queue is full")
        return await asyncio.wrap_future(result)

    def _execute(self, sql: str, params: tuple) -> None:
        self._conn.execute(sql, params)  # type: ignore[union-attr]

    async def begin_event(
        self,
        *,
        event_id: str,
        trigger_kind: str,
        peak_score_aec_on: float | None,
        peak_score_aec_off: float | None,
        peak_offset_ms_on: int | None = None,
        peak_offset_ms_off: int | None = None,
        threshold: float,
        wake_model: str,
        music_active: bool = False,
        music_renderer: str | None = None,
        music_volume_db: float | None = None,
        condition_class: str | None = None,
        voice_provider: str | None = None,
        bridge_config: dict[str, Any] | None = None,
        mic_muted: bool | None = None,
        mic_rms_dbfs_on: float | None = None,
        mic_rms_dbfs_off: float | None = None,
        peak_score_dtln_aec: float | None = None,
        peak_offset_ms_dtln: int | None = None,
        mic_rms_dbfs_dtln: float | None = None,
        fired_legs: str | None = None,
        peak_score_chip_aec_150: float | None = None,
        peak_score_chip_aec_210: float | None = None,
        peak_offset_ms_chip_aec_150: int | None = None,
        peak_offset_ms_chip_aec_210: int | None = None,
        mic_rms_dbfs_chip_aec_150: float | None = None,
        mic_rms_dbfs_chip_aec_210: float | None = None,
    ) -> bool:
        bridge_config_json = (
            json.dumps(bridge_config, sort_keys=True)
            if bridge_config else None
        )
        return self._enqueue(self._execute,
            """
            INSERT INTO wake_events (
              event_id, ts_utc, trigger_kind,
              peak_score_aec_on, peak_score_aec_off,
              peak_offset_ms_on, peak_offset_ms_off,
              threshold, outcome,
              wake_model,
              music_active, music_renderer, music_volume_db,
              condition_class,
              voice_provider, bridge_config_json,
              mic_muted, mic_rms_dbfs_on, mic_rms_dbfs_off,
              peak_score_dtln_aec, peak_offset_ms_dtln,
              mic_rms_dbfs_dtln, fired_legs,
              peak_score_chip_aec_150, peak_score_chip_aec_210,
              peak_offset_ms_chip_aec_150, peak_offset_ms_chip_aec_210,
              mic_rms_dbfs_chip_aec_150, mic_rms_dbfs_chip_aec_210
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?
            )
            """,
            (
                event_id, _now_iso(), trigger_kind,
                peak_score_aec_on, peak_score_aec_off,
                peak_offset_ms_on, peak_offset_ms_off,
                threshold,
                wake_model,
                1 if music_active else 0,
                music_renderer, music_volume_db,
                condition_class,
                voice_provider, bridge_config_json,
                None if mic_muted is None else (1 if mic_muted else 0),
                mic_rms_dbfs_on, mic_rms_dbfs_off,
                peak_score_dtln_aec, peak_offset_ms_dtln,
                mic_rms_dbfs_dtln, fired_legs,
                peak_score_chip_aec_150, peak_score_chip_aec_210,
                peak_offset_ms_chip_aec_150, peak_offset_ms_chip_aec_210,
                mic_rms_dbfs_chip_aec_150, mic_rms_dbfs_chip_aec_210,
            ),
        ) is not None

    async def attach_audio(
        self,
        *,
        event_id: str,
        audio_on: bytes | None,
        audio_off: bytes | None,
        audio_dtln: bytes | None = None,
        audio_chip_aec_150: bytes | None = None,
        audio_chip_aec_210: bytes | None = None,
    ) -> bool:
        legs = ("on", "off", "dtln", "chip-aec-150", "chip-aec-210")
        audio = (audio_on, audio_off, audio_dtln, audio_chip_aec_150, audio_chip_aec_210)
        files = tuple(
            (f"{event_id}.aec-{leg}.wav", bytes(pcm)) if pcm is not None else (None, None)
            for leg, pcm in zip(legs, audio)
        )
        return self._enqueue(self._attach_audio, event_id, files) is not None

    def _attach_audio(self, event_id: str, files: tuple) -> None:
        written_bytes = self._write_wavs_blocking(files)
        if self._audio_bytes_estimate is not None:
            self._audio_bytes_estimate += written_bytes
        self._execute(
            """UPDATE wake_events SET audio_on_path = ?, audio_off_path = ?,
               audio_dtln_path = ?, audio_chip_aec_150_path = ?,
               audio_chip_aec_210_path = ? WHERE event_id = ?""",
            (*[name for name, _ in files], event_id),
        )
        self._retention_sweep()

    async def update_stage(
        self,
        event_id: str,
        stage: str,
        ts: str | None = None,
        *,
        tool_name: str | None = None,
    ) -> bool:
        column = _STAGE_TO_COLUMN.get(stage)
        if column is None:
            raise ValueError(
                f"unknown wake-event stage {stage!r}; "
                f"expected one of {sorted(_STAGE_TO_COLUMN)}"
            )
        timestamp = ts or _now_iso()
        if stage == "tool_called":
            return self._enqueue(self._execute,
                f"""
                UPDATE wake_events
                SET {column} = COALESCE({column}, ?),
                    tool_name = COALESCE(tool_name, ?)
                WHERE event_id = ?
                """,
                (timestamp, tool_name, event_id),
            ) is not None
        elif stage == "tool_completed":
            return self._enqueue(self._execute,
                f"""
                UPDATE wake_events
                SET {column} = COALESCE({column}, ?)
                WHERE event_id = ?
                """,
                (timestamp, event_id),
            ) is not None
        else:
            return self._enqueue(self._execute,
                f"UPDATE wake_events SET {column} = ? WHERE event_id = ?",
                (timestamp, event_id),
            ) is not None

    async def set_outcome(
        self,
        event_id: str,
        outcome: str,
        outcome_detail: str | None = None,
        tool_name: str | None = None,
    ) -> bool:
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"unknown wake-event outcome {outcome!r}; "
                f"expected one of {sorted(_VALID_OUTCOMES)}"
            )
        return self._enqueue(self._execute,
            """
            UPDATE wake_events
            SET outcome = ?, outcome_detail = ?, tool_name = COALESCE(?, tool_name)
            WHERE event_id = ?
            """,
            (outcome, outcome_detail, tool_name, event_id),
        ) is not None

    async def update_session_vad(
        self,
        event_id: str,
        *,
        max_silero_aec: float | None = None,
        max_silero_raw: float | None = None,
        silero_aec_armed_at_ms: int | None = None,
        silero_raw_armed_at_ms: int | None = None,
        endpointer: str | None = None,
        transcript_nonempty: bool | None = None,
        music_playing_at_turn: bool | None = None,
        music_db_at_turn: float | None = None,
    ) -> bool:
        return self._enqueue(self._execute,
            """
            UPDATE wake_events SET
                max_silero_aec = COALESCE(?, max_silero_aec),
                max_silero_raw = COALESCE(?, max_silero_raw),
                silero_aec_armed_at_ms = COALESCE(?, silero_aec_armed_at_ms),
                silero_raw_armed_at_ms = COALESCE(?, silero_raw_armed_at_ms),
                endpointer = COALESCE(?, endpointer),
                transcript_nonempty = COALESCE(?, transcript_nonempty),
                music_playing_at_turn = COALESCE(?, music_playing_at_turn),
                music_db_at_turn = COALESCE(?, music_db_at_turn)
            WHERE event_id = ?
            """,
            (
                max_silero_aec, max_silero_raw,
                silero_aec_armed_at_ms, silero_raw_armed_at_ms,
                endpointer,
                int(transcript_nonempty) if transcript_nonempty is not None else None,
                int(music_playing_at_turn) if music_playing_at_turn is not None else None,
                music_db_at_turn,
                event_id,
            ),
        ) is not None

    async def record_flag(self, reason: str) -> dict[str, Any] | None:
        return await self._result(self._record_flag, f"{_now_iso()}|{reason}")

    def _record_flag(self, label_notes: str) -> dict[str, Any] | None:
        self._conn.execute("BEGIN IMMEDIATE")  # type: ignore[union-attr]
        try:
            rows = self._conn.execute(  # type: ignore[union-attr]
                """SELECT event_id, ts_utc, outcome FROM wake_events
                   WHERE label IS NULL OR label != 'flag_action'
                   ORDER BY ts_utc DESC, rowid DESC LIMIT 2""",
            ).fetchall()
            if len(rows) < 2:
                return None
            current, target = rows
            self._execute(
                "UPDATE wake_events SET label = 'voice_flagged', label_notes = ? WHERE event_id = ?",
                (label_notes, target[0]),
            )
            self._execute(
                "UPDATE wake_events SET label = 'flag_action' WHERE event_id = ?", (current[0],),
            )
            self._conn.execute("COMMIT")  # type: ignore[union-attr]
            return {
                "flagged_event_id": target[0], "flagged_ts_utc": target[1],
                "flagged_outcome": target[2], "flag_action_event_id": current[0],
            }
        finally:
            if self._conn.in_transaction:  # type: ignore[union-attr]
                self._conn.execute("ROLLBACK")  # type: ignore[union-attr]

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Read after all previously accepted work; cancellation leaves it ordered."""
        return await self._result(self._get_event, event_id)

    def _get_event(self, event_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM wake_events WHERE event_id = ?", (event_id,),
        )
        row = cur.fetchone()
        return dict(zip((d[0] for d in cur.description), row)) if row else None

    def _write_wavs_blocking(
        self, to_write: tuple,
    ) -> int:
        written = 0
        for filename, pcm in to_write:
            if filename is None:
                continue
            path = self._base_dir / filename
            _write_wav(path, pcm)
            written += path.stat().st_size
        return written

    def _retention_sweep(self) -> None:
        if (
            self._audio_bytes_estimate is not None
            and self._audio_bytes_estimate <= self._max_audio_bytes
        ):
            return
        deleted_event_ids, total = self._scan_and_prune_blocking()
        self._audio_bytes_estimate = total
        if deleted_event_ids:
            self._mark_audio_rolled_off(deleted_event_ids)
            logger.info(
                "wake_events: retention pruned %d event(s) audio "
                "(dir now %.1f MB / cap %.1f MB)",
                len(deleted_event_ids),
                total / (1024 * 1024),
                self._max_audio_bytes / (1024 * 1024),
            )

    def _scan_and_prune_blocking(self) -> tuple[set[str], int]:
        files = sorted(
            self._base_dir.glob("*.wav"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        )
        total = sum(f.stat().st_size for f in files)
        deleted_event_ids: set[str] = set()
        for f in files:
            if total <= self._max_audio_bytes:
                break
            sz = f.stat().st_size
            try:
                f.unlink()
            except OSError as e:
                logger.warning("wake_events: failed to delete %s: %s", f, e)
                continue
            total -= sz
            event_id = f.name.rsplit(".aec-", 1)[0]
            deleted_event_ids.add(event_id)
        return deleted_event_ids, total

    def _mark_audio_rolled_off(self, event_ids: Iterable[str]) -> None:
        self._conn.executemany(  # type: ignore[union-attr]
            """
            UPDATE wake_events
            SET audio_on_path  = CASE WHEN audio_on_path  IS NOT NULL
                                      THEN ? ELSE NULL END,
                audio_off_path = CASE WHEN audio_off_path IS NOT NULL
                                      THEN ? ELSE NULL END,
                audio_dtln_path = CASE WHEN audio_dtln_path IS NOT NULL
                                       THEN ? ELSE NULL END,
                audio_chip_aec_150_path =
                    CASE WHEN audio_chip_aec_150_path IS NOT NULL
                         THEN ? ELSE NULL END,
                audio_chip_aec_210_path =
                    CASE WHEN audio_chip_aec_210_path IS NOT NULL
                         THEN ? ELSE NULL END
            WHERE event_id = ?
            """,
            [
                (
                    ROLLED_OFF_SENTINEL,
                    ROLLED_OFF_SENTINEL,
                    ROLLED_OFF_SENTINEL,
                    ROLLED_OFF_SENTINEL,
                    ROLLED_OFF_SENTINEL,
                    eid,
                )
                for eid in event_ids
            ],
        )
