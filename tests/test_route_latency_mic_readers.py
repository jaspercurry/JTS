# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Focused contract tests for route-latency mic source construction and I/O."""

from __future__ import annotations

import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

from jasper.route_latency import mic_readers
from jasper.route_latency.mic_readers import (
    RAW0_SAMPLE_RATE_HZ,
    RAW0_UDP_HOST,
    RAW0_UDP_PORT,
    AlsaMicReader,
    MicChunk,
    MicSourceUnavailableError,
    UdpMicReader,
    build_mic_reader,
)


class _FakeDatagramSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None
        self.bound_address: tuple[str, int] | None = None
        self.packets: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def bind(self, address: tuple[str, int]) -> None:
        self.bound_address = address

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        if not self.packets:
            raise AssertionError("test did not enqueue a UDP packet")
        return self.packets.pop(0)

    def getsockname(self) -> tuple[str, int]:
        assert self.bound_address is not None
        return self.bound_address

    def close(self) -> None:
        self.closed = True


@dataclass
class _FakeSocketFactory:
    created: list[_FakeDatagramSocket] = field(default_factory=list)

    def __call__(self, family: int, kind: int) -> _FakeDatagramSocket:
        assert family == socket.AF_INET
        assert kind == socket.SOCK_DGRAM
        sock = _FakeDatagramSocket()
        self.created.append(sock)
        return sock


@pytest.fixture
def fake_socket_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeSocketFactory:
    factory = _FakeSocketFactory()
    monkeypatch.setattr(mic_readers.socket, "socket", factory)
    return factory


@pytest.mark.parametrize(
    "spec",
    ["", "udp", "   ", " udp ", f"udp:{RAW0_UDP_PORT}", f" udp:{RAW0_UDP_PORT} "],
)
def test_build_mic_reader_default_udp_aliases_bind_raw0(
    spec: str,
    fake_socket_factory: _FakeSocketFactory,
) -> None:
    reader = build_mic_reader(spec)

    assert isinstance(reader, UdpMicReader)
    assert fake_socket_factory.created[-1].bound_address == (
        RAW0_UDP_HOST,
        RAW0_UDP_PORT,
    )
    assert (
        fake_socket_factory.created[-1].timeout
        == mic_readers.DEFAULT_UDP_READ_TIMEOUT_SECONDS
    )
    reader.close()


def test_build_mic_reader_custom_udp_port(
    fake_socket_factory: _FakeSocketFactory,
) -> None:
    reader = build_mic_reader("udp:9999")

    assert isinstance(reader, UdpMicReader)
    assert fake_socket_factory.created[-1].bound_address == (RAW0_UDP_HOST, 9999)
    reader.close()


def test_udp_reader_returns_exact_data_with_fresh_arrival_timestamp_and_delegates_close(
    monkeypatch: pytest.MonkeyPatch,
    fake_socket_factory: _FakeSocketFactory,
) -> None:
    reader = UdpMicReader(port=9999)
    sock = fake_socket_factory.created[-1]
    sock.packets = [
        (b"first-packet", (RAW0_UDP_HOST, 1234)),
        (b"second-packet", (RAW0_UDP_HOST, 1234)),
    ]
    timestamps: Iterator[int] = iter((10_000, 20_000))
    monkeypatch.setattr(mic_readers.time, "monotonic_ns", lambda: next(timestamps))

    assert reader.read_chunk() == MicChunk(b"first-packet", 10_000, RAW0_SAMPLE_RATE_HZ)
    assert reader.read_chunk() == MicChunk(
        b"second-packet", 20_000, RAW0_SAMPLE_RATE_HZ
    )

    reader.close()
    assert sock.closed is True


class _FakePcm:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.reads: list[tuple[int, bytes]] = []
        self.closed = False

    def read(self) -> tuple[int, bytes]:
        if not self.reads:
            raise AssertionError("test did not enqueue an ALSA period")
        return self.reads.pop(0)

    def close(self) -> None:
        self.closed = True


@dataclass
class _FakeAlsaState:
    capture_constant: int = 101
    normal_constant: int = 202
    s16le_constant: int = 303
    pcms: list[_FakePcm] = field(default_factory=list)

    def open_pcm(self, **kwargs: Any) -> _FakePcm:
        pcm = _FakePcm(kwargs)
        self.pcms.append(pcm)
        return pcm


@pytest.fixture
def fake_alsaaudio(monkeypatch: pytest.MonkeyPatch) -> _FakeAlsaState:
    state = _FakeAlsaState()
    module = ModuleType("alsaaudio")
    setattr(module, "PCM_CAPTURE", state.capture_constant)
    setattr(module, "PCM_NORMAL", state.normal_constant)
    setattr(module, "PCM_FORMAT_S16_LE", state.s16le_constant)
    setattr(module, "PCM", state.open_pcm)
    monkeypatch.setitem(sys.modules, "alsaaudio", module)
    return state


def test_build_alsa_reader_preserves_colons_and_uses_capture_contract(
    fake_alsaaudio: _FakeAlsaState,
) -> None:
    device = "plughw:CARD=Measurement:1,0"
    reader = build_mic_reader(f"alsa:{device}")

    assert isinstance(reader, AlsaMicReader)
    assert fake_alsaaudio.pcms[-1].kwargs == {
        "type": fake_alsaaudio.capture_constant,
        "mode": fake_alsaaudio.normal_constant,
        "device": device,
        "rate": 16_000,
        "channels": 1,
        "format": fake_alsaaudio.s16le_constant,
        "periodsize": 1280,
    }
    reader.close()


def test_alsa_reader_returns_exact_data_with_fresh_arrival_timestamp_and_delegates_close(
    monkeypatch: pytest.MonkeyPatch,
    fake_alsaaudio: _FakeAlsaState,
) -> None:
    reader = AlsaMicReader("hw:1,0", sample_rate_hz=48_000, period_frames=480)
    pcm = fake_alsaaudio.pcms[-1]
    pcm.reads = [(480, b"first-period"), (240, b"second-period")]
    timestamps: Iterator[int] = iter((30_000, 40_000))
    monkeypatch.setattr(mic_readers.time, "monotonic_ns", lambda: next(timestamps))

    assert reader.read_chunk() == MicChunk(b"first-period", 30_000, 48_000)
    assert reader.read_chunk() == MicChunk(b"second-period", 40_000, 48_000)

    reader.close()
    assert pcm.closed is True


@pytest.mark.parametrize("length", [0, -1])
def test_alsa_reader_rejects_nonpositive_frame_count(
    length: int,
    fake_alsaaudio: _FakeAlsaState,
) -> None:
    reader = AlsaMicReader("hw:1,0")
    fake_alsaaudio.pcms[-1].reads = [(length, b"")]

    with pytest.raises(MicSourceUnavailableError, match=rf"rc={length}"):
        reader.read_chunk()
    reader.close()


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("alsa:", "requires a device name"),
        ("bogus", "unrecognized --mic spec"),
    ],
)
def test_build_mic_reader_rejects_unknown_or_empty_alsa_spec(
    spec: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_mic_reader(spec)


@pytest.mark.parametrize("spec", ["udp:", "udp:not-a-port", "udp:12:34"])
def test_build_mic_reader_rejects_malformed_udp_port(spec: str) -> None:
    with pytest.raises(ValueError):
        build_mic_reader(spec)
