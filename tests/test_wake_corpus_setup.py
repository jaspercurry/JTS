"""Tests for jasper.web.wake_corpus_setup.

Covers the RecordingBackend state machine, metadata persistence, the
RecordingTask audio collection logic (with a fake UdpMicCapture), and
the HTTP handler routing. Hardware-free.

Strategy for the async parts: the backend's asyncio loop runs in a
daemon thread; tests start + shutdown the backend explicitly. The
UdpMicCapture used by RecordingTask is lazy-imported via
`from jasper.audio_io import UdpMicCapture`, so tests monkeypatch
`jasper.audio_io.UdpMicCapture` to inject a fake before the
RecordingTask is constructed.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from jasper.web import wake_corpus_setup


# ---------------------------------------------------------------------------
# Fake UDP capture — yields fixed frames at a fast rate, deterministic.
# ---------------------------------------------------------------------------


class _FakeUdpMicCapture:
    """Async context manager + frames() iterator.

    Matches the public surface UdpMicCapture exposes that
    RecordingTask uses. Each instance produces frames with a fixed
    sample value so tests can assert per-leg routing (the AEC ON
    leg's frames stay distinct from the AEC OFF leg's frames in the
    output WAV bytes).
    """

    # Test hook: tests append (port → sample_value) entries so
    # different ports yield distinguishable audio.
    port_to_value: dict[int, int] = {}

    def __init__(self, host: str = "127.0.0.1", port: int = 9876) -> None:
        self._port = port
        self._value = self.port_to_value.get(port, 0)
        self._closed = False

    async def __aenter__(self) -> "_FakeUdpMicCapture":
        return self

    async def __aexit__(self, *exc) -> None:
        self._closed = True

    async def frames(self):
        # Yield ~1 frame per 5 ms (much faster than real-time so 0.1 s
        # of capture produces ~20 frames).
        while not self._closed:
            yield np.full(1280, self._value, dtype=np.int16)
            await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def _patch_udp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake into jasper.audio_io so RecordingTask's lazy
    import picks it up."""
    import jasper.audio_io as audio_io
    monkeypatch.setattr(audio_io, "UdpMicCapture", _FakeUdpMicCapture)
    # Reset the per-port value map between tests
    _FakeUdpMicCapture.port_to_value = {}


# ---------------------------------------------------------------------------
# RecordingTask — direct exercise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_task_collects_frames_per_leg() -> None:
    _FakeUdpMicCapture.port_to_value = {9876: 11, 9877: 22, 9878: 33}
    task = wake_corpus_setup.RecordingTask(
        ports={"on": 9876, "off": 9877, "dtln": 9878},
    )
    await task.start()
    await asyncio.sleep(0.1)  # let the background task collect ~20 frames/leg
    pcm = await task.stop()

    assert set(pcm.keys()) == {"on", "off", "dtln"}
    # Each leg's bytes should be all the same value as its fake.
    on_samples = np.frombuffer(pcm["on"], dtype=np.int16)
    off_samples = np.frombuffer(pcm["off"], dtype=np.int16)
    dtln_samples = np.frombuffer(pcm["dtln"], dtype=np.int16)
    assert len(on_samples) > 0
    assert (on_samples == 11).all()
    assert (off_samples == 22).all()
    assert (dtln_samples == 33).all()


@pytest.mark.asyncio
async def test_recording_task_elapsed_grows() -> None:
    task = wake_corpus_setup.RecordingTask(ports={"on": 9876})
    await task.start()
    assert task.elapsed_sec() < 0.05
    await asyncio.sleep(0.1)
    assert task.elapsed_sec() >= 0.1
    await task.stop()


# ---------------------------------------------------------------------------
# RecordingBackend — start/shutdown
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path):
    """Construct + start a backend rooted in a tmp dir, tear down on
    test exit."""
    b = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        ports={"on": 9876, "off": 9877, "dtln": 9878},
        max_duration_sec=10.0,  # long enough to not auto-stop during tests
    )
    b.start()
    yield b
    b.shutdown()


def test_backend_start_is_idempotent(tmp_path: Path) -> None:
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    b.start()
    b.start()  # second call must not raise + not spawn a 2nd thread
    b.shutdown()


# ---------------------------------------------------------------------------
# begin_session
# ---------------------------------------------------------------------------


def test_begin_session_sets_id_and_member(backend) -> None:
    sid = backend.begin_session("jasper")
    assert sid is not None
    assert backend.session_id() == sid
    assert backend.member() == "jasper"


def test_begin_session_sanitizes_member(backend) -> None:
    backend.begin_session("Jasper Curry!")
    assert backend.member() == "jaspercurry"


def test_begin_session_rejects_empty_member(backend) -> None:
    with pytest.raises(ValueError, match="no usable chars"):
        backend.begin_session("   ")


def test_begin_session_rejects_during_recording(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        with pytest.raises(wake_corpus_setup.StateError):
            backend.begin_session("brittany")
    finally:
        backend.stop_recording()


# ---------------------------------------------------------------------------
# start_recording / stop_recording
# ---------------------------------------------------------------------------


def test_start_recording_validates_condition(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(ValueError, match="unknown condition"):
        backend.start_recording("loud", "near")


def test_start_recording_validates_distance(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(ValueError, match="unknown distance"):
        backend.start_recording("quiet", "across-the-house")


def test_start_recording_requires_session(backend) -> None:
    with pytest.raises(wake_corpus_setup.StateError, match="begin_session"):
        backend.start_recording("quiet", "near")


def test_start_recording_rejects_double_start(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        with pytest.raises(wake_corpus_setup.StateError, match="in progress"):
            backend.start_recording("quiet", "near")
    finally:
        backend.stop_recording()


def test_stop_recording_without_start_raises(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(wake_corpus_setup.StateError, match="no recording"):
        backend.stop_recording()


def test_start_stop_writes_wavs_to_correct_quadrant(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper")
    result = backend.start_recording("music", "far")
    assert "clip_id" in result
    time.sleep(0.1)  # collect ~20 frames per leg
    clip = backend.stop_recording()

    # Files landed in aec_<leg>_music/ since condition=music
    out = tmp_path / "out"
    assert (out / "aec_on_music").is_dir()
    assert (out / "aec_off_music").is_dir()
    assert (out / "aec_dtln_music").is_dir()
    on_wavs = list((out / "aec_on_music").glob("*.aec-on.wav"))
    off_wavs = list((out / "aec_off_music").glob("*.aec-off.wav"))
    dtln_wavs = list((out / "aec_dtln_music").glob("*.aec-dtln.wav"))
    assert len(on_wavs) == 1
    assert len(off_wavs) == 1
    assert len(dtln_wavs) == 1
    # Filename pattern: enroll_<member>_<session>_<seq>.aec-<leg>.wav
    assert on_wavs[0].name.startswith("enroll_jasper_")
    assert on_wavs[0].name.endswith("_001.aec-on.wav")
    # ClipMetadata reflects all of this
    assert clip.member == "jasper"
    assert clip.condition == "music"
    assert clip.distance == "far"
    assert clip.seq == 1
    assert set(clip.files.keys()) == {"on", "off", "dtln"}


def test_start_stop_writes_wav_in_correct_format(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.1)
    backend.stop_recording()

    wavs = list((tmp_path / "out").rglob("*.aec-on.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0])) as w:
        assert w.getnchannels() == wake_corpus_setup.CHANNELS
        assert w.getsampwidth() == wake_corpus_setup.SAMPLE_WIDTH_BYTES
        assert w.getframerate() == wake_corpus_setup.SAMPLE_RATE_HZ
        assert w.getnframes() > 0  # actual audio captured


def test_sequential_clips_get_incrementing_seq(backend) -> None:
    backend.begin_session("jasper")
    seqs = []
    for _ in range(3):
        backend.start_recording("quiet", "near")
        time.sleep(0.05)
        clip = backend.stop_recording()
        seqs.append(clip.seq)
    assert seqs == [1, 2, 3]


def test_sequence_excludes_deleted_clips(backend) -> None:
    """Deleting clip 1 means the next new clip becomes seq=2
    (count of non-deleted + 1)."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near"); time.sleep(0.05)
    clip1 = backend.stop_recording()
    backend.start_recording("quiet", "near"); time.sleep(0.05)
    clip2 = backend.stop_recording()
    assert clip1.seq == 1
    assert clip2.seq == 2

    backend.delete_clip(clip1.clip_id)

    backend.start_recording("quiet", "near"); time.sleep(0.05)
    clip3 = backend.stop_recording()
    # 1 deleted, 1 alive (clip2) → next seq = 2 (count + 1)
    assert clip3.seq == 2


# ---------------------------------------------------------------------------
# delete_clip
# ---------------------------------------------------------------------------


def test_delete_clip_removes_wavs_and_marks_deleted(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = backend.stop_recording()

    assert all(Path(p).is_file() for p in clip.files.values())
    assert backend.delete_clip(clip.clip_id) is True

    # WAVs gone from disk
    assert all(not Path(p).is_file() for p in clip.files.values())
    # Clip not in non-deleted list
    assert clip.clip_id not in {c.clip_id for c in backend.list_clips()}
    # But still in include_deleted
    all_clips = backend.list_clips(include_deleted=True)
    assert any(c.clip_id == clip.clip_id and c.deleted for c in all_clips)


def test_delete_clip_idempotent_on_missing(backend) -> None:
    assert backend.delete_clip("nonexistent-uuid") is False


def test_delete_clip_idempotent_on_already_deleted(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near"); time.sleep(0.05)
    clip = backend.stop_recording()
    backend.delete_clip(clip.clip_id)
    # Second delete returns False (already deleted)
    assert backend.delete_clip(clip.clip_id) is False


# ---------------------------------------------------------------------------
# Metadata persistence — JSON sidecar
# ---------------------------------------------------------------------------


def test_metadata_written_per_session(backend, tmp_path: Path) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near"); time.sleep(0.05)
    clip = backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("*.json"))
    assert len(json_files) == 1
    assert json_files[0].name.startswith("enroll_jasper_")

    data = json.loads(json_files[0].read_text())
    assert data["member"] == "jasper"
    assert data["session_id"] == backend.session_id()
    assert len(data["clips"]) == 1
    assert data["clips"][0]["clip_id"] == clip.clip_id
    assert data["clips"][0]["condition"] == "quiet"
    assert data["clips"][0]["distance"] == "near"


def test_metadata_updated_on_delete(backend, tmp_path: Path) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near"); time.sleep(0.05)
    clip = backend.stop_recording()
    backend.delete_clip(clip.clip_id)

    json_files = list((tmp_path / "out" / "metadata").glob("*.json"))
    data = json.loads(json_files[0].read_text())
    # The clip is still in the metadata list, marked deleted (audit trail)
    matching = [c for c in data["clips"] if c["clip_id"] == clip.clip_id]
    assert len(matching) == 1
    assert matching[0]["deleted"] is True


def test_metadata_atomic_no_tmp_left_behind(backend, tmp_path: Path) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near"); time.sleep(0.05)
    backend.stop_recording()

    md_dir = tmp_path / "out" / "metadata"
    json_files = list(md_dir.glob("*.json"))
    tmp_files = list(md_dir.glob("*.tmp"))
    assert len(json_files) == 1
    assert tmp_files == []


# ---------------------------------------------------------------------------
# Auto-stop on excessive duration
# ---------------------------------------------------------------------------


def test_auto_stop_fires_on_max_duration(tmp_path: Path) -> None:
    """A forgotten Stop click should auto-stop at MAX_DURATION_SEC
    with the auto_stopped flag set so the operator notices."""
    b = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        ports={"on": 9876},
        max_duration_sec=0.3,  # short for the test
    )
    b.start()
    try:
        b.begin_session("jasper")
        b.start_recording("quiet", "near")
        # Wait long enough for auto-stop to fire + the worker
        # thread to complete the save.
        time.sleep(0.8)
        assert not b.is_recording()
        clips = b.list_clips()
        assert len(clips) == 1
        assert clips[0].auto_stopped is True
    finally:
        b.shutdown()


# ---------------------------------------------------------------------------
# HTML rendering — quick sanity
# ---------------------------------------------------------------------------


def test_index_html_is_valid_shape() -> None:
    """Not a full HTML validator — just enough to catch obvious
    breakage like missing </body>, unmatched template strings, etc."""
    html = wake_corpus_setup._render_index_html()
    assert "<!DOCTYPE html>" in html
    assert "<title>JTS Wake-Word Corpus Recorder</title>" in html
    assert "</body>" in html
    assert "</html>" in html
    # Key API paths must be referenced
    assert "/api/status" in html
    assert "/api/session" in html
    assert "/api/clip/start" in html
    assert "/api/clip/stop" in html
    # All three conditions + distances must be selectable
    for c in ("quiet", "music"):
        assert f'value="{c}"' in html
    for d in ("near", "mid", "far"):
        assert f'value="{d}"' in html
