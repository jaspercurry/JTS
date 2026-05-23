"""Unit tests for jasper.wake_events.WakeEventStore.

Covers the public contract:
  - Schema migration is idempotent (open()-twice is safe).
  - begin_event inserts a row in 'in_progress' state with the
    metadata fields populated.
  - update_stage sets exactly the named ts_* column.
  - update_stage rejects unknown stage names with ValueError
    (catches typos at the hook site, not in production).
  - set_outcome enforces the closed outcome set.
  - attach_audio writes WAVs atomically + updates the row.
  - Retention deletes oldest WAVs first when over the size cap,
    leaves the DB row intact with sentinel paths.
  - Rolled-off audio paths still allow funnel queries.

No real mics, no real audio capture, no jasper-voice integration —
those live in the daemon tests. This file pins the storage contract
alone.
"""
from __future__ import annotations

import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jasper.wake_events import (
    DEFAULT_MAX_AUDIO_BYTES,
    ROLLED_OFF_SENTINEL,
    SAMPLE_RATE_HZ,
    WakeEventStore,
    make_event_id,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> WakeEventStore:
    """Fresh store rooted at a tmp dir; opened, ready for writes."""
    s = WakeEventStore(tmp_path)
    s.open()
    yield s
    s.close()


def _pcm(seconds: float = 1.0) -> bytes:
    """Generate `seconds` of silent int16 PCM at 16 kHz mono."""
    n_samples = int(seconds * SAMPLE_RATE_HZ)
    return b"\x00\x00" * n_samples


def _wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


# ---------------------------------------------------------------------------
# make_event_id
# ---------------------------------------------------------------------------


def test_make_event_id_is_sortable():
    """Sequential event ids sort lexicographically in time order so
    `ls` in the wake-events dir stays chronological."""
    early = datetime(2026, 5, 22, 14, 30, 11, tzinfo=timezone.utc)
    late = datetime(2026, 5, 22, 14, 30, 12, tzinfo=timezone.utc)
    assert make_event_id(early, 1) < make_event_id(late, 1)
    assert make_event_id(early, 1) < make_event_id(early, 2)


def test_make_event_id_pads_sequence_to_three_digits():
    """Burst-of-N-in-one-second handling: seq 1..999 all sort
    correctly inside a one-second bucket."""
    now = datetime(2026, 5, 22, 14, 30, 11, tzinfo=timezone.utc)
    assert make_event_id(now, 1).endswith("-001")
    assert make_event_id(now, 42).endswith("-042")
    # Strict ordering 9 < 10 < 100 holds because of zero-padding.
    assert make_event_id(now, 9) < make_event_id(now, 10) < make_event_id(now, 100)


# ---------------------------------------------------------------------------
# Schema migration / lifecycle
# ---------------------------------------------------------------------------


def test_open_creates_schema_and_directory(tmp_path: Path):
    """First open() creates the directory + the table + indexes."""
    base = tmp_path / "wake-events"
    assert not base.exists()
    s = WakeEventStore(base)
    s.open()
    try:
        assert base.is_dir()
        # The DB file is created on first execute()
        conn = sqlite3.connect(str(base / "wake-events.sqlite3"))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='wake_events'"
            )
            assert cur.fetchone() == ("wake_events",)
            # Expected indexes exist
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_wake_events_%'"
            )
            idx_names = {row[0] for row in cur.fetchall()}
            assert "idx_wake_events_ts" in idx_names
            assert "idx_wake_events_outcome" in idx_names
            assert "idx_wake_events_trigger" in idx_names
            assert "idx_wake_events_label" in idx_names
        finally:
            conn.close()
    finally:
        s.close()


def test_open_is_idempotent(tmp_path: Path):
    """Calling open() twice is a no-op rather than re-running DDL."""
    s = WakeEventStore(tmp_path)
    s.open()
    s.open()  # should not raise
    s.close()


def test_require_open_raises_before_open(tmp_path: Path):
    """Public methods refuse to run if open() hasn't been called —
    daemon construction is supposed to open before any wake fires."""
    s = WakeEventStore(tmp_path)
    with pytest.raises(RuntimeError, match="open"):
        # Calling the sync helper directly — no event loop needed for
        # this check; the open-guard fires first.
        s._require_open()


# ---------------------------------------------------------------------------
# begin_event
# ---------------------------------------------------------------------------


async def test_begin_event_inserts_row_in_progress(store: WakeEventStore):
    await store.begin_event(
        event_id="evt-1",
        trigger_kind="fire_aec_on",
        peak_score_aec_on=0.85,
        peak_score_aec_off=0.32,
        peak_offset_ms_on=4120,
        peak_offset_ms_off=4080,
        threshold=0.5,
        wake_model="jarvis_v2.onnx",
        music_active=True,
        music_renderer="spotify",
        music_volume_db=-12.0,
        voice_provider="openai",
        bridge_config={"ns": "low", "agc1": True, "mic_gain_db": 6},
    )
    row = await store.get_event("evt-1")
    assert row is not None
    assert row["event_id"] == "evt-1"
    assert row["trigger_kind"] == "fire_aec_on"
    assert row["peak_score_aec_on"] == pytest.approx(0.85)
    assert row["peak_score_aec_off"] == pytest.approx(0.32)
    assert row["peak_offset_ms_on"] == 4120
    assert row["peak_offset_ms_off"] == 4080
    assert row["threshold"] == pytest.approx(0.5)
    assert row["wake_model"] == "jarvis_v2.onnx"
    assert row["music_active"] == 1
    assert row["music_renderer"] == "spotify"
    assert row["music_volume_db"] == pytest.approx(-12.0)
    assert row["voice_provider"] == "openai"
    assert row["outcome"] == "in_progress"
    # All ts_* funnel columns start NULL — no stages reached yet.
    for col in (
        "ts_late_cancel", "ts_peer_lost", "ts_gate_blocked",
        "ts_turn_opened", "ts_speech_detected", "ts_response_started",
        "ts_tool_called", "ts_tool_completed", "ts_turn_complete",
    ):
        assert row[col] is None
    # bridge_config serialized as deterministic JSON
    import json
    assert json.loads(row["bridge_config_json"]) == {
        "agc1": True, "mic_gain_db": 6, "ns": "low",
    }


async def test_begin_event_allows_null_off_score(store: WakeEventStore):
    """Single-stream callers (no AEC OFF leg) pass None for the
    secondary scores; the row stores NULL cleanly."""
    await store.begin_event(
        event_id="evt-single",
        trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9,
        peak_score_aec_off=None,
        threshold=0.5,
        wake_model="jarvis_v2.onnx",
    )
    row = await store.get_event("evt-single")
    assert row["peak_score_aec_off"] is None


# ---------------------------------------------------------------------------
# update_stage
# ---------------------------------------------------------------------------


async def test_update_stage_sets_named_column(store: WakeEventStore):
    await store.begin_event(
        event_id="evt-stage", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=None,
        threshold=0.5, wake_model="jarvis_v2.onnx",
    )
    await store.update_stage("evt-stage", "turn_opened")
    row = await store.get_event("evt-stage")
    assert row["ts_turn_opened"] is not None
    # All OTHER ts_* columns remain NULL — single-column update
    for col in (
        "ts_late_cancel", "ts_peer_lost", "ts_gate_blocked",
        "ts_speech_detected", "ts_response_started",
        "ts_tool_called", "ts_tool_completed", "ts_turn_complete",
    ):
        assert row[col] is None


async def test_update_stage_unknown_stage_raises(store: WakeEventStore):
    """Typo guard at the hook site — better to fail in dev than
    silently no-op in production."""
    with pytest.raises(ValueError, match="unknown wake-event stage"):
        await store.update_stage("evt-missing", "totally_made_up_stage")


async def test_update_stage_full_funnel(store: WakeEventStore):
    """Exhaustive — every stage name in the public contract has a
    matching ts_* column and the UPDATE succeeds."""
    await store.begin_event(
        event_id="evt-funnel", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=None,
        threshold=0.5, wake_model="jarvis_v2.onnx",
    )
    stages_and_cols = [
        ("late_cancel",      "ts_late_cancel"),
        ("peer_lost",        "ts_peer_lost"),
        ("gate_blocked",     "ts_gate_blocked"),
        ("turn_opened",      "ts_turn_opened"),
        ("speech_detected",  "ts_speech_detected"),
        ("response_started", "ts_response_started"),
        ("tool_called",      "ts_tool_called"),
        ("tool_completed",   "ts_tool_completed"),
        ("turn_complete",    "ts_turn_complete"),
    ]
    for stage, _ in stages_and_cols:
        await store.update_stage("evt-funnel", stage)
    row = await store.get_event("evt-funnel")
    for _, col in stages_and_cols:
        assert row[col] is not None, f"{col} should be set after update"


# ---------------------------------------------------------------------------
# set_outcome
# ---------------------------------------------------------------------------


async def test_set_outcome_records_terminal_state(store: WakeEventStore):
    await store.begin_event(
        event_id="evt-out", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=None,
        threshold=0.5, wake_model="jarvis_v2.onnx",
    )
    await store.set_outcome("evt-out", "no_speech", "vad never armed")
    row = await store.get_event("evt-out")
    assert row["outcome"] == "no_speech"
    assert row["outcome_detail"] == "vad never armed"


async def test_set_outcome_records_tool_name(store: WakeEventStore):
    """`tool_name` is set on outcomes describing a tool call;
    set_outcome should preserve a previously-set tool_name when
    later writes don't pass one (the COALESCE pattern)."""
    await store.begin_event(
        event_id="evt-tool", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=None,
        threshold=0.5, wake_model="jarvis_v2.onnx",
    )
    await store.set_outcome(
        "evt-tool", "tool_failed", "timeout", tool_name="spotify_play",
    )
    row = await store.get_event("evt-tool")
    assert row["tool_name"] == "spotify_play"
    # A later outcome update without tool_name doesn't wipe it
    await store.set_outcome("evt-tool", "tool_failed", "second take")
    row = await store.get_event("evt-tool")
    assert row["tool_name"] == "spotify_play"


async def test_set_outcome_unknown_raises(store: WakeEventStore):
    with pytest.raises(ValueError, match="unknown wake-event outcome"):
        await store.set_outcome("evt", "completely_bogus_outcome", None)


# ---------------------------------------------------------------------------
# attach_audio
# ---------------------------------------------------------------------------


async def test_attach_audio_writes_wavs_and_links_row(
    store: WakeEventStore, tmp_path: Path,
):
    await store.begin_event(
        event_id="evt-audio", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=None,
        threshold=0.5, wake_model="jarvis_v2.onnx",
    )
    audio = _pcm(seconds=2.5)
    await store.attach_audio(
        event_id="evt-audio",
        audio_on=audio,
        audio_off=audio,
    )
    row = await store.get_event("evt-audio")
    assert row["audio_on_path"] == "evt-audio.aec-on.wav"
    assert row["audio_off_path"] == "evt-audio.aec-off.wav"
    # Files exist with the right duration / sample rate
    on_path = tmp_path / "evt-audio.aec-on.wav"
    off_path = tmp_path / "evt-audio.aec-off.wav"
    assert on_path.exists() and off_path.exists()
    assert _wav_duration(on_path) == pytest.approx(2.5, abs=0.01)
    assert _wav_duration(off_path) == pytest.approx(2.5, abs=0.01)


async def test_attach_audio_handles_missing_off_leg(
    store: WakeEventStore, tmp_path: Path,
):
    """Single-stream operation (no AEC OFF mic): audio_off=None
    means the row's audio_off_path stays NULL and no WAV is written."""
    await store.begin_event(
        event_id="evt-on-only", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=None,
        threshold=0.5, wake_model="jarvis_v2.onnx",
    )
    await store.attach_audio(
        event_id="evt-on-only",
        audio_on=_pcm(1.0),
        audio_off=None,
    )
    row = await store.get_event("evt-on-only")
    assert row["audio_on_path"] == "evt-on-only.aec-on.wav"
    assert row["audio_off_path"] is None
    assert (tmp_path / "evt-on-only.aec-on.wav").exists()
    assert not (tmp_path / "evt-on-only.aec-off.wav").exists()


async def test_attach_audio_is_atomic_no_partial_wav_visible(
    tmp_path: Path,
):
    """Tempfile + rename means any reader (the future /wake-review/
    UI) sees either the complete WAV or nothing — never a partial
    write. Verifies the .tmp suffix is gone after the call returns."""
    s = WakeEventStore(tmp_path)
    s.open()
    try:
        await s.begin_event(
            event_id="evt-atomic", trigger_kind="fire_aec_on",
            peak_score_aec_on=0.9, peak_score_aec_off=None,
            threshold=0.5, wake_model="jarvis_v2.onnx",
        )
        await s.attach_audio(
            event_id="evt-atomic",
            audio_on=_pcm(1.0),
            audio_off=None,
        )
        # No leftover .tmp files
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def test_retention_deletes_oldest_when_over_cap(tmp_path: Path):
    """When total WAV bytes exceed cap, oldest-first deletion until
    under cap. DB rows survive; audio_*_path becomes the sentinel."""
    # Tiny cap so a few small WAVs trip retention deterministically.
    # 1.0s of mono 16 kHz int16 = 32000 bytes; with WAV header overhead
    # each file is ~32044 bytes. Cap at 100000 lets ~3 files coexist.
    s = WakeEventStore(tmp_path, max_audio_bytes=100_000)
    s.open()
    try:
        # Write 5 events at 1 s each. Last 3 should survive.
        for i in range(1, 6):
            eid = f"evt-{i:02d}"
            await s.begin_event(
                event_id=eid, trigger_kind="fire_aec_on",
                peak_score_aec_on=0.9, peak_score_aec_off=None,
                threshold=0.5, wake_model="jarvis_v2.onnx",
            )
            await s.attach_audio(
                event_id=eid, audio_on=_pcm(1.0), audio_off=None,
            )
        wavs = sorted(tmp_path.glob("*.wav"))
        # Cap should leave at most 3 WAV files (older deleted oldest-first)
        assert 2 <= len(wavs) <= 3
        # Oldest event(s) should have sentinel audio_on_path; newer
        # ones should still point to a real file.
        row_01 = await s.get_event("evt-01")
        row_05 = await s.get_event("evt-05")
        assert row_01["audio_on_path"] == ROLLED_OFF_SENTINEL
        assert row_05["audio_on_path"] == "evt-05.aec-on.wav"
        # The DB row survives even when audio is gone
        assert row_01 is not None
        assert row_01["outcome"] == "in_progress"
    finally:
        s.close()


async def test_retention_no_op_under_cap(tmp_path: Path):
    """Common case — total under cap, retention is a cheap dir scan
    with no deletions and no sentinel writes."""
    s = WakeEventStore(tmp_path, max_audio_bytes=DEFAULT_MAX_AUDIO_BYTES)
    s.open()
    try:
        await s.begin_event(
            event_id="evt-tiny", trigger_kind="fire_aec_on",
            peak_score_aec_on=0.9, peak_score_aec_off=None,
            threshold=0.5, wake_model="jarvis_v2.onnx",
        )
        await s.attach_audio(
            event_id="evt-tiny", audio_on=_pcm(0.5), audio_off=None,
        )
        row = await s.get_event("evt-tiny")
        assert row["audio_on_path"] == "evt-tiny.aec-on.wav"
        assert row["audio_on_path"] != ROLLED_OFF_SENTINEL
    finally:
        s.close()


async def test_begin_event_populates_new_debug_columns(store: WakeEventStore):
    """The added-after-v1 columns (mic_muted, per-leg mic RMS) are
    populated on INSERT when the caller provides them. NULL when
    omitted — matches single-stream / test-setup callsites that
    don't have the values to hand."""
    await store.begin_event(
        event_id="evt-debug", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=0.1,
        threshold=0.5, wake_model="jarvis_v2.onnx",
        mic_muted=False,
        mic_rms_dbfs_on=-18.5,
        mic_rms_dbfs_off=-22.0,
    )
    row = await store.get_event("evt-debug")
    assert row["mic_muted"] == 0
    assert row["mic_rms_dbfs_on"] == pytest.approx(-18.5)
    assert row["mic_rms_dbfs_off"] == pytest.approx(-22.0)


async def test_begin_event_null_debug_columns_when_omitted(store: WakeEventStore):
    """Single-stream / minimal callers can omit the new columns;
    they store NULL cleanly without raising."""
    await store.begin_event(
        event_id="evt-minimal", trigger_kind="fire_aec_on",
        peak_score_aec_on=0.9, peak_score_aec_off=None,
        threshold=0.5, wake_model="jarvis_v2.onnx",
    )
    row = await store.get_event("evt-minimal")
    assert row["mic_muted"] is None
    assert row["mic_rms_dbfs_on"] is None
    assert row["mic_rms_dbfs_off"] is None


def test_schema_migration_adds_columns_to_existing_db(tmp_path: Path):
    """An older DB (created without the post-v1 columns) gets
    them added via ALTER TABLE on open(). Existing rows survive
    unchanged with NULL in the new columns."""
    db_path = tmp_path / "wake-events.sqlite3"
    # Simulate a pre-migration DB: create the table WITHOUT the new
    # columns + insert one row.
    legacy_conn = sqlite3.connect(str(db_path))
    legacy_conn.execute("""
        CREATE TABLE wake_events (
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
        )
    """)
    legacy_conn.execute(
        "INSERT INTO wake_events (event_id, ts_utc, trigger_kind, "
        "threshold, outcome, wake_model) VALUES (?, ?, ?, ?, ?, ?)",
        ("legacy-1", "2026-05-21T20:00:00Z", "fire_aec_on",
         0.5, "completed", "jarvis_v2.onnx"),
    )
    legacy_conn.commit()
    legacy_conn.close()

    # Open via WakeEventStore — should ALTER in the new columns
    # idempotently, preserving the legacy row.
    s = WakeEventStore(tmp_path)
    s.open()
    try:
        # Legacy row survives with the new columns set to NULL
        cur = s._conn.execute(  # type: ignore[union-attr]
            "SELECT event_id, mic_muted, mic_rms_dbfs_on, mic_rms_dbfs_off "
            "FROM wake_events WHERE event_id='legacy-1'"
        )
        row = cur.fetchone()
        assert row == ("legacy-1", None, None, None)
        # New columns are now in the table schema
        cur = s._conn.execute(  # type: ignore[union-attr]
            "PRAGMA table_info(wake_events)"
        )
        cols = {r[1] for r in cur.fetchall()}
        assert "mic_muted" in cols
        assert "mic_rms_dbfs_on" in cols
        assert "mic_rms_dbfs_off" in cols
    finally:
        s.close()

    # Calling open() again is still idempotent (no duplicate-column
    # error from running ALTER TABLE a second time).
    s2 = WakeEventStore(tmp_path)
    s2.open()
    s2.close()


async def test_retention_only_writes_sentinel_for_legs_that_had_audio(
    tmp_path: Path,
):
    """Rolled-off-sentinel applies only to audio_*_path columns that
    were non-NULL pre-retention. NULL columns stay NULL (the row
    never had audio for that leg in the first place)."""
    s = WakeEventStore(tmp_path, max_audio_bytes=50_000)
    s.open()
    try:
        # Two events, both single-leg (audio_off_path=NULL).
        for i in (1, 2):
            eid = f"evt-{i}"
            await s.begin_event(
                event_id=eid, trigger_kind="fire_aec_on",
                peak_score_aec_on=0.9, peak_score_aec_off=None,
                threshold=0.5, wake_model="jarvis_v2.onnx",
            )
            await s.attach_audio(
                event_id=eid, audio_on=_pcm(1.0), audio_off=None,
            )
        # The first event's WAV got pruned to make room for the
        # second. The DB row for evt-1 now has the ON-leg sentinel
        # but the OFF leg stays NULL (never had audio).
        row = await s.get_event("evt-1")
        assert row["audio_on_path"] == ROLLED_OFF_SENTINEL
        assert row["audio_off_path"] is None
    finally:
        s.close()


async def test_retention_marks_dtln_path_as_rolled_off(tmp_path: Path):
    """Triple-stream regression — `_mark_audio_rolled_off` originally
    only updated audio_on_path + audio_off_path. After retention
    pruned a DTLN WAV, audio_dtln_path stayed pointing at the
    deleted file, and any query filtering
    `audio_*_path != 'rolled_off' AND IS NOT NULL` would try to
    load missing files. Fixed in the 2026-05-23 pre-merge review."""
    s = WakeEventStore(tmp_path, max_audio_bytes=100_000)
    s.open()
    try:
        # Two triple-stream events at 1 s each (3 WAVs × ~32 KB
        # = ~96 KB per event). After event 2 attaches, total is
        # ~192 KB > 100 KB cap → event 1's WAVs are deleted to
        # make room.
        for i in (1, 2):
            eid = f"evt-{i}"
            await s.begin_event(
                event_id=eid, trigger_kind="fire_dtln",
                peak_score_aec_on=0.0, peak_score_aec_off=0.0,
                peak_score_dtln_aec=0.9,
                threshold=0.5, wake_model="jarvis_v2.onnx",
                fired_legs="dtln",
            )
            await s.attach_audio(
                event_id=eid,
                audio_on=_pcm(1.0),
                audio_off=_pcm(1.0),
                audio_dtln=_pcm(1.0),
            )
        # First event's audio was pruned. ALL three audio path
        # columns should now be the sentinel — not just on / off.
        row = await s.get_event("evt-1")
        assert row["audio_on_path"] == ROLLED_OFF_SENTINEL
        assert row["audio_off_path"] == ROLLED_OFF_SENTINEL
        assert row["audio_dtln_path"] == ROLLED_OFF_SENTINEL, (
            "audio_dtln_path still points at the deleted file; the "
            "sentinel update missed the third leg. Check "
            "_mark_audio_rolled_off in jasper/wake_events.py."
        )
        # Second event keeps its real paths
        row2 = await s.get_event("evt-2")
        assert row2["audio_dtln_path"] == "evt-2.aec-dtln.wav"
    finally:
        s.close()
