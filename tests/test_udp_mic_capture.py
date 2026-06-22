# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `UdpMicCapture` and `make_mic_capture`.

The UDP transport replaces the snd-aloop LoopbackAEC path that the
bridge previously used to deliver AEC'd mic to jasper-voice (see
the `UdpMicCapture` docstring in jasper/audio_io.py for why). These
tests pin the contract that voice's WakeLoop relies on:

  - Each datagram becomes one int16 numpy frame yielded by `frames()`.
  - Same frame shape as `MicCapture` (1280 samples @ 16 kHz).
  - The factory dispatches to UDP for `udp:<port>` / `udp://HOST:PORT`.
  - Malformed UDP forms raise ValueError at parse time (typo guard).
  - Malformed packets are dropped, not propagated as crashes.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import types

import numpy as np
import pytest

# audio_io.py imports sounddevice at module level. Stub it in case
# the venv doesn't have it — same pattern as tests/test_doctor.py
# and tests/test_aec_bridge_stall.py.
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")

from jasper.audio_io import (  # noqa: E402
    MicCapture,
    UdpMicCapture,
    make_mic_capture,
    parse_udp_device,
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_udp_capture_drops_odd_byte_count():
    """A malformed sender (or a corrupted packet) sending an odd byte
    count must NOT crash the daemon — drop and log."""
    cap = UdpMicCapture(host="127.0.0.1", port=0)
    async with cap as bound:
        port = bound._transport.get_extra_info("sockname")[1]

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 3 bytes — can't be an int16 frame.
            sender.sendto(b"\x01\x02\x03", ("127.0.0.1", port))
            # Follow with a valid frame so we can verify the receiver
            # is still running after the malformed drop.
            good = np.array([1, 2, 3, 4], dtype=np.int16)
            sender.sendto(good.tobytes(), ("127.0.0.1", port))
        finally:
            sender.close()

        gen = bound.frames()
        received = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        # The malformed packet was dropped; the next yielded frame
        # is the good one.
        assert received.tolist() == [1, 2, 3, 4]


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_udp_capture_frame_size_constant_matches_micapture():
    """The UDP frame size contract is the SAME as MicCapture's output
    contract. Voice's WakeLoop is transport-agnostic only as long as
    these stay in sync."""
    assert UdpMicCapture.OUTPUT_FRAME_SAMPLES == MicCapture.OUTPUT_FRAME_SAMPLES
    assert UdpMicCapture.OUTPUT_RATE == MicCapture.OUTPUT_RATE
