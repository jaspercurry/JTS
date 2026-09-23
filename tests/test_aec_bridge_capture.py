# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins for `jasper.aec.bridge_capture`.

The near-end capture threads own what the AEC loop can never recover: the
stream geometry PortAudio actually negotiated, and — for the corpus USB mic —
which resampler its card rate forces, and therefore whether the resident
daemon imports scipy at all.
"""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jasper.aec import bridge_capture
from jasper.aec.bridge_capture import usb_resampler
from jasper.aec.bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.aec.bridge_telemetry import BridgeStats
from tests._aec_bridge_helpers import IDENTITY
from tests._sounddevice_stub import stub_sounddevice


def _input_stream(stream):
    class InputStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return stream

        def __exit__(self, *_args):
            return False

    return MagicMock(side_effect=InputStream)


def _stopped() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


def test_mic_thread_logs_negotiated_input_latency(monkeypatch):
    stream = SimpleNamespace(
        latency=0.025,
        samplerate=15_990,
        blocksize=319,
    )
    input_stream = _input_stream(stream)
    stub_sounddevice(monkeypatch, SimpleNamespace(InputStream=input_stream))
    event = MagicMock()
    monkeypatch.setattr(bridge_capture, "log_event", event)
    stats = BridgeStats(IDENTITY)

    bridge_capture.mic_thread(
        MagicMock(),
        mic_device="test-mic",
        capture_latency="",
        stats=stats,
        shutdown=_stopped(),
    )

    input_stream.assert_called_once()
    assert input_stream.call_args.kwargs == {
        "device": "test-mic",
        "samplerate": SAMPLE_RATE,
        "channels": bridge_capture.MIC_CHANNELS,
        "dtype": "int16",
        "blocksize": FRAME_SAMPLES,
        "callback": input_stream.call_args.kwargs["callback"],
    }
    event.assert_called_once_with(
        bridge_capture.logger,
        "aec.mic_stream_latency",
        latency_s=0.025,
        requested_latency="default",
        samplerate=15_990,
        blocksize=319,
    )
    assert stats.snapshot()["capture_stream"] == {
        "sample_rate_hz": 15_990,
        "block_frames": 319,
        "input_latency_seconds": 0.025,
        "input_latency_frames": 400,
    }


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("low", "low"), ("0.04", 0.04)],
)
def test_mic_thread_passes_configured_capture_latency(
    monkeypatch,
    configured,
    expected,
):
    stream = SimpleNamespace(
        latency=0.02,
        samplerate=SAMPLE_RATE,
        blocksize=FRAME_SAMPLES,
    )
    input_stream = _input_stream(stream)
    stub_sounddevice(monkeypatch, SimpleNamespace(InputStream=input_stream))

    bridge_capture.mic_thread(
        MagicMock(),
        mic_device="test-mic",
        capture_latency=configured,
        stats=BridgeStats(IDENTITY),
        shutdown=_stopped(),
    )

    assert input_stream.call_args.kwargs["latency"] == expected


def test_a_card_already_at_the_mic_rate_is_not_resampled():
    assert usb_resampler(SAMPLE_RATE) == (None, 1, 1)


@pytest.mark.parametrize(
    ("usb_rate", "ratio", "wants_scipy"),
    [
        (8_000, (2, 1), False),
        (24_000, (2, 3), False),
        (32_000, (1, 2), False),
        (48_000, (1, 3), False),
        (96_000, (1, 6), False),
        (44_100, (160, 441), True),
        (22_050, (320, 441), True),
    ],
)
def test_only_the_44_1_khz_family_makes_the_bridge_import_scipy(
    usb_rate, ratio, wants_scipy,
):
    """Every integer-ratio card must stay on the numpy kernel.

    scipy here is resident RSS in a `MemorySwapMax=0` slice
    (`jasper.dsp_numpy` owns the figure), paid for the life of the daemon, and
    the numpy kernel is measurably faster at these ratios. Only 44.1 kHz
    reduces to hundreds of polyphase branches, which numpy would run in a
    Python loop inside the capture callback.
    """
    resample, up, down = usb_resampler(usb_rate)

    assert (up, down) == ratio
    assert resample.__module__.startswith("scipy") is wants_scipy


def test_a_44_1_khz_card_falls_back_to_numpy_when_scipy_is_absent(monkeypatch):
    """A missing scipy must slow the corpus leg down, not kill its thread."""
    monkeypatch.setitem(sys.modules, "scipy.signal", None)

    resample, up, down = usb_resampler(44_100)

    assert (up, down) == (160, 441)
    assert resample.__module__ == "jasper.dsp_numpy"
