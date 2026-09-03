# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins for `jasper.cli.aec_bridge_capture`.

The near-end capture threads own what the AEC loop can never recover: the
stream geometry PortAudio actually negotiated, and — for the corpus USB mic —
which resampler its card rate forces, and therefore whether the resident
daemon imports scipy at all.

`main()`'s wiring of both threads is pinned here too. A throwaway stats
object or Event leaves a capture thread running, unreportable and
unstoppable, and no test would notice: nothing else opens a real card.
"""
from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jasper.cli import aec_bridge_capture
from jasper.cli.aec_bridge_capture import usb_resampler
from jasper.cli.aec_bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.cli.aec_bridge_telemetry import StatsIdentity, _BridgeStats

BRIDGE_SOURCE = Path(__file__).resolve().parents[1] / "jasper" / "cli" / "aec_bridge.py"

IDENTITY = StatsIdentity(
    sample_rate_hz=SAMPLE_RATE,
    frame_samples=FRAME_SAMPLES,
    reference_source="outputd_udp",
    reference_endpoint="127.0.0.1:9891",
)


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
    monkeypatch.setattr(
        aec_bridge_capture, "sd", SimpleNamespace(InputStream=input_stream),
    )
    event = MagicMock()
    monkeypatch.setattr(aec_bridge_capture, "log_event", event)
    stats = _BridgeStats(IDENTITY)

    aec_bridge_capture.mic_thread(
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
        "channels": aec_bridge_capture.MIC_CHANNELS,
        "dtype": "int16",
        "blocksize": FRAME_SAMPLES,
        "callback": input_stream.call_args.kwargs["callback"],
    }
    event.assert_called_once_with(
        aec_bridge_capture.logger,
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
    monkeypatch.setattr(
        aec_bridge_capture, "sd", SimpleNamespace(InputStream=input_stream),
    )

    aec_bridge_capture.mic_thread(
        MagicMock(),
        mic_device="test-mic",
        capture_latency=configured,
        stats=_BridgeStats(IDENTITY),
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


def _thread_kwargs(tree: ast.AST, target: str) -> dict[str, str]:
    thread = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(
            kw.arg == "target" and ast.unparse(kw.value) == target
            for kw in node.keywords
        )
    )
    kwargs = next(kw.value for kw in thread.keywords if kw.arg == "kwargs")
    return {
        key.value: ast.unparse(value)
        for key, value in zip(kwargs.keys, kwargs.values)
    }


def test_main_wires_the_capture_threads_to_the_process_stats_and_shutdown():
    """Both capture threads read their device settings from the resolved
    config and share the process's stats and shutdown Event.
    """
    tree = ast.parse(BRIDGE_SOURCE.read_text())

    assert _thread_kwargs(tree, "mic_thread") == {
        "mic_device": "config.mic_device",
        "capture_latency": "config.capture_latency",
        "stats": "_bridge_stats",
        "shutdown": "_shutdown",
    }
    assert _thread_kwargs(tree, "usb_mic_thread") == {
        "usb_mic_device": "config.usb_mic_device",
        "usb_mic_rate": "config.usb_mic_rate",
        "stats": "_bridge_stats",
        "shutdown": "_shutdown",
    }
