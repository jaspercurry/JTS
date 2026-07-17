# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import struct

import pytest

from jasper.cli import usb_mic as usb_mic_cli
from jasper.cli.usb_mic import (
    ALSA_BUFFER_FRAMES,
    ALSA_PERIOD_BYTES,
    ALSA_PERIOD_FRAMES,
    ALSA_PERIODS,
    AlsaGadgetSink,
    DropRegimeCounters,
    HostPcmSnapshot,
    HostProgressTracker,
    PACKET_BYTES,
    PERIOD_BYTES,
    QueuedFrame,
    QUEUE_PERIODS,
    SequenceTracker,
    SourceAgeSnapshot,
    V2_PACKET_BYTES,
    WriterSnapshot,
    _audio_health_snapshot,
    _decode_audio_packet,
    _read_host_pcm_status,
    _split_writer_periods,
    _source_age_percentiles,
)
from jasper.music_sources import Source
from jasper.source_intent import intent_env_key
from jasper.usb_mic import (
    USB_MIC_LEG_KEY,
    USB_MIC_PRIMARY_LEG,
    USB_MIC_HEADER_BYTES,
    USB_MIC_HEADER_STRUCT,
    USB_MIC_PACKET_MAGIC,
    USB_MIC_PACKET_VERSION,
    build_usb_mic_status,
    read_intent,
    read_usb_mic_leg,
    usb_mic_leg_choices,
    usb_mic_enabled,
    write_usb_mic_enabled,
    write_usb_mic_leg,
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


def test_usb_mic_leg_defaults_and_preserves_enable_intent(tmp_path: Path) -> None:
    path = tmp_path / "usb_mic.env"
    assert read_usb_mic_leg(path) == USB_MIC_PRIMARY_LEG

    write_usb_mic_enabled(True, path)
    write_usb_mic_leg("chip_aec_210", path)
    assert usb_mic_enabled(path) is True
    assert read_usb_mic_leg(path) == "chip_aec_210"

    write_usb_mic_enabled(False, path)
    assert usb_mic_enabled(path) is False
    assert read_usb_mic_leg(path) == "chip_aec_210"
    assert USB_MIC_LEG_KEY in path.read_text()


def test_usb_mic_leg_writer_rejects_blank_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        write_usb_mic_leg("  ", tmp_path / "usb_mic.env")


def test_usb_mic_leg_choices_follow_active_chip_beam_plan() -> None:
    choices = usb_mic_leg_choices({
        "JASPER_XVF_CHIP_BEAM_PLAN": "xvf_square_fixed_150_210",
    })

    assert [choice["value"] for choice in choices] == [
        "primary",
        "chip_aec_150",
        "chip_aec_210",
    ]
    assert choices[0]["label"] == "Same as JTS voice"
    assert choices[1]["azimuth_deg"] == 150.0
    assert usb_mic_leg_choices({}) == [
        {
            "value": "primary",
            "label": "Same as JTS voice",
            "description": "Follows the microphone stream JTS uses for voice.",
        },
    ]


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


def test_status_reports_streaming_only_when_descriptor_and_relay_are_live(
    tmp_path: Path,
) -> None:
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
    status = _status_fixture(
        tmp_path,
        relay_overrides={
            "schema_version": 3,
            "source_age_basis": "bridge_emit_monotonic_v2",
            "source_age_scope": "bridge_emit_to_relay_dequeue",
            "source_age_sample_count": 100,
            "source_age_samples_appended": 1234,
            "source_age_window_generation": 4,
            "source_age_window_started_epoch_sec": 95.0,
            "source_age_ms_p50": 31.2,
            "source_age_ms_p95": 47.8,
            "source_age_ms_p99": 52.1,
            "packets_lost": 2,
            "sequence_resets": 1,
            "sequence_reorders": 2,
            "sequence_discontinuities": 3,
            "periods_dropped_streaming": 3,
            "periods_dropped_idle": 40,
            "drop_regime_basis": "status_interval_host_hw_ptr_advance",
            "writer_fill_ms": 15.75,
            "writer_target_ms": 20.0,
            "writer_pcm_rate_hz": 16_000,
            "writer_pcm_period_frames": 160,
            "writer_pcm_buffer_frames": 640,
            "writer_splices": 2,
            "writer_xruns": 0,
            "writer_resets": 1,
        },
    )

    assert status["relay_schema_version"] == 3
    assert status["source_age_basis"] == "bridge_emit_monotonic_v2"
    assert status["source_age_scope"] == "bridge_emit_to_relay_dequeue"
    assert status["source_age_sample_count"] == 100
    assert status["source_age_samples_appended"] == 1234
    assert status["source_age_window_generation"] == 4
    assert status["source_age_window_started_epoch_sec"] == 95.0
    assert status["source_age_ms_p50"] == 31.2
    assert status["source_age_ms_p95"] == 47.8
    assert status["source_age_ms_p99"] == 52.1
    assert status["packets_lost"] == 2
    assert status["sequence_resets"] == 1
    assert status["sequence_reorders"] == 2
    assert status["sequence_discontinuities"] == 3
    assert status["periods_dropped_streaming"] == 3
    assert status["periods_dropped_idle"] == 40
    assert status["drop_regime_basis"] == ("status_interval_host_hw_ptr_advance")
    assert status["writer_fill_ms"] == 15.75
    assert status["writer_target_ms"] == 20.0
    assert status["writer_pcm_rate_hz"] == 16_000
    assert status["writer_pcm_period_frames"] == 160
    assert status["writer_pcm_buffer_frames"] == 640
    assert status["writer_splices"] == 2
    assert status["writer_xruns"] == 0
    assert status["writer_resets"] == 1


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
    assert ALSA_PERIOD_FRAMES == 160
    assert ALSA_PERIOD_BYTES == PERIOD_BYTES // 2
    assert ALSA_PERIODS == 4
    assert ALSA_BUFFER_FRAMES == 640
    assert usb_mic_cli.WRITER_POLL_SECONDS < usb_mic_cli.ALSA_PERIOD_MS / 1000.0


class _FakePcm:
    def __init__(
        self,
        *,
        write_results: tuple[int, ...] = (),
        info_overrides: dict[str, int] | None = None,
        on_write=None,
    ) -> None:
        self.writes: list[bytes] = []
        self.write_results = deque(write_results)
        self.dropped = False
        self.closed = False
        self.state_value = 3
        self.on_write = on_write
        self.info_values = {
            "rate": 16_000,
            "channels": 1,
            "period_size": 160,
            "buffer_size": 640,
        }
        self.info_values.update(info_overrides or {})

    def info(self) -> dict[str, int]:
        return dict(self.info_values)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self.write_results:
            written = self.write_results.popleft()
        else:
            written = ALSA_PERIOD_FRAMES
        if written > 0 and self.on_write is not None:
            self.on_write(written)
        return written

    def state(self) -> int:
        return self.state_value

    def drop(self) -> None:
        self.dropped = True

    def close(self) -> None:
        self.closed = True


class _FakeAlsa:
    PCM_PLAYBACK = 0
    PCM_NONBLOCK = 1
    PCM_FORMAT_S16_LE = 2
    PCM_STATE_XRUN = 4
    ALSAAudioError = RuntimeError

    def __init__(self, pcms: list[_FakePcm] | None = None) -> None:
        self.pcms = pcms or [_FakePcm()]
        self.calls: list[dict] = []

    def PCM(self, **kwargs):
        self.calls.append(kwargs)
        if not self.pcms:
            self.pcms.append(_FakePcm())
        return self.pcms.pop(0)


def _make_sink(
    *,
    pcms: list[_FakePcm] | None = None,
    status_reader=lambda: HostPcmSnapshot(True, 0, 1920),
) -> tuple[AlsaGadgetSink, _FakeAlsa]:
    fake_alsa = _FakeAlsa(pcms)
    sink = AlsaGadgetSink(
        alsaaudio_module=fake_alsa,
        status_reader=status_reader,
        start_thread=False,
    )
    return sink, fake_alsa


def test_alsa_gadget_sink_opens_nonblocking_playback_pcm() -> None:
    pcm = _FakePcm()
    sink, fake_alsa = _make_sink(pcms=[pcm])

    assert fake_alsa.calls == [
        {
            "type": fake_alsa.PCM_PLAYBACK,
            "mode": fake_alsa.PCM_NONBLOCK,
            "device": usb_mic_cli.UAC2_DEVICE,
            "rate": 16_000,
            "channels": 1,
            "format": fake_alsa.PCM_FORMAT_S16_LE,
            "periodsize": 160,
            "periods": 4,
        }
    ]
    assert pcm.writes == [bytes(ALSA_PERIOD_BYTES)] * 4
    sink.close()


def test_alsa_gadget_sink_rejects_unexpected_realized_geometry() -> None:
    pcm = _FakePcm(info_overrides={"buffer_size": 1280})
    with pytest.raises(usb_mic_cli.RelayError, match="unexpected ALSA geometry"):
        _make_sink(pcms=[pcm])
    assert pcm.closed is True


def test_alsa_gadget_sink_reports_expected_open_failure_cleanly() -> None:
    class BusyAlsa(_FakeAlsa):
        def PCM(self, **kwargs):
            raise self.ALSAAudioError("device busy")

    with pytest.raises(usb_mic_cli.RelayError, match="ALSA PCM open failed"):
        AlsaGadgetSink(
            alsaaudio_module=BusyAlsa(),
            status_reader=lambda: HostPcmSnapshot(True, 0, 1920),
            start_thread=False,
        )


def test_source_frame_splits_into_two_exact_alsa_periods() -> None:
    frame = QueuedFrame(123, 7, b"\x01\x02" * (PERIOD_BYTES // 2))
    periods = _split_writer_periods(frame)

    assert [len(period.pcm) for period in periods] == [320, 320]
    assert periods[0].pcm + periods[1].pcm == frame.pcm
    assert periods[0].record_source_age is False
    assert periods[1].record_source_age is True


def test_relay_data_plane_no_longer_references_aplay_or_popen() -> None:
    source = Path(usb_mic_cli.__file__).read_text(encoding="utf-8")
    assert "subprocess.Popen" not in source
    assert '"aplay"' not in source


class _StatusReader:
    def __init__(self, snapshot: HostPcmSnapshot) -> None:
        self.snapshot = snapshot

    def __call__(self) -> HostPcmSnapshot:
        return self.snapshot


def _active_sink(
    *,
    pcm: _FakePcm | None = None,
    reader: _StatusReader | None = None,
    extra_pcms: list[_FakePcm] | None = None,
) -> tuple[AlsaGadgetSink, _FakePcm, _StatusReader, _FakeAlsa]:
    active_pcm = pcm or _FakePcm()
    status = reader or _StatusReader(HostPcmSnapshot(True, 200, 1160))
    sink, fake_alsa = _make_sink(
        pcms=[active_pcm, *(extra_pcms or [])],
        status_reader=status,
    )
    active_pcm.writes.clear()
    sink._settling = False
    sink._last_hw_ptr = 100
    sink._frozen_since_monotonic = 1.0
    return sink, active_pcm, status, fake_alsa


def test_writer_sanitizes_frozen_idle_once_with_only_silence() -> None:
    first = _FakePcm()
    sanitized = _FakePcm()
    spare = _FakePcm()
    reader = _StatusReader(HostPcmSnapshot(True, 100, 2020))
    sink, fake_alsa = _make_sink(
        pcms=[first, sanitized, spare],
        status_reader=reader,
    )
    sink._last_hw_ptr = 100
    sink._frozen_since_monotonic = 1.0
    sink.queue.put(QueuedFrame(1, 1, b"\x55\xaa" * (PERIOD_BYTES // 2)))

    sink._writer_step(now_monotonic=1.25)
    assert first.dropped is True
    assert first.closed is True
    assert sanitized.writes == [bytes(ALSA_PERIOD_BYTES)] * 4
    assert sink.writer_snapshot().idle_sanitizations == 1

    sink._writer_step(now_monotonic=1.50)
    assert len(fake_alsa.calls) == 2
    assert spare.writes == []
    sink.close()


def test_writer_resume_reset_primes_silence_then_only_freshest_audio() -> None:
    initial = _FakePcm()
    resumed = _FakePcm()
    reader = _StatusReader(HostPcmSnapshot(True, 200, 2120))
    sink, _fake_alsa = _make_sink(
        pcms=[initial, resumed],
        status_reader=reader,
    )
    stale = QueuedFrame(1, 1, b"\x11\x22" * (PERIOD_BYTES // 2))
    fresh = QueuedFrame(2, 2, b"\x33\x44" * (PERIOD_BYTES // 2))
    sink.queue.put(stale)
    sink.queue.put(fresh)
    sink._idle_sanitized = True
    sink._last_hw_ptr = 100
    sink._frozen_since_monotonic = 1.0

    sink._writer_step(now_monotonic=1.25)

    assert resumed.writes[:2] == [bytes(ALSA_PERIOD_BYTES)] * 2
    assert resumed.writes[2:] == [
        fresh.pcm[:ALSA_PERIOD_BYTES],
        fresh.pcm[ALSA_PERIOD_BYTES:],
    ]
    assert stale.pcm[:ALSA_PERIOD_BYTES] not in resumed.writes
    assert stale.pcm[ALSA_PERIOD_BYTES:] not in resumed.writes
    assert sink.writer_snapshot().resets == 1
    sink.close()


def test_writer_writes_each_source_frame_as_two_exact_periods(monkeypatch) -> None:
    sink, pcm, reader, _fake_alsa = _active_sink()
    audio = b"\x01\x02" * (PERIOD_BYTES // 2)
    sink.queue.put(QueuedFrame(1_000_000_000, 7, audio))
    monkeypatch.setattr(
        usb_mic_cli.time,
        "clock_gettime_ns",
        lambda _clock: 1_050_000_000,
    )

    sink._writer_step(now_monotonic=2.0)

    assert pcm.writes == [
        audio[:ALSA_PERIOD_BYTES],
        audio[ALSA_PERIOD_BYTES:],
    ]
    assert sink.source_ages_ms() == (50.0,)
    assert sink.source_age_snapshot().samples_appended == 1
    sink.close()


def test_writer_replaces_held_audio_only_when_newer_audio_reaches_high_watermark() -> (
    None
):
    reader = _StatusReader(HostPcmSnapshot(True, 200, 2120))
    sink, pcm, _reader, _fake_alsa = _active_sink(reader=reader)
    stale = QueuedFrame(1, 1, b"\x01\x02" * (PERIOD_BYTES // 2))
    fresh = QueuedFrame(2, 2, b"\x03\x04" * (PERIOD_BYTES // 2))
    sink._pending.extend(_split_writer_periods(stale))
    sink.queue.put(fresh)

    sink._writer_step(now_monotonic=2.0)

    assert pcm.writes == []
    assert [period.pcm for period in sink._pending] == [
        fresh.pcm[:ALSA_PERIOD_BYTES],
        fresh.pcm[ALSA_PERIOD_BYTES:],
    ]
    assert sink.writer_snapshot().splices == 1
    assert sink.writer_snapshot().discarded_periods == 2
    sink.close()


def test_writer_inserts_silence_only_when_low_and_source_stays_empty() -> None:
    reader = _StatusReader(HostPcmSnapshot(True, 200, 440))
    sink, pcm, _reader, _fake_alsa = _active_sink(reader=reader)

    sink._writer_step(now_monotonic=2.0)

    assert pcm.writes == [bytes(ALSA_PERIOD_BYTES)] * 2
    assert sink.writer_snapshot().splices == 1
    assert sink.writer_snapshot().silence_periods == 6
    sink.close()


def test_writer_drops_attempted_period_on_nonblocking_backpressure() -> None:
    pcm = _FakePcm(write_results=(160, 160, 160, 160, 0))
    sink, _pcm, _reader, _fake_alsa = _active_sink(pcm=pcm)
    sink.queue.put(QueuedFrame(1, 1, bytes(PERIOD_BYTES)))

    sink._writer_step(now_monotonic=2.0)

    assert len(sink._pending) == 1
    assert sink.writer_snapshot().splices == 1
    assert sink.writer_snapshot().discarded_periods == 1
    assert sink.writer_snapshot().xruns == 0
    sink.close()


@pytest.mark.parametrize("bad_result", (-32, 80))
def test_writer_recovers_negative_or_short_period_writes(bad_result: int) -> None:
    broken = _FakePcm(write_results=(160, 160, 160, 160, bad_result))
    recovered = _FakePcm()
    sink, _pcm, _reader, _fake_alsa = _active_sink(
        pcm=broken,
        extra_pcms=[recovered],
    )
    sink.queue.put(QueuedFrame(1, 1, bytes(PERIOD_BYTES)))

    sink._writer_step(now_monotonic=2.0)

    assert broken.closed is True
    assert recovered.writes == [bytes(ALSA_PERIOD_BYTES)] * 4
    assert sink.writer_snapshot().xruns == 1
    sink.close()


def test_writer_bounds_repeated_xrun_recovery() -> None:
    pcms = [_FakePcm() for _ in range(7)]
    sink, _fake_alsa = _make_sink(pcms=pcms)

    for index in range(5):
        sink._recover_xrun(detail="test", now_monotonic=1.0 + index)
    with pytest.raises(usb_mic_cli.RelayError, match="repeated ALSA writer xruns"):
        sink._recover_xrun(detail="test", now_monotonic=6.0)
    sink.close()


@pytest.mark.parametrize(
    "snapshot",
    (
        HostPcmSnapshot(True, 200, 199),
        HostPcmSnapshot(True, 200, 2200),
        HostPcmSnapshot(True, 200, None),
    ),
)
def test_writer_rejects_invalid_or_implausible_fill(
    snapshot: HostPcmSnapshot,
) -> None:
    assert AlsaGadgetSink._fill_from_snapshot(snapshot) is None


def test_writer_fails_after_bounded_invalid_fill_grace() -> None:
    reader = _StatusReader(HostPcmSnapshot(True, 200, None))
    sink, _pcm, _reader, _fake_alsa = _active_sink(reader=reader)

    sink._writer_step(now_monotonic=2.0)
    with pytest.raises(usb_mic_cli.RelayError, match="occupancy unavailable"):
        sink._writer_step(now_monotonic=4.1)
    sink.close()


def test_writer_sustains_clocked_ring_and_has_catch_up_authority() -> None:
    class ClockedRing:
        def __init__(self) -> None:
            self.hw_ptr = 0
            self.appl_ptr = 0

        def wrote(self, frames: int) -> None:
            self.appl_ptr += frames * 3

        def __call__(self) -> HostPcmSnapshot:
            return HostPcmSnapshot(True, self.hw_ptr, self.appl_ptr)

    ring = ClockedRing()
    pcm = _FakePcm(on_write=ring.wrote)
    sink, _fake_alsa = _make_sink(pcms=[pcm], status_reader=ring)
    pcm.writes.clear()
    sink._settling = False
    ring.hw_ptr = 720
    sink._last_hw_ptr = 480
    sink._frozen_since_monotonic = 1.0

    for tick in range(1, 401):
        if tick % 4 == 1:
            value = tick % 251 + 1
            sink.queue.put(QueuedFrame(tick, tick, bytes([value, 0]) * 320))
        ring.hw_ptr += 240
        assert ring.hw_ptr <= ring.appl_ptr
        sink._writer_step(now_monotonic=2.0 + tick * 0.005)
        fill_ms = (ring.appl_ptr - ring.hw_ptr) / 48.0
        assert 20.0 <= fill_ms <= 40.0

    snapshot = sink.writer_snapshot()
    assert snapshot.xruns == 0
    assert snapshot.splices == 0
    assert snapshot.fill_ms == (ring.appl_ptr - ring.hw_ptr) / 48.0
    sink.close()


def test_writer_writes_two_periods_to_recover_from_missed_deadline() -> None:
    class ClockedRing:
        def __init__(self) -> None:
            self.hw_ptr = 0
            self.appl_ptr = 0

        def wrote(self, frames: int) -> None:
            self.appl_ptr += frames * 3

        def __call__(self) -> HostPcmSnapshot:
            return HostPcmSnapshot(True, self.hw_ptr, self.appl_ptr)

    ring = ClockedRing()
    pcm = _FakePcm(on_write=ring.wrote)
    sink, _fake_alsa = _make_sink(pcms=[pcm], status_reader=ring)
    pcm.writes.clear()
    sink._settling = False
    sink._last_hw_ptr = 960
    sink._frozen_since_monotonic = 1.0
    ring.hw_ptr = 1920
    audio = b"\x12\x34" * 320
    sink.queue.put(QueuedFrame(1, 1, audio))

    sink._writer_step(now_monotonic=2.02)

    assert pcm.writes == [
        audio[:ALSA_PERIOD_BYTES],
        audio[ALSA_PERIOD_BYTES:],
    ]
    assert sink.writer_snapshot().fill_ms == 20.0
    sink.close()


def test_writer_never_projects_a_write_above_high_watermark() -> None:
    reader = _StatusReader(HostPcmSnapshot(True, 200, 1880))
    sink, pcm, _reader, _fake_alsa = _active_sink(reader=reader)
    sink.queue.put(QueuedFrame(1, 1, bytes(PERIOD_BYTES)))

    sink._writer_step(now_monotonic=2.0)

    assert pcm.writes == []
    assert sink.writer_snapshot().fill_ms == 35.0
    assert len(sink._pending) == 2
    sink.close()


def test_status_writer_owns_schema_and_timestamp(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    monkeypatch.setattr(usb_mic_cli.time, "time", lambda: 123.5)

    usb_mic_cli._write_status(
        str(path),
        {"schema_version": 2, "updated_epoch_sec": 1.0, "state": "running"},
    )

    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 4
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

        def source_age_snapshot(self) -> SourceAgeSnapshot:
            return SourceAgeSnapshot((), 0, 0.0)

        def writer_snapshot(self) -> WriterSnapshot:
            return WriterSnapshot(None, 0, 0, 0, 0, 0, 0, 20.0, 10.0, 40.0)

        def reset_source_age_window(self) -> None:
            pass

        def close(self) -> None:
            pass

    fake_socket = FakeSocket()
    fake_sink = FakeSink()
    monkeypatch.setattr(usb_mic_cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(usb_mic_cli.socket, "socket", lambda *_args: fake_socket)
    monkeypatch.setattr(usb_mic_cli, "AlsaGadgetSink", lambda: fake_sink)
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
) -> tuple[AlsaGadgetSink, _FakePcm]:
    pcm = _FakePcm()
    reader = _StatusReader(HostPcmSnapshot(True, 200, 1160))
    sink, _fake_alsa = _make_sink(
        pcms=[pcm],
        status_reader=reader,
    )
    pcm.writes.clear()
    sink._settling = False
    sink._last_hw_ptr = 100
    sink._frozen_since_monotonic = 1.0
    sink.queue.put(frame)
    monkeypatch.setattr(
        usb_mic_cli.time,
        "clock_gettime_ns",
        lambda _clock: now_ns,
    )
    sink._writer_step(now_monotonic=2.0)
    return sink, pcm


def test_relay_parses_v2_header_and_measures_age(monkeypatch) -> None:
    pcm = b"\x11\x22" * (PERIOD_BYTES // 2)
    bridge_emit_ns = 1_000_000_000
    packet = (
        struct.pack(
            USB_MIC_HEADER_STRUCT,
            USB_MIC_PACKET_MAGIC,
            USB_MIC_PACKET_VERSION,
            0,
            7,
            bridge_emit_ns,
        )
        + pcm
    )

    frames = _decode_audio_packet(packet, received_monotonic_ns=9_000_000_000)
    assert frames == (QueuedFrame(bridge_emit_ns, 7, pcm),)
    assert len(packet) == V2_PACKET_BYTES

    sink, fake_pcm = _write_one_frame(
        monkeypatch,
        frames[0],
        now_ns=bridge_emit_ns + 50_000_000,
    )

    assert fake_pcm.writes == [
        pcm[:ALSA_PERIOD_BYTES],
        pcm[ALSA_PERIOD_BYTES:],
    ]
    assert _source_age_percentiles(sink.source_ages_ms()) == {
        "source_age_ms_p50": 50.0,
        "source_age_ms_p95": 50.0,
        "source_age_ms_p99": 50.0,
    }


def test_relay_accepts_v1_raw_packets_without_false_emit_age(monkeypatch) -> None:
    pcm = b"\x01\x02" * (PERIOD_BYTES // 2)
    frames = _decode_audio_packet(pcm, received_monotonic_ns=123_000)

    assert frames == (QueuedFrame(123_000, None, pcm),)
    sink, fake_pcm = _write_one_frame(monkeypatch, frames[0], now_ns=999_000)
    assert fake_pcm.writes == [
        pcm[:ALSA_PERIOD_BYTES],
        pcm[ALSA_PERIOD_BYTES:],
    ]
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
    assert all(frame.t_bridge_emit_ns == 123_000 for frame in frames)
    assert all(frame.seq is None for frame in frames)


@pytest.mark.parametrize(
    ("magic", "version", "flags", "bridge_emit_ns", "received_ns"),
    (
        (b"NO", USB_MIC_PACKET_VERSION, 0, 1_000, 2_000),
        (USB_MIC_PACKET_MAGIC, 99, 0, 1_000, 2_000),
        (USB_MIC_PACKET_MAGIC, USB_MIC_PACKET_VERSION, 1, 1_000, 2_000),
        (USB_MIC_PACKET_MAGIC, USB_MIC_PACKET_VERSION, 0, 0, 2_000),
        (USB_MIC_PACKET_MAGIC, USB_MIC_PACKET_VERSION, 0, 2_001, 2_000),
    ),
)
def test_relay_rejects_malformed_v2_headers(
    magic: bytes,
    version: int,
    flags: int,
    bridge_emit_ns: int,
    received_ns: int,
) -> None:
    packet = struct.pack(
        USB_MIC_HEADER_STRUCT,
        magic,
        version,
        flags,
        1,
        bridge_emit_ns,
    ) + bytes(PERIOD_BYTES)

    assert len(packet) == USB_MIC_HEADER_BYTES + PERIOD_BYTES
    assert _decode_audio_packet(packet, received_monotonic_ns=received_ns) is None


def test_sequence_tracker_counts_only_plausible_forward_loss() -> None:
    tracker = SequenceTracker()
    assert tracker.observe(0) == 0
    assert tracker.observe(1) == 0
    assert tracker.observe(3) == 1
    assert tracker.resets == 0
    assert tracker.reorders == 0
    assert tracker.discontinuities == 0


def test_sequence_tracker_handles_wrap_reset_reorder_and_discontinuity() -> None:
    wrap = SequenceTracker(last_seq=0xFFFFFFFF)
    assert wrap.observe(0) == 0
    assert wrap.resets == 0

    reset = SequenceTracker(last_seq=0x80000001)
    assert reset.observe(0) == 0
    assert reset.observe(1) == 0
    assert reset.resets == 1

    reorder = SequenceTracker(last_seq=100)
    assert reorder.observe(99) == 0
    assert reorder.last_seq == 100
    assert reorder.observe(101) == 0
    assert reorder.reorders == 1

    discontinuity = SequenceTracker(last_seq=0)
    assert discontinuity.observe(4098) == 0
    assert discontinuity.discontinuities == 1


def test_source_age_window_reset_clears_prior_session(monkeypatch) -> None:
    sink, _fake_alsa = _make_sink()
    sink._source_ages_ms = deque((10.0, 20.0), maxlen=512)
    sink._source_age_generation = 3
    sink._source_age_started_epoch_sec = 100.0
    monkeypatch.setattr(usb_mic_cli.time, "time", lambda: 200.0)

    sink.reset_source_age_window()

    assert sink.source_age_snapshot() == SourceAgeSnapshot((), 4, 200.0)
    sink.close()


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
        return (
            struct.pack(
                USB_MIC_HEADER_STRUCT,
                USB_MIC_PACKET_MAGIC,
                USB_MIC_PACKET_VERSION,
                0,
                seq,
                1_000_000_000 + seq,
            )
            + pcm
        )

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
            self.age_window_resets = 0

        def check(self) -> None:
            pass

        def progress(self) -> tuple[int, float, float]:
            return 0, 1.4, 100.0

        def source_age_snapshot(self) -> SourceAgeSnapshot:
            return SourceAgeSnapshot(
                (10.0, 20.0, 30.0),
                self.age_window_resets,
                900.0,
            )

        def writer_snapshot(self) -> WriterSnapshot:
            return WriterSnapshot(20.0, 2, 0, 1, 1, 4, 0, 20.0, 10.0, 40.0)

        def reset_source_age_window(self) -> None:
            self.age_window_resets += 1

        def close(self) -> None:
            pass

    statuses: list[dict] = []

    def capture_status(_path: str, payload: dict) -> None:
        if payload["state"] != "running":
            return
        statuses.append(payload)
        if payload["packets_received"] == 3:
            raise StopRelay

    monotonic_values = iter(
        (
            1_000.0,
            1_000.1,
            1_000.2,
            1_000.7,
            1_000.8,
            1_001.3,
            1_001.4,
        )
    )
    host_snapshots = iter(
        (
            HostPcmSnapshot(True, 100),
            HostPcmSnapshot(True, 100),
            HostPcmSnapshot(True, 200),
        )
    )
    fake_sink = FakeSink()
    monkeypatch.setattr(usb_mic_cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(usb_mic_cli.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(usb_mic_cli, "AlsaGadgetSink", lambda: fake_sink)
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
    assert status["sequence_resets"] == 0
    assert status["sequence_reorders"] == 0
    assert status["sequence_discontinuities"] == 0
    assert status["source_age_scope"] == "bridge_emit_to_alsa_write"
    assert status["source_age_window_generation"] == 2
    assert status["source_age_window_started_epoch_sec"] == 900.0
    assert status["source_age_samples_appended"] == 0
    assert status["source_age_ms_p50"] == 20.0
    assert status["source_age_ms_p95"] == 30.0
    assert status["source_age_ms_p99"] == 30.0
    assert status["periods_dropped_idle"] == 2
    assert status["periods_dropped_streaming"] == 1
    assert status["periods_dropped"] == 3
    assert status["drop_regime_basis"] == ("status_interval_host_hw_ptr_advance")
    assert status["writer_fill_ms"] == 20.0
    assert status["writer_pcm_rate_hz"] == 16_000
    assert status["writer_pcm_period_frames"] == 160
    assert status["writer_pcm_buffer_frames"] == 640
    assert status["gadget_hardware_rate_hz"] == 48_000
    assert status["writer_splices"] == 2
    assert status["writer_xruns"] == 0
    assert status["writer_resets"] == 1
    assert status["relay_pid"] > 0
    assert status["relay_started_epoch_sec"] > 0
    assert fake_sink.age_window_resets == 2


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

    assert (
        tracker.observe(
            HostPcmSnapshot(True, 100),
            now_monotonic=1.0,
            now_epoch_sec=101.0,
        )
        is False
    )
    assert (
        tracker.observe(
            HostPcmSnapshot(True, 200),
            now_monotonic=1.5,
            now_epoch_sec=101.5,
        )
        is True
    )
    assert (
        tracker.observe(
            HostPcmSnapshot(True, 200),
            now_monotonic=2.0,
            now_epoch_sec=102.0,
        )
        is False
    )


def test_host_progress_does_not_treat_pcm_pointer_reset_as_host_advance() -> None:
    tracker = HostProgressTracker()
    assert (
        tracker.observe(
            HostPcmSnapshot(True, 10_000, 11_000),
            now_monotonic=1.0,
            now_epoch_sec=101.0,
        )
        is False
    )
    assert (
        tracker.observe(
            HostPcmSnapshot(True, 0, 1920),
            now_monotonic=1.5,
            now_epoch_sec=101.5,
        )
        is False
    )
    assert (
        tracker.observe(
            HostPcmSnapshot(True, 480, 1920),
            now_monotonic=2.0,
            now_epoch_sec=102.0,
        )
        is True
    )


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


def test_host_pcm_status_parser_reads_hw_and_application_pointers(
    tmp_path: Path,
) -> None:
    status = tmp_path / "status"
    status.write_text("state: RUNNING\nhw_ptr      : 4800\nappl_ptr    : 6240\n")
    assert _read_host_pcm_status(status) == HostPcmSnapshot(True, 4800, 6240)


def test_status_uses_canonical_speaker_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("jasper.usb_mic.runtime_name", lambda: "Kitchen")
    status = _status_fixture(tmp_path)
    assert status["label"] == "Kitchen Mic"
    assert "Kitchen Mic" in status["detail"]


def test_status_degrades_when_relay_reports_stalled_audio(tmp_path: Path) -> None:
    status = _status_fixture(
        tmp_path,
        relay_overrides={
            "host_streaming": False,
            "audio_stalled": True,
            "source_stalled": True,
        },
    )
    assert status["state"] == "degraded"
    assert "stopped before" in status["detail"]


def test_status_returns_ready_when_host_clock_is_idle(tmp_path: Path) -> None:
    status = _status_fixture(
        tmp_path,
        relay_overrides={
            "host_streaming": False,
            "audio_stalled": False,
            "host_stalled": True,
            "sink_stalled": True,
            "sustained_drops": True,
        },
    )
    assert status["state"] == "ready"
    assert status["host_streaming"] is False
