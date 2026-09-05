# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Wake-corpus saved-session lifecycle tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from jasper.web import wake_corpus_setup

from tests.wake_corpus_setup_fixtures import (
    _backend_fixture,
    _block_recording_task_start,
    _patch_udp,
)

_IMPORTED_FIXTURES = (_backend_fixture, _patch_udp)


def test_list_sessions_empty_dir(tmp_path: Path) -> None:
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    assert b.list_sessions() == []


def test_list_sessions_returns_summaries_newest_first(
    tmp_path: Path,
) -> None:
    """list_sessions scans the metadata dir + summarizes each
    session. Sort order is newest-first by mtime."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    # Two sessions, the second one newer
    (md / "enroll_jasper_old.json").write_text(json.dumps({
        "session_id": "old", "member": "jasper",
        "ports": {}, "include_raw_mic_0": False,
        "clips": [
            {"clip_id": "1", "member": "jasper", "condition": "quiet",
             "distance": "near", "session_id": "old", "seq": 1,
             "start_ts": "x", "stop_ts": "y", "duration_sec": 1.0,
             "files": {}, "deleted": False, "auto_stopped": False, "notes": ""},
        ],
    }))
    (md / "enroll_jasper_new.json").write_text(json.dumps({
        "session_id": "new", "member": "jasper",
        "ports": {}, "include_raw_mic_0": True,
        "clips": [
            {"clip_id": "2", "member": "jasper", "condition": "ambient",
             "distance": "far", "session_id": "new", "seq": 1,
             "start_ts": "x", "stop_ts": "y", "duration_sec": 1.0,
             "files": {}, "deleted": False, "auto_stopped": False, "notes": ""},
        ],
    }))
    import os as _os
    # Force the "new" file's mtime to be later than the "old" file's
    now = time.time()
    _os.utime(md / "enroll_jasper_old.json", (now - 10, now - 10))
    _os.utime(md / "enroll_jasper_new.json", (now, now))

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    sessions = b.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == "new"  # newest first
    assert sessions[1]["session_id"] == "old"
    assert sessions[0]["include_raw_mic_0"] is True
    assert sessions[1]["include_raw_mic_0"] is False
    assert sessions[0]["include_dtln"] is True
    assert sessions[0]["include_usb_dtln"] is False
    assert sessions[0]["clip_count"] == 1
    assert sessions[0]["conditions"] == {"ambient": 1}


def test_list_sessions_marks_active(tmp_path: Path) -> None:
    """The session currently loaded in memory is flagged is_active so
    the UI can render the row differently (and disable Load)."""
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    b.start()
    try:
        b.begin_session("jasper")
        active_id = b.session_id()
        sessions = b.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == active_id
        assert sessions[0]["is_active"] is True
    finally:
        b.shutdown()


def test_list_sessions_skips_corrupt_files(tmp_path: Path) -> None:
    """A single corrupt JSON file must not break the whole list."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_good.json").write_text(json.dumps({
        "session_id": "good", "member": "jasper",
        "ports": {}, "clips": [],
    }))
    (md / "enroll_jasper_bad.json").write_text("{not valid json")

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    sessions = b.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "good"


def test_list_sessions_skips_bad_aec3_sweep_source(tmp_path: Path) -> None:
    """A sidecar with an unrecognized aec3_sweep_source raises inside the
    per-file parse (Aec3SweepConfigError, a ValueError) and must be
    skipped like any other malformed file, not 500 the whole list."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_good.json").write_text(json.dumps({
        "session_id": "good", "member": "jasper",
        "ports": {}, "clips": [],
    }))
    (md / "enroll_jasper_bad.json").write_text(json.dumps({
        "session_id": "bad", "member": "jasper",
        "ports": {}, "clips": [], "aec3_sweep_source": "not-a-real-source",
    }))

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    sessions = b.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "good"


def test_list_sessions_survives_delete_race_after_glob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A sidecar deleted between glob() and stat() (a concurrent delete
    on another thread, e.g. a session the UI's 30 s poll raced against
    delete_session) must be skipped, not raise FileNotFoundError out of
    the whole list."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_good.json").write_text(json.dumps({
        "session_id": "good", "member": "jasper",
        "ports": {}, "clips": [],
    }))
    ghost = md / "enroll_jasper_ghost.json"  # never created on disk
    real_glob = Path.glob

    def fake_glob(self: Path, *args: object, **kwargs: object) -> list[Path]:
        matches = list(real_glob(self, *args, **kwargs))
        return [*matches, ghost] if self == md else matches

    monkeypatch.setattr(Path, "glob", fake_glob)

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    sessions = b.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "good"


def test_load_session_switches_active(backend, tmp_path: Path) -> None:
    """load_session swaps the in-memory active session to an
    existing one on disk."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    first_id = backend.session_id()
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    backend.begin_session("jasper")  # creates a 2nd session (sleeps to differ ts)
    time.sleep(0.05)
    second_id = backend.session_id()
    assert first_id != second_id

    # Switch back to the first session
    result = backend.load_session(first_id)
    assert result["session_id"] == first_id
    assert result["include_raw_mic_0"] is True
    assert result["include_dtln"] is True
    assert result["include_usb_dtln"] is False
    assert backend.session_id() == first_id
    assert backend.include_raw_mic_0() is True
    # And the loaded session's clips are now visible
    assert len(backend.list_clips()) == 1


def test_load_session_refuses_during_recording(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        with pytest.raises(wake_corpus_setup.StateError):
            backend.load_session("anything")
    finally:
        backend.stop_recording()


@pytest.mark.parametrize("transition", ["load", "unload", "delete"])
def test_session_transition_refuses_while_recording_start_is_reserved(
    backend,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    """Session ownership cannot change during the slow UDP-bind window."""
    first_session_id = backend.begin_session("jasper")
    active_session_id = backend.begin_session("brittany")
    entered, release = _block_recording_task_start(monkeypatch)
    started: list[dict[str, str]] = []
    errors: list[BaseException] = []

    def start_clip() -> None:
        try:
            started.append(backend.start_recording("quiet", "near"))
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=start_clip)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(
            wake_corpus_setup.StateError,
            match="recording or session transition in progress",
        ):
            if transition == "load":
                backend.load_session(first_session_id)
            elif transition == "unload":
                backend.unload_session()
            else:
                backend.delete_session(active_session_id)
        assert backend.session_id() == active_session_id
        active_metadata = backend._find_session_metadata(active_session_id)
        assert active_metadata is not None and active_metadata.is_file()
    finally:
        release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert len(started) == 1
    backend.stop_recording()


def test_load_session_unknown_raises(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(ValueError, match="not found"):
        backend.load_session("nonexistent-id")


def test_loaded_legacy_capture_plan_requires_rebuild_before_append(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_legacy.json").write_text(json.dumps({
        "metadata_schema_version": wake_corpus_setup.METADATA_SCHEMA_VERSION,
        "session_id": "legacy",
        "member": "jasper",
        "ports": {"on": 9876},
        "include_dtln": False,
        "enabled_legs": ["on"],
        "capture_plan": {
            "schema_version": wake_corpus_setup.CAPTURE_PLAN_SCHEMA_VERSION,
            "selected_legs": ["on"],
        },
        "clips": [],
    }))
    b = wake_corpus_setup.RecordingBackend(
        output_dir=out,
        ports={"on": 9876},
        max_duration_sec=10.0,
    )
    b.start()
    try:
        b.load_session("legacy")
        with pytest.raises(wake_corpus_setup.StateError, match="predates"):
            b.start_recording("quiet", "near")
    finally:
        b.shutdown()


def test_delete_session_removes_wavs_and_json(
    backend, tmp_path: Path,
) -> None:
    """delete_session hard-removes the WAV files and the JSON
    sidecar. The session is no longer listable."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = backend.stop_recording()
    sid = backend.session_id()

    # WAVs and JSON present
    assert all(Path(p).is_file() for p in clip.files.values())
    md_path = tmp_path / "out" / "metadata" / f"enroll_jasper_{sid}.json"
    assert md_path.is_file()

    result = backend.delete_session(sid)
    assert result["wavs_deleted"] >= 1  # at least one per leg
    assert all(not Path(p).is_file() for p in clip.files.values())
    assert not md_path.is_file()
    # No longer in list_sessions
    assert sid not in {s["session_id"] for s in backend.list_sessions()}


def test_delete_active_session_clears_in_memory_state(
    backend,
) -> None:
    """When the operator deletes the session they have open in
    memory, the in-memory active state must be cleared so the UI
    doesn't show 'phantom' clips with broken WAV links."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    sid = backend.session_id()

    backend.delete_session(sid)
    assert backend.session_id() is None
    assert backend.member() is None
    assert backend.list_clips() == []
    assert backend.include_raw_mic_0() is False
    assert backend.include_dtln() is False
    assert backend.include_usb_dtln() is False


def test_unload_session_clears_state_but_keeps_metadata(
    backend, tmp_path: Path,
) -> None:
    """Unload is the non-destructive end-of-session operation."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    sid = backend.session_id()
    md_dir = tmp_path / "out" / "metadata"
    md_path = md_dir / f"enroll_jasper_{sid}.json"
    marker = md_dir / wake_corpus_setup.ACTIVE_SESSION_MARKER
    assert md_path.is_file()
    assert marker.is_file()

    assert backend.unload_session() == sid

    assert md_path.is_file()
    assert not marker.exists()
    assert backend.session_id() is None
    assert backend.member() is None
    assert backend.list_clips() == []
    assert backend.include_raw_mic_0() is False


def test_delete_session_refuses_during_recording(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    sid = backend.session_id()
    try:
        with pytest.raises(wake_corpus_setup.StateError):
            backend.delete_session(sid)
    finally:
        backend.stop_recording()


def test_delete_session_unknown_raises(backend) -> None:
    with pytest.raises(ValueError, match="not found"):
        backend.delete_session("nonexistent-id")
