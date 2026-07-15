# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jasper.cli import usb_mic as usb_mic_cli
from jasper.cli.usb_mic import (
    ALSA_BUFFER_US,
    HostPcmSnapshot,
    HostProgressTracker,
    PACKET_BYTES,
    PERIOD_BYTES,
    QUEUE_PERIODS,
    _audio_health_snapshot,
    _read_host_pcm_status,
)
from jasper.music_sources import Source
from jasper.source_intent import intent_env_key
from jasper.usb_mic import (
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
    monkeypatch.setattr(usb_mic_cli.subprocess, "Popen", popen)
    monkeypatch.setattr(usb_mic_cli.fcntl, "F_SETPIPE_SZ", 1031, raising=False)
    monkeypatch.setattr(usb_mic_cli.fcntl, "fcntl", lambda *_args: None)

    sink = usb_mic_cli.AplaySink()
    command = popen.call_args.args[0]
    assert command[command.index("-F") + 1] == "10000"
    assert command[command.index("-B") + 1] == "40000"
    sink.close()


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
            self.items: list[bytes] = []
            self.dropped = 0

        def put(self, payload: bytes) -> None:
            self.items.append(payload)

    class FakeSink:
        def __init__(self) -> None:
            self.queue = FakeQueue()

        def check(self) -> None:
            pass

        def progress(self) -> tuple[int, float, float]:
            return (0, 0.0, 0.0)

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

    assert fake_sink.queue.items == [b"\x01\x02" * (PERIOD_BYTES // 2)]


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


def test_status_returns_ready_when_mac_clock_is_idle(tmp_path: Path) -> None:
    status = _status_fixture(tmp_path, relay_overrides={
        "host_streaming": False,
        "audio_stalled": False,
        "host_stalled": True,
        "sink_stalled": True,
        "sustained_drops": True,
    })
    assert status["state"] == "ready"
    assert status["host_streaming"] is False
