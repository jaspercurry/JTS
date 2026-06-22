# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.cli.noise_capture's pure helpers + streaming-to-WAV.

Hardware-dependent bits (systemctl orchestration, real UDP) are
exercised on the Pi. This file pins the per-condition output naming,
the stream_to_wav write loop (using a fake capture), and the
heartbeat callback.
"""
from __future__ import annotations

import asyncio
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from jasper.cli import noise_capture


# ---------------------------------------------------------------------------
# noise_dirs + noise_filename — pure naming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("condition,expected", [
    ("quiet", ("aec_on_nomusic", "aec_off_nomusic")),
    ("music", ("aec_on_music", "aec_off_music")),
])
def test_noise_dirs(condition: str, expected: tuple[str, str]) -> None:
    assert noise_capture.noise_dirs(condition) == expected


def test_noise_dirs_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        noise_capture.noise_dirs("loud")


def test_noise_filename_format() -> None:
    now = datetime(2026, 5, 23, 14, 30, 0, tzinfo=timezone.utc)
    assert noise_capture.noise_filename("quiet", "on", now=now) == (
        "noise_quiet_20260523T143000Z_aec-on.wav"
    )
    assert noise_capture.noise_filename("music", "off", now=now) == (
        "noise_music_20260523T143000Z_aec-off.wav"
    )
    # DTLN added in PR #253 — same `aec-<leg>.wav` filename pattern.
    assert noise_capture.noise_filename("music", "dtln", now=now) == (
        "noise_music_20260523T143000Z_aec-dtln.wav"
    )


def test_all_noise_dirs_includes_dtln() -> None:
    assert noise_capture.all_noise_dirs("quiet") == {
        "on": "aec_on_nomusic",
        "off": "aec_off_nomusic",
        "dtln": "aec_dtln_nomusic",
    }
    assert noise_capture.all_noise_dirs("music") == {
        "on": "aec_on_music",
        "off": "aec_off_music",
        "dtln": "aec_dtln_music",
    }


def test_all_noise_dirs_rejects_unknown_condition() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        noise_capture.all_noise_dirs("loud")


@pytest.mark.parametrize("bad_leg", ["left", "ON", "1", ""])
def test_noise_filename_rejects_bad_leg(bad_leg: str) -> None:
    with pytest.raises(ValueError, match="leg must be"):
        noise_capture.noise_filename("quiet", bad_leg)


def test_noise_filename_rejects_bad_condition() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        noise_capture.noise_filename("loud", "on")


# ---------------------------------------------------------------------------
# stream_to_wav — fake UDP capture + constant-memory streaming
# ---------------------------------------------------------------------------


class _FakeUdpCapture:
    """Yields a steady stream of fixed frames at ~5 ms intervals.

    Same stand-in shape as the wake_enroll tests. Speed is much faster
    than real-time so a 0.2 s test capture window collects many frames."""

    def __init__(self, sample_value: int = 0) -> None:
        self._value = sample_value

    async def frames(self):
        while True:
            yield np.full(
                noise_capture.SAMPLE_RATE_HZ // 200,  # ~5 ms worth
                self._value, dtype=np.int16,
            )
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_stream_to_wav_writes_correct_format(tmp_path: Path) -> None:
    cap = _FakeUdpCapture(sample_value=1234)
    wav = tmp_path / "noise.wav"
    samples = await noise_capture.stream_to_wav(cap, wav, duration_sec=0.2)

    assert wav.is_file()
    # tmpfile cleaned up after atomic rename
    assert not wav.with_suffix(wav.suffix + ".tmp").exists()
    assert samples > 0

    with wave.open(str(wav)) as w:
        assert w.getnchannels() == noise_capture.CHANNELS
        assert w.getsampwidth() == noise_capture.SAMPLE_WIDTH_BYTES
        assert w.getframerate() == noise_capture.SAMPLE_RATE_HZ
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert len(data) == samples
    # Every sample is the fake's fixed value — confirms frames flowed
    # through write loop intact.
    assert (data == 1234).all()


@pytest.mark.asyncio
async def test_stream_to_wav_heartbeat_fires(tmp_path: Path) -> None:
    """Heartbeat must fire when configured + elapsed >= HEARTBEAT_SEC.

    Override the module constant briefly so the test doesn't have to
    run for 30 real seconds."""
    cap = _FakeUdpCapture()
    wav = tmp_path / "noise.wav"
    calls: list[tuple[float, int]] = []

    original_heartbeat = noise_capture.HEARTBEAT_SEC
    noise_capture.HEARTBEAT_SEC = 0.05  # 50 ms — fires ~3 times during 0.2 s capture
    try:
        await noise_capture.stream_to_wav(
            cap, wav, duration_sec=0.2,
            on_heartbeat=lambda elapsed, n: calls.append((elapsed, n)),
        )
    finally:
        noise_capture.HEARTBEAT_SEC = original_heartbeat

    # At least one heartbeat call.
    assert len(calls) >= 1
    # Each call must have non-negative elapsed + monotonically growing samples.
    elapsed_values = [c[0] for c in calls]
    sample_values = [c[1] for c in calls]
    assert all(e >= 0 for e in elapsed_values)
    assert sample_values == sorted(sample_values)


@pytest.mark.asyncio
async def test_stream_to_wav_empty_capture_writes_empty_wav(tmp_path: Path) -> None:
    """Capture that produces no frames must still leave a valid (empty)
    WAV on disk — otherwise the operator gets a 'where's my file?'
    mystery instead of a clear zero-sample WAV they can poke at."""

    class _Silent:
        async def frames(self):
            if False:
                yield np.array([], dtype=np.int16)
            while True:
                await asyncio.sleep(1.0)

    wav = tmp_path / "silent.wav"
    samples = await noise_capture.stream_to_wav(_Silent(), wav, duration_sec=0.05)
    assert wav.is_file()
    assert samples == 0
    with wave.open(str(wav)) as w:
        assert w.getnframes() == 0
        assert w.getnchannels() == noise_capture.CHANNELS
        assert w.getframerate() == noise_capture.SAMPLE_RATE_HZ
