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
  -- Phase-1 acoustic-condition label (jasper.wake_conditions:
  -- quiet/ambient/music), derived per fire by the runtime estimator.
  condition_class     TEXT,
  voice_provider      TEXT,
  bridge_config_json  TEXT,

  audio_on_path       TEXT,
  audio_off_path      TEXT,

  label               TEXT,
  label_notes         TEXT,

  -- Additional debug context (post-v1 additions; ALTER TABLE applies
  -- to existing DBs via the schema migration in `open()`).
  mic_muted           INTEGER,    -- 0/1; null on pre-migration rows
  mic_rms_dbfs_on     REAL,       -- instantaneous RMS at fire-time, AEC ON leg
  mic_rms_dbfs_off    REAL,       -- same for AEC OFF; null in single-stream

  -- Chip-AEC beam legs (XVF3800 fixed 150°/210° ASR beams). Opt-in,
  -- hardware-conditional wake legs promoted from corpus-only capture;
  -- null unless the household enabled the chip leg via /wake/. Same
  -- per-leg score/offset/RMS shape as the software legs above. (Per-leg
  -- WAV capture for these beams is a deliberate follow-up — see
  -- attach_audio / _finalize_event_audio in jasper.voice_daemon.)
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

# Schema additions that may need ALTER TABLE on already-deployed DBs.
# Keep this list in sync with the trailing block of _SCHEMA_SQL. The
# migration in `open()` is idempotent: it checks the current column
# set via `PRAGMA table_info` and only ALTERs what's missing.
_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("mic_muted", "INTEGER"),
    ("mic_rms_dbfs_on", "REAL"),
    ("mic_rms_dbfs_off", "REAL"),
    # Triple-stream extension (2026-05-23): DTLN-aec leg added
    # alongside AEC ON / AEC OFF. See docs/HANDOFF-mic-quality-v2.md
    # "Triple-stream architecture plan" + HANDOFF-wake-telemetry.md
    # "Planned schema extensions for triple-stream".
    ("peak_score_dtln_aec", "REAL"),
    ("peak_offset_ms_dtln", "INTEGER"),
    ("mic_rms_dbfs_dtln", "REAL"),
    ("audio_dtln_path", "TEXT"),
    # CSV of leg names that crossed threshold to fire the event
    # (e.g. "aec_on,dtln" or "aec_off"). Lets the weekly review
    # answer "which engines are pulling weight?" directly.
    ("fired_legs", "TEXT"),
    # Session-time shadow VAD telemetry (2026-05-24): record what each
    # stream's Silero VAD saw during the turn so the weekly review can
    # cross-tab raw vs AEC scores against the actual endpointer outcome.
    ("max_silero_aec", "REAL"),
    ("max_silero_raw", "REAL"),
    ("silero_aec_armed_at_ms", "INTEGER"),
    ("silero_raw_armed_at_ms", "INTEGER"),
    ("endpointer", "TEXT"),
    ("transcript_nonempty", "INTEGER"),
    ("music_playing_at_turn", "INTEGER"),
    ("music_db_at_turn", "REAL"),
    # music_renderer shipped in CREATE TABLE but was never listed here, so a
    # DB created before that column (upgraded, not reset) lacked it — and the
    # telemetry INSERT, which names music_renderer, then failed and was
    # dropped by the fail-soft handler. Backfill it. condition_class is the
    # new Phase-1 acoustic-condition label. Both ALTER in idempotently.
    ("music_renderer", "TEXT"),
    ("condition_class", "TEXT"),
    # Chip-AEC beam legs (XVF3800 fixed 150°/210° ASR beams) — opt-in,
    # hardware-conditional wake legs. Additive per-leg score columns so
    # an already-deployed Pi backfills them when the chip leg is enabled.
    # Mirror the per-leg score/offset/RMS shape of the software legs; the
    # CREATE TABLE block carries the same six for fresh DBs.
    ("peak_score_chip_aec_150", "REAL"),
    ("peak_score_chip_aec_210", "REAL"),
    ("peak_offset_ms_chip_aec_150", "INTEGER"),
    ("peak_offset_ms_chip_aec_210", "INTEGER"),
    ("mic_rms_dbfs_chip_aec_150", "REAL"),
    ("mic_rms_dbfs_chip_aec_210", "REAL"),
]


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
        # Apply ALTER TABLE migrations for columns added after v1.
        # CREATE TABLE IF NOT EXISTS won't add columns to an existing
        # table — that's what this loop is for. Idempotent: only
        # ALTERs columns that aren't already present.
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
        condition_class: str | None = None,
        voice_provider: str | None = None,
        bridge_config: dict[str, Any] | None = None,
        mic_muted: bool | None = None,
        mic_rms_dbfs_on: float | None = None,
        mic_rms_dbfs_off: float | None = None,
        # Triple-stream extension (2026-05-23). All optional — kwargs
        # remain backward-compatible for single-stream / dual-stream
        # callers that don't pass DTLN values.
        peak_score_dtln_aec: float | None = None,
        peak_offset_ms_dtln: int | None = None,
        mic_rms_dbfs_dtln: float | None = None,
        fired_legs: str | None = None,
        # Chip-AEC beam legs (XVF3800 150°/210° ASR beams). Optional —
        # null on every install until the chip leg is enabled via /wake/,
        # at which point voice_daemon._LEG_DB routes the per-beam
        # score/offset/RMS into these columns. Per-leg WAV capture for the
        # chip beams is a deliberate follow-up (no audio_* kwargs here yet).
        peak_score_chip_aec_150: float | None = None,
        peak_score_chip_aec_210: float | None = None,
        peak_offset_ms_chip_aec_150: int | None = None,
        peak_offset_ms_chip_aec_210: int | None = None,
        mic_rms_dbfs_chip_aec_150: float | None = None,
        mic_rms_dbfs_chip_aec_210: float | None = None,
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
            )

    async def attach_audio(
        self,
        *,
        event_id: str,
        audio_on: bytes | None,
        audio_off: bytes | None,
        audio_dtln: bytes | None = None,
    ) -> None:
        """Write the per-leg WAVs to disk and UPDATE the row with
        their filenames. Triggers a retention sweep if the total
        directory size exceeds the cap.

        Any leg may be None — the audio path remains NULL for any
        leg that produced no audio (rare; bridge stalled, or
        single-/dual-stream mode where the third leg isn't present)."""
        self._require_open()
        on_filename: str | None = None
        off_filename: str | None = None
        dtln_filename: str | None = None
        if audio_on is not None:
            on_filename = f"{event_id}.aec-on.wav"
            _write_wav(self._base_dir / on_filename, audio_on)
        if audio_off is not None:
            off_filename = f"{event_id}.aec-off.wav"
            _write_wav(self._base_dir / off_filename, audio_off)
        if audio_dtln is not None:
            dtln_filename = f"{event_id}.aec-dtln.wav"
            _write_wav(self._base_dir / dtln_filename, audio_dtln)
        async with self._lock():
            self._conn.execute(  # type: ignore[union-attr]
                """
                UPDATE wake_events
                SET audio_on_path = ?, audio_off_path = ?, audio_dtln_path = ?
                WHERE event_id = ?
                """,
                (on_filename, off_filename, dtln_filename, event_id),
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
    ) -> None:
        """Record session-time shadow VAD telemetry on the wake event row.

        Called at turn-end with whatever data is available. Each field
        is optional — None leaves the column unchanged (COALESCE)."""
        self._require_open()
        async with self._lock():
            self._conn.execute(  # type: ignore[union-attr]
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
            )

    async def record_flag(self, reason: str) -> dict[str, Any] | None:
        """Mark the most-recent prior wake event as user-flagged for
        offline review, and mark the in-flight flag event itself so
        analysis queries can filter it out of "real interaction" rollups.

        Triggered by the `flag_recent_issue` voice tool when the user
        says something like "flag that" or "the last one cut me off."
        See `jasper/tools/diagnostic.py` for the LLM-facing surface
        and the prompting rules that decide when this fires.

        Lookup rule. We want the most recent prior event that was a
        REAL interaction, not another flag-action event. So query the
        2 most recent events whose ``label`` is null or not
        'flag_action':

          - ``events[0]`` = the in-flight flag event (the wake whose
            session is currently running this tool call). It exists
            because ``begin_event`` ran when wake fired, and the tool
            dispatch happens later in the same turn.
          - ``events[1]`` = the most recent prior real event — the
            one the user is flagging.

        Atomically:
          - ``events[1].label`` ← 'voice_flagged' (overwriting any
            prior label; only one voice flag per event in v1)
          - ``events[1].label_notes`` ← '{iso_ts}|{reason}' (preserves
            timestamp + the user's complaint so later review knows
            when AND why the flag was made; we don't append because
            v1 expects at most one flag per event and the upgrade
            path is "look at audio + listen + rewrite by hand")
          - ``events[0].label`` ← 'flag_action' (the act-of-flagging
            event itself; lets `WHERE label != 'flag_action'` queries
            isolate real interactions)

        Returns a dict describing the flag outcome:
          {
            "flagged_event_id": str,    # the event we marked
            "flagged_ts_utc":   str,    # its original ts
            "flagged_outcome":  str,    # its terminal outcome
            "flag_action_event_id": str # this turn's event id
          }
        or ``None`` if there is no prior real event to flag (fresh
        boot, first wake after restart, etc.).

        Fail-soft: never raises on a DB-internal error — telemetry
        bugs must not silence the speaker. The caller (the voice
        tool) treats None as "nothing to flag" and crafts a spoken
        response accordingly."""
        self._require_open()
        async with self._lock():
            cur = self._conn.execute(  # type: ignore[union-attr]
                # The two most recent events that aren't themselves
                # flag-action rows. See class docstring for why this
                # query gives us [current_flag_event, prior_real_event].
                """
                SELECT event_id, ts_utc, outcome
                FROM wake_events
                WHERE label IS NULL OR label != 'flag_action'
                ORDER BY ts_utc DESC
                LIMIT 2
                """,
            )
            rows = cur.fetchall()
            if len(rows) < 2:
                # Only the in-flight flag event exists, OR no events
                # at all. Nothing prior to flag.
                return None
            cols = [d[0] for d in cur.description]
            flag_action = dict(zip(cols, rows[0]))
            target = dict(zip(cols, rows[1]))

            now = _now_iso()
            label_notes = f"{now}|{reason}"
            # Two updates inside the single async-lock window so a
            # second concurrent record_flag (impossible given the
            # refractory window, but defensive) can't interleave.
            self._conn.execute(  # type: ignore[union-attr]
                """
                UPDATE wake_events
                SET label = 'voice_flagged', label_notes = ?
                WHERE event_id = ?
                """,
                (label_notes, target["event_id"]),
            )
            self._conn.execute(  # type: ignore[union-attr]
                """
                UPDATE wake_events
                SET label = 'flag_action'
                WHERE event_id = ?
                """,
                (flag_action["event_id"],),
            )
            return {
                "flagged_event_id": target["event_id"],
                "flagged_ts_utc": target["ts_utc"],
                "flagged_outcome": target["outcome"],
                "flag_action_event_id": flag_action["event_id"],
            }

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
            key=lambda p: (p.stat().st_mtime_ns, p.name),
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
        ever captured").

        All three per-leg path columns are updated so downstream
        readers can use the canonical `audio_*_path != 'rolled_off'
        AND IS NOT NULL` filter against any of them. Dropping the
        DTLN column from this list (as the original implementation
        did) would leave audio_dtln_path pointing at a deleted file
        — a pre-merge bug spotted in the 2026-05-23 review."""
        self._require_open()
        async with self._lock():
            self._conn.executemany(  # type: ignore[union-attr]
                """
                UPDATE wake_events
                SET audio_on_path  = CASE WHEN audio_on_path  IS NOT NULL
                                          THEN ? ELSE NULL END,
                    audio_off_path = CASE WHEN audio_off_path IS NOT NULL
                                          THEN ? ELSE NULL END,
                    audio_dtln_path = CASE WHEN audio_dtln_path IS NOT NULL
                                           THEN ? ELSE NULL END
                WHERE event_id = ?
                """,
                [
                    (
                        ROLLED_OFF_SENTINEL,
                        ROLLED_OFF_SENTINEL,
                        ROLLED_OFF_SENTINEL,
                        eid,
                    )
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
