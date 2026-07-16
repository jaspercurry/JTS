# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import struct
import threading
from unittest.mock import MagicMock

import pytest

from jasper.cli import usb_mic as usb_mic_cli
from jasper.cli.usb_mic import (
    ALSA_BUFFER_US,
    DropRegimeCounters,
    HostPcmSnapshot,
    HostProgressTracker,
    PACKET_BYTES,
    PERIOD_BYTES,
    QueuedFrame,
    QUEUE_PERIODS,
    V2_PACKET_BYTES,
    _audio_health_snapshot,
    _decode_audio_packet,
    _read_host_pcm_status,
    _sequence_gap,
    _source_age_percentiles,
)
from jasper.music_sources import Source
from jasper.source_intent import intent_env_key
from jasper.usb_mic import (
    USB_MIC_HEADER_BYTES,
    USB_MIC_HEADER_STRUCT,
    USB_MIC_PACKET_MAGIC,
    USB_MIC_PACKET_VERSION,
    build_usb_mic_status,
    read_intent,
    usb_mic_enabled,
    write_usb_mic_enabled,
)


def test_intent_is_explicit_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "usb_mic.env"
    assert read_intent(path).valid is False
    assert usb_mic_enabled(path) is False
    write_usb_mic_enabled(True, path)
    assert usb_mic_enabled(path) is True
    assert path.read_text() == "JASPER_USB_MIC=enabled\n"
    write_usb_mic_enabled(False, path)
    assert usb_mic_enabled(path) is False
    assert read_intent(path).valid is True


def test_intent_reads_reject_symlinks_and_oversized_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.env"
    target.write_text("JASPER_USB_MIC=enabled\n")
    intent_link = tmp_path / "intent-link.env"
    intent_link.symlink_to(target)
    assert read_intent(intent_link).valid is False
    assert usb_mic_enabled(intent_link) is False

    oversized = tmp_path / "oversized.env"
    oversized.write_bytes(b"JASPER_USB_MIC=enabled\n" + (b"x" * 4096))
    assert read_intent(oversized).valid is False
    assert usb_mic_enabled(oversized) is False


def _status_fixture(
    tmp_path: Path,
    *,
    relay_overrides: dict | None = None,
) -> dict:
    intent = tmp_path / "usb_mic.env"
    source_intent = tmp_path / "source_intent.env"
    gadget = tmp_path / "gadget"
    relay = tmp_path / "status.json"
    write_usb_mic_enabled(True, intent)
    source_intent.write_text(f"{intent_env_key(Source.USBSINK)}=enabled\n")
    function = gadget / "functions/uac2.usb0"
    function.mkdir(parents=True)
    (function / "p_chmask").write_text("1\n")
    (gadget / "bcdDevice").write_text("0x0210\n")
    relay_payload = {
        "updated_epoch_sec": 100.0,
        "state": "running",
        "host_streaming": True,
    }
    relay_payload.update(relay_overrides or {})
    relay.write_text(json.dumps(relay_payload))
    return build_usb_mic_status(
        {
            "bridge_active": True,
            "microphone": {"detected": True},
            "audio_profile": {"active": "xvf_chip_aec"},
        },
        intent_path=intent,
        source_intent_path=source_intent,
        gadget_path=gadget,
        relay_status_path=relay,
        systemd_active=lambda _unit: True,
        now=100.5,
    )


def test_status_reports_streaming_only_when_descriptor_and_relay_are_live(tmp_path: Path) -> None:
    status = _status_fixture(tmp_path)
    assert status["enabled"] is True
    assert status["advertised"] is True
    assert status["state"] == "streaming"
    assert status["host_streaming"] is True
    assert status["toggle_enabled"] is True
    assert status["descriptor_revision_ok"] is True
    assert "computer" in status["detail"].lower()
    assert "Mac" not in status["detail"]


def test_status_surfaces_relay_latency_and_loss_telemetry(tmp_path: Path) -> None:
    status = _status_fixture(tmp_path, relay_overrides={
        "schema_version": 3,
        "source_age_basis": "bridge_emit_monotonic_v2",
        "source_age_sample_count": 100,
        "source_age_ms_p50": 31.2,
        "source_age_ms_p95": 47.8,
        "source_age_ms_p99": 52.1,
        "packets_lost": 2,
        "periods_dropped_streaming": 3,
        "periods_dropped_idle": 40,
    })

    assert status["relay_schema_version"] == 3
    assert status["source_age_basis"] == "bridge_emit_monotonic_v2"
    assert status["source_age_sample_count"] == 100
    assert status["source_age_ms_p50"] == 31.2
    assert status["source_age_ms_p95"] == 47.8
    assert status["source_age_ms_p99"] == 52.1
    assert status["packets_lost"] == 2
    assert status["periods_dropped_streaming"] == 3
    assert status["periods_dropped_idle"] == 40


def test_status_does_not_conflate_assistant_pause_with_usb_export(
    tmp_path: Path,
) -> None:
    # The assistant's persisted pause state may exist beside USB intent, but it
    # is not an input to USB export policy or status.
    (tmp_path / "mic_mute.env").write_text("JASPER_MIC_MUTED=1\n")
    status = _status_fixture(tmp_path)
    assert status["state"] == "streaming"
    assert "mic_muted" not in status


def test_status_reports_stale_descriptor_revision_as_degraded(tmp_path: Path) -> None:
    _status_fixture(tmp_path)
    gadget = tmp_path / "gadget"
    (gadget / "bcdDevice").write_text("0x0200\n")
    status = build_usb_mic_status(
        {
            "bridge_active": True,
            "microphone": {"detected": True},
            "audio_profile": {"active": "xvf_chip_aec"},
        },
        intent_path=tmp_path / "usb_mic.env",
        source_intent_path=tmp_path / "source_intent.env",
        gadget_path=gadget,
        relay_status_path=tmp_path / "status.json",
        systemd_active=lambda _unit: True,
        now=100.5,
    )
    assert status["state"] == "degraded"
    assert status["descriptor_revision_ok"] is False


def test_status_gates_enablement_on_existing_usb_audio_source(tmp_path: Path) -> None:
    intent = tmp_path / "usb_mic.env"
    source_intent = tmp_path / "source_intent.env"
    write_usb_mic_enabled(False, intent)
    source_intent.write_text(f"{intent_env_key(Source.USBSINK)}=disabled\n")
    status = build_usb_mic_status(
        {
            "bridge_active": True,
            "microphone": {"detected": True},
            "audio_profile": {"active": "xvf_chip_aec"},
        },
        intent_path=intent,
        source_intent_path=source_intent,
        gadget_path=tmp_path / "missing-gadget",
        relay_status_path=tmp_path / "missing-status.json",
        systemd_active=lambda _unit: False,
    )
    assert status["toggle_enabled"] is False
    assert "USB Audio Input" in status["detail"]


def test_usb_mic_transport_uses_one_aec_frame_and_conservative_buffers() -> None:
    assert PACKET_BYTES == PERIOD_BYTES
    assert QUEUE_PERIODS == 2
    assert ALSA_BUFFER_US == 40_000


def test_aplay_sink_applies_the_latency_buffer_contract(monkeypatch) -> None:
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdin.fileno.return_value = 10
    process.stderr = MagicMock()
    process.poll.return_value = None
    popen = MagicMock(return_value=process)
    thread = MagicMock()
    monkeypatch.setattr(usb_mic_cli.subprocess, "Popen", popen)
    monkeypatch.setattr(usb_mic_cli.threading, "Thread", lambda **_kwargs: thread)
    monkeypatch.setattr(usb_mic_cli.fcntl, "F_SETPIPE_SZ", 1031, raising=False)
    monkeypatch.setattr(usb_mic_cli.fcntl, "F_GETPIPE_SZ", 1032, raising=False)
    monkeypatch.setattr(
        usb_mic_cli.fcntl,
        "fcntl",
        lambda _fd, operation, *_args: 16_384 if operation == 1032 else 4_096,
    )

    sink = usb_mic_cli.AplaySink()
    command = popen.call_args.args[0]
    assert command[command.index("-F") + 1] == "10000"
    assert command[command.index("-B") + 1] == "40000"
    thread.start.assert_called_once()
    sink.close()


def test_aplay_sink_logs_actual_pipe_capacity_and_occupancy(monkeypatch) -> None:
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdin.fileno.return_value = 10
    process.stderr = MagicMock()
    process.poll.return_value = None
    events = MagicMock()

    def fake_fcntl(_fd, operation, *_args):
        return 16_384 if operation == 1032 else 4_096

    def fake_ioctl(_fd, _operation, pending, _mutate):
        pending[0] = 15_680
        return 0

    monkeypatch.setattr(usb_mic_cli.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(
        usb_mic_cli.threading,
        "Thread",
        lambda **_kwargs: MagicMock(),
    )
    monkeypatch.setattr(usb_mic_cli.fcntl, "F_SETPIPE_SZ", 1031, raising=False)
    monkeypatch.setattr(usb_mic_cli.fcntl, "F_GETPIPE_SZ", 1032, raising=False)
    monkeypatch.setattr(usb_mic_cli.fcntl, "fcntl", fake_fcntl)
    monkeypatch.setattr(usb_mic_cli.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(usb_mic_cli, "log_event", events)

    sink = usb_mic_cli.AplaySink()
    sink.log_pipe_baseline_once()
    sink.log_pipe_baseline_once()

    assert events.call_args_list[0].args[:2] == (
        usb_mic_cli.logger,
        "usb_mic.pipe_configured",
    )
    assert events.call_args_list[0].kwargs == {
        "requested": 4_096,
        "actual": 16_384,
        "error": "",
    }
    assert events.call_args_list[1].args[:2] == (
        usb_mic_cli.logger,
        "usb_mic.pipe_baseline",
    )
    assert events.call_args_list[1].kwargs == {
        "capacity_bytes": 16_384,
        "pending_bytes": 15_680,
        "pending_ms": 490.0,
        "error": "",
    }
    assert events.call_count == 2
    sink.close()


def test_pipe_diagnostics_fail_once_without_crashing(monkeypatch) -> None:
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdin.fileno.return_value = 10
    process.stderr = MagicMock()
    process.poll.return_value = None
    events = MagicMock()

    def unsupported(*_args):
        raise OSError("unsupported")

    monkeypatch.setattr(usb_mic_cli.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(
        usb_mic_cli.threading,
        "Thread",
        lambda **_kwargs: MagicMock(),
    )
    monkeypatch.setattr(usb_mic_cli.fcntl, "F_SETPIPE_SZ", 1031, raising=False)
    monkeypatch.setattr(usb_mic_cli.fcntl, "F_GETPIPE_SZ", 1032, raising=False)
    monkeypatch.setattr(usb_mic_cli.fcntl, "fcntl", unsupported)
    monkeypatch.setattr(usb_mic_cli, "log_event", events)

    sink = usb_mic_cli.AplaySink()
    sink.log_pipe_baseline_once()
    sink.log_pipe_baseline_once()

    assert events.call_count == 2
    assert "unsupported" in events.call_args_list[0].kwargs["error"]
    assert "unsupported" in events.call_args_list[1].kwargs["error"]
    sink.close()


def test_status_writer_owns_schema_and_timestamp(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    monkeypatch.setattr(usb_mic_cli.time, "time", lambda: 123.5)

    usb_mic_cli._write_status(
        str(path),
        {"schema_version": 2, "updated_epoch_sec": 1.0, "state": "running"},
    )

    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 3
    assert payload["updated_epoch_sec"] == 123.5


def test_relay_forwards_nonzero_pcm_unchanged(monkeypatch, tmp_path: Path) -> None:
    """Assistant pause cannot turn the independent USB export into silence."""

    class StopRelay(Exception):
        pass

    class FakeSocket:
        def __init__(self) -> None:
            self.calls = 0

        def setsockopt(self, *_args) -> None:
            pass

        def bind(self, *_args) -> None:
            pass

        def settimeout(self, *_args) -> None:
            pass

        def recvfrom(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return (b"\x01\x02" * (PERIOD_BYTES // 2), None)
            raise StopRelay

        def close(self) -> None:
            pass

    class FakeQueue:
        def __init__(self) -> None:
            self.items: list[QueuedFrame] = []
            self.dropped = 0

        def put(self, frame: QueuedFrame) -> None:
            self.items.append(frame)

    class FakeSink:
        def __init__(self) -> None:
            self.queue = FakeQueue()

        def check(self) -> None:
            pass

        def progress(self) -> tuple[int, float, float]:
            return (0, 0.0, 0.0)

        def source_ages_ms(self) -> tuple[float, ...]:
            return ()

        def log_pipe_baseline_once(self) -> None:
            pass

        def close(self) -> None:
            pass

    fake_socket = FakeSocket()
    fake_sink = FakeSink()
    monkeypatch.setattr(usb_mic_cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(usb_mic_cli.socket, "socket", lambda *_args: fake_socket)
    monkeypatch.setattr(usb_mic_cli, "AplaySink", lambda: fake_sink)
    monkeypatch.setattr(
        usb_mic_cli,
        "_read_host_pcm_status",
        lambda: HostPcmSnapshot(False, None),
    )
    monkeypatch.setattr(usb_mic_cli, "_write_status", lambda *_args: None)

    with pytest.raises(StopRelay):
        usb_mic_cli.run_relay(status_path=str(tmp_path / "status.json"))

    assert [frame.pcm for frame in fake_sink.queue.items] == [
        b"\x01\x02" * (PERIOD_BYTES // 2)
    ]
    assert fake_sink.queue.items[0].seq is None


def _write_one_frame(
    monkeypatch,
    frame: QueuedFrame,
    *,
    now_ns: int,
) -> usb_mic_cli.AplaySink:
    class ScriptedQueue:
        _closed = False

        def __init__(self) -> None:
            self._items = [frame, None]

        def get(self, timeout: float) -> QueuedFrame | None:
            return self._items.pop(0)

    sink = usb_mic_cli.AplaySink.__new__(usb_mic_cli.AplaySink)
    sink.queue = ScriptedQueue()
    sink.process = MagicMock()
    sink.process.stdin = MagicMock()
    sink.process.poll.return_value = 1
    sink._progress_lock = threading.Lock()
    sink._source_ages_ms = deque(maxlen=512)
    sink.frames_written = 0
    sink.last_progress_epoch_sec = 0.0
    sink.last_progress_monotonic = 0.0
    sink.error = ""
    monkeypatch.setattr(
        usb_mic_cli.time,
        "clock_gettime_ns",
        lambda _clock: now_ns,
    )
    sink._write_loop()
    return sink


def test_relay_parses_v2_header_and_measures_age(monkeypatch) -> None:
    pcm = b"\x11\x22" * (PERIOD_BYTES // 2)
    captured_ns = 1_000_000_000
    packet = struct.pack(
        USB_MIC_HEADER_STRUCT,
        USB_MIC_PACKET_MAGIC,
        USB_MIC_PACKET_VERSION,
        0,
        7,
        captured_ns,
    ) + pcm

    frames = _decode_audio_packet(packet, received_monotonic_ns=9_000_000_000)
    assert frames == (QueuedFrame(captured_ns, 7, pcm),)
    assert len(packet) == V2_PACKET_BYTES

    sink = _write_one_frame(
        monkeypatch,
        frames[0],
        now_ns=captured_ns + 50_000_000,
    )

    sink.process.stdin.write.assert_called_once_with(pcm)
    assert _source_age_percentiles(sink.source_ages_ms()) == {
        "source_age_ms_p50": 50.0,
        "source_age_ms_p95": 50.0,
        "source_age_ms_p99": 50.0,
    }


def test_relay_accepts_v1_raw_packets_without_false_emit_age(monkeypatch) -> None:
    pcm = b"\x01\x02" * (PERIOD_BYTES // 2)
    frames = _decode_audio_packet(pcm, received_monotonic_ns=123_000)

    assert frames == (QueuedFrame(123_000, None, pcm),)
    sink = _write_one_frame(monkeypatch, frames[0], now_ns=999_000)
    sink.process.stdin.write.assert_called_once_with(pcm)
    assert sink.source_ages_ms() == ()


def test_relay_splits_legacy_packet_into_ordered_native_frames() -> None:
    chunks = tuple(bytes([value]) * PERIOD_BYTES for value in range(4))
    frames = _decode_audio_packet(
        b"".join(chunks),
        received_monotonic_ns=123_000,
    )

    assert frames is not None
    assert len(frames) == 4
    assert tuple(frame.pcm for frame in frames) == chunks
    assert all(frame.t_capture_ns == 123_000 for frame in frames)
    assert all(frame.seq is None for frame in frames)


@pytest.mark.parametrize("field", ["magic", "version"])
def test_relay_rejects_malformed_v2_headers(field: str) -> None:
    magic = b"NO" if field == "magic" else USB_MIC_PACKET_MAGIC
    version = 99 if field == "version" else USB_MIC_PACKET_VERSION
    packet = struct.pack(
        USB_MIC_HEADER_STRUCT,
        magic,
        version,
        0,
        1,
        1_000,
    ) + bytes(PERIOD_BYTES)

    assert len(packet) == USB_MIC_HEADER_BYTES + PERIOD_BYTES
    assert _decode_audio_packet(packet, received_monotonic_ns=2_000) is None


def test_relay_counts_seq_gaps_without_inflating_resets() -> None:
    assert _sequence_gap(None, 0) == 0
    assert _sequence_gap(0, 1) == 0
    assert _sequence_gap(1, 3) == 1
    assert _sequence_gap(0xFFFFFFFF, 0) == 0
    assert _sequence_gap(100, 0) == 0
    assert _sequence_gap(10, 10) == 0


def test_relay_splits_drops_by_regime() -> None:
    counters = DropRegimeCounters()
    counters.record(3, host_clock_advancing=False)
    counters.record(2, host_clock_advancing=True)
    counters.record(0, host_clock_advancing=True)

    assert counters.idle == 3
    assert counters.streaming == 2
    assert counters.idle + counters.streaming == 5


def test_relay_reports_v2_loss_percentiles_and_drop_regimes(monkeypatch) -> None:
    class StopRelay(Exception):
        pass

    pcm = bytes(PERIOD_BYTES)

    def packet(seq: int) -> bytes:
        return struct.pack(
            USB_MIC_HEADER_STRUCT,
            USB_MIC_PACKET_MAGIC,
            USB_MIC_PACKET_VERSION,
            0,
            seq,
            1_000_000_000 + seq,
        ) + pcm

    class FakeSocket:
        def __init__(self) -> None:
            self.packets = iter((packet(0), packet(1), packet(3)))

        def setsockopt(self, *_args) -> None:
            pass

        def bind(self, *_args) -> None:
            pass

        def settimeout(self, *_args) -> None:
            pass

        def recvfrom(self, *_args):
            return next(self.packets), None

        def close(self) -> None:
            pass

    class FakeQueue:
        def __init__(self) -> None:
            self.dropped = 0

        def put(self, _frame: QueuedFrame) -> None:
            self.dropped += 1

    class FakeSink:
        def __init__(self) -> None:
            self.queue = FakeQueue()
            self.baselines = 0
            self.baseline_drop_count = None

        def check(self) -> None:
            pass

        def progress(self) -> tuple[int, float, float]:
            return 0, 1.4, 100.0

        def source_ages_ms(self) -> tuple[float, ...]:
            return 10.0, 20.0, 30.0

        def log_pipe_baseline_once(self) -> None:
            if self.baselines == 0:
                self.baselines += 1
                self.baseline_drop_count = self.queue.dropped

        def close(self) -> None:
            pass

    statuses: list[dict] = []

    def capture_status(_path: str, payload: dict) -> None:
        if payload["state"] != "running":
            return
        statuses.append(payload)
        if payload["packets_received"] == 3:
            raise StopRelay

    monotonic_values = iter((
        1_000.0,
        1_000.1,
        1_000.2,
        1_000.7,
        1_000.8,
        1_001.3,
        1_001.4,
    ))
    host_snapshots = iter((
        HostPcmSnapshot(True, 100),
        HostPcmSnapshot(True, 100),
        HostPcmSnapshot(True, 200),
    ))
    fake_sink = FakeSink()
    monkeypatch.setattr(usb_mic_cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(usb_mic_cli.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(usb_mic_cli, "AplaySink", lambda: fake_sink)
    monkeypatch.setattr(
        usb_mic_cli.time,
        "monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        usb_mic_cli.time,
        "clock_gettime_ns",
        lambda _clock: 2_000_000_000,
    )
    monkeypatch.setattr(
        usb_mic_cli,
        "_read_host_pcm_status",
        lambda: next(host_snapshots),
    )
    monkeypatch.setattr(usb_mic_cli, "_write_status", capture_status)

    with pytest.raises(StopRelay):
        usb_mic_cli.run_relay()

    status = statuses[-1]
    assert status["packets_received"] == 3
    assert status["v2_packets_received"] == 3
    assert status["packets_lost"] == 1
    assert status["source_age_ms_p50"] == 20.0
    assert status["source_age_ms_p95"] == 30.0
    assert status["source_age_ms_p99"] == 30.0
    assert status["periods_dropped_idle"] == 2
    assert status["periods_dropped_streaming"] == 1
    assert status["periods_dropped"] == 3
    assert fake_sink.baselines == 1
    assert fake_sink.baseline_drop_count == 2


def test_host_streaming_requires_advancing_host_and_sink_progress() -> None:
    tracker = HostProgressTracker()
    tracker.observe(
        HostPcmSnapshot(True, 100),
        now_monotonic=1.0,
        now_epoch_sec=101.0,
    )
    tracker.observe(
        HostPcmSnapshot(True, 200),
        now_monotonic=1.5,
        now_epoch_sec=101.5,
    )

    health = _audio_health_snapshot(
        now_monotonic=1.6,
        started_monotonic=1.0,
        last_packet_monotonic=1.5,
        last_sink_progress_monotonic=1.5,
        host_snapshot=HostPcmSnapshot(True, 200),
        host_progress=tracker,
        sustained_drops=False,
    )

    assert health["audio_healthy"] is True
    assert health["host_streaming"] is True


def test_host_progress_observe_reports_only_current_interval_advance() -> None:
    tracker = HostProgressTracker()

    assert tracker.observe(
        HostPcmSnapshot(True, 100),
        now_monotonic=1.0,
        now_epoch_sec=101.0,
    ) is False
    assert tracker.observe(
        HostPcmSnapshot(True, 200),
        now_monotonic=1.5,
        now_epoch_sec=101.5,
    ) is True
    assert tracker.observe(
        HostPcmSnapshot(True, 200),
        now_monotonic=2.0,
        now_epoch_sec=102.0,
    ) is False


def test_never_started_host_clock_remains_ready_despite_idle_queue_drops() -> None:
    tracker = HostProgressTracker()
    tracker.observe(
        HostPcmSnapshot(True, 100),
        now_monotonic=1.0,
        now_epoch_sec=101.0,
    )
    tracker.observe(
        HostPcmSnapshot(True, 100),
        now_monotonic=3.5,
        now_epoch_sec=103.5,
    )

    health = _audio_health_snapshot(
        now_monotonic=3.5,
        started_monotonic=1.0,
        last_packet_monotonic=3.4,
        last_sink_progress_monotonic=1.0,
        host_snapshot=HostPcmSnapshot(True, 100),
        host_progress=tracker,
        sustained_drops=True,
    )

    assert health["audio_healthy"] is True
    assert health["host_streaming"] is False
    assert health["host_stalled"] is False
    assert health["sink_stalled"] is False


def test_previously_streaming_then_idle_host_returns_to_ready() -> None:
    tracker = HostProgressTracker()
    tracker.observe(
        HostPcmSnapshot(True, 100),
        now_monotonic=1.0,
        now_epoch_sec=101.0,
    )
    tracker.observe(
        HostPcmSnapshot(True, 200),
        now_monotonic=1.2,
        now_epoch_sec=101.2,
    )
    tracker.observe(
        HostPcmSnapshot(True, 200),
        now_monotonic=3.5,
        now_epoch_sec=103.5,
    )

    health = _audio_health_snapshot(
        now_monotonic=3.5,
        started_monotonic=1.0,
        last_packet_monotonic=3.4,
        last_sink_progress_monotonic=1.2,
        host_snapshot=HostPcmSnapshot(True, 200),
        host_progress=tracker,
        sustained_drops=True,
    )

    assert health["audio_healthy"] is True
    assert health["host_streaming"] is False
    assert health["host_stalled"] is True
    assert health["sink_stalled"] is True
    assert health["audio_health_detail"] == ""


def test_sustained_drops_while_host_clock_advances_are_unhealthy() -> None:
    tracker = HostProgressTracker()
    tracker.observe(
        HostPcmSnapshot(True, 100),
        now_monotonic=1.0,
        now_epoch_sec=101.0,
    )
    tracker.observe(
        HostPcmSnapshot(True, 200),
        now_monotonic=1.5,
        now_epoch_sec=101.5,
    )

    health = _audio_health_snapshot(
        now_monotonic=1.6,
        started_monotonic=1.0,
        last_packet_monotonic=1.5,
        last_sink_progress_monotonic=1.5,
        host_snapshot=HostPcmSnapshot(True, 200),
        host_progress=tracker,
        sustained_drops=True,
    )

    assert health["audio_healthy"] is False
    assert health["host_streaming"] is False
    assert "dropping continuously" in str(health["audio_health_detail"])


def test_host_pcm_status_parser_requires_running_and_reads_hw_ptr(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("state: RUNNING\nhw_ptr      : 4800\n")
    assert _read_host_pcm_status(status) == HostPcmSnapshot(True, 4800)


def test_status_uses_canonical_speaker_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("jasper.usb_mic.runtime_name", lambda: "Kitchen")
    status = _status_fixture(tmp_path)
    assert status["label"] == "Kitchen Mic"
    assert "Kitchen Mic" in status["detail"]


def test_status_degrades_when_relay_reports_stalled_audio(tmp_path: Path) -> None:
    status = _status_fixture(tmp_path, relay_overrides={
        "host_streaming": False,
        "audio_stalled": True,
        "source_stalled": True,
    })
    assert status["state"] == "degraded"
    assert "stopped before" in status["detail"]


def test_status_returns_ready_when_host_clock_is_idle(tmp_path: Path) -> None:
    status = _status_fixture(tmp_path, relay_overrides={
        "host_streaming": False,
        "audio_stalled": False,
        "host_stalled": True,
        "sink_stalled": True,
        "sustained_drops": True,
    })
    assert status["state"] == "ready"
    assert status["host_streaming"] is False
