"""Tests for jasper.cli.wake_enroll's pure helpers.

The interactive recording loop and systemctl orchestration are
hardware-dependent (UDP sockets, openwakeword model, systemd) — those
get exercised on the Pi. This file pins the pure helpers + the
SessionStats summarization.
"""
from __future__ import annotations

import argparse
import asyncio
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from jasper.cli import wake_enroll


def _write_mute(path: Path, muted: bool) -> None:
    path.write_text(f"JASPER_MIC_MUTED={1 if muted else 0}\n")


# ---------------------------------------------------------------------------
# quadrant_dirs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("condition,expected", [
    ("quiet", ("aec_on_nomusic", "aec_off_nomusic")),
    ("music", ("aec_on_music", "aec_off_music")),
])
def test_quadrant_dirs(condition: str, expected: tuple[str, str]) -> None:
    assert wake_enroll.quadrant_dirs(condition) == expected


def test_quadrant_dirs_rejects_unknown_condition() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        wake_enroll.quadrant_dirs("loud")


@pytest.mark.parametrize("condition,expected", [
    ("quiet", {"on": "aec_on_nomusic", "off": "aec_off_nomusic", "dtln": "aec_dtln_nomusic"}),
    ("music", {"on": "aec_on_music", "off": "aec_off_music", "dtln": "aec_dtln_music"}),
])
def test_all_quadrant_dirs_includes_dtln(
    condition: str, expected: dict[str, str],
) -> None:
    assert wake_enroll.all_quadrant_dirs(condition) == expected


def test_all_quadrant_dirs_rejects_unknown_condition() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        wake_enroll.all_quadrant_dirs("loud")


# ---------------------------------------------------------------------------
# make_session_id
# ---------------------------------------------------------------------------


def test_make_session_id_format() -> None:
    now = datetime(2026, 5, 23, 14, 20, 0, tzinfo=timezone.utc)
    assert wake_enroll.make_session_id("jasper", now=now) == (
        "enroll_jasper_20260523T142000Z"
    )


def test_make_session_id_lowercases_and_strips() -> None:
    now = datetime(2026, 5, 23, 14, 20, 0, tzinfo=timezone.utc)
    # Hyphens / spaces dropped; case lowered. Underscores kept.
    assert wake_enroll.make_session_id("Jasper Curry-1", now=now) == (
        "enroll_jaspercurry1_20260523T142000Z"
    )


def test_make_session_id_rejects_empty_member() -> None:
    with pytest.raises(ValueError, match="no usable chars"):
        wake_enroll.make_session_id("   ")


# ---------------------------------------------------------------------------
# clip_basename
# ---------------------------------------------------------------------------


def test_clip_basename_zero_pads_seq() -> None:
    sid = "enroll_jasper_20260523T142000Z"
    assert wake_enroll.clip_basename(sid, 1, "on") == (
        "enroll_jasper_20260523T142000Z_01.aec-on.wav"
    )
    assert wake_enroll.clip_basename(sid, 30, "off") == (
        "enroll_jasper_20260523T142000Z_30.aec-off.wav"
    )


def test_clip_basename_rejects_unknown_leg() -> None:
    with pytest.raises(ValueError, match="leg must be"):
        wake_enroll.clip_basename("enroll_x_y", 1, "bogus")


def test_clip_basename_accepts_dtln_leg() -> None:
    """DTLN was added as a third leg in PR #253. Filename pattern stays
    the same (`.aec-<leg>.wav`) — confirm the validator accepts it."""
    assert wake_enroll.clip_basename("enroll_x", 1, "dtln") == (
        "enroll_x_01.aec-dtln.wav"
    )


# ---------------------------------------------------------------------------
# write_wav — atomic + correct format
# ---------------------------------------------------------------------------


def test_write_wav_atomic_and_format(tmp_path: Path) -> None:
    samples = np.array([100, -100, 200, -200] * 100, dtype=np.int16)
    path = tmp_path / "clip.wav"
    wake_enroll.write_wav(path, samples.tobytes())

    # File exists; tmpfile cleaned up.
    assert path.is_file()
    assert not path.with_suffix(path.suffix + ".tmp").exists()

    with wave.open(str(path)) as w:
        assert w.getnchannels() == wake_enroll.CHANNELS
        assert w.getsampwidth() == wake_enroll.SAMPLE_WIDTH_BYTES
        assert w.getframerate() == wake_enroll.SAMPLE_RATE_HZ
        roundtrip = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert np.array_equal(roundtrip, samples)


# ---------------------------------------------------------------------------
# compute_peak_score — using a fake detector to avoid loading openwakeword
# ---------------------------------------------------------------------------


class _FakeDetector:
    """Returns the absolute mean of each frame, scaled to ~0..1.

    Stand-in for `WakeWordDetector` so the test stays hardware-free.
    Verifies the iteration logic: the helper must return the highest
    per-frame score, not the average or the last."""

    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def score_frame(self, frame: np.ndarray) -> float:
        self.calls.append(frame)
        return float(np.abs(frame.astype(np.float64)).mean()) / 32768.0


def test_compute_peak_score_returns_max_across_frames() -> None:
    # Three frames: low-amplitude, high-amplitude, mid-amplitude.
    # The middle frame should win.
    low = np.full(wake_enroll.FRAME_SAMPLES, 100, dtype=np.int16)
    high = np.full(wake_enroll.FRAME_SAMPLES, 16000, dtype=np.int16)
    mid = np.full(wake_enroll.FRAME_SAMPLES, 4000, dtype=np.int16)
    pcm = np.concatenate([low, high, mid]).tobytes()
    detector = _FakeDetector()
    peak = wake_enroll.compute_peak_score(detector, pcm)
    assert len(detector.calls) == 3
    # High frame: 16000 / 32768 ≈ 0.488
    assert peak == pytest.approx(16000 / 32768.0, rel=1e-6)


def test_compute_peak_score_truncates_partial_tail() -> None:
    # 1.5 frames worth — the partial tail must NOT get scored
    # (would distort the score with a short window).
    pcm = np.full(
        int(wake_enroll.FRAME_SAMPLES * 1.5), 1000, dtype=np.int16
    ).tobytes()
    detector = _FakeDetector()
    peak = wake_enroll.compute_peak_score(detector, pcm)
    assert len(detector.calls) == 1  # only the full frame
    assert peak == pytest.approx(1000 / 32768.0, rel=1e-6)


def test_compute_peak_score_handles_empty_input() -> None:
    detector = _FakeDetector()
    assert wake_enroll.compute_peak_score(detector, b"") == 0.0
    assert detector.calls == []


# ---------------------------------------------------------------------------
# Mic mute — enrollment captures UDP mic legs directly while jasper-voice is
# stopped, so it must honor the persisted household privacy switch itself.
# ---------------------------------------------------------------------------


def test_refuse_if_muted_blocks_start(tmp_path: Path, caplog) -> None:
    mute_path = tmp_path / "mic_mute.env"
    _write_mute(mute_path, True)

    with pytest.raises(wake_enroll.MicMutedError, match="mic is muted"):
        wake_enroll._refuse_if_muted("start_session", mute_path)

    assert "event=wake_enroll.mute_refused" in caplog.text


@pytest.mark.asyncio
async def test_run_session_refuses_while_muted_before_audio_imports(
    tmp_path: Path,
) -> None:
    mute_path = tmp_path / "mic_mute.env"
    _write_mute(mute_path, True)

    with pytest.raises(wake_enroll.MicMutedError, match="mic is muted"):
        await wake_enroll.run_session(argparse.Namespace(mic_mute_path=mute_path))


# ---------------------------------------------------------------------------
# SessionStats
# ---------------------------------------------------------------------------


def test_session_stats_summary_with_no_clips() -> None:
    s = wake_enroll.SessionStats()
    assert "no clips captured" in s.summary()


def test_session_stats_records_and_summarizes() -> None:
    s = wake_enroll.SessionStats()
    s.record(0.80, 0.60)
    s.record(0.05, 0.04)  # weak clip — all captured legs below 0.10
    s.record(0.50, 0.45)
    summary = s.summary()
    # Wording shifted post-triple-leg: now "all captured legs" because
    # weakness depends on whichever legs we actually captured.
    assert "weak clips (all captured legs <0.10): 1/3" in summary
    assert "median=0.50" in summary  # AEC ON sorted = [0.05, 0.50, 0.80]


def test_session_stats_weak_count_only_when_both_below() -> None:
    s = wake_enroll.SessionStats()
    s.record(0.05, 0.5)   # OFF saved it → not weak
    s.record(0.5, 0.05)   # ON saved it → not weak
    s.record(0.05, 0.05)  # both weak
    s.record(0.0, 0.0)    # both weak
    assert s.weak_count() == 2


# ---------------------------------------------------------------------------
# Async capture — _collect_for + record_window with a fake UDP source
# ---------------------------------------------------------------------------


class _FakeUdpCapture:
    """Yields a steady stream of fixed frames until cancelled. Stand-in
    for `UdpMicCapture` so the async-timeout logic can be tested
    without a real socket."""

    def __init__(self, sample_value: int) -> None:
        self._value = sample_value

    async def frames(self):
        # Emit ~1 frame per 5 ms — much faster than real-time so the
        # 0.2 s test window captures plenty.
        while True:
            yield np.full(
                wake_enroll.FRAME_SAMPLES, self._value, dtype=np.int16,
            )
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_collect_for_returns_bytes_in_window() -> None:
    cap = _FakeUdpCapture(sample_value=42)
    pcm = await wake_enroll._collect_for(cap, duration_sec=0.2)
    # Should have captured many frames; bytes are int16 = 2 bytes per sample
    samples = np.frombuffer(pcm, dtype=np.int16)
    assert len(samples) > wake_enroll.FRAME_SAMPLES  # multiple frames
    assert (samples == 42).all()


@pytest.mark.asyncio
async def test_record_window_returns_paired_bytes() -> None:
    on_cap = _FakeUdpCapture(sample_value=100)
    off_cap = _FakeUdpCapture(sample_value=200)
    on_pcm, off_pcm = await wake_enroll.record_window(
        on_cap, off_cap, duration_sec=0.15,
    )
    on_samples = np.frombuffer(on_pcm, dtype=np.int16)
    off_samples = np.frombuffer(off_pcm, dtype=np.int16)
    assert (on_samples == 100).all()
    assert (off_samples == 200).all()
    # Both legs ran off the same asyncio.timeout; length should match
    # within one packet of slack (5 ms = 80 samples at our fake rate).
    assert abs(len(on_samples) - len(off_samples)) <= wake_enroll.FRAME_SAMPLES


@pytest.mark.asyncio
async def test_record_window_threads_mute_gate(tmp_path: Path) -> None:
    mute_path = tmp_path / "mic_mute.env"
    _write_mute(mute_path, True)

    with pytest.raises(wake_enroll.MicMutedError, match="mic is muted"):
        await wake_enroll.record_window(
            _FakeUdpCapture(sample_value=100),
            _FakeUdpCapture(sample_value=200),
            duration_sec=0.15,
            mic_mute_path=mute_path,
            mute_poll_interval_sec=0.01,
        )


@pytest.mark.asyncio
async def test_record_legs_stops_when_mute_flips_mid_capture(
    tmp_path: Path,
) -> None:
    mute_path = tmp_path / "mic_mute.env"
    _write_mute(mute_path, False)
    captures = {
        "on": _FakeUdpCapture(sample_value=100),
        "off": _FakeUdpCapture(sample_value=200),
    }

    async def flip_mute() -> None:
        await asyncio.sleep(0.03)
        _write_mute(mute_path, True)

    flipper = asyncio.create_task(flip_mute())
    with pytest.raises(wake_enroll.MicMutedError, match="mic is muted"):
        await wake_enroll.record_legs(
            captures,
            duration_sec=1.0,
            mic_mute_path=mute_path,
            mute_poll_interval_sec=0.01,
        )
    await flipper


@pytest.mark.asyncio
async def test_record_legs_dict_api_handles_three_legs() -> None:
    """Triple-leg recording via the generic dict API. Each leg gets a
    different fake sample value so we can confirm the returned dict
    keys map back to the right capture."""
    captures = {
        "on": _FakeUdpCapture(sample_value=11),
        "off": _FakeUdpCapture(sample_value=22),
        "dtln": _FakeUdpCapture(sample_value=33),
    }
    result = await wake_enroll.record_legs(captures, duration_sec=0.15)
    assert set(result.keys()) == {"on", "off", "dtln"}
    for leg, expected_value in (("on", 11), ("off", 22), ("dtln", 33)):
        samples = np.frombuffer(result[leg], dtype=np.int16)
        assert len(samples) > 0
        assert (samples == expected_value).all()


@pytest.mark.asyncio
async def test_session_stats_handles_three_legs() -> None:
    s = wake_enroll.SessionStats()
    s.record(0.80, 0.60, 0.70)
    s.record(0.05, 0.04, 0.06)  # all 3 weak
    s.record(0.50, 0.45, 0.55)
    summary = s.summary()
    assert "AEC ON" in summary
    assert "AEC OFF" in summary
    assert "DTLN" in summary
    assert "weak clips (all captured legs <0.10): 1/3" in summary


def test_session_stats_dtln_optional_in_record() -> None:
    """`record(peak_on, peak_off)` without dtln still works (2-leg
    backward compat for tests / Pis without DTLN)."""
    s = wake_enroll.SessionStats()
    s.record(0.5, 0.5)
    s.record(0.5, 0.5)
    assert "DTLN" not in s.summary()
    assert s.peaks_on == [0.5, 0.5]
    assert s.peaks_off == [0.5, 0.5]


@pytest.mark.asyncio
async def test_collect_for_empty_when_no_frames() -> None:
    """A capture that never yields any frames must return b'' rather
    than crashing on np.concatenate([])."""

    class _Silent:
        async def frames(self):
            # `if False: yield` is the standard idiom to mark this as
            # an async generator without ever actually yielding — what
            # the UdpMicCapture stand-in needs to simulate "bridge is
            # running but no audio is arriving".
            if False:
                yield np.array([], dtype=np.int16)
            while True:
                await asyncio.sleep(1.0)

    pcm = await wake_enroll._collect_for(_Silent(), duration_sec=0.05)
    assert pcm == b""
