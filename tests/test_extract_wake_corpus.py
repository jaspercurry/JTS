# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/_extract_wake_corpus.py.

Build a synthetic wake_events SQLite + matching WAV files in tmp_path,
then exercise the extractor's filter / split / copy / manifest pipeline.

No real audio capture, no Pi-side daemons — every byte is constructed
in-process. Production schema is loaded via `WakeEventStore.open()`
so this test stays honest about schema drift (any column we read in
the extractor must exist in the live store).
"""
from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
import wave
from pathlib import Path

import pytest

from jasper.wake_events import SAMPLE_RATE_HZ, WakeEventStore

# Import the script under test by absolute path since scripts/ isn't
# a package on sys.path.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "_extract_wake_corpus.py"
_spec = importlib.util.spec_from_file_location("extract_wake_corpus", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
extract = importlib.util.module_from_spec(_spec)
sys.modules["extract_wake_corpus"] = extract
_spec.loader.exec_module(extract)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    """Pristine corpus dir with the wake_events schema applied.

    Uses WakeEventStore.open() to lay down the production schema +
    pragmas, then closes the store so the test can do its own raw
    SQL writes against `wake-events.sqlite3`. This guarantees the
    test exercises the same schema the extractor reads in production."""
    store = WakeEventStore(tmp_path)
    store.open()
    store.close()
    return tmp_path


def _make_wav(path: Path, duration_sec: float = 6.0) -> None:
    """Write a valid silent 16 kHz mono int16 WAV at `path`."""
    n_samples = int(SAMPLE_RATE_HZ * duration_sec)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE_HZ)
        w.writeframes(b"\x00\x00" * n_samples)


def _insert_event(
    db_path: Path,
    event_id: str,
    *,
    ts_utc: str | None = None,
    music_active: int = 0,
    outcome: str = "completed",
    has_speech: bool = True,
    audio_on: str | None = "auto",
    audio_off: str | None = "auto",
    audio_dtln: str | None = None,
    mic_muted: int | None = 0,
    label: str | None = None,
    trigger_kind: str = "fire_aec_on",
    threshold: float = 0.5,
    wake_model: str = "jarvis_v2",
    peak_on: float | None = 0.6,
    peak_off: float | None = 0.55,
    peak_dtln: float | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Insert a single wake_events row + (optionally) its paired WAVs.

    Returns `(audio_on_path, audio_off_path, audio_dtln_path)` names
    actually written, so the caller can assert against them. Pass
    "auto" to create the corresponding .aec-{leg}.wav file under the
    standard name; pass "rolled_off" / None / arbitrary-name to test
    edge cases without creating files. DTLN defaults to None
    (2-leg event, simulating a pre-PR-253 capture or `JASPER_WAKE_LEG_DTLN=0`).
    """
    corpus = db_path.parent

    def _resolve(leg: str, value: str | None) -> str | None:
        if value == "auto":
            name = f"{event_id}.aec-{leg}.wav"
            _make_wav(corpus / name)
            return name
        return value

    on_name = _resolve("on", audio_on)
    off_name = _resolve("off", audio_off)
    dtln_name = _resolve("dtln", audio_dtln)

    ts_utc = ts_utc or f"2026-05-23T12:00:{int(event_id[-2:] or 0):02d}.000+00:00"
    speech_ts = ts_utc if has_speech else None

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO wake_events (
              event_id, ts_utc, trigger_kind,
              peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec,
              threshold, outcome, wake_model,
              music_active, mic_muted,
              audio_on_path, audio_off_path, audio_dtln_path,
              label, ts_speech_detected, ts_turn_opened
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id, ts_utc, trigger_kind,
                peak_on, peak_off, peak_dtln,
                threshold, outcome, wake_model,
                music_active, mic_muted,
                on_name, off_name, dtln_name,
                label, speech_ts, ts_utc,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return on_name, off_name, dtln_name


def _read_manifest(output_dir: Path) -> list[dict]:
    with open(output_dir / "manifest.csv") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Helper-level: quadrant_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("leg,music,expected", [
    ("on", 0, "aec_on_nomusic"),
    ("on", 1, "aec_on_music"),
    ("off", 0, "aec_off_nomusic"),
    ("off", 1, "aec_off_music"),
    ("dtln", 0, "aec_dtln_nomusic"),
    ("dtln", 1, "aec_dtln_music"),
])
def test_quadrant_for(leg: str, music: int, expected: str) -> None:
    assert extract.quadrant_for(leg, music) == expected


def test_quadrant_for_rejects_unknown_leg() -> None:
    with pytest.raises(ValueError, match="unknown leg"):
        extract.quadrant_for("bogus", 0)


# ---------------------------------------------------------------------------
# Filter behavior — high-precision SQL conditions
# ---------------------------------------------------------------------------


def test_filter_excludes_incomplete_outcome(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001")  # passes
    _insert_event(db, "20260523T120002Z-001", outcome="no_speech")
    _insert_event(db, "20260523T120003Z-001", outcome="session_failed")
    events = extract.select_events(db, corpus_dir)
    assert [e.event_id for e in events] == ["20260523T120001Z-001"]


def test_filter_excludes_no_speech(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001")
    _insert_event(db, "20260523T120002Z-001", has_speech=False)
    events = extract.select_events(db, corpus_dir)
    assert [e.event_id for e in events] == ["20260523T120001Z-001"]


def test_filter_excludes_rolled_off(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001")
    _insert_event(db, "20260523T120002Z-001", audio_on="rolled_off")
    _insert_event(db, "20260523T120003Z-001", audio_off="rolled_off")
    _insert_event(db, "20260523T120004Z-001", audio_on=None)
    events = extract.select_events(db, corpus_dir)
    assert [e.event_id for e in events] == ["20260523T120001Z-001"]


def test_filter_excludes_muted(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001")  # mic_muted=0
    _insert_event(db, "20260523T120002Z-001", mic_muted=1)
    _insert_event(db, "20260523T120003Z-001", mic_muted=None)  # NULL allowed
    events = extract.select_events(db, corpus_dir)
    got = sorted(e.event_id for e in events)
    assert got == ["20260523T120001Z-001", "20260523T120003Z-001"]


def test_require_label_filter(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001", label="real_attempt")
    _insert_event(db, "20260523T120002Z-001", label="music")
    _insert_event(db, "20260523T120003Z-001", label=None)
    events = extract.select_events(db, corpus_dir, require_label="real_attempt")
    assert [e.event_id for e in events] == ["20260523T120001Z-001"]


def test_missing_wav_skipped(corpus_dir: Path) -> None:
    """Row passes SQL but a WAV file is absent → silently skipped at
    select_events level. The CLI summarizes the count separately."""
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001")  # both WAVs created
    # This row's audio paths refer to filenames we don't create.
    _insert_event(
        db, "20260523T120002Z-001",
        audio_on="ghost.aec-on.wav", audio_off="ghost.aec-off.wav",
    )
    events = extract.select_events(db, corpus_dir)
    assert [e.event_id for e in events] == ["20260523T120001Z-001"]


# ---------------------------------------------------------------------------
# Split — per-music-state, paired-leg, deterministic
# ---------------------------------------------------------------------------


def test_split_pairs_legs_within_event(corpus_dir: Path) -> None:
    """An event in eval for aec_on_music must also be in eval for
    aec_off_music — same event_id, same split assignment."""
    db = corpus_dir / "wake-events.sqlite3"
    # 10 music events; 0.3 eval fraction → 3 in eval.
    for i in range(10):
        _insert_event(db, f"20260523T1200{i:02d}Z-001", music_active=1)
    events = extract.select_events(db, corpus_dir)
    splits = extract.split_events(events, eval_fraction=0.3, seed=42)
    assert len(splits["music"]["eval"]) == 3
    assert len(splits["music"]["train"]) == 7
    # The same event_ids should appear in both legs after write_corpus.
    output = corpus_dir / "out"
    manifest = extract.write_corpus(splits, output)
    by_event = {}
    for row in manifest:
        by_event.setdefault(row["event_id"], set()).add(
            (row["leg"], row["split"])
        )
    for event_id, legs in by_event.items():
        assert {leg for leg, _ in legs} == {"on", "off"}
        splits_seen = {s for _, s in legs}
        assert len(splits_seen) == 1, (
            f"event {event_id} legs split across train+eval: {legs}"
        )


def test_split_deterministic(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    for i in range(10):
        _insert_event(db, f"20260523T1200{i:02d}Z-001", music_active=i % 2)
    events = extract.select_events(db, corpus_dir)
    a = extract.split_events(events, eval_fraction=0.2, seed=42)
    b = extract.split_events(events, eval_fraction=0.2, seed=42)
    for state in ("music", "nomusic"):
        assert [e.event_id for e in a[state]["train"]] == [
            e.event_id for e in b[state]["train"]
        ]
        assert [e.event_id for e in a[state]["eval"]] == [
            e.event_id for e in b[state]["eval"]
        ]
    c = extract.split_events(events, eval_fraction=0.2, seed=7)
    # Different seed should produce different ordering for at least
    # one state with > 1 events.
    different = False
    for state in ("music", "nomusic"):
        if [e.event_id for e in a[state]["train"]] != [
            e.event_id for e in c[state]["train"]
        ]:
            different = True
            break
    assert different, "seed=42 and seed=7 produced identical splits — RNG broken"


def test_split_at_least_one_eval_per_state(corpus_dir: Path) -> None:
    """Floor the eval count at 1 per music_state — otherwise a sparse
    quadrant ships a model that's literally untestable."""
    db = corpus_dir / "wake-events.sqlite3"
    for i in range(3):
        _insert_event(db, f"20260523T1200{i:02d}Z-001", music_active=1)
    events = extract.select_events(db, corpus_dir)
    splits = extract.split_events(events, eval_fraction=0.01, seed=42)
    assert len(splits["music"]["eval"]) == 1
    assert len(splits["music"]["train"]) == 2


# ---------------------------------------------------------------------------
# End-to-end: write_corpus + manifest + main()
# ---------------------------------------------------------------------------


def test_end_to_end_writes_all_quadrants(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    # 4 music + 4 nomusic events. eval_fraction=0.25 → 1 in eval per state.
    for i in range(4):
        _insert_event(db, f"20260523T1200{i:02d}Z-001", music_active=1)
        _insert_event(db, f"20260523T1201{i:02d}Z-001", music_active=0)

    output = corpus_dir / "out"
    rc = extract.main([
        str(corpus_dir), str(output),
        "--eval-fraction", "0.25", "--seed", "42",
    ])
    assert rc == 0

    # All four quadrant trees exist.
    for q in extract.QUADRANTS:
        assert (output / q / "train").is_dir()
        assert (output / q / "eval").is_dir()

    # Each music event contributes ON+OFF clips to (aec_on_music, aec_off_music).
    # 4 music events × 0.25 eval → 1 in eval per leg; 3 in train per leg.
    train_on_music = list((output / "aec_on_music" / "train").iterdir())
    eval_on_music = list((output / "aec_on_music" / "eval").iterdir())
    assert len(train_on_music) == 3
    assert len(eval_on_music) == 1

    # Manifest: 8 events × 2 legs = 16 rows.
    manifest = _read_manifest(output)
    assert len(manifest) == 16
    assert set(manifest[0].keys()) == set(extract._MANIFEST_FIELDNAMES)

    # Summary file populated + mentions the quadrants.
    summary = (output / "summary.txt").read_text()
    for q in extract.QUADRANTS:
        assert q in summary
    assert "TOTAL clips" in summary


def test_refuse_on_existing_output_unless_force(corpus_dir: Path) -> None:
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001")
    output = corpus_dir / "out"
    assert extract.main([str(corpus_dir), str(output)]) == 0
    (output / "marker.txt").write_text("don't delete me")

    rc = extract.main([str(corpus_dir), str(output)])
    assert rc == 2
    assert (output / "marker.txt").exists()

    rc = extract.main([str(corpus_dir), str(output), "--force"])
    assert rc == 0
    # marker.txt wiped, quadrant trees + manifest present
    assert not (output / "marker.txt").exists()
    assert (output / "manifest.csv").exists()


def test_force_remove_guard_protects_source_and_host_roots(tmp_path: Path) -> None:
    corpus = tmp_path / "workspace" / "corpus"
    corpus.mkdir(parents=True)

    for protected in (
        Path("/"),
        Path.home(),
        Path.cwd(),
        corpus,
        corpus.parent,
    ):
        assert not extract._safe_to_remove_output(
            protected,
            corpus_dir=corpus,
        )

    assert not extract._safe_to_remove_output(corpus / "out", corpus_dir=corpus)
    assert not extract._safe_to_remove_output(tmp_path / "other", corpus_dir=corpus)


def test_force_refuses_to_remove_corpus_ancestor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    corpus = workspace / "corpus"
    store = WakeEventStore(corpus)
    store.open()
    store.close()
    sibling = workspace / "keep-me.txt"
    sibling.write_text("irreplaceable")

    rc = extract.main([str(corpus), str(workspace), "--force"])

    assert rc == 2
    assert (corpus / "wake-events.sqlite3").is_file()
    assert sibling.read_text() == "irreplaceable"


def test_force_refuses_unowned_populated_directory(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    store = WakeEventStore(corpus)
    store.open()
    store.close()
    unrelated = tmp_path / "tax-records"
    unrelated.mkdir()
    sentinel = unrelated / "irreplaceable.txt"
    sentinel.write_text("keep")

    rc = extract.main([str(corpus), str(unrelated), "--force"])

    assert rc == 2
    assert sentinel.read_text() == "keep"


def test_force_refuses_lexical_ancestor_of_symlinked_corpus(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    actual_corpus = tmp_path / "external-corpus"
    store = WakeEventStore(actual_corpus)
    store.open()
    store.close()
    corpus_link = workspace / "latest"
    corpus_link.symlink_to(actual_corpus, target_is_directory=True)
    sentinel = workspace / "keep-me.txt"
    sentinel.write_text("keep")

    rc = extract.main([str(corpus_link), str(workspace), "--force"])

    assert rc == 2
    assert (actual_corpus / "wake-events.sqlite3").is_file()
    assert sentinel.read_text() == "keep"


def test_force_refuses_case_variant_corpus_ancestor(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_store = WakeEventStore(source)
    source_store.open()
    source_store.close()
    output = tmp_path / "OwnedOutput"
    assert extract.main([str(source), str(output)]) == 0

    corpus = output / "Corpus"
    corpus_store = WakeEventStore(corpus)
    corpus_store.open()
    corpus_store.close()
    case_variant_output = tmp_path / "ownedoutput"
    try:
        same_output = case_variant_output.samefile(output)
    except FileNotFoundError:
        same_output = False
    if not same_output:
        pytest.skip("temporary filesystem is case-sensitive")
    case_variant_corpus = case_variant_output / "corpus"

    assert not extract._safe_to_remove_output(
        output,
        corpus_dir=case_variant_corpus,
    )
    rc = extract.main([
        str(case_variant_corpus),
        str(output),
        "--force",
    ])

    assert rc == 2
    assert (corpus / "wake-events.sqlite3").is_file()


def test_main_errors_on_missing_db(tmp_path: Path) -> None:
    rc = extract.main([str(tmp_path), str(tmp_path / "out")])
    assert rc == 2  # corpus_dir has no wake-events.sqlite3


# ---------------------------------------------------------------------------
# Triple-leg extraction (DTLN-aec, added PR #253)
# ---------------------------------------------------------------------------


def test_dtln_leg_extracted_when_present(corpus_dir: Path) -> None:
    """An event with audio_dtln_path populated + WAV on disk contributes
    a 3rd leg to the aec_dtln_<state>/ quadrant. The on/off legs still
    land in their usual quadrants."""
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(
        db, "20260523T120001Z-001",
        music_active=1,
        audio_dtln="auto",     # creates 20260523T120001Z-001.aec-dtln.wav
        peak_dtln=0.72,
    )
    output = corpus_dir / "out"
    rc = extract.main([
        str(corpus_dir), str(output),
        "--eval-fraction", "0.5", "--seed", "42",
    ])
    assert rc == 0

    manifest = _read_manifest(output)
    legs = {row["leg"] for row in manifest}
    assert legs == {"on", "off", "dtln"}, (
        f"expected all 3 legs in manifest, got {legs}"
    )
    # The dtln file must have landed in an aec_dtln_music/ subdir
    # (music_active=1 → music state).
    dtln_files = list((output / "aec_dtln_music").rglob("*.aec-dtln.wav"))
    assert len(dtln_files) == 1


def test_dtln_leg_absent_when_path_null(corpus_dir: Path) -> None:
    """An event with audio_dtln_path=NULL is a 2-leg event. Manifest
    has no dtln row; aec_dtln_*/ dirs exist (write_corpus mkdirs them)
    but stay empty."""
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001", music_active=0)
    output = corpus_dir / "out"
    rc = extract.main([str(corpus_dir), str(output), "--eval-fraction", "0.5"])
    assert rc == 0

    manifest = _read_manifest(output)
    assert {row["leg"] for row in manifest} == {"on", "off"}
    assert list((output / "aec_dtln_nomusic").rglob("*.wav")) == []
    assert list((output / "aec_dtln_music").rglob("*.wav")) == []


def test_dtln_leg_skipped_when_file_missing(corpus_dir: Path) -> None:
    """If audio_dtln_path is set in the DB but the WAV isn't on disk
    (partial fetch), the dtln leg is silently skipped for that event;
    on/off still extract normally."""
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(
        db, "20260523T120001Z-001",
        # Reference a dtln file we never create.
        audio_dtln="ghost.aec-dtln.wav",
    )
    output = corpus_dir / "out"
    rc = extract.main([str(corpus_dir), str(output), "--eval-fraction", "0.5"])
    assert rc == 0

    manifest = _read_manifest(output)
    assert {row["leg"] for row in manifest} == {"on", "off"}


def test_dtln_leg_skipped_when_path_is_rolled_off(corpus_dir: Path) -> None:
    """If audio_dtln_path is the 'rolled_off' sentinel (retention
    pruned the WAV), treat it the same as missing — extract the 2-leg
    on/off but not dtln."""
    db = corpus_dir / "wake-events.sqlite3"
    _insert_event(db, "20260523T120001Z-001", audio_dtln="rolled_off")
    output = corpus_dir / "out"
    rc = extract.main([str(corpus_dir), str(output), "--eval-fraction", "0.5"])
    assert rc == 0
    manifest = _read_manifest(output)
    assert {row["leg"] for row in manifest} == {"on", "off"}


def test_mixed_2_and_3_leg_events(corpus_dir: Path) -> None:
    """A corpus with both 2-leg and 3-leg events (real-world: Pi was
    upgraded mid-corpus) extracts each event's legs appropriately."""
    db = corpus_dir / "wake-events.sqlite3"
    # Event 1: full 3-leg.
    _insert_event(
        db, "20260523T120001Z-001", music_active=1, audio_dtln="auto",
    )
    # Event 2: 2-leg only (pre-PR-253).
    _insert_event(db, "20260523T120002Z-001", music_active=1)
    # Event 3: 3-leg in the quiet condition.
    _insert_event(
        db, "20260523T120003Z-001", music_active=0, audio_dtln="auto",
    )

    output = corpus_dir / "out"
    rc = extract.main([str(corpus_dir), str(output), "--eval-fraction", "0.5"])
    assert rc == 0

    manifest = _read_manifest(output)
    # Event 1 + 3 contribute dtln; event 2 does not.
    dtln_rows = [r for r in manifest if r["leg"] == "dtln"]
    assert {r["event_id"] for r in dtln_rows} == {
        "20260523T120001Z-001", "20260523T120003Z-001",
    }
    # All 3 events contribute on + off.
    on_rows = [r for r in manifest if r["leg"] == "on"]
    assert {r["event_id"] for r in on_rows} == {
        "20260523T120001Z-001",
        "20260523T120002Z-001",
        "20260523T120003Z-001",
    }
