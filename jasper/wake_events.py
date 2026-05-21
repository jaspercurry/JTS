"""Wake-event telemetry store — SQLite + audio capture + retention.

PR 3 of the wake-telemetry series. The architectural spec and the
operator-facing decisions live in `docs/HANDOFF-wake-telemetry.md` —
read that first for the schema rationale, retention policy, and the
funnel-stage semantics.

Per-wake-event flow:

  1. `begin_event(...)`     — wake fires, INSERT row with trigger
                              metadata + context; outcome='in_progress'.
                              Cheap (~1ms in WAL mode), happens on the
                              wake hot path.
  2. `attach_audio(...)`    — ~2s later, WAVs are flushed to disk and
                              the row's audio_*_path columns get
                              populated. Async, off the wake path.
  3. `update_stage(...)`    — each funnel transition (turn_opened,
                              speech_detected, ...). Single-column
                              UPDATE; safe to call any number of
                              times per event.
  4. `set_outcome(...)`     — terminal state. Sets the `outcome`
                              + `outcome_detail` fields.
  5. Retention sweep        — on every audio attach, total dir size is
                              checked; oldest WAVs deleted oldest-
                              first until under the 500 MB cap.
                              DB rows are kept forever — only the
                              audio_*_path columns get a `'rolled_off'`
                              sentinel.

Threading / async model: SQLite with WAL mode permits multiple
readers + a single writer per connection. We use one connection per
store instance + an asyncio.Lock around writes so concurrent funnel
UPDATEs from different async tasks serialize cleanly. The DB calls
are CPU-bound but short (~1ms each); we do NOT wrap them in
`run_in_executor` because that adds more latency than it saves and
SQLite's busy-handler covers the contention case.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# 16 kHz mono int16 — the format MicCapture and UdpMicCapture both emit.
# Captures retain the original byte stream verbatim so any future
# offline analysis sees exactly what the wake-word model saw.
SAMPLE_RATE_HZ = 16000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1

# Capture window. 4 s pre + 2 s post around the wake event = 6 s total
# per leg. At 16 kHz mono int16 that's 192 KB per leg = 384 KB per
# event. The 6 s window is sized to give human reviewers enough lead-in
# context to recognise the wake utterance + a couple of seconds of
# what came after.
CAPTURE_PRE_SEC = 4.0
CAPTURE_POST_SEC = 2.0

# Retention cap — total bytes of WAV files in the directory. DB rows
# (and their referenced sentinel paths) survive forever; only audio
# gets pruned. At ~400 KB/event this holds ~1250 events ≈ 3-6 weeks at
# typical use, which matches the human-review cadence the operator is
# planning.
DEFAULT_MAX_AUDIO_BYTES = 500 * 1024 * 1024  # 500 MB

# Sentinel written into audio_*_path when retention deletes the WAV.
# Queries can filter `audio_on_path != 'rolled_off' AND IS NOT NULL`
# to restrict to events that still have audio on disk.
ROLLED_OFF_SENTINEL = "rolled_off"


# Funnel-stage names. Mapped to the corresponding ts_* column in
# `update_stage`. Validated up-front so a typo in a hook call site
# fails loudly during dev rather than silently no-op'ing in production.
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

# Outcomes terminal-state codes can take. Validation lives in
# set_outcome; this is the source of truth + the docs cross-ref.
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


# DDL — single statement that initialises a fresh store. Idempotent
# via `CREATE TABLE IF NOT EXISTS`. Schema changes after first ship
# need a migration step (add a `schema_version` table + `ALTER TABLE`
# stanzas keyed by the recorded version); we defer that until needed.
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
  voice_provider      TEXT,
  bridge_config_json  TEXT,

  audio_on_path       TEXT,
  audio_off_path      TEXT,

  label               TEXT,
  label_notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_wake_events_ts       ON wake_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_wake_events_outcome  ON wake_events(outcome);
CREATE INDEX IF NOT EXISTS idx_wake_events_trigger  ON wake_events(trigger_kind);
CREATE INDEX IF NOT EXISTS idx_wake_events_label    ON wake_events(label);
"""


def make_event_id(now: datetime | None = None, seq: int = 1) -> str:
    """Generate a sortable event id like `20260522T143011Z-001`.

    Sortability matters — flat file listings in
    /var/lib/jasper/wake-events/ stay chronological under the default
    `ls`. The 3-digit sequence handles burst-fires within the same
    second (rare given the 0.7 s refractory, but possible after a
    daemon restart in the same wall-clock second)."""
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{seq:03d}"


def _now_iso() -> str:
    """UTC ISO8601 timestamp with millisecond precision. Stored
    verbatim in ts_* columns — readable in SQL output, comparable
    lexicographically (lex-order matches time-order for ISO8601)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_wav(path: Path, pcm: bytes) -> None:
    """Write 16 kHz mono int16 PCM to a WAV. Atomic via tempfile +
    rename — the wake-review UI may scan the directory while writes
    are in flight, and a partially-written WAV would be opened as a
    short or corrupt file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH_BYTES)
        w.setframerate(SAMPLE_RATE_HZ)
        w.writeframes(pcm)
    os.replace(tmp, path)


class WakeEventStore:
    """SQLite-backed wake-event log with audio capture + retention.

    Construct once per daemon; pass the same instance to every code
    path that needs to record wake-event state. All public methods
    are async and serialise through `_write_lock` so concurrent
    funnel UPDATEs from independent async tasks stay consistent."""

    def __init__(
        self,
        base_dir: Path | str,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._max_audio_bytes = int(max_audio_bytes)
        self._db_path = self._base_dir / "wake-events.sqlite3"
        self._conn: sqlite3.Connection | None = None
        # Lazy-initialized inside an async method so the lock binds
        # to the running event loop. In Python 3.9, constructing
        # `asyncio.Lock()` at module-or-class-construction time pins
        # it to the loop active *then* — but pytest fixtures often
        # construct the store outside an async context, and the test
        # then runs with a different loop. Initialising on first
        # async use side-steps this entirely.
        self._write_lock: asyncio.Lock | None = None

    # ----- lifecycle ------------------------------------------------

    def open(self) -> None:
        """Initialise the directory + open the SQLite connection.

        Synchronous because it runs at daemon startup before any
        async work begins. Idempotent — calling twice is safe."""
        if self._conn is not None:
            return
        self._base_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we use WAL durability
        )
        # WAL mode: writers don't block readers; per-row writes are
        # durable without explicit BEGIN/COMMIT cycles. The synchronous
        # mode NORMAL is the WAL-tuned default (FULL would fsync on
        # every commit — overkill for this workload).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA_SQL)
        self._conn = conn
        logger.info(
            "wake_events: opened %s (max_audio_bytes=%d MB)",
            self._db_path, self._max_audio_bytes // (1024 * 1024),
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ----- writes ---------------------------------------------------

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
        voice_provider: str | None = None,
        bridge_config: dict[str, Any] | None = None,
    ) -> None:
        """INSERT a new wake event row. Audio is attached separately
        via `attach_audio` after the post-fire capture window closes —
        this method is on the wake hot path and stays at SQLite-only
        cost (~1ms)."""
        self._require_open()
        bridge_config_json = (
            json.dumps(bridge_config, sort_keys=True)
            if bridge_config else None
        )
        async with self._lock():
            self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO wake_events (
                  event_id, ts_utc, trigger_kind,
                  peak_score_aec_on, peak_score_aec_off,
                  peak_offset_ms_on, peak_offset_ms_off,
                  threshold, outcome,
                  wake_model,
                  music_active, music_renderer, music_volume_db,
                  voice_provider, bridge_config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, _now_iso(), trigger_kind,
                    peak_score_aec_on, peak_score_aec_off,
                    peak_offset_ms_on, peak_offset_ms_off,
                    threshold,
                    wake_model,
                    1 if music_active else 0,
                    music_renderer, music_volume_db,
                    voice_provider, bridge_config_json,
                ),
            )

    async def attach_audio(
        self,
        *,
        event_id: str,
        audio_on: bytes | None,
        audio_off: bytes | None,
    ) -> None:
        """Write the per-leg WAVs to disk and UPDATE the row with
        their filenames. Triggers a retention sweep if the total
        directory size exceeds the cap.

        Either or both legs may be None — the audio path remains NULL
        for any leg that produced no audio (rare; bridge stalled, or
        single-stream mode where there's no AEC OFF leg)."""
        self._require_open()
        on_filename: str | None = None
        off_filename: str | None = None
        if audio_on is not None:
            on_filename = f"{event_id}.aec-on.wav"
            _write_wav(self._base_dir / on_filename, audio_on)
        if audio_off is not None:
            off_filename = f"{event_id}.aec-off.wav"
            _write_wav(self._base_dir / off_filename, audio_off)
        async with self._lock():
            self._conn.execute(  # type: ignore[union-attr]
                """
                UPDATE wake_events
                SET audio_on_path = ?, audio_off_path = ?
                WHERE event_id = ?
                """,
                (on_filename, off_filename, event_id),
            )
        # Retention sweep AFTER the new files are written, so the
        # newly-written WAVs are eligible to survive the sweep (the
        # OS-level mtime is set on rename above).
        await self._retention_sweep()

    async def update_stage(
        self,
        event_id: str,
        stage: str,
        ts: str | None = None,
    ) -> None:
        """Set the named ts_* column to `ts` (default: now()).
        Idempotent — calling twice just overwrites with the later
        timestamp, which is harmless. The stage name must be one of
        the keys in _STAGE_TO_COLUMN; invalid names raise ValueError."""
        column = _STAGE_TO_COLUMN.get(stage)
        if column is None:
            raise ValueError(
                f"unknown wake-event stage {stage!r}; "
                f"expected one of {sorted(_STAGE_TO_COLUMN)}"
            )
        self._require_open()
        async with self._lock():
            self._conn.execute(  # type: ignore[union-attr]
                # Column name is from a closed allowlist, not user input —
                # safe to interpolate.
                f"UPDATE wake_events SET {column} = ? WHERE event_id = ?",
                (ts or _now_iso(), event_id),
            )

    async def set_outcome(
        self,
        event_id: str,
        outcome: str,
        outcome_detail: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        """Set the terminal outcome for the event. Validates the
        outcome string against the closed set to catch typos at the
        hook site rather than letting nonsense ship to production
        SQL."""
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"unknown wake-event outcome {outcome!r}; "
                f"expected one of {sorted(_VALID_OUTCOMES)}"
            )
        self._require_open()
        async with self._lock():
            self._conn.execute(  # type: ignore[union-attr]
                """
                UPDATE wake_events
                SET outcome = ?, outcome_detail = ?, tool_name = COALESCE(?, tool_name)
                WHERE event_id = ?
                """,
                (outcome, outcome_detail, tool_name, event_id),
            )

    # ----- reads (mostly for tests / future review UI) --------------

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Fetch a single event row as a dict; None if missing.
        Mostly used by tests today; the future /wake-review/ UI will
        use richer batched reads."""
        self._require_open()
        async with self._lock():
            cur = self._conn.execute(  # type: ignore[union-attr]
                "SELECT * FROM wake_events WHERE event_id = ?",
                (event_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    # ----- retention ------------------------------------------------

    async def _retention_sweep(self) -> None:
        """If total WAV bytes exceed the cap, delete oldest WAVs
        oldest-first until under the cap. The DB rows survive — only
        their audio_*_path values are rewritten to the sentinel so
        queries can still see which events used to have audio.

        Called after every `attach_audio`. Cheap when under the cap
        (a single dir-scan), more work when pruning kicks in (still
        bounded — we never delete more than necessary)."""
        files = sorted(
            self._base_dir.glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(f.stat().st_size for f in files)
        if total <= self._max_audio_bytes:
            return
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
            # Filename shape: `<event_id>.aec-on.wav` or `.aec-off.wav`.
            # Strip both extensions to recover the event_id.
            event_id = f.name.rsplit(".aec-", 1)[0]
            deleted_event_ids.add(event_id)
        if deleted_event_ids:
            await self._mark_audio_rolled_off(deleted_event_ids)
            logger.info(
                "wake_events: retention pruned %d event(s) audio "
                "(dir now %.1f MB / cap %.1f MB)",
                len(deleted_event_ids),
                total / (1024 * 1024),
                self._max_audio_bytes / (1024 * 1024),
            )

    async def _mark_audio_rolled_off(self, event_ids: Iterable[str]) -> None:
        """Bulk-UPDATE audio_*_path → sentinel for events whose WAVs
        were just pruned. The sentinel preserves the historical fact
        that audio existed (vs NULL, which would mean "no audio was
        ever captured")."""
        self._require_open()
        async with self._lock():
            self._conn.executemany(  # type: ignore[union-attr]
                """
                UPDATE wake_events
                SET audio_on_path  = CASE WHEN audio_on_path  IS NOT NULL
                                          THEN ? ELSE NULL END,
                    audio_off_path = CASE WHEN audio_off_path IS NOT NULL
                                          THEN ? ELSE NULL END
                WHERE event_id = ?
                """,
                [
                    (ROLLED_OFF_SENTINEL, ROLLED_OFF_SENTINEL, eid)
                    for eid in event_ids
                ],
            )

    # ----- internal -------------------------------------------------

    def _require_open(self) -> None:
        if self._conn is None:
            raise RuntimeError(
                "WakeEventStore.open() must be called before any read/write"
            )

    def _lock(self) -> asyncio.Lock:
        """Return the write lock, creating it on first call from
        within an async context. See `__init__` for the lazy-init
        rationale."""
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock
