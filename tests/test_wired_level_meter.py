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

import threading
import time

import pytest

from jasper.audio_measurement.wired_capture import WiredCaptureError
from jasper.audio_measurement.wired_level_meter import (
    _FULL_SCALE_COUNTS as FULL_SCALE_COUNTS,
    WiredLevelMeter,
)

from tests.wired_capture_fixtures import FakePcm, frames_bytes

RATE = 48_000
CHANNELS = 2


def _meter(script, *, channels=CHANNELS):
    return WiredLevelMeter(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=channels,
        pcm_factory=lambda: FakePcm(script),
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
    # Which loud failure wins is scheduler luck, not behavior: the ready
    # timeout (50 ms) and the 8-consecutive-read guard (8 x 10 ms) land within
    # tens of ms of each other. Pin that start() fails, not which message.
    with pytest.raises(WiredCaptureError):
        meter.start(ready_timeout_s=0.05)


def test_level_meter_drain_reraises_a_reader_failure():
    """A mic yanked mid-ramp must surface loudly, not as an empty feed."""
    explode = threading.Event()

    class ExplodingPcm:
        def read(self):
            if explode.wait(timeout=0.005):
                raise OSError("mic yanked")
            return 16, frames_bytes([(1000, 0)] * 16)

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


def test_level_meter_start_survives_reader_death_after_first_chunk():
    """#2797: a scheduling gap between the reader delivering its first good
    chunk and start() checking reader-error state could convert a healthy
    mic into "the microphone is gone". Deterministically injects the exact
    ordering — first chunk delivered, then a reader failure, THEN start()
    observes — by making start()'s post-wait check block until the second
    read has already failed, and asserts start() still succeeds; the later
    death must surface on drain() instead."""
    reached_second_read = threading.Event()

    class OneGoodThenDeadPcm:
        def __init__(self):
            self._reads = 0

        def read(self):
            self._reads += 1
            if self._reads == 1:
                return 16, frames_bytes([(1000, 0)] * 16)
            reached_second_read.set()
            raise OSError("mic hiccup right after the first chunk")

        def close(self):
            pass

    meter = WiredLevelMeter(
        "fake:pcm", sample_rate_hz=RATE, channels=CHANNELS,
        pcm_factory=OneGoodThenDeadPcm,
    )
    real_wait = meter._first_chunk.wait

    def wait_then_let_reader_race_ahead(timeout=None):
        ok = real_wait(timeout)
        assert reached_second_read.wait(timeout=2.0), (
            "reader never reached its second read"
        )
        return ok

    meter._first_chunk.wait = wait_then_let_reader_race_ahead
    try:
        meter.start(ready_timeout_s=5.0)
    finally:
        meter.stop()

    with pytest.raises(WiredCaptureError, match="mic hiccup"):
        meter.drain()


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
                return 4, frames_bytes([(1000, 0)] * 3) + b"\x01\x02"
            return 16, frames_bytes([(1000, 0)] * 16)

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
