# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Capture input order, gaps, and device lifetime without hardware."""
from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from jasper.wake_ports import parse_udp_device

from tests._sounddevice_stub import stub_sounddevice
from jasper.audio_io import CAPTURE_MAX_FRAMES, _CaptureQueue, _UdpMicProtocol

from jasper.audio_io import (
    MicCapture,
    UdpMicCapture,
    make_mic_capture,
)


# ---- parse_udp_device ----


def test_parse_udp_shorthand():
    assert parse_udp_device("udp:9876") == ("127.0.0.1", 9876)


def test_parse_udp_url():
    assert parse_udp_device("udp://10.0.0.5:5000") == ("10.0.0.5", 5000)


def test_parse_udp_uppercase():
    """Case-insensitive scheme — operator typo guard."""
    assert parse_udp_device("UDP:1234") == ("127.0.0.1", 1234)


def test_parse_non_udp_returns_none():
    """Anything not starting with `udp` is passed through unchanged."""
    assert parse_udp_device("Array") is None
    assert parse_udp_device("hw:5,1") is None
    assert parse_udp_device("CARD=Loopback") is None
    assert parse_udp_device("") is None


def test_parse_udp_malformed_missing_port():
    with pytest.raises(ValueError, match="missing port"):
        parse_udp_device("udp://hostonly")


def test_parse_udp_malformed_bad_separator():
    with pytest.raises(ValueError, match="malformed"):
        parse_udp_device("udp9876")  # no separator


def test_parse_udp_malformed_non_integer_port():
    with pytest.raises(ValueError, match="non-integer port"):
        parse_udp_device("udp:abc")


def test_parse_udp_port_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        parse_udp_device("udp:99999")
    with pytest.raises(ValueError, match="out of range"):
        parse_udp_device("udp:0")


# ---- make_mic_capture factory ----


def test_factory_returns_udp_for_udp_device():
    cap = make_mic_capture("udp:9876")
    assert isinstance(cap, UdpMicCapture)


def test_factory_returns_micapture_for_alsa_device():
    cap = make_mic_capture("Array", capture_rate=16000, capture_channels=1)
    assert isinstance(cap, MicCapture)


def test_factory_returns_micapture_for_hw_shorthand():
    cap = make_mic_capture("hw:5,1", capture_rate=16000, capture_channels=1)
    assert isinstance(cap, MicCapture)


# ---- UdpMicCapture end-to-end ----


async def test_udp_capture_receives_one_frame():
    """End-to-end: bind UdpMicCapture, send one packet via a raw
    socket, verify `frames()` yields exactly that data as int16."""
    cap = UdpMicCapture(host="127.0.0.1", port=0)  # OS-assigned port
    # We need the actual port for the test — patch in two phases.
    async with cap as bound:
        # Pull the port the OS assigned to us.
        port = bound._transport.get_extra_info("sockname")[1]

        # Send a frame of 1280 int16 samples (the canonical frame size).
        frame = np.arange(1280, dtype=np.int16)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(frame.tobytes(), ("127.0.0.1", port))
        finally:
            sender.close()

        # frames() should yield our frame.
        gen = bound.frames()
        received = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert received.dtype == np.int16
        assert received.tolist() == frame.tolist()


async def test_udp_capture_marks_only_the_frame_after_malformed_pcm():
    queue = _CaptureQueue()
    protocol = _UdpMicProtocol(queue)
    for data in (b"\x01\x00", b"bad", b"\x02\x00", b"\x03\x00"):
        protocol.datagram_received(data, None)
    for tag, gap in ((1, False), (2, True), (3, False)):
        assert (await queue.get()).tolist() == [tag]
        assert queue.last_frame.discontinuity is gap


@pytest.mark.parametrize("failure_at", ["start", "stop"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_direct_capture_closes_device_when_lifecycle_fails(
    monkeypatch, failure_at, close_fails,
):
    failure = RuntimeError(failure_at)
    stream = Mock()
    getattr(stream, failure_at).side_effect = failure
    if close_fails:
        stream.close.side_effect = RuntimeError("close")
    stub_sounddevice(monkeypatch, SimpleNamespace(InputStream=lambda **kwargs: stream))
    monkeypatch.setattr("jasper.audio_io._log_audio_open_failure", Mock())
    cap = MicCapture("unused")
    with pytest.raises(RuntimeError) as caught:
        async with cap:
            pass
    assert caught.value is failure
    stream.close.assert_called_once_with()
    assert cap._stream is None


async def test_udp_capture_drops_empty_datagram():
    """Zero-length UDP packets are legal but useless. Drop without
    crashing — `np.frombuffer(b'', dtype=int16)` would otherwise
    yield a zero-element array, which would be silently misleading
    downstream."""
    cap = UdpMicCapture(host="127.0.0.1", port=0)
    async with cap as bound:
        port = bound._transport.get_extra_info("sockname")[1]
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"", ("127.0.0.1", port))
            good = np.array([99], dtype=np.int16)
            sender.sendto(good.tobytes(), ("127.0.0.1", port))
        finally:
            sender.close()
        gen = bound.frames()
        received = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert received.tolist() == [99]


async def test_udp_capture_frame_size_constant_matches_micapture():
    """The UDP frame size contract is the SAME as MicCapture's output
    contract. Voice's WakeLoop is transport-agnostic only as long as
    these stay in sync."""
    assert UdpMicCapture.OUTPUT_FRAME_SAMPLES == MicCapture.OUTPUT_FRAME_SAMPLES
    assert UdpMicCapture.OUTPUT_RATE == MicCapture.OUTPUT_RATE


@pytest.mark.parametrize("transport", ["portaudio", "udp"])
async def test_capture_overload_keeps_recent_order_and_bounds_notifications(transport):
    queue = _CaptureQueue()
    notifications = []
    queue._loop = SimpleNamespace(call_soon_threadsafe=notifications.append)
    cap = MicCapture("unused")
    cap._queue = queue
    protocol = _UdpMicProtocol(queue)
    for tag in range(CAPTURE_MAX_FRAMES + 6):
        pcm = np.full((1280, 1), tag, dtype=np.int16)
        if transport == "portaudio":
            cap._callback(pcm, 1280, None, None)
        else:
            protocol.datagram_received(pcm.tobytes(), None)
    assert len(notifications) == 1
    assert queue.dropped_frames == 6
    assert int((await queue.get())[0]) == 6
    assert queue.last_frame.discontinuity
    assert int((await queue.get())[0]) == 7
    assert not queue.last_frame.discontinuity


async def test_capture_discards_expired_audio_and_reports_gap(monkeypatch):
    queue = _CaptureQueue()
    now = [10.0]
    monkeypatch.setattr("jasper.audio_io.time.monotonic", lambda: now[0])
    queue.put_nowait(np.array([1], dtype=np.int16))
    now[0] = 12.0
    queue.put_nowait(np.array([2], dtype=np.int16))
    assert (await queue.get()).tolist() == [2]
    assert queue.last_frame.captured_at == 12.0
    assert queue.last_frame.discontinuity
    assert queue.dropped_frames == 1
