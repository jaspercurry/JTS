# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The wired mic's live level meter — the closed-loop ramp's feed.

Pins the arithmetic (a known sample magnitude must produce a known dBFS, since
every SPL decision downstream is built on it), the loudest-channel rule, clip
detection at the 24-bit rail, and the loud-failure contract: a mic that never
delivers fails ``start()``, and one that dies mid-ramp surfaces on ``drain()``
rather than degrading into a silent feed.
"""

from __future__ import annotations

import struct
import threading
import time

import pytest

from jasper.audio_measurement.wired_capture import WiredCaptureError
from jasper.audio_measurement.wired_level_meter import (
    _FULL_SCALE_COUNTS as FULL_SCALE_COUNTS,
    WiredLevelMeter,
)

RATE = 48_000
CHANNELS = 2


def _frames_bytes(values):
    """Interleaved S32_LE frames: ``values`` is [(ch0, ch1), ...]."""
    return b"".join(struct.pack("<ii", a, b) for a, b in values)


class FakePcm:
    """A scripted capture PCM: ``(frames, values)`` steps, then silence."""

    def __init__(self, script, *, idle_frames=0):
        self._script = list(script)
        self._idle_frames = idle_frames
        self.closed = False

    def read(self):
        if self._script:
            frames, values = self._script.pop(0)
            return frames, _frames_bytes(values)
        time.sleep(0.001)
        if not self._idle_frames:
            return 0, b""
        return self._idle_frames, _frames_bytes([(0, 0)] * self._idle_frames)

    def close(self):
        self.closed = True

def _meter(script, *, channels=CHANNELS):
    return WiredLevelMeter(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=channels,
        pcm_factory=lambda: FakePcm(script, idle_frames=0),
    )


def _first_sample(script):
    meter = _meter(script)
    meter.start(ready_timeout_s=5.0)
    try:
        for _ in range(200):
            batch = meter.drain()
            if batch:
                return batch[0]
            time.sleep(0.005)
    finally:
        meter.stop()
    raise AssertionError("the meter produced no sample")


def test_level_meter_dbfs_matches_hand_computation():
    # A constant half-full-scale frame is exactly -6.0206 dBFS
    # (20*log10(0.5)); the second channel is silent, and the meter reports the
    # LOUDER channel, so both RMS and peak land there.
    half = int(FULL_SCALE_COUNTS // 2)
    sample = _first_sample([(16, [(half, 0)] * 16)])
    assert sample.rms_dbfs == pytest.approx(-6.0206, abs=0.01)
    assert sample.peak_dbfs == pytest.approx(-6.0206, abs=0.01)
    assert sample.clip is False
    # A wired ALSA capture has no browser gain control in the path.
    assert sample.agc_frozen is True


def test_level_meter_reports_the_louder_channel():
    quiet = int(FULL_SCALE_COUNTS // 1000)
    loud = int(FULL_SCALE_COUNTS // 2)
    # Capsule on channel 1, near-silence on channel 0: the capsule must win.
    sample = _first_sample([(16, [(quiet, loud)] * 16)])
    assert sample.rms_dbfs == pytest.approx(-6.0206, abs=0.01)


def test_level_meter_floors_a_silent_chunk():
    sample = _first_sample([(16, [(0, 0)] * 16)])
    assert sample.rms_dbfs == -120.0
    assert sample.peak_dbfs == -120.0


def test_level_meter_flags_a_full_scale_sample_as_clipping():
    rail = int(FULL_SCALE_COUNTS)
    assert _first_sample([(16, [(rail, 0)] * 16)]).clip is True
    # Comfortably below the rail (more than the 256-count guard): no clip.
    assert _first_sample([(16, [(rail - 1000, 0)] * 16)]).clip is False


def test_level_meter_start_fails_loudly_on_a_dead_device():
    class DeadPcm:
        def read(self):
            time.sleep(0.01)
            return 0, b""

        def close(self):
            pass

    meter = WiredLevelMeter(
        "fake:pcm", sample_rate_hz=RATE, channels=CHANNELS, pcm_factory=DeadPcm
    )
    with pytest.raises(WiredCaptureError, match="not delivering samples"):
        meter.start(ready_timeout_s=0.05)


def test_level_meter_drain_reraises_a_reader_failure():
    """A mic yanked mid-ramp must surface loudly, not as an empty feed."""
    explode = threading.Event()

    class ExplodingPcm:
        def read(self):
            if explode.wait(timeout=0.005):
                raise OSError("mic yanked")
            return 16, _frames_bytes([(1000, 0)] * 16)

        def close(self):
            pass

    meter = WiredLevelMeter(
        "fake:pcm", sample_rate_hz=RATE, channels=CHANNELS, pcm_factory=ExplodingPcm
    )
    meter.start(ready_timeout_s=5.0)
    explode.set()
    try:
        for _ in range(200):
            try:
                meter.drain()
            except WiredCaptureError as exc:
                assert "mic yanked" in str(exc)
                return
            time.sleep(0.005)
    finally:
        meter.stop()
    raise AssertionError("a dead mic must surface as a loud drain failure")


def test_level_meter_stop_is_idempotent_and_closes_the_pcm():
    pcm = FakePcm([(16, [(1000, 0)] * 16)])
    meter = WiredLevelMeter(
        "fake:pcm", sample_rate_hz=RATE, channels=CHANNELS, pcm_factory=lambda: pcm
    )
    meter.start(ready_timeout_s=5.0)
    meter.stop()
    meter.stop()
    assert pcm.closed is True


def test_level_meter_partial_frame_is_a_loud_failure():
    """A short read that is not a whole number of frames must SAY so.

    The reshape cannot represent it, and a meter that silently skipped such
    chunks would go quiet without ever reporting why.
    """
    go_partial = threading.Event()

    class PartialPcm:
        def read(self):
            if go_partial.wait(timeout=0.005):
                # claims 4 frames but the buffer is not a whole number of
                # samples, let alone frames — the shape numpy cannot represent
                return 4, _frames_bytes([(1000, 0)] * 3) + b"\x01\x02"
            return 16, _frames_bytes([(1000, 0)] * 16)

        def close(self):
            pass

    meter = WiredLevelMeter(
        "fake:pcm", sample_rate_hz=RATE, channels=CHANNELS, pcm_factory=PartialPcm
    )
    meter.start(ready_timeout_s=5.0)
    go_partial.set()
    try:
        for _ in range(200):
            try:
                meter.drain()
            except WiredCaptureError as exc:
                assert "partial frame" in str(exc)
                return
            time.sleep(0.005)
    finally:
        meter.stop()
    raise AssertionError("a partial frame must surface as a loud drain failure")
