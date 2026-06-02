"""Tests for scripts/_analyze_three_leg.py.

The script is intentionally stdlib-only and run off fetched SQLite
snapshots, so exercise it as a subprocess against tiny synthetic DBs.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "_analyze_three_leg.py"


BASE_COLUMNS = """
    event_id TEXT PRIMARY KEY,
    ts_utc TEXT,
    trigger_kind TEXT,
    fired_legs TEXT,
    peak_score_aec_on REAL,
    peak_score_aec_off REAL,
    peak_score_dtln_aec REAL,
    audio_on_path TEXT,
    audio_off_path TEXT,
    audio_dtln_path TEXT,
    outcome TEXT,
    outcome_detail TEXT,
    ts_turn_opened TEXT,
    ts_speech_detected TEXT,
    ts_tool_called TEXT,
    music_active INTEGER,
    label TEXT
"""


def _run(corpus: Path) -> str:
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--top", "2", str(corpus)],
        check=True,
        text=True,
        capture_output=True,
    )
    return res.stdout


def _run_args(corpus: Path, *args: str) -> str:
    res = subprocess.run(
        [sys.executable, str(SCRIPT), *args, str(corpus)],
        check=True,
        text=True,
        capture_output=True,
    )
    return res.stdout


def _create_db(corpus: Path, *, include_chip: bool) -> sqlite3.Connection:
    corpus.mkdir()
    conn = sqlite3.connect(str(corpus / "wake-events.sqlite3"))
    chip_columns = """
        ,
        peak_score_chip_aec_150 REAL,
        peak_score_chip_aec_210 REAL,
        audio_chip_aec_150_path TEXT,
        audio_chip_aec_210_path TEXT
    """ if include_chip else ""
    conn.execute(f"CREATE TABLE wake_events ({BASE_COLUMNS}{chip_columns})")
    return conn


def test_analyze_script_preserves_legacy_three_leg_output(tmp_path: Path) -> None:
    corpus = tmp_path / "legacy"
    conn = _create_db(corpus, include_chip=False)
    conn.execute(
        """
        INSERT INTO wake_events (
            event_id, ts_utc, trigger_kind, fired_legs,
            peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec,
            audio_on_path, audio_off_path, audio_dtln_path,
            outcome, music_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "evt-1", "2026-05-31T12:00:00Z", "fire_dtln",
            "dtln,off,on", 0.65, 0.72, 0.81,
            "evt-1.aec-on.wav", "evt-1.aec-off.wav", "evt-1.aec-dtln.wav",
            "turn_complete", 1,
        ),
    )
    conn.commit()
    conn.close()

    out = _run(corpus)

    assert "Analyzed legs:       AEC3, Chip-direct, DTLN-aec" in out
    assert "Chip AEC 150" not in out
    assert "dtln,off,on" in out


def test_analyze_script_includes_chip_aec_legs(tmp_path: Path) -> None:
    corpus = tmp_path / "chip"
    conn = _create_db(corpus, include_chip=True)
    conn.executemany(
        """
        INSERT INTO wake_events (
            event_id, ts_utc, trigger_kind, fired_legs,
            peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec,
            audio_on_path, audio_off_path, audio_dtln_path,
            outcome, ts_turn_opened, ts_speech_detected, ts_tool_called,
            music_active, peak_score_chip_aec_150, peak_score_chip_aec_210,
            audio_chip_aec_150_path, audio_chip_aec_210_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "evt-chip", "2026-05-31T12:00:00Z", "fire_chip_aec_150",
                "chip_aec_150", 0.21, 0.12, 0.08,
                "evt-chip.aec-on.wav", "evt-chip.aec-off.wav",
                "evt-chip.aec-dtln.wav", "turn_complete",
                "2026-05-31T12:00:01Z", "2026-05-31T12:00:02Z",
                None, 1, 0.84, 0.31,
                "evt-chip.aec-chip-aec-150.wav",
                "evt-chip.aec-chip-aec-210.wav",
            ),
            (
                "evt-combo", "2026-05-31T12:01:00Z", "fire_aec_on",
                "chip_aec_150,on", 0.77, 0.22, 0.19,
                "evt-combo.aec-on.wav", "evt-combo.aec-off.wav",
                "evt-combo.aec-dtln.wav", "turn_complete",
                "2026-05-31T12:01:01Z", None, None, 1, 0.82, 0.35,
                "evt-combo.aec-chip-aec-150.wav",
                "evt-combo.aec-chip-aec-210.wav",
            ),
        ],
    )
    conn.commit()
    conn.close()

    out = _run(corpus)

    assert (
        "Analyzed legs:       AEC3, Chip-direct, DTLN-aec, "
        "Chip AEC 150, Chip AEC 210"
    ) in out
    assert "Only Chip AEC 150 fired" in out
    assert "chip_aec_150,on" in out
    assert f"afplay {corpus}/evt-chip.aec-chip-aec-150.wav" in out


def test_analyze_script_filters_to_validation_window(tmp_path: Path) -> None:
    corpus = tmp_path / "filtered"
    conn = _create_db(corpus, include_chip=True)
    conn.executemany(
        """
        INSERT INTO wake_events (
            event_id, ts_utc, trigger_kind, fired_legs,
            peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec,
            audio_on_path, audio_off_path, audio_dtln_path,
            outcome, music_active, peak_score_chip_aec_150,
            peak_score_chip_aec_210, audio_chip_aec_150_path,
            audio_chip_aec_210_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "old", "2026-05-31T23:59:59Z", "fire_aec_on", "on",
                0.91, 0.01, 0.01, "old.aec-on.wav", "", "",
                "completed", 1, 0.0, 0.0, "", "",
            ),
            (
                "new", "2026-06-01T12:00:00+00:00", "fire_chip_aec_150",
                "chip_aec_150", 0.01, 0.0, 0.0, "new.aec-on.wav", "", "",
                "completed", 1, 0.88, 0.02,
                "new.aec-chip-aec-150.wav", "new.aec-chip-aec-210.wav",
            ),
        ],
    )
    conn.commit()
    conn.close()

    out = _run_args(corpus, "--top", "2", "--since", "2026-06-01")

    assert "Source events:     2" in out
    assert "Total events:        1" in out
    assert "Only Chip AEC 150 fired" in out
    assert "Only AEC3 fired:    0 events" in out
