# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wire + stats pin for `jasper.aec.bridge_telemetry`.

Consumers read the emitted packets (jasper-voice, jasper-usbmic) and the
counter deltas in the stats file (the wake-corpus recorder, jasper-doctor),
so one fixed emit sequence pins both surfaces together.
"""
from __future__ import annotations

import json
import os
import struct
import time

import pytest

from jasper.aec.bridge_telemetry import (
    DropLogDebouncer,
    LegEmitter,
    TimestampedLegEmitter,
    _BridgeStats,
)
from jasper.usb_mic import (
    USB_MIC_HEADER_BYTES,
    USB_MIC_HEADER_STRUCT,
    USB_MIC_PACKET_MAGIC,
    USB_MIC_PACKET_VERSION,
)
from tests._aec_bridge_helpers import IDENTITY

ON_DEST = ("127.0.0.1", 9876)
USB_DEST = ("127.0.0.1", 9894)
FRAMES = [
    bytes((i * 7 + j) % 256 for j in range(320 * 2))
    for i in range(10)
]


class _FakeSock:
    """Records sendto payloads; refuses the attempt numbers in `refuse`."""

    def __init__(self, refuse: frozenset[int] = frozenset()) -> None:
        self.refuse = refuse
        self.attempts = 0
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, packet: bytes, dest: tuple[str, int]) -> None:
        self.attempts += 1
        if self.attempts in self.refuse:
            raise BlockingIOError("full")
        self.sent.append((packet, dest))


def test_emit_sequence_pins_packets_and_stats_snapshot(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(
        "jasper.aec.bridge_telemetry.time.monotonic", lambda: 1234.5,
    )
    clock_ids = []

    def clock_gettime_ns(clock_id: int) -> int:
        clock_ids.append(clock_id)
        return 7_000_000_123

    monkeypatch.setattr(
        "jasper.aec.bridge_telemetry.time.clock_gettime_ns",
        clock_gettime_ns,
    )
    stats = _BridgeStats(IDENTITY)
    on_sock = _FakeSock(refuse=frozenset({2}))
    usb_sock = _FakeSock()
    on = LegEmitter(
        sock=on_sock,  # type: ignore[arg-type]
        dest=ON_DEST,
        batch=bytearray(),
        stats_key="on",
        stats=stats,
    )
    usb = TimestampedLegEmitter(
        sock=usb_sock,  # type: ignore[arg-type]
        dest=USB_DEST,
        batch=bytearray(),
        stats_key="usb_host_mic",
        stats=stats,
        frame_samples=IDENTITY.frame_samples,
    )

    for frame in FRAMES:
        on.emit(frame)
        usb.emit(frame)

    # Ten 320-sample frames batch into two 1280-sample wake packets; the
    # second sendto is refused and is dropped rather than retried.
    assert on_sock.attempts == 2
    assert on_sock.sent == [(b"".join(FRAMES[:4]), ON_DEST)]
    assert [dest for _packet, dest in usb_sock.sent] == [USB_DEST] * 10
    assert [packet[USB_MIC_HEADER_BYTES:] for packet, _dest in usb_sock.sent] == FRAMES
    assert clock_ids == [time.CLOCK_MONOTONIC] * 10
    assert [
        struct.unpack(USB_MIC_HEADER_STRUCT, packet[:USB_MIC_HEADER_BYTES])
        for packet, _dest in usb_sock.sent
    ] == [
        (
            USB_MIC_PACKET_MAGIC,
            USB_MIC_PACKET_VERSION,
            0,
            seq,
            7_000_000_123,
        )
        for seq in range(10)
    ]

    stats.inc("frames_processed", 10)
    stats.inc("ref_starved_frames")
    stats.inc_nested("queue_drops", "mic", 3)
    stats.record_reference_frames(4)
    stats.set_capture_stream(
        sample_rate_hz=16000, block_frames=320, input_latency_seconds=0.08,
    )
    stats.set_leg_engine("dtln", enabled=True, loaded=True)
    stats.set_active_capture_plan(
        wake_corpus_plan_id="plan-1",
        expected_legs=("on", "raw0", "dtln"),
        emitted_legs=["on", "raw0", "dtln"],
        corpus_flags={"ref": True},
        beam_plan={"plan_id": "xvf_square_fixed_150_210"},
        ports={"on": 9876, "raw0": 9879, "dtln": 9878},
        mic_reference_identity={"ref_source": "outputd_udp"},
        usb_mic_source={"selection": "primary", "mode": "raw0", "leg": "raw0"},
        mic_fingerprint="mic-a",
        dac_reference_fingerprint="dac-a",
    )
    stats.set_usb_mic_effective_source(
        mode="chip_aec", leg="chip_aec_150", fallback_active=True,
    )
    stats.mark_leg_unavailable("dtln", error="no onnx")

    path = tmp_path / "aec_bridge_stats.json"
    stats.write_snapshot(path)
    written = json.loads(path.read_text())
    assert written["pid"] == os.getpid()

    snapshot = stats.snapshot()
    for volatile in ("pid", "started_epoch_sec", "updated_epoch_sec"):
        snapshot.pop(volatile)
        written.pop(volatile)
    assert written == snapshot

    assert snapshot == {
        "schema_version": 4,
        "sample_rate_hz": 16000,
        "frame_samples": 320,
        "out_frame_samples": 1280,
        "counters": {
            "frames_processed": 10,
            "ref_starved_frames": 1,
            "usb_mic_source_fallback_frames": 0,
            "queue_drops": {
                "mic": 3, "chip": 0, "raw0": 0, "usb": 0, "ref": 0,
            },
            "packets_sent_by_leg": {
                "on": 1, "usb_host_mic": 10, "off": 0, "dtln": 0, "raw0": 0,
                "ref": 0, "usb_raw": 0, "usb_webrtc": 0, "usb_dtln": 0,
                "xvf_raw0_webrtc_aec3": 0, "xvf_raw0_dtln": 0,
                "chip_aec_150": 0, "chip_aec_210": 0,
                "aec3_variant_1": 0, "aec3_variant_2": 0, "aec3_variant_3": 0,
            },
            "udp_send_drops_by_leg": {
                "on": 1, "off": 0, "dtln": 0, "raw0": 0, "ref": 0,
                "usb_raw": 0, "usb_webrtc": 0, "usb_dtln": 0,
                "xvf_raw0_webrtc_aec3": 0, "xvf_raw0_dtln": 0,
                "chip_aec_150": 0, "chip_aec_210": 0,
                "aec3_variant_1": 0, "aec3_variant_2": 0, "aec3_variant_3": 0,
            },
        },
        "leg_engines": {
            "dtln": {"enabled": True, "loaded": False, "error": "no onnx"},
        },
        "capture_stream": {
            "sample_rate_hz": 16000,
            "block_frames": 320,
            "input_latency_seconds": 0.08,
            "input_latency_frames": 1280,
        },
        "reference_input": {
            "source": "outputd_udp",
            "endpoint": "127.0.0.1:9891",
            "frames_enqueued": 4,
            "last_frame_age_ms": 0,
            "snapshot_monotonic_ms": 1234500,
            "process_age_ms": 0,
        },
        "active_capture_plan": {
            "wake_corpus_plan_id": "plan-1",
            "expected_legs": ["on", "raw0", "dtln"],
            "emitted_legs": ["on", "raw0"],
            "enabled_corpus_flags": {"ref": True},
            "beam_plan": {"plan_id": "xvf_square_fixed_150_210"},
            "ports": {"on": 9876, "raw0": 9879},
            "mic_reference_identity": {"ref_source": "outputd_udp"},
            "usb_mic_source": {
                "selection": "primary",
                "mode": "chip_aec",
                "leg": "chip_aec_150",
                "fallback_active": True,
            },
            "mic_fingerprint": "mic-a",
            "dac_reference_fingerprint": "dac-a",
        },
        "wake_corpus_plan_id": "plan-1",
        "emitted_legs": ["on", "raw0"],
    }


def test_timestamped_sequence_wraps_and_survives_a_refused_send(
    monkeypatch,
) -> None:
    """The u32 sequence is the receiver's only drop detector, so it wraps
    rather than widening and still advances across a refused send."""
    monkeypatch.setattr(
        "jasper.aec.bridge_telemetry.time.clock_gettime_ns",
        lambda _clock: 1,
    )
    stats = _BridgeStats(IDENTITY)
    sock = _FakeSock(refuse=frozenset({2}))
    emitter = TimestampedLegEmitter(
        sock=sock,  # type: ignore[arg-type]
        dest=USB_DEST,
        batch=bytearray(),
        stats_key="usb_host_mic",
        stats=stats,
        frame_samples=IDENTITY.frame_samples,
    )
    emitter._seq = 0xFFFFFFFF

    for frame in FRAMES[:3]:
        emitter.emit(frame)

    # The dropped middle packet still consumes sequence 0: the receiver sees
    # the gap rather than a renumbered stream.
    assert [
        struct.unpack(USB_MIC_HEADER_STRUCT, packet[:USB_MIC_HEADER_BYTES])[3]
        for packet, _dest in sock.sent
    ] == [0xFFFFFFFF, 1]
    assert sock.attempts == 3
    counters = stats.snapshot()["counters"]
    assert counters["udp_send_drops_by_leg"]["usb_host_mic"] == 1
    assert counters["packets_sent_by_leg"]["usb_host_mic"] == 2


def test_leg_engine_status_reloads_and_clears_on_reset() -> None:
    """`leg_engines` is the journal-independent surface jasper-doctor's
    check_aec_bridge_dtln_engine reads to catch a silent DTLN load failure."""
    stats = _BridgeStats(IDENTITY)
    stats.set_leg_engine("dtln", enabled=True, loaded=False, error="no onnx")
    stats.set_leg_engine("dtln", enabled=True, loaded=True)

    assert stats.snapshot()["leg_engines"] == {
        "dtln": {"enabled": True, "loaded": True, "error": None},
    }

    stats.reset()

    assert stats.snapshot()["leg_engines"] == {}


def test_drop_log_debouncer_aggregates_one_second_windows():
    debouncer = DropLogDebouncer()

    assert debouncer.record(10.0) == (1, 1.0)
    assert debouncer.record(10.25) is None
    assert debouncer.record(10.50) is None

    drops, window_sec = debouncer.record(11.10)
    assert drops == 3
    assert window_sec == pytest.approx(1.1)
