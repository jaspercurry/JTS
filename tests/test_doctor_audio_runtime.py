# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor audio-runtime domain."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from jasper import audio_runtime_plan, audio_validation
from jasper.cli import doctor
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
)
import os
import re
import struct
import subprocess
from types import SimpleNamespace
from jasper.audio_hardware.dac import (
    HIFIBERRY_DAC8X_STUDIO_ID,
    LatencyFloor,
    latency_floor_for,
)
from jasper.cli.doctor import audio_runtime
from jasper.fanin_coupling import (
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_PLAYBACK_DEVICE,
)
from jasper.output_topology import OutputTopologyError

from .active_speaker_fixtures import (
    PASSIVE_ONLY_DAC_ID,
    PASSIVE_ONLY_DAC_LABEL,
    register_passive_only_dac,
)


# ---- shairport-sync.conf output_device check ---------------------------


def _patch_asound_conf(
    monkeypatch,
    conf_text: str,
    tmp_path: Path,
    *,
    stale_topology_env: bool = False,
):
    target = tmp_path / "asound.conf"
    target.write_text(conf_text)
    stale = tmp_path / "audio_topology.env"
    if stale_topology_env:
        stale.write_text("JASPER_AUDIO_TOPOLOGY=dmix\n")
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/asound.conf":
            return target
        if arg == "/var/lib/jasper/audio_topology.env":
            return stale
        return real_path_cls(arg)

    monkeypatch.setattr(doctor.audio_runtime, "Path", fake_path)


_FANIN_ASOUND = """
pcm.librespot_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,0"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.shairport_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,1"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.bluealsa_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,2"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.correction_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,4"
        rate 48000
        channels 2
        format S16_LE
    }
}
"""


def test_fanin_asound_wiring_ok(monkeypatch, tmp_path):
    _patch_asound_conf(monkeypatch, _FANIN_ASOUND, tmp_path)
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "ok"
    assert "renderer/test lanes" in r.detail


class _FakeSocket:
    def __init__(
        self,
        payload: bytes = b"",
        error: OSError | None = None,
        *,
        chunks: list[bytes] | None = None,
        recv_error: OSError | None = None,
    ):
        self._chunks = list(chunks) if chunks is not None else [payload, b""]
        self._error = error
        self._recv_error = recv_error
        self.timeout = None
        self.connected_path = None
        self.sent: list[bytes] = []
        self.recv_sizes: list[int] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, path):
        self.connected_path = path
        if self._error is not None:
            raise self._error

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        self.recv_sizes.append(size)
        if self._recv_error is not None:
            raise self._recv_error
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


def _patch_camilla_systemctl(monkeypatch, *, enabled="enabled", active="active"):
    def fake_run(cmd, *args, **kwargs):
        stdout = ""
        if cmd[:2] == ["systemctl", "is-enabled"]:
            stdout = enabled + "\n"
        elif cmd[:2] == ["systemctl", "is-active"]:
            stdout = active + "\n"
        return type("P", (), {"stdout": stdout, "stderr": "", "returncode": 0})()

    monkeypatch.setattr(doctor.audio_runtime, "_run", fake_run)


def test_check_camilla_service_ok_when_enabled_and_active(monkeypatch):
    _patch_camilla_systemctl(monkeypatch)

    result = doctor.check_camilla_service()

    assert result.status == "ok"
    assert result.detail == "enabled and active"


def test_check_camilla_service_fails_on_a_clean_stop(monkeypatch):
    """The #2163 state: enabled, cleanly inactive, never `failed`.

    `check_service_runtime_state` returns ok for this, and
    `check_camilla_websocket` reports it as an unreachable websocket.
    """
    _patch_camilla_systemctl(monkeypatch, active="inactive")

    result = doctor.check_camilla_service()

    assert result.status == "fail"
    assert "enabled but state=inactive" in result.detail
    assert "jasper-camilla-recover" in result.detail


def test_check_camilla_service_fails_when_disabled(monkeypatch):
    _patch_camilla_systemctl(monkeypatch, enabled="disabled", active="inactive")

    result = doctor.check_camilla_service()

    assert result.status == "fail"
    assert "state=disabled" in result.detail
    assert "systemctl enable --now jasper-camilla.service" in result.detail


def test_check_camilla_service_fails_when_unit_is_not_installed(monkeypatch):
    _patch_camilla_systemctl(monkeypatch, enabled="not-found", active="inactive")

    result = doctor.check_camilla_service()

    assert result.status == "fail"
    assert "not installed" in result.detail


def _patch_fanin_systemctl(monkeypatch, *, enabled="enabled", active="active"):
    def fake_run(cmd, *args, **kwargs):
        stdout = ""
        if cmd[:2] == ["systemctl", "is-enabled"]:
            stdout = enabled + "\n"
        elif cmd[:2] == ["systemctl", "is-active"]:
            stdout = active + "\n"
        return type("P", (), {"stdout": stdout, "stderr": "", "returncode": 0})()

    monkeypatch.setattr(doctor.audio_runtime, "_run", fake_run)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "loopback",
    )


# The healthy Ring A block a running fan-in always publishes (ADR-0100 — the
# ring is the only transport, so there is no ring-less live STATUS).
_FANIN_RING_BLOCK = {
    "path": "/dev/shm/jts-ring/program.ring",
    "slots": 2,
    "wire_format": "S32_LE",
    "channels": 2,
    "occupancy": 1,
    "published": 4242,
    "full_waits": 0,
    "stuck_reader_drops": 0,
    "drop_no_reader": 0,
    "stall_active": False,
    "last_stall_ms": 0,
}


def _fanin_status_payload(
    *,
    input_buffer_frames: int = 4096,
    output_buffer_frames: int = 1024,
    progress_age_ms: int = 2,
    transport: str = "shm_ring",
    ring: dict | None = _FANIN_RING_BLOCK,
) -> bytes:
    """A fan-in STATUS payload. Defaults to the ONLY shape a live daemon can
    report: transport=shm_ring with a ring block. Pass ``ring=None`` to build
    the malformed no-ring-block shape."""
    output = {
        "pcm": doctor._FANIN_EXPECTED_OUTPUT_PCM,
        "transport": transport,
        "buffer_frames": output_buffer_frames,
        "frames_written": 1234,
        "xrun_count": 0,
    }
    if ring is not None:
        output["ring"] = dict(ring)
    return json.dumps(
        {
            "input_buffer_frames": input_buffer_frames,
            "output": output,
            "inputs": [
                {"label": label, "pcm": pcm, "xrun_count": 0}
                for label, pcm in doctor._FANIN_EXPECTED_ALOOP_INPUTS
            ],
            "tts": {
                "enabled": True,
                "pending_frames": 0,
                "max_pending_frames": 96000,
                "budget_frames": 96000,
                "dropped_commands": 0,
                "dropped_audio_frames": 0,
                "flush_requests": 0,
                "flushed_frames": 0,
                "assistant_loudness": {
                    "content_short_lufs": -31.2,
                    "content_anchor_lufs": -30.8,
                    "decision_seen": False,
                    "calibrated": False,
                    "profile_confidence": 0.0,
                    "baseline_lufs": None,
                    "target_lufs": None,
                    "source_lufs": None,
                    "source_peak_dbfs": None,
                    "requested_gain_db": None,
                    "peak_cap_gain_db": None,
                    "final_gain_db": None,
                },
            },
            "watchdog": {"last_progress_age_ms": progress_age_ms},
        }
    ).encode()


def _host_clock_status(
    *,
    ladder="l0_locked",
    reason=None,
    ready=True,
    capture_generation=4,
    control_generation=4,
    phase=None,
    attempt=1,
    retries=0,
):
    return {
        "host_clock": {
            "enabled": True,
            "ladder": ladder,
            "fallback_reason": reason,
            "actuator": {
                "ready": ready,
                "capture_generation": capture_generation,
                "control_generation": control_generation,
                "refreshes": 4,
                "open_failures": 0,
                "write_failures": 1,
            },
            "probe": {
                "phase": phase,
                "attempt": attempt,
                "max_attempts": 2,
                "final_result": "pass" if ladder == "l0_locked" else "none",
                "retries": retries,
            },
        }
    }


def test_host_clock_doctor_ok_for_l0_and_bounded_retry():
    l0 = doctor.audio_runtime._host_clock_health_from_status(_host_clock_status())
    assert l0.status == "ok"
    assert "ladder=l0_locked" in l0.detail

    retry = doctor.audio_runtime._host_clock_health_from_status(
        _host_clock_status(ladder="probing", phase="retry_wait", attempt=2, retries=1)
    )
    assert retry.status == "ok"
    assert "phase=retry_wait" in retry.detail
    assert "attempt=2/2" in retry.detail


@pytest.mark.parametrize(
    "reason",
    ["probe_noncompliant", "lost_authority", "actuator_unavailable"],
)
def test_host_clock_doctor_warns_with_exact_l2_reason(reason):
    result = doctor.audio_runtime._host_clock_health_from_status(
        _host_clock_status(ladder="l2_fallback", reason=reason)
    )
    assert result.status == "warn"
    assert f"fallback_reason={reason}" in result.detail


def test_host_clock_doctor_warns_on_unavailable_or_generation_mismatch():
    unavailable = doctor.audio_runtime._host_clock_health_from_status(
        _host_clock_status(
            ladder="l2_fallback",
            reason="actuator_unavailable",
            ready=False,
            control_generation=None,
        )
    )
    assert unavailable.status == "warn"
    assert "actuator unavailable/mismatched" in unavailable.detail
    assert "capture_generation=4" in unavailable.detail
    assert "control_generation=None" in unavailable.detail

    mismatch = doctor.audio_runtime._host_clock_health_from_status(
        _host_clock_status(control_generation=3)
    )
    assert mismatch.status == "warn"
    assert "capture_generation=4" in mismatch.detail
    assert "control_generation=3" in mismatch.detail


def _outputd_status_payload(
    *,
    backend: str = "alsa",
    sink_mode: str = "single_alsa",
    content_pcm: str = doctor._OUTPUTD_EXPECTED_CONTENT_PCM,
    dac_pcm: str = doctor._OUTPUTD_EXPECTED_DAC_PCM,
    content_buffer_frames: int = 4096,
    dac_buffer_frames: int = 3072,
    period_frames: int = 1024,
    progress_age_ms: int = 2,
    dual_apple_status: dict | None = None,
    content_source: str = "alsa",
    shm_ring_slots: int = 2,
    shm_ring_slot_frames: int | None = None,
    shm_ring_capacity_frames: int | None = None,
    shm_ring_occupancy: int = 0,
) -> bytes:
    content = {
        "source": content_source,
        "pcm": content_pcm,
        "period_frames": period_frames,
        "buffer_frames": content_buffer_frames,
        "frames_read": 1234,
        "empty_periods": 2,
        "partial_periods": 1,
        "eagain_count": 1,
        "xrun_count": 0,
    }
    if content_source == "shm_ring":
        # Ring B: outputd reports the honest capacity contract in content.ring
        # next to the synthetic content.buffer_frames (period-sized). Full runtime
        # health rides the top-level shm_ring block. Mirror both here.
        _slot_frames = (
            shm_ring_slot_frames if shm_ring_slot_frames is not None else period_frames
        )
        _capacity = (
            shm_ring_capacity_frames
            if shm_ring_capacity_frames is not None
            else shm_ring_slots * _slot_frames
        )
        content["ring"] = {
            "slots": shm_ring_slots,
            "slot_frames": _slot_frames,
            "capacity_frames": _capacity,
        }
    payload = {
        "backend": backend,
        "sink_mode": sink_mode,
        "content": content,
        "dac": {
            "pcm": dac_pcm,
            "sample_rate": 48000,
            "period_frames": period_frames,
            "buffer_frames": dac_buffer_frames,
            "frames_written": 2048,
            "xrun_count": 0,
        },
        "mix": {"reference_sequence": 1, "clipped_samples": 0},
        "reference_outputs": {
            "speaker_reference_source": "outputd_final_electrical",
            "speaker_reference_is_fallback": False,
            "speaker_reference_active": False,
            "speaker_reference_sample_rate": 48000,
            "speaker_reference_channels": 2,
            "chip_ref_pcm": None,
            "chip_ref_sample_rate": 16000,
            "chip_ref_period_frames": 320,
            "chip_ref_buffer_frames": 1280,
            "udp_target": None,
        },
        "content_bridge": {
            "mode": "direct",
            "enabled": False,
            "locked": False,
            "ring_frames": 16384,
            "target_fill_frames": 4096,
            "fill_frames": 0,
            "min_fill_frames": 0,
            "max_fill_frames": 0,
            "ratio_ppm": 0.0,
            "input_frames": 0,
            "output_frames": 0,
            "silence_frames": 0,
            "underrun_frames": 0,
            "overrun_frames": 0,
            "resync_count": 0,
            "reset_count": 0,
            "ratio_clamp_count": 0,
            "lock_count": 0,
            "unlock_count": 0,
        },
        "tts": {
            "pending_frames": 0,
            "budget_frames": 96000,
            "max_pending_frames": 4096,
            "over_budget": False,
            "over_budget_periods": 0,
            "over_budget_ms": 0,
            "over_budget_streak_ms": 0,
            "dropped_commands": 0,
            "dropped_audio_frames": 0,
        },
        "assistant_loudness": {
            "content_short_lufs": -31.2,
            "content_anchor_lufs": -30.8,
            "decision_seen": False,
            "calibrated": False,
            "profile_confidence": 0.0,
            "baseline_lufs": None,
            "target_lufs": None,
            "source_lufs": None,
            "source_peak_dbfs": None,
            "requested_gain_db": None,
            "peak_cap_gain_db": None,
            "final_gain_db": None,
        },
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    }
    if sink_mode == "dual_apple":
        payload["dual_apple"] = dual_apple_status or {
            "dac_a_pcm": "hw:CARD=A,DEV=0",
            "dac_b_pcm": "hw:CARD=A_1,DEV=0",
            "linked": True,
            "delay_delta_frames": 0,
            "delay_delta_baseline_frames": 0,
            "delay_delta_error_frames": 0,
            "max_delay_delta_frames": 2,
        }
    if content_source == "shm_ring":
        content_ring = content["ring"]
        payload["shm_ring"] = {
            "enabled": True,
            "path": "/dev/shm/jts-ring/content.ring",
            "attached": True,
            "slots": content_ring["slots"],
            "slot_frames": content_ring["slot_frames"],
            "capacity_frames": content_ring["capacity_frames"],
            "occupancy": shm_ring_occupancy,
            "frames_read": 2048,
            "startup_empty_reads": 4,
            "empty_reads": 3,
            "writer_alive": True,
            "writer_pid": 4242,
            "writer_heartbeat_age_ms": 12,
        }
    return json.dumps(payload).encode()


def _patch_ring_coupled_box(
    monkeypatch,
    tmp_path,
    *,
    active_endpoint: bool = False,
    active_channels: int | None = None,
):
    """Put the box on the shm_ring coupling, the way a converged box actually is.

    #2285 P2: the ACTIVE snd-aloop endpoint is retired, so the roleful shapes
    below (composite, active single-ALSA) no longer have a direct-bridge form to
    model. The reconciler writes an explicit-EMPTY ``JASPER_OUTPUTD_CONTENT_PCM``
    for both, and ``Config::from_env`` refuses a composite sink on the DIRECT
    bridge outright (EX_CONFIG), so a composite box that is running at all is a
    ring box. ``active_endpoint=True`` adds the reconciler's endpoint marker,
    which is what selects the ACTIVE-ring transport shape rather than the
    full-range stereo Ring B — the marker, never the observed device.

    Call this LAST, after ``_patch_fanin_systemctl`` and
    ``_patch_fanin_status_socket``: the first pins the persisted coupling to
    ``loopback`` and the second points the endpoint evidence at the stereo ring,
    so an earlier call is silently undone.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
    )

    env_lines = ["JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring"]
    if active_channels is not None:
        env_lines.append(f"JASPER_OUTPUTD_ACTIVE_CHANNELS={active_channels}")
    if active_endpoint:
        # An armed box's three facts travel together: the marker, and the ring
        # PATH it must read. outputd bails at startup on the crossed pair, so a
        # fixture that armed the marker over Ring B's path would model a box
        # that cannot boot.
        env_lines.append("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1")
        env_lines.append(
            f"JASPER_OUTPUTD_SHM_RING_PATH={DEFAULT_OUTPUTD_ACTIVE_RING_PATH}"
        )
    env_path = tmp_path / "outputd.env"
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "shm_ring",
    )
    if active_endpoint:
        # ...and the third: CamillaDSP plays the ACTIVE ring, not Ring B.
        monkeypatch.setattr(
            audio_runtime_plan,
            "output_endpoint_evidence_from_statefiles",
            lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
                devices={
                    "playback_device": RING_ACTIVE_PLAYBACK_DEVICE,
                    "capture_device": RING_CAPTURE_DEVICE,
                }
            ),
        )


def _ring_coupled_status_payload(**kwargs) -> bytes:
    """A STATUS payload for a ring-coupled box: no content PCM, honest synthetic.

    ``content_pcm=""`` is what outputd publishes once the reconciler has written
    explicit-empty — ``env_str`` defaults only when a variable is UNSET
    (``rust/jasper-env/src/lib.rs``), so the empty value is preserved. The
    period-sized content buffer is the real synthetic a ring box publishes,
    because outputd never opens a content ALSA PCM under this bridge.
    """
    kwargs.setdefault("content_pcm", "")
    kwargs.setdefault("content_buffer_frames", 1024)
    kwargs.setdefault("period_frames", 1024)
    return _outputd_status_payload(content_source="shm_ring", **kwargs)


def _patch_fanin_status_socket(monkeypatch, payload: bytes):
    monkeypatch.setattr(
        doctor.socket,
        "socket",
        lambda *a, **kw: _FakeSocket(payload=payload),
    )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(decoded, dict):
        return
    content = decoded.get("content")
    if not isinstance(content, dict):
        return
    content_pcm = str(content.get("pcm") or "")
    if content.get("source") == "shm_ring":
        playback_device = "jts_ring_playback"
        capture_device = "jts_ring_capture"
    else:
        playback_device = (
            "outputd_active_content_playback"
            if content_pcm == doctor._OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM
            else "outputd_content_playback"
        )
        capture_device = "plug:jasper_capture"
    monkeypatch.setattr(
        audio_runtime_plan,
        "output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
            devices={
                "playback_device": playback_device,
                "capture_device": capture_device,
            }
        ),
    )


def test_status_socket_byte_reader_owns_fragmented_protocol_and_cleanup(monkeypatch):
    fake = _FakeSocket(chunks=[b'{"ok":', b"true}", b""])
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: fake)

    payload = doctor.audio_runtime._read_status_socket_bytes("/run/test.sock", timeout=1.25)

    assert payload == b'{"ok":true}'
    assert 0 < fake.timeout <= 1.25
    assert fake.connected_path == "/run/test.sock"
    assert fake.sent == [b"STATUS\n"]
    assert fake.recv_sizes == [65536, 65536, 65536]
    assert fake.closed is True


def test_status_socket_byte_reader_accepts_exact_response_cap(monkeypatch):
    cap = doctor.audio_runtime._STATUS_RESPONSE_MAX_BYTES
    fake = _FakeSocket(chunks=[b"x" * 65536] * 16 + [b""])
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: fake)

    payload = doctor.audio_runtime._read_status_socket_bytes("/run/test.sock", timeout=2.0)

    assert len(payload) == cap
    assert fake.recv_sizes == [65536] * 17
    assert fake.closed is True


def test_status_socket_byte_reader_rejects_response_over_cap(monkeypatch):
    fake = _FakeSocket(chunks=[b"x" * 65536] * 16 + [b"y"])
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: fake)

    with pytest.raises(OSError, match="exceeds byte limit"):
        doctor.audio_runtime._read_status_socket_bytes("/run/test.sock", timeout=2.0)

    assert fake.recv_sizes == [65536] * 17
    assert fake.closed is True


def test_status_socket_byte_reader_enforces_total_deadline(monkeypatch):
    fake = _FakeSocket(chunks=[b"x", b"y", b""])
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: fake)
    monkeypatch.setattr(
        doctor.audio_runtime.time,
        "monotonic",
        Mock(side_effect=[0.0, 0.0, 0.1, 0.2, 1.1]),
    )

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        doctor.audio_runtime._read_status_socket_bytes("/run/test.sock", timeout=1.0)

    assert fake.recv_sizes == [65536]
    assert fake.closed is True


@pytest.mark.parametrize("failure_stage", ["connect", "recv"])
def test_status_socket_byte_reader_closes_on_failure(monkeypatch, failure_stage):
    error = OSError(f"{failure_stage} failed")
    fake = _FakeSocket(
        error=error if failure_stage == "connect" else None,
        recv_error=error if failure_stage == "recv" else None,
    )
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: fake)

    with pytest.raises(OSError, match=f"{failure_stage} failed"):
        doctor.audio_runtime._read_status_socket_bytes("/run/test.sock", timeout=2.0)

    assert fake.closed is True


def test_status_socket_strict_wrapper_and_lossy_caller_keep_decode_ownership(
    monkeypatch,
):
    strict = _FakeSocket(payload=b'{"note":"\xff"}')
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: strict)

    with pytest.raises(UnicodeDecodeError):
        doctor.audio_runtime._read_status_socket("/run/test.sock")

    assert 0 < strict.timeout <= 1.0
    assert strict.closed is True

    lossy = _FakeSocket(payload=b'{"note":"\xff","tts":{"enabled":false}}')
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: lossy)

    result = doctor.check_fanin_tts_drops()

    assert result.status == "ok"
    assert "disabled" in result.detail
    assert 0 < lossy.timeout <= 2.0
    assert lossy.closed is True


def test_check_fanin_service_keeps_one_bounded_status_retry(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(doctor.audio_runtime.time, "sleep", lambda _: None)
    first = _FakeSocket(error=OSError("transient refusal"))
    second = _FakeSocket(payload=_fanin_status_payload())
    pending = [first, second]
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: pending.pop(0))

    result = doctor.check_fanin_service()

    assert result.status == "ok"
    assert pending == []
    assert 0 < first.timeout <= 2.0
    assert 0 < second.timeout <= 2.0
    assert first.closed is True
    assert second.closed is True


@pytest.mark.parametrize(
    ("check", "expected_status", "detail"),
    [
        (doctor.check_fanin_service, "fail", "expected object"),
        (doctor.check_fanin_tts_drops, "ok", "not probed (ValueError)"),
        (doctor.check_outputd_service, "fail", "expected object"),
        (doctor.check_aec_clock_drift, "ok", "skipped"),
    ],
)
def test_status_consumers_classify_non_object_root_without_crashing(
    monkeypatch, check, expected_status, detail
):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, b"[]")

    result = check()

    assert result.status == expected_status
    assert detail in result.detail


def test_check_fanin_service_ok_with_expected_status(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload())
    r = doctor.check_fanin_service()
    assert r.status == "ok"
    assert "transport=shm_ring" in r.detail
    assert "input_buffer_frames=4096" in r.detail
    assert "output_buffer_frames=1024" in r.detail
    assert "tts_enabled=true" in r.detail
    assert "assistant_loudness_decision=False" in r.detail


@pytest.mark.parametrize("persisted", ["shm_ring", "loopback", None])
def test_check_fanin_service_expects_the_ring_whatever_the_file_says(
    monkeypatch, persisted
):
    """The expected transport is a CONSTANT, not a read of the persisted file.

    Fan-in refuses every non-ring declaration at config parse (exit 78), so a
    LIVE STATUS can only come from a ring box. Deriving the expectation from
    /var/lib/jasper/fanin.env FAILed a healthy box whose key was unwritten —
    coupling-auto runs After=jasper-fanin.service, so that is every fresh boot.
    """
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: persisted,
    )
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload())

    r = doctor.check_fanin_service()

    assert r.status == "ok"


def test_check_fanin_service_fails_on_a_non_ring_live_transport(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch, _fanin_status_payload(transport="loopback")
    )

    r = doctor.check_fanin_service()

    assert r.status == "fail"
    assert "output.transport='loopback'" in r.detail


def test_check_fanin_service_fails_when_status_carries_no_ring_block(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload(ring=None))

    r = doctor.check_fanin_service()

    assert r.status == "fail"
    assert "output.ring" in r.detail


def test_check_fanin_service_reports_pre_dsp_tts_loudness(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = {
        "enabled": True,
        "pending_frames": 0,
        "assistant_loudness": {
            "content_short_lufs": -31.2,
            "content_anchor_lufs": -30.8,
            "decision_seen": True,
            "calibrated": True,
            "profile_confidence": 1.0,
            "baseline_lufs": -38.0,
            "target_lufs": -36.5,
            "source_lufs": -25.0,
            "source_peak_dbfs": -8.0,
            "requested_gain_db": -11.5,
            "peak_cap_gain_db": 5.0,
            "final_gain_db": -11.5,
        },
    }
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_fanin_service()

    assert r.status == "ok"
    assert "tts_enabled=true" in r.detail
    assert "assistant_loudness_decision=True" in r.detail
    assert "assistant_final_gain_db=-11.5" in r.detail


# The assistant-gain contract, boundary by boundary (#2345). The engine computes
# final = max(MIN_TTS_GAIN_DB, min(requested, peak_cap)); the doctor asserts that
# relation, NOT a fixed range, because there is deliberately no fixed positive
# ceiling — a pre-DSP decision goes positive to pre-compensate for CamillaDSP's
# downstream attenuation.
@pytest.mark.parametrize(
    ("loudness", "faulty"),
    [
        # Ordinary attenuating decision, exactly on contract.
        ({"requested_gain_db": -11.5, "peak_cap_gain_db": 5.0, "final_gain_db": -11.5}, False),
        # #2345 as observed on jts3: a dense probe tone became the loudness
        # anchor, fan-in asked for +5.0 and the peak cap allowed +3.0. Positive
        # and peak-capped is the contract working, not a clamp leak.
        ({"requested_gain_db": 5.0, "peak_cap_gain_db": 3.0, "final_gain_db": 3.0}, False),
        # Today's publish rounding is monotone, so the comparison is exact and
        # this 0.1 disagreement cannot arise in the field. The tolerance is a
        # cushion against future publish-path drift, not a derived bound; this
        # case is what pins it.
        ({"requested_gain_db": 5.0, "peak_cap_gain_db": 3.0, "final_gain_db": 3.1}, False),
        # Louder than the peak cap allowed — the hearing-safety failure the check
        # exists for.
        ({"requested_gain_db": 5.0, "peak_cap_gain_db": 3.0, "final_gain_db": 3.4}, True),
        # Quieter than the contract: the decided gain was not the one applied.
        ({"requested_gain_db": -11.5, "peak_cap_gain_db": 5.0, "final_gain_db": -20.0}, True),
        # The floor wins over an even quieter target, so final legitimately sits
        # ABOVE min(requested, peak_cap).
        ({"requested_gain_db": -80.0, "peak_cap_gain_db": 5.0, "final_gain_db": -60.0}, False),
        # Nothing may sit below the floor.
        ({"requested_gain_db": -80.0, "peak_cap_gain_db": 5.0, "final_gain_db": -75.0}, True),
        # A daemon too old to publish the two inputs is held to the floor alone.
        ({"final_gain_db": 3.0}, False),
        ({"final_gain_db": -75.0}, True),
        ({"requested_gain_db": None, "peak_cap_gain_db": None, "final_gain_db": 3.0}, False),
        # No decision yet: nothing to judge (the malformed-value path owns this).
        ({"final_gain_db": None}, False),
    ],
)
def test_assistant_gain_fault_pins_the_shared_loudness_contract(loudness, faulty):
    fault = doctor.audio_runtime._assistant_gain_fault(loudness)
    assert (fault is not None) is faulty, fault


def test_check_fanin_service_ok_with_peak_capped_positive_gain(monkeypatch):
    """#2345: a peak-capped positive gain is the contract, not a warning."""
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = {
        "enabled": True,
        "pending_frames": 0,
        "assistant_loudness": {
            "decision_seen": True,
            "calibrated": False,
            "source_peak_dbfs": -6.0,
            "requested_gain_db": 5.0,
            "peak_cap_gain_db": 3.0,
            "final_gain_db": 3.0,
        },
    }
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_fanin_service()

    assert r.status == "ok"
    assert "assistant_final_gain_db=3.0" in r.detail


def test_check_fanin_service_warns_when_gain_exceeds_the_peak_cap(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = {
        "enabled": True,
        "pending_frames": 0,
        "assistant_loudness": {
            "decision_seen": True,
            "calibrated": True,
            "requested_gain_db": 5.0,
            "peak_cap_gain_db": 3.0,
            "final_gain_db": 5.0,
        },
    }
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_fanin_service()

    assert r.status == "warn"
    assert "final_gain_db=5.0" in r.detail
    assert "peak_cap_gain_db=3.0" in r.detail


def test_check_fanin_service_warns_on_malformed_pre_dsp_tts_loudness(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = {
        "enabled": True,
        "pending_frames": 0,
        "assistant_loudness": {
            "decision_seen": True,
            "calibrated": False,
            "final_gain_db": None,
        },
    }
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_fanin_service()

    assert r.status == "warn"
    assert "decision_seen=true" in r.detail


def test_check_fanin_service_fails_on_invalid_status_json(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, b"not-json")
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "invalid JSON" in r.detail


def test_check_fanin_service_fails_when_status_socket_unreachable(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        doctor.socket,
        "socket",
        lambda *a, **kw: _FakeSocket(error=OSError("connection refused")),
    )
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "UDS probe" in r.detail


def test_check_fanin_service_fails_on_small_runtime_buffers(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_status_payload(input_buffer_frames=2048),
    )
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "input_buffer_frames=2048" in r.detail

    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_status_payload(output_buffer_frames=512),
    )
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "output_buffer_frames=512" in r.detail


def test_check_fanin_service_accepts_new_low_latency_output_default(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_status_payload(output_buffer_frames=1024),
    )
    r = doctor.check_fanin_service()
    assert r.status == "ok"
    assert "output_buffer_frames=1024" in r.detail


def test_outputd_service_fails_when_disabled(monkeypatch):
    _patch_fanin_systemctl(monkeypatch, enabled="disabled")
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert "expected enabled" in r.detail


def test_outputd_service_ok_with_expected_status(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _outputd_status_payload())
    r = doctor.check_outputd_service()
    assert r.status == "ok"
    assert "backend=alsa" in r.detail
    assert "content_buffer_frames=4096" in r.detail
    assert "dac_buffer_frames=3072" in r.detail
    assert "content_empty_periods=2" in r.detail
    assert "content_eagain_count=1" in r.detail
    assert "content_source=alsa" in r.detail
    assert "content_bridge=direct" in r.detail
    assert "speaker_reference_source=outputd_final_electrical" in r.detail


def test_the_arm_waypoint_is_reported_once_by_the_check_that_owns_it(
    monkeypatch, tmp_path
):
    """ONE fact, ONE check. This pins the de-duplication, in both directions.

    The ACTIVE-ring arm waypoint — a loaded graph naming the ACTIVE ring under a
    non-ring coupling — used to surface twice: `check_active_ring_split_transport`
    FAILed on it, and `check_outputd_service` separately elevated the same
    detector's note to a WARN. Same statefile, same two terms, two check names,
    two severities: a household or an operator reading `jasper-doctor` saw one
    problem written up as two, and the louder of the two already carried the
    runnable remedy.

    So `check_outputd_service` is now silent on the waypoint and the split check
    still names it. Asserting only the first half would pass just as well if the
    finding had been dropped altogether, which is the failure this whole wave is
    supposed to avoid — so the FAIL is asserted in the same test.
    """
    from jasper.audio_runtime_plan import (
        output_endpoint_evidence_from_statefiles as _real_endpoint_evidence,
    )
    from jasper.cli.doctor import audio_runtime as _audio_runtime
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    # ONE on-disk statefile pair drives BOTH halves — no stubbed evidence
    # resolution. The first version of this guard canned
    # `output_endpoint_evidence_from_statefiles` for half one and
    # `_loaded_playback_device` for half two, which meant the two surfaces were
    # fed by two independent fixtures and could disagree about the box without
    # reddening anything. That is precisely how the gap this de-duplication
    # inherited (the split check reading one statefile while the deleted note
    # read two) survived unseen. Same bytes to both, or the guard is theatre.
    config = tmp_path / "loaded.yml"
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  capture:\n    type: Alsa\n    device: jts_ring_capture\n"
        f"  playback:\n    type: Alsa\n    device: {RING_ACTIVE_PLAYBACK_DEVICE}\n",
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    absent = tmp_path / "crossover-statefile.yml"

    monkeypatch.setattr(
        _audio_runtime, "_active_camilla_config_path",
        lambda: (str(statefile), str(config)),
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA_STATEFILE_PATH", str(statefile)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(absent)
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _outputd_status_payload())
    # `_patch_fanin_status_socket` also cans `output_endpoint_evidence_from_statefiles`
    # from the STATUS payload — convenient for the checks whose subject is the
    # payload, and fatal for this one, whose subject IS the evidence resolution.
    # Put the real reader back so both halves resolve the statefiles above.
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        _real_endpoint_evidence,
    )

    r = doctor.check_outputd_service()

    # Half one: outputd no longer restates the split.
    assert r.status == "ok", r.detail
    assert "arm waypoint" not in r.detail

    # Half two: the check that OWNS the split still fails on it, with the
    # remedy — off the same statefile half one just read.
    split = _audio_runtime.check_active_ring_split_transport()

    assert split.status == "fail", split.detail
    assert "jasper-fanin-coupling-reconcile shm_ring" in split.detail
    # #2285 P2: the retired rollback endpoint must never be printed as a
    # remediation — argparse rejects it, and a command the operator cannot run
    # is worse than none, because they do the work of trying it.
    assert "--endpoint aloop" not in split.detail


def test_outputd_content_bridge_detail_reports_every_mode():
    """The surviving `/state.content_bridge` readout is a plain mode string.

    The rate-matched bridge's fill/ppm/lock counters were deleted with it, so
    this is all the doctor reports for the axis now. Pinned across all three
    branches because `direct` alone would pass even if the function hardcoded
    it: `shm_ring` proves it reads the payload, and the two malformed shapes
    prove it degrades to `missing` rather than raising inside a doctor check.
    """
    from jasper.cli.doctor.audio_runtime import _outputd_content_bridge_detail

    assert (
        _outputd_content_bridge_detail({"content_bridge": {"mode": "direct"}})
        == "content_bridge=direct"
    )
    assert (
        _outputd_content_bridge_detail({"content_bridge": {"mode": "shm_ring"}})
        == "content_bridge=shm_ring"
    )
    for malformed in ({}, {"content_bridge": None}, {"content_bridge": {}},
                      {"content_bridge": {"mode": ""}}):
        assert _outputd_content_bridge_detail(malformed) == "content_bridge=missing", malformed


def test_outputd_service_ok_with_shm_ring_content_source(monkeypatch, tmp_path):
    """Ring-coupled box: coupling=shm_ring + content.source='shm_ring' is OK.

    Uses the REAL synthetic content.buffer_frames a shm_ring box publishes
    (== dac.period_frames, because outputd never opens the content ALSA PCM),
    NOT the 4096 default that masked the bug. On pre-fix doctor code the generic
    ">= 2x period" floor rejects it (period < 2*period), so this asserts the new
    shm_ring branch that exempts the floor and validates the content.ring
    geometry contract instead (jts.local, 2026-07-06 first post-default-flip
    smoke, made honest end-to-end)."""
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "shm_ring",
    )
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(
            content_source="shm_ring",
            # The honest synthetic: content.buffer_frames == period. This is
            # below the generic 2*period floor and MUST pass only via the
            # shm_ring geometry branch, not the masking 4096 default.
            content_buffer_frames=1024,
            period_frames=1024,
        ),
    )

    r = doctor.check_outputd_service()

    assert r.status == "ok"
    assert "content_source=shm_ring" in r.detail
    assert "shm_ring_slots=2" in r.detail
    assert "shm_ring_slot_frames=1024" in r.detail
    assert "shm_ring_capacity_frames=2048" in r.detail


def test_outputd_service_fails_shm_ring_missing_ring_geometry(monkeypatch, tmp_path):
    """shm_ring with no content.ring geometry contract fails loud (a
    pre-honesty-fix outputd binary, or a corrupt STATUS)."""
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "shm_ring",
    )
    payload = json.loads(
        _outputd_status_payload(
            content_source="shm_ring",
            content_buffer_frames=1024,
            period_frames=1024,
        ).decode()
    )
    del payload["content"]["ring"]
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "content.ring" in r.detail


def test_outputd_service_fails_shm_ring_slot_frames_mismatch(monkeypatch, tmp_path):
    """The shm_ring branch keeps its teeth: a ring slot that does not match the
    DAC period is a real geometry break, not an exempted synthetic."""
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "shm_ring",
    )
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(
            content_source="shm_ring",
            content_buffer_frames=1024,
            period_frames=1024,
            shm_ring_slot_frames=512,
        ),
    )

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "slot_frames" in r.detail


def test_outputd_service_fails_shm_ring_capacity_incoherent(monkeypatch, tmp_path):
    """content.ring.capacity_frames must equal n_slots*slot_frames — a mismatch
    is dishonest STATUS and fails loud."""
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "shm_ring",
    )
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(
            content_source="shm_ring",
            content_buffer_frames=1024,
            period_frames=1024,
            shm_ring_capacity_frames=9999,
        ),
    )

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "capacity_frames" in r.detail


def test_outputd_service_fails_on_coupling_content_source_mismatch(monkeypatch):
    """The check keeps its teeth: shm_ring coupling with outputd still on the
    ALSA content lane (a real incoherence — e.g. outputd missed the flip
    restart) fails with the reconcile remedy."""
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "shm_ring",
    )
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(content_source="alsa"),
    )

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "expected 'shm_ring'" in r.detail
    assert "jasper-fanin-coupling-reconcile" in r.detail


def test_outputd_service_fails_on_live_source_mismatch(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: "shm_ring",
    )
    _patch_fanin_status_socket(monkeypatch, _outputd_status_payload())

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "content.source='alsa'" in r.detail
    assert "expected 'shm_ring'" in r.detail


def test_audio_runtime_plan_doctor_warns_on_shadowed_knob(monkeypatch):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_audio_runtime_plan()

    assert r.status == "warn"
    assert "one knob has two homes" in r.detail


def test_audio_runtime_plan_doctor_fails_unsupported_route(monkeypatch):
    # The route is unsupported only when the bond actually reads outputd's
    # dac_content lane, so the plan carries that fact alongside the route mode.
    plan = audio_runtime_plan.build_audio_runtime_plan(
        fanin_env={"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"},
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring"},
        route_mode="active_leader",
        dac_content_lane_armed=True,
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_audio_runtime_plan()

    assert r.status == "fail"
    assert "shm_ring is not supported while" in r.detail


def test_audio_runtime_plan_doctor_passes_a_bonded_box_with_a_cleared_lane(
    monkeypatch,
):
    """The narrowing, at the doctor: an ACTIVE endpoint's dac_content lane is
    cleared, so a ring-armed bonded box is no longer reported as an unsupported
    route. Without this the doctor would red every box the hardware pass needs."""
    plan = audio_runtime_plan.build_audio_runtime_plan(
        fanin_env={"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"},
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring"},
        route_mode="active_leader",
        dac_content_lane_armed=False,
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_audio_runtime_plan()

    assert "shm_ring is not supported while" not in r.detail


def test_audio_runtime_plan_doctor_fails_usb_route_with_legacy_lab_transport(
    monkeypatch,
):
    # A stale non-direct outputd bridge literal (the REMOVED rate_match, or a
    # typo) is a partial flip: outputd fail-safes it to `direct`, but the route
    # policy compares the raw value, so certification stays red. (transport_pipe was
    # removed 2026-07-11); a non-direct bridge without a matching shm_ring pair is
    # a partial flip the USB low-latency route refuses.
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "rate_match"},
        route_mode="solo",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_audio_runtime_plan()

    assert r.status == "fail"
    assert "partial flip" in r.detail


def test_route_latency_evidence_skips_non_claiming_route(monkeypatch):
    plan = audio_runtime_plan.build_audio_runtime_plan(route_mode="solo")
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_route_latency_evidence()

    assert r.status == "ok"
    assert "no low-latency claim" in r.detail


def test_route_latency_evidence_fails_runtime_plan_errors(monkeypatch):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "rate_match"},
        route_mode="solo",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_route_latency_evidence()

    assert r.status == "fail"
    assert "runtime plan errors block latency certification" in r.detail


def test_route_latency_evidence_fails_missing_claim_artifact(monkeypatch, tmp_path):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_route_live_state_issues_for_doctor",
        lambda observed_plan, **_kwargs: (),
    )
    monkeypatch.setenv("JASPER_AUDIO_VALIDATION_DIR", str(tmp_path))

    r = doctor.check_route_latency_evidence()

    assert r.status == "fail"
    assert "artifact_status=fail" in r.detail


def test_route_latency_evidence_warns_when_p99_not_certified(
    monkeypatch,
    tmp_path,
):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    identity = plan.route_latency_identity()
    artifact = audio_validation.make_route_latency_artifact(
        route_id=audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K,
        source_id="usbsink",
        dac_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_config_hash=plan.route_config_hash,
        camilla_config_hash=str(identity["camilla_config_hash"]),
        fanin_direct_config=identity["fanin_direct_config"],
        fanin_direct_negotiated_buffer_frames=768,
        fanin_resampler_config=identity["fanin_resampler_config"],
        outputd_config=identity["outputd_config"],
        uac2_gadget_attrs=identity["uac2_gadget_attrs"],
        p95_ms=38.0,
        p99_ms=None,
        sample_count=200,
        duration_seconds=5 * 60,
    )
    audio_validation.write_artifact(artifact, directory=tmp_path)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_route_live_state_issues_for_doctor",
        lambda observed_plan, **_kwargs: (),
    )
    monkeypatch.setenv("JASPER_AUDIO_VALIDATION_DIR", str(tmp_path))

    r = doctor.check_route_latency_evidence()

    assert r.status == "warn"
    assert "p99_missing" in r.detail


def test_route_latency_evidence_passes_certified_promotion_artifact(
    monkeypatch,
    tmp_path,
):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    identity = plan.route_latency_identity()
    artifact = audio_validation.make_route_latency_artifact(
        route_id=audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K,
        source_id="usbsink",
        dac_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_config_hash=plan.route_config_hash,
        camilla_config_hash=str(identity["camilla_config_hash"]),
        fanin_direct_config=identity["fanin_direct_config"],
        fanin_direct_negotiated_buffer_frames=768,
        fanin_resampler_config=identity["fanin_resampler_config"],
        outputd_config=identity["outputd_config"],
        uac2_gadget_attrs=identity["uac2_gadget_attrs"],
        p95_ms=38.0,
        p99_ms=41.0,
        sample_count=1000,
        duration_seconds=30 * 60,
        impulse_spacing_jittered=True,
    )
    audio_validation.write_artifact(artifact, directory=tmp_path)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_route_live_state_issues_for_doctor",
        lambda observed_plan, **_kwargs: (),
    )
    monkeypatch.setenv("JASPER_AUDIO_VALIDATION_DIR", str(tmp_path))

    r = doctor.check_route_latency_evidence()

    assert r.status == "ok"
    assert "artifact_status=pass" in r.detail


def test_route_latency_evidence_passes_certified_artifact_while_lane_idle(
    monkeypatch,
    tmp_path,
):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    identity = plan.route_latency_identity()
    artifact = audio_validation.make_route_latency_artifact(
        route_id=audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K,
        source_id="usbsink",
        dac_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_config_hash=plan.route_config_hash,
        camilla_config_hash=str(identity["camilla_config_hash"]),
        fanin_direct_config=identity["fanin_direct_config"],
        fanin_direct_negotiated_buffer_frames=768,
        fanin_resampler_config=identity["fanin_resampler_config"],
        outputd_config=identity["outputd_config"],
        uac2_gadget_attrs=identity["uac2_gadget_attrs"],
        p95_ms=38.0,
        p99_ms=41.0,
        sample_count=1000,
        duration_seconds=30 * 60,
        impulse_spacing_jittered=True,
    )
    artifact_dir = tmp_path / "artifacts"
    audio_validation.write_artifact(artifact, directory=artifact_dir)
    direct = identity["fanin_direct_config"]
    resampler = identity["fanin_resampler_config"]
    expected_target = resampler["target_frames"] + resampler["warmup_cushion_frames"]
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_read_status_socket",
        lambda _path: {
            "inputs": [
                {
                    "label": "usbsink",
                    "source": "direct",
                    "direct": {
                        "device": direct["device"],
                        "health": "idle",
                        "period_frames": direct["period_frames"],
                        "buffer_frames": direct["min_buffer_frames"],
                    },
                    "resampler": {
                        "locked": False,
                        "target_fill_frames": expected_target,
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )
    monkeypatch.setenv("JASPER_AUDIO_VALIDATION_DIR", str(artifact_dir))

    r = doctor.check_route_latency_evidence()

    assert r.status == "ok"
    assert "artifact_status=pass" in r.detail
    assert "live_fanin_resampler_unlocked" not in r.detail


def test_route_latency_live_state_rejects_changed_negotiated_direct_buffer(
    monkeypatch,
):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    direct = plan.route_latency_identity()["fanin_direct_config"]
    resampler = plan.route_latency_identity()["fanin_resampler_config"]
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_read_status_socket",
        lambda _path: {
            "inputs": [
                {
                    "label": "usbsink",
                    "source": "direct",
                    "direct": {
                        "device": direct["device"],
                        "health": "capturing",
                        "period_frames": direct["period_frames"],
                        "buffer_frames": 1024,
                    },
                    "resampler": {
                        "locked": True,
                        "target_fill_frames": (
                            resampler["target_frames"]
                            + resampler["warmup_cushion_frames"]
                        ),
                    },
                }
            ]
        },
    )

    issues = doctor.audio_runtime._route_live_state_issues_for_doctor(
        plan,
        negotiated_buffer_frames=768,
    )

    assert "live_fanin_direct_mismatch:usbsink:negotiated_buffer_frames" in issues


def test_route_latency_evidence_warns_on_aged_out_in_budget_proof(
    monkeypatch,
    tmp_path,
):
    # ADR-0101: doctor inherits the assessor's disclosing warn for a proof the
    # 24h window aged out. Its own fails (plan errors, live issues) are pinned
    # by the neighbouring tests and stay fail.
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    identity = plan.route_latency_identity()
    artifact = audio_validation.make_route_latency_artifact(
        route_id=audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K,
        source_id="usbsink",
        dac_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_config_hash=plan.route_config_hash,
        camilla_config_hash=str(identity["camilla_config_hash"]),
        fanin_direct_config=identity["fanin_direct_config"],
        fanin_direct_negotiated_buffer_frames=768,
        fanin_resampler_config=identity["fanin_resampler_config"],
        outputd_config=identity["outputd_config"],
        uac2_gadget_attrs=identity["uac2_gadget_attrs"],
        p95_ms=38.0,
        p99_ms=41.0,
        sample_count=1000,
        duration_seconds=30 * 60,
        impulse_spacing_jittered=True,
        validated_at=(
            datetime.now(timezone.utc)
            - audio_validation.ROUTE_LATENCY_STALE_AFTER
            - timedelta(hours=1)
        ),
    )
    audio_validation.write_artifact(artifact, directory=tmp_path)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_route_live_state_issues_for_doctor",
        lambda observed_plan, **_kwargs: (),
    )
    monkeypatch.setenv("JASPER_AUDIO_VALIDATION_DIR", str(tmp_path))

    r = doctor.check_route_latency_evidence()

    assert r.status == "warn"
    assert "artifact_stale" in r.detail


def test_route_latency_evidence_fails_live_state_mismatch(
    monkeypatch,
    tmp_path,
):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    identity = plan.route_latency_identity()
    artifact = audio_validation.make_route_latency_artifact(
        route_id=audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K,
        source_id="usbsink",
        dac_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_config_hash=plan.route_config_hash,
        camilla_config_hash=str(identity["camilla_config_hash"]),
        fanin_direct_config=identity["fanin_direct_config"],
        fanin_direct_negotiated_buffer_frames=768,
        fanin_resampler_config=identity["fanin_resampler_config"],
        outputd_config=identity["outputd_config"],
        uac2_gadget_attrs=identity["uac2_gadget_attrs"],
        p95_ms=38.0,
        p99_ms=41.0,
        sample_count=1000,
        duration_seconds=30 * 60,
        impulse_spacing_jittered=True,
    )
    audio_validation.write_artifact(artifact, directory=tmp_path)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_route_live_state_issues_for_doctor",
        lambda observed_plan, **_kwargs: ("live_fanin_resampler_unlocked:usbsink",),
    )
    monkeypatch.setenv("JASPER_AUDIO_VALIDATION_DIR", str(tmp_path))

    r = doctor.check_route_latency_evidence()

    assert r.status == "fail"
    assert "live_fanin_resampler_unlocked:usbsink" in r.detail


def test_outputd_service_ok_with_single_alsa_active_lane(monkeypatch, tmp_path):
    """An ACTIVE single-ALSA box is healthy on the ring, declaring NO content PCM.

    #2285 P2 moved this off the direct bridge. It used to model the same box
    declaring ``outputd_active_content_capture``; that lane is deleted (#2534),
    the reconciler now writes explicit-empty for an active single-ALSA box, and
    under the direct bridge outputd would open whatever it was handed
    (``content_pcm_skipped`` is false there) — so the old shape describes no box
    that can run. The width readout is still the assertion that matters, and it
    comes from the env, independently of the bridge.
    """
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _ring_coupled_status_payload(dac_pcm=doctor._OUTPUTD_EXPECTED_DAC_PCM),
    )
    _patch_ring_coupled_box(
        monkeypatch, tmp_path, active_endpoint=True, active_channels=2
    )

    r = doctor.check_outputd_service()

    assert r.status == "ok", r.detail
    assert "active_channels=2" in r.detail


def test_outputd_service_fails_when_a_ring_box_still_names_the_retired_content_pcm(
    monkeypatch,
    tmp_path,
):
    """The negative guard the retired name survives FOR.

    #2285 P2 re-pointed this from ``..._when_active_env_has_legacy_content_pcm``,
    which pinned the sink-keyed expectation: an ACTIVE box was compared against
    ``outputd_active_content_capture`` and failed for declaring anything else.
    That expectation is gone — the name it demanded is a PCM #2534 deleted and
    no reconcile writes — so the comparison inverted. The same "a legacy content
    PCM must not pass quietly" property now lands here: under the ring outputd
    opens no content PCM at all, so the retired snd-aloop lane appearing in
    ``outputd.env`` is the one value that means something stale survived.

    The message must both NAME the stale value and carry the reconcile that
    clears it, because the operator cannot infer either from a bare mismatch.
    """
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _ring_coupled_status_payload(
            content_pcm=doctor._OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM,
        ),
    )
    _patch_ring_coupled_box(monkeypatch, tmp_path, active_endpoint=True)

    r = doctor.check_outputd_service()

    assert r.status == "fail", r.detail
    assert "outputd_active_content_capture" in r.detail
    assert "jasper-audio-hardware-reconcile" in r.detail


def test_outputd_service_ok_when_a_flat_ring_box_declares_its_passive_lane(
    monkeypatch,
    tmp_path,
):
    """CONTROL for the guard above — and the fleet's most common ring box.

    A flat (passive single-ALSA) box on the ring still declares
    ``outputd_content_capture``: the hardware reconciler writes it, the unit
    carries it as an ``Environment=`` default, and ``Config::from_env``'s
    ``SingleAlsa`` arm defaults to it. Under the ring nothing opens it, so the
    declaration is inert — a live asoundrc definition the transport happens not
    to use.

    Without this control the guard above would also pass if the ring branch
    rejected ANY content PCM, which is the shape that would have FAILED every
    flat ring box in the fleet.
    """
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _ring_coupled_status_payload(
            content_pcm=doctor._OUTPUTD_EXPECTED_CONTENT_PCM,
        ),
    )
    _patch_ring_coupled_box(monkeypatch, tmp_path)

    r = doctor.check_outputd_service()

    assert r.status == "ok", r.detail
    assert "content_source=shm_ring" in r.detail


# #2285 P2 (A6) retired the snd-aloop ACTIVE lane's outputd capture PAIRING with
# the endpoint itself, so this shape stopped reporting a capture MISMATCH — there
# is no registered capture left to mismatch against, and the unpaired-device arm
# of `transport_coherence_errors` reports it instead. Same box, same verdict, a
# different sentence. Kept as one constant so the two tests below cannot drift
# apart from each other.
_ROUTE_UNPAIRED = (
    "post-DSP route has no registered outputd capture for "
    "Camilla playback='outputd_active_content_playback'"
)


def _patch_disconnected_post_dsp_route(monkeypatch) -> None:
    """Camilla on the active playback lane, outputd reading the passive one."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _outputd_status_payload())
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
            devices={
                "playback_device": "outputd_active_content_playback",
                "capture_device": "plug:jasper_capture",
            }
        ),
    )


def _write_no_lane_active_topology(path: Path) -> None:
    """Save a roleful layout on a DAC that declares no active outputd lane.

    Uses the synthetic passive-only profile: every DAC in the shipped registry
    now declares an active lane, so this remedy is pinned against the stand-in
    for the next lane-less board. Callers must register it first with
    ``register_passive_only_dac(monkeypatch)``.
    """
    from jasper.output_topology import (
        OUTPUT_TOPOLOGY_KIND,
        OutputTopology,
        save_output_topology,
    )

    save_output_topology(
        OutputTopology.from_mapping({
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "default",
            "name": "Mono active 2-way",
            "status": "verified",
            "hardware": {
                "device_id": PASSIVE_ONLY_DAC_ID,
                "device_label": PASSIVE_ONLY_DAC_LABEL,
                "physical_output_count": 2,
            },
            "speaker_groups": [
                {
                    "id": "main",
                    "label": "Main active speaker",
                    "kind": "mono",
                    "mode": "active_2_way",
                    "channels": [
                        {
                            "role": "woofer",
                            "physical_output_index": 0,
                            "identity_verified": True,
                        },
                        {
                            "role": "tweeter",
                            "physical_output_index": 1,
                            "identity_verified": True,
                            "startup_muted": True,
                            "protection_required": True,
                            "protection_status": "present",
                        },
                    ],
                }
            ],
            "routing": {"mono_group_id": "main"},
        }),
        path,
    )


def test_outputd_service_fails_when_active_graph_feeds_passive_reader(
    monkeypatch,
    tmp_path,
):
    # Pin the saved topology: an unconfigured one is the reconcilable case, so
    # the remedy names the reconciler.
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json")
    )
    _patch_disconnected_post_dsp_route(monkeypatch)

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert _ROUTE_UNPAIRED in r.detail
    assert "audio-hardware-reconcile" in r.detail


def test_route_disconnect_remedy_does_not_recommend_an_impossible_reconcile(
    monkeypatch,
    tmp_path,
):
    """When the saved layout needs a lane the DAC does not have, running the
    reconciler cannot help — it is already resolving passive correctly. The
    remedy must say what actually clears it instead of sending the operator
    into a loop."""
    register_passive_only_dac(monkeypatch)
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))
    _write_no_lane_active_topology(topo_path)
    _patch_disconnected_post_dsp_route(monkeypatch)

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert _ROUTE_UNPAIRED in r.detail
    assert PASSIVE_ONLY_DAC_LABEL in r.detail
    assert "/sound/setup/" in r.detail
    assert "audio-hardware-reconcile" not in r.detail
    # Passive is not a free remedy: it sends full-range into every assigned
    # output, which on an actively-wired cabinet reaches a bare tweeter. An
    # operator following doctor's advice must be told that.
    assert "full-range audio to every output" in r.detail
    assert "built-in passive crossover" in r.detail
    assert "attach an active-capable DAC" in r.detail


def test_outputd_service_warns_when_transport_evidence_is_unavailable(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _outputd_status_payload())
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
            devices=None,
            errors=("statefile unavailable",),
        ),
    )

    r = doctor.check_outputd_service()

    assert r.status == "warn"
    assert "transport coherence unknown" in r.detail
    assert "statefile unavailable" in r.detail


def test_outputd_service_ok_when_loudness_is_owned_by_fanin(monkeypatch):
    payload = json.loads(_outputd_status_payload().decode())
    payload.pop("assistant_loudness", None)
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "ok"
    assert "assistant_loudness=fan-in-owned" in r.detail


def test_outputd_service_warns_when_gain_exceeds_the_peak_cap(monkeypatch):
    """Outputd's post-DSP lane runs the same engine, so the same contract."""
    payload = json.loads(_outputd_status_payload().decode())
    payload["assistant_loudness"].update(
        {
            "decision_seen": True,
            "calibrated": True,
            "requested_gain_db": -4.0,
            "peak_cap_gain_db": -6.0,
            "final_gain_db": -4.0,
        }
    )
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "warn"
    assert "final_gain_db=-4.0" in r.detail
    assert "peak_cap_gain_db=-6.0" in r.detail


def test_outputd_service_fails_when_dual_apple_status_missing(monkeypatch, tmp_path):
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(
        _ring_coupled_status_payload(
            sink_mode="dual_apple",
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
        ).decode()
    )
    payload.pop("dual_apple", None)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())
    _patch_ring_coupled_box(monkeypatch, tmp_path, active_endpoint=True)

    r = doctor.check_outputd_service()
    assert r.status == "fail", r.detail
    assert "STATUS missing dual_apple runtime health" in r.detail


def test_outputd_service_warns_when_dual_apple_pcm_link_missing(monkeypatch, tmp_path):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _ring_coupled_status_payload(
            sink_mode="dual_apple",
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
            dual_apple_status={
                "dac_a_pcm": "hw:CARD=A,DEV=0",
                "dac_b_pcm": "hw:CARD=A_1,DEV=0",
                "linked": False,
                "delay_delta_frames": 0,
                "delay_delta_baseline_frames": 0,
                "delay_delta_error_frames": 0,
                "max_delay_delta_frames": 2,
            },
        ),
    )
    _patch_ring_coupled_box(monkeypatch, tmp_path, active_endpoint=True)
    r = doctor.check_outputd_service()
    assert r.status == "warn", r.detail
    assert "not ALSA-linked" in r.detail


def test_outputd_service_ok_with_dual_apple_status(monkeypatch, tmp_path):
    """The armed composite box on the ring — jts.local's own shape.

    #2285 P2 moved the three dual_apple tests off the direct bridge. A composite
    sink declaring no content PCM is REFUSED at parse on the direct bridge
    (``Config::from_env``, EX_CONFIG), and the only other direct-bridge shape —
    a composite naming the passive lane — is the 4ch-over-a-2ch-slave reuse this
    PR rejected as hearing-adjacent. A running composite box is a ring box.
    """
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _ring_coupled_status_payload(
            sink_mode="dual_apple",
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
        ),
    )
    _patch_ring_coupled_box(monkeypatch, tmp_path, active_endpoint=True)
    r = doctor.check_outputd_service()
    assert r.status == "ok", r.detail
    assert "backend=alsa" in r.detail
    assert "dual_a_pcm=hw:CARD=A,DEV=0" in r.detail
    assert "dual_linked=True" in r.detail


def test_outputd_service_fails_on_fake_backend(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(backend="fake"),
    )
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert "backend='fake'" in r.detail


def test_outputd_service_fails_on_small_runtime_buffers(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(dac_buffer_frames=1024),
    )
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert "dac.buffer_frames=1024" in r.detail


def test_outputd_service_fails_when_reference_contract_missing(monkeypatch):
    payload = json.loads(_outputd_status_payload().decode())
    payload["reference_outputs"] = {}
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "speaker_reference_source" in r.detail


def _outputd_aec_clock_payload(
    *,
    chip_ref_pcm: str | None = "plughw:CARD=Array,DEV=0",
    aec_clock: dict | None = None,
    chip_ref_active: bool = True,
) -> bytes:
    """An outputd STATUS payload whose reference_outputs carries a chip-ref
    and (optionally) an aec_clock block — the surface check_aec_clock_drift
    reads."""
    payload = json.loads(_outputd_status_payload().decode())
    payload["reference_outputs"]["chip_ref_pcm"] = chip_ref_pcm
    if chip_ref_pcm is not None:
        payload["reference_outputs"]["chip_ref_writer"] = {
            "desired": True,
            "enabled": chip_ref_active,
            "active": chip_ref_active,
            "status": "active" if chip_ref_active else "degraded",
            "open_error_count": 1 if not chip_ref_active else 0,
            "retry_count": 3 if not chip_ref_active else 1,
        }
    if aec_clock is not None:
        payload["reference_outputs"]["aec_clock"] = aec_clock
    return json.dumps(payload).encode()


def _aec_clock_block(*, verdict: str, status: str, ppm, observe: bool = False) -> dict:
    return {
        "chip_ref_sro_ppm": ppm,
        "sro_estimator_status": status,
        "verdict": verdict,
        "verdict_reason": f"{verdict}/{status}",
        "observe": observe,
        "latency": {
            "dac_presentation_ms": 21.3,
            "playback_queue_ms": 64.0,
            "chip_ref_queue_ms": 80.0,
        },
    }


def test_aec_clock_drift_ok_when_coherent(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(verdict="coherent", status="locked", ppm=1.2)
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "verdict=coherent" in r.detail
    assert "chip_ref_sro_ppm=1.2" in r.detail
    assert "observe=False" in r.detail
    assert "playback_queue_ms=64.0" in r.detail


def test_aec_clock_drift_surfaces_observe_mode(monkeypatch):
    """Chip-ref observe mode (writer armed purely to MEASURE drift on the
    software-AEC3 path) is healthy and surfaced in the detail so an operator
    can tell why the chip-ref writer is running."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(
                verdict="compensable", status="locked", ppm=42.0, observe=True
            )
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "observe=True" in r.detail


def test_aec_clock_drift_ok_when_compensable(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(verdict="compensable", status="locked", ppm=42.0)
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "verdict=compensable" in r.detail
    assert "chip_ref_sro_ppm=42.0" in r.detail


def test_aec_clock_drift_warns_when_untrusted(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(verdict="fallback", status="untrusted", ppm=None)
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "warn"
    assert "cannot be trusted" in r.detail
    assert "sro_estimator_status=untrusted" in r.detail


def test_aec_clock_drift_ok_while_observing(monkeypatch):
    """The initial lock window (status=observing, which maps to a fallback
    verdict) is healthy, not a warning — warning there would cry wolf on every
    boot before the estimator has enough samples."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(verdict="fallback", status="observing", ppm=None)
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "sro_estimator_status=observing" in r.detail


def test_aec_clock_drift_skips_when_chip_ref_not_configured(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(chip_ref_pcm=None),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "skipped" in r.detail
    assert "chip reference not configured" in r.detail


def test_aec_clock_drift_warns_when_optional_chip_reference_is_unavailable(
    monkeypatch,
):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            chip_ref_active=False,
            aec_clock=_aec_clock_block(
                verdict="fallback", status="observing", ppm=None
            ),
        ),
    )

    r = doctor.check_aec_clock_drift()

    assert r.status == "warn"
    assert "desired but unavailable" in r.detail
    assert "speaker playback remains active" in r.detail
    assert "retries=3" in r.detail


def test_aec_clock_drift_skips_on_pre_layer0_build(monkeypatch):
    """A chip-ref is present but the outputd build has no aec_clock block."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(aec_clock=None),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "skipped" in r.detail
    assert "predates aec_clock" in r.detail


def test_aec_clock_drift_skips_when_outputd_disabled(monkeypatch):
    _patch_fanin_systemctl(monkeypatch, enabled="disabled")
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "skipped" in r.detail
    assert "not enabled" in r.detail


def test_fanin_asound_wiring_fails_on_bare_renderer_lane(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            'slave {\n        pcm "hw:Loopback,0,1"\n        rate 48000\n        channels 2\n        format S16_LE\n    }',
            'slave.pcm "hw:Loopback,0,1"',
        ),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "shairport_substream" in r.detail


def test_fanin_asound_wiring_fails_on_legacy_renderer_dmix(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND + "\npcm.jasper_renderer_mix {\n    type dmix\n}\n",
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "legacy renderer dmix" in r.detail


def test_fanin_asound_wiring_warns_on_stale_topology_env(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND,
        tmp_path,
        stale_topology_env=True,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "warn"
    assert "stale" in r.detail


# ---------------------------------------------------------------------------
# check_fanin_tts_drops — dropped TTS audio at the pending budget means the
# user heard garbled/"fast-forward" replies (the 2026-06-11 JTS3 incident).
# ---------------------------------------------------------------------------


def _fanin_payload_with_tts(tts: dict) -> bytes:
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = tts
    return json.dumps(payload).encode()


def test_check_fanin_tts_drops_ok_when_counters_zero(monkeypatch):
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_payload_with_tts(
            {
                "enabled": True,
                "pending_frames": 0,
                "budget_frames": 96000,
                "dropped_commands": 0,
                "dropped_audio_frames": 0,
            }
        ),
    )
    r = doctor.check_fanin_tts_drops()
    assert r.status == "ok"
    assert "none since fan-in start" in r.detail


def test_check_fanin_tts_drops_warns_with_seconds_and_hint(monkeypatch):
    # 82 dropped commands / 523200 frames ≈ 10.9 s at 48 kHz — the real
    # incident's order of magnitude.
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_payload_with_tts(
            {
                "enabled": True,
                "pending_frames": 89216,
                "budget_frames": 96000,
                "dropped_commands": 82,
                "dropped_audio_frames": 523200,
            }
        ),
    )
    r = doctor.check_fanin_tts_drops()
    assert r.status == "warn"
    assert "82 audio command(s)" in r.detail
    assert "~10.9s" in r.detail
    assert "tts_command_dropped" in r.detail  # journalctl breadcrumb


def test_check_fanin_tts_drops_ok_when_lane_disabled(monkeypatch):
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_payload_with_tts({"enabled": False}),
    )
    r = doctor.check_fanin_tts_drops()
    assert r.status == "ok"
    assert "disabled" in r.detail


def test_check_fanin_tts_drops_ok_when_status_unreachable(monkeypatch):
    # Reachability is the 'jasper-fanin service' check's job; this check
    # must not double-report a down daemon.
    monkeypatch.setattr(
        doctor.socket,
        "socket",
        lambda *a, **kw: _FakeSocket(error=OSError("connection refused")),
    )
    r = doctor.check_fanin_tts_drops()
    assert r.status == "ok"
    assert "jasper-fanin service" in r.detail


# ---------------------------------------------------------------------------
# check_fanin_ring_stall — a live fan-in→CamillaDSP ring stall (issue #1524):
# the SHM ring is full and CamillaDSP is not draining it. Skip-if-loopback.
# ---------------------------------------------------------------------------


def _fanin_payload_with_ring(ring: dict | None) -> bytes:
    """A shm_ring-transport fan-in STATUS payload with (or without) a ring block."""
    payload = json.loads(_fanin_status_payload(transport="shm_ring").decode())
    if ring is not None:
        payload["output"]["ring"] = ring
    return json.dumps(payload).encode()


def test_check_fanin_ring_stall_ok_when_status_has_no_ring_block(monkeypatch):
    # A STATUS-shape guard, not a topology branch: with no counters there is
    # nothing to assess, and the missing block is the 'jasper-fanin service'
    # check's failure to report.
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload(ring=None))
    r = doctor.check_fanin_ring_stall()
    assert r.status == "ok"


def test_check_fanin_ring_stall_ok_when_draining(monkeypatch):
    # shm_ring, ring present, no active stall → ok with the un-folded counts.
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_payload_with_ring(
            {
                "path": "/dev/shm/jts-ring/program.ring",
                "slots": 8,
                "occupancy": 2,
                "published": 123456,
                "full_waits": 0,
                "stuck_reader_drops": 0,
                "drop_no_reader": 0,
                "stall_active": False,
                "last_stall_ms": 0,
            }
        ),
    )
    r = doctor.check_fanin_ring_stall()
    assert r.status == "ok"
    assert "no active stall" in r.detail
    assert "stuck_reader_drops=0" in r.detail


def test_check_fanin_ring_stall_warns_when_active(monkeypatch):
    # A live stall (stall_active) → warn, with the stuck/no-reader split and the
    # journalctl breadcrumb.
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_payload_with_ring(
            {
                "path": "/dev/shm/jts-ring/program.ring",
                "slots": 8,
                "occupancy": 8,
                "published": 500,
                "full_waits": 32,
                "stuck_reader_drops": 375,
                "drop_no_reader": 0,
                "stall_active": True,
                "last_stall_ms": 4200,
            }
        ),
    )
    r = doctor.check_fanin_ring_stall()
    assert r.status == "warn"
    assert "CURRENTLY active" in r.detail
    assert "stuck_reader_drops=375" in r.detail
    assert "last_stall_ms=4200" in r.detail
    assert "event=fanin.ring.stall" in r.detail  # journalctl breadcrumb


def test_check_fanin_ring_stall_ok_when_status_unreachable(monkeypatch):
    # Reachability is the 'jasper-fanin service' check's job; this check must
    # not double-report a down daemon.
    monkeypatch.setattr(
        doctor.socket,
        "socket",
        lambda *a, **kw: _FakeSocket(error=OSError("connection refused")),
    )
    r = doctor.check_fanin_ring_stall()
    assert r.status == "ok"
    assert "jasper-fanin service" in r.detail


# ---- renderer ring lanes: the unarmed fleet default is healthy ----------


def test_unarmed_renderer_lanes_report_ok(monkeypatch):
    """An unarmed box (the fleet default) is healthy, not failing.

    The doctor's status vocabulary is ok|warn|fail (CheckResult.status),
    and render() maps anything else to a red X through its else-branch.
    The first ship of this check returned a novel "skip" for the unarmed
    branch, which made EVERY unarmed box render a failure and exit 1
    (AGENTS.md: "Returns 0 if all critical checks pass"). Unarmed/
    unconfigured-is-ok is the established doctor convention —
    check_chip_reference (this same domain), Spotify auth, the capture
    relay, and Google integrations all return ok + a skipped-style detail.
    """
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(rl, "read_armed_labels", lambda *a, **kw: ())
    result = doctor.audio_runtime.check_renderer_ring_lanes()
    assert result.status == "ok"
    assert "no renderer lane armed" in result.detail
    assert "fleet default" in result.detail


def test_unarmed_renderer_lanes_exit_zero_through_render(monkeypatch, capsys):
    """The fleet-wide repro, inverted into a guard: drive the unarmed
    result through the doctor's REAL render/exit path and require exit 0.
    A status outside the vocabulary reaches render()'s else-branch and
    becomes exit 1 — exactly how the "skip" ship failed — so this pin
    breaks on the defect CLASS, not just today's literal."""
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(rl, "read_armed_labels", lambda *a, **kw: ())
    result = doctor.audio_runtime.check_renderer_ring_lanes()
    exit_code = doctor.render([result])
    capsys.readouterr()  # swallow render()'s printed report
    assert exit_code == 0


def _resting_ring_entry(label):
    """An armed lane's STATUS entry: attached, never fed since attach."""
    from jasper.fanin.status import FANIN_INPUT_SOURCE_RING

    return {
        "label": label,
        "source": FANIN_INPUT_SOURCE_RING,
        "frames_read": 0,
        "ring": {
            "attached": True,
            "startup_empty_reads": 40,
            "empty_reads": 0,
            "writer_alive": False,
            "occupancy": 0,
            "epoch_resets": 0,
        },
    }


def test_armed_on_demand_lane_resting_state_is_healthy(monkeypatch):
    """An armed correction lane that has never been fed is RESTING, not
    broken (U3/P6c-ii): its writers are ephemeral measurement spawns, so
    between measurements the ring sits attached with only startup empty
    reads — the same "not a fault" class as a paused renderer. The
    never-fed WARN diagnoses a DAEMON renderer that failed to reopen its
    ring after an arm; applying it to an on-demand lane would put a
    standing false warning on every armed box.

    The detail must also CARRY the carve-out's stated residual: resting
    and broken-at-open are byte-identical to this check, so the healthy
    line must hint at the unproven writer path rather than implying it
    was verified (an operator reading "ok" as "the lane works" would be
    over-reading — the hint is what keeps the reading honest)."""
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(rl, "read_armed_labels", lambda *a, **kw: ("correction",))
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_read_status_socket",
        lambda _path: {"inputs": [_resting_ring_entry("correction")]},
    )
    result = doctor.audio_runtime.check_renderer_ring_lanes()
    assert result.status == "ok"
    assert "on-demand" in result.detail
    assert "no measurement played yet" in result.detail
    assert "a writer that cannot open looks identical here" in result.detail
    assert "run a measurement to confirm" in result.detail


def test_armed_daemon_lane_never_fed_still_warns(monkeypatch):
    """Control for the on-demand carve-out: a unit-ful lane in the same
    never-fed state keeps the WARN and its restart-the-unit remedy — the
    carve-out is keyed on the lane's writers, not applied lane-wide."""
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(
        rl, "read_armed_labels", lambda *a, **kw: ("spotify", "correction")
    )
    monkeypatch.setattr(
        doctor.audio_runtime,
        "_read_status_socket",
        lambda _path: {
            "inputs": [
                _resting_ring_entry("spotify"),
                _resting_ring_entry("correction"),
            ]
        },
    )
    result = doctor.audio_runtime.check_renderer_ring_lanes()
    assert result.status == "warn"
    assert "spotify" in result.detail and "NEVER FED" in result.detail
    assert "librespot.service" in result.detail
    assert "correction:" not in result.detail  # no correction problem entry


# ===========================================================================
# check_aloop_registered_substreams — the snd-aloop remnant guard (#2285 P9-C)
#
# Every OPEN snd-aloop substream must have a purpose registered by one of the
# constants that own the pair allocation; anything holding a substream with no
# such purpose (pair 5, whose PCM definitions P9-C deleted) is a FAIL that
# names the offender. The guard measures fan-in's lanes and outputd's passive
# content lane only: grouping's bonded ingress rides jasper.multiroom
# .grouping_ring's SHM ring and declares no aloop pair at all.
# ===========================================================================

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODPROBE_CONF = _REPO_ROOT / "deploy" / "modprobe.d" / "snd-aloop.conf"
_ASOUNDRC_JASPER = _REPO_ROOT / "deploy" / "alsa" / "asoundrc.jasper"

_OPEN_STATUS = (
    "state: RUNNING\n"
    "owner_pid   : 4242\n"
    "trigger_time: 10741.879940354\n"
    "tstamp      : 0.000000000\n"
    "delay       : 320\n"
)


def _make_card(tmp_path: Path, open_subs: dict[str, list[int]] | None = None) -> Path:
    """Build a synthetic /proc/asound tree with an snd-aloop card.

    ``open_subs`` maps a pcm dir (``pcm0p``/``pcm1c``/…) to the substream
    indices that should read as open; every other substream reads ``closed``,
    exactly as the kernel prints it.
    """
    open_subs = open_subs or {}
    root = tmp_path / "asound"
    card = root / audio_runtime._ALOOP_CARD_ID
    for pcm_dir in audio_runtime._ALOOP_PCM_DIRS:
        for pair in range(audio_runtime._ALOOP_SUBSTREAMS):
            sub = card / pcm_dir / f"sub{pair}"
            sub.mkdir(parents=True)
            is_open = pair in open_subs.get(pcm_dir, [])
            (sub / "status").write_text(
                _OPEN_STATUS if is_open else "closed\n", encoding="utf-8"
            )
    return root


@pytest.fixture()
def proc_root(monkeypatch, tmp_path):
    def _set(root: Path) -> None:
        monkeypatch.setenv(audio_runtime._ALOOP_PROC_ROOT_ENV, str(root))

    return _set


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

def test_no_aloop_card_is_ok(proc_root, tmp_path):
    """A box without snd-aloop has no remnant to police."""
    empty = tmp_path / "asound"
    empty.mkdir()
    proc_root(empty)
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "ok"
    assert "not loaded" in result.detail


def test_all_closed_is_ok(proc_root, tmp_path):
    proc_root(_make_card(tmp_path))
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "ok"
    assert "no other pair currently open" in result.detail
    # The remnant's size is REPORTED, not merely asserted — risk 5.1 in the
    # design is "the remnant becomes permanent by silence".
    assert "aloop remnant on pair 6" in result.detail
    # …and it names the lane that still owns the pair, not a closed issue
    # number (see test_check_text_names_the_remnants_remaining_owner).
    assert "passive content lane" in result.detail


def test_registered_open_pair_is_ok_jts4_shape(proc_root, tmp_path):
    """THE FALSE-POSITIVE REGRESSION GUARD.

    Observed on jts4, 2026-08-14: /proc/asound/Loopback/pcm1c/sub3 in
    `state: RUNNING`, owner cgroup jasper-fanin.service — the usbsink lane's
    idle-read fallback documented in deploy/modprobe.d/snd-aloop.conf. That
    box is HEALTHY. If this check ever fails it, the check is wrong.
    """
    proc_root(_make_card(tmp_path, {"pcm1c": [3]}))
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "ok"
    assert "[3]" in result.detail


def test_content_pair_open_is_ok(proc_root, tmp_path):
    """A box holding pair 6 is the remnant working as designed — that pair is
    outputd's passive content lane."""
    proc_root(_make_card(tmp_path, {"pcm0p": [6], "pcm1c": [6]}))
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "ok"
    # Pair 6 IS the content pair, so it is never one of the "other" pairs.
    assert "no other pair currently open" in result.detail


@pytest.mark.parametrize("pcm_dir", ["pcm0p", "pcm0c", "pcm1p", "pcm1c"])
def test_positive_control_foreign_substream_fails(proc_root, tmp_path, pcm_dir):
    """POSITIVE CONTROL — a deliberately-opened foreign substream trips it.

    Pair 5 is the foreign one: P9-C deleted its PCM definitions, so a holder
    there resurrected a deleted lane. Parametrised across all four PCM
    directions so a walker that only scanned the playback side would fail this.
    """
    proc_root(_make_card(tmp_path, {pcm_dir: [5]}))
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "fail"
    assert f"{pcm_dir}/sub5" in result.detail
    assert "no registered purpose" in result.detail


def test_positive_control_names_the_offender(proc_root, tmp_path):
    """The FAIL names the offender — the design asks for pid/process."""
    proc_root(_make_card(tmp_path, {"pcm0p": [5]}))
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "fail"
    assert "pid=4242" in result.detail
    # And it tells the operator what to do about it.
    assert "deploy-to-pi.sh" in result.detail


def test_offender_detail_is_bounded(proc_root, tmp_path, monkeypatch):
    """A pathological box cannot produce an unbounded doctor line.

    The registered set is shrunk to the content pair alone, so every other pair
    reads as an offender and the offender count (28) genuinely exceeds the cap.
    An earlier version of this test opened only pair 5 across the four PCM dirs,
    giving exactly 4 offenders against a cap of 4; `[:cap]` and `[:]` were then
    indistinguishable and the bound was asserted but never proven.
    """
    monkeypatch.setattr(
        audio_runtime,
        "_derive_registered_pairs",
        lambda: ({6: "outputd passive content lane"}, 6),
    )
    proc_root(
        _make_card(
            tmp_path,
            {
                pcm: [p for p in range(audio_runtime._ALOOP_SUBSTREAMS) if p != 6]
                for pcm in audio_runtime._ALOOP_PCM_DIRS
            },
        )
    )
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "fail"
    cap = audio_runtime._ALOOP_OFFENDER_DETAIL_CAP
    shown = result.detail.count("/sub")
    assert shown <= cap, f"detail listed {shown} offenders, cap is {cap}"
    # The truncation is DISCLOSED, not silent — an operator must not think
    # four offenders is the whole story.
    assert "more)" in result.detail


def test_unreadable_proc_is_warn_not_fail(proc_root, tmp_path):
    """FAIL-SOFT: 'I could not look' must never become 'something is wrong'.

    Every status path is made a DIRECTORY, so read_text raises IsADirectoryError
    (an OSError) without needing permission games that behave differently under
    root in CI.
    """
    root = tmp_path / "asound"
    card = root / audio_runtime._ALOOP_CARD_ID
    for pcm_dir in audio_runtime._ALOOP_PCM_DIRS:
        for pair in range(audio_runtime._ALOOP_SUBSTREAMS):
            (card / pcm_dir / f"sub{pair}" / "status").mkdir(parents=True)
    proc_root(root)
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "warn"
    assert "could not be verified" in result.detail


def test_missing_substreams_are_not_evidence(proc_root, tmp_path):
    """A narrower snd-aloop is not a fault — absence is not an offender."""
    root = tmp_path / "asound"
    card = root / audio_runtime._ALOOP_CARD_ID
    sub = card / "pcm0p" / "sub0"
    sub.mkdir(parents=True)
    (sub / "status").write_text("closed\n", encoding="utf-8")
    proc_root(root)
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "ok"


def test_check_never_raises_on_hostile_status(proc_root, tmp_path):
    """A garbage/empty status file must not crash the doctor."""
    root = _make_card(tmp_path)
    card = root / audio_runtime._ALOOP_CARD_ID
    (card / "pcm0p" / "sub0" / "status").write_text("", encoding="utf-8")
    (card / "pcm0p" / "sub1" / "status").write_text(
        "\x00\xff garbage", encoding="utf-8", errors="replace"
    )
    proc_root(root)
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status in {"ok", "warn", "fail"}


# --------------------------------------------------------------------------
# Contract pins: an owning constant must not name a pair the module never
# creates, so a `pcm_substreams` reduction fails here instead of on a speaker
# whose lane silently has no substream to open. That pin lives over the DERIVED
# set (`test_registered_pairs_are_within_the_module_range` below), which reads
# every owner of the allocation. It once had a second, narrower copy over the
# grouping round-trip's own two constants; those are gone — the bonded ingress
# rides `jasper.multiroom.grouping_ring`'s SHM ring and names no aloop pair.
# --------------------------------------------------------------------------

def _modprobe_substreams() -> int:
    text = _MODPROBE_CONF.read_text(encoding="utf-8")
    options = [
        line for line in text.splitlines()
        if line.strip().startswith("options snd-aloop")
    ]
    assert len(options) == 1, f"expected one options line, got {options!r}"
    m = re.search(r"pcm_substreams=(\d+)", options[0])
    assert m, f"no pcm_substreams= in {options[0]!r}"
    return int(m.group(1))


def test_walker_range_matches_modprobe_pcm_substreams():
    """The walker must scan exactly the substreams the module creates."""
    assert audio_runtime._ALOOP_SUBSTREAMS == _modprobe_substreams()


def test_outputd_content_aloop_pcm_matches_asoundrc_slave():
    """`_OUTPUTD_CONTENT_ALOOP_PCM` — pair 6's registration source since
    design §6.1(a) — must name the same PCM asoundrc.jasper's
    `pcm.outputd_content_playback` slave actually opens, or the doctor's
    derivation and the shipped ALSA config drift apart silently.
    """
    text = audio_runtime._asound_non_comment_text(
        _ASOUNDRC_JASPER.read_text(encoding="utf-8")
    )
    block = audio_runtime._asound_pcm_block(text, "outputd_content_playback")
    assert block is not None
    m = re.search(r'slave\s*\{\s*pcm\s+"([^"]+)"', block)
    assert m, f"no slave pcm found in outputd_content_playback block: {block!r}"
    assert m.group(1) == audio_runtime._OUTPUTD_CONTENT_ALOOP_PCM


# --------------------------------------------------------------------------
# Derivation — the registered set is READ from its owners, never restated
# --------------------------------------------------------------------------

#: The pairs that must be registered at this head, written as a LITERAL on
#: purpose. Deriving this expectation from the same constants the production
#: code derives from would make it move in lockstep with a regression — drop
#: pair 0 from `_FANIN_EXPECTED_ALOOP_INPUTS` and a derived expectation drops
#: it too, so nothing fails. A literal is what makes a source-constant
#: deletion detectable.
_EXPECTED_REGISTERED_PAIRS = (0, 1, 2, 3, 4, 6, 7)


def test_derived_set_matches_the_expected_allocation():
    """Dropping a pair from any owning constant changes this set.

    Also the re-sourcing pin (design §6.1a): pair 6 derives from
    `_OUTPUTD_CONTENT_ALOOP_PCM`, and `_EXPECTED_REGISTERED_PAIRS` is the same
    literal it was before that move — the registered set is byte-identical
    across it, and across the grouping ingress leaving snd-aloop entirely.

    The re-sourcing's negative control — breaking the grouping round-trip's own
    constant and watching the set stay identical — retired with the constant:
    the reconciler names no aloop pair at all now, so nothing in the grouping
    path can feed this derivation to begin with.
    """
    derived, content_pair = audio_runtime._derive_registered_pairs()
    assert tuple(sorted(derived)) == _EXPECTED_REGISTERED_PAIRS
    assert content_pair == 6


@pytest.mark.parametrize("pair", _EXPECTED_REGISTERED_PAIRS)
def test_every_registered_pair_open_is_ok(proc_root, tmp_path, pair):
    """THE PER-ROW GUARD.

    One case per registered pair, each opening that pair and requiring `ok`.
    Dropping a pair from its owning constant makes that pair unregistered, and
    this test then FAILs it — which is the whole point: an unregistered pair
    that a real box holds open is a red doctor on healthy hardware.
    """
    proc_root(_make_card(tmp_path, {"pcm0p": [pair], "pcm1c": [pair]}))
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "ok", f"pair {pair} open read as {result.detail}"


def test_content_pair_is_always_registered():
    """Replaces the old 'content pair missing from the registry' warn branch.

    That branch existed because a hand-maintained table could drift from its
    source constant. The derived set inserts the content pair (pair 6) from
    `_OUTPUTD_CONTENT_ALOOP_PCM` in the same function that returns it, so the
    drift is now structurally impossible and the branch was deleted as
    unreachable. This is the invariant that replaced it.
    """
    derived, content_pair = audio_runtime._derive_registered_pairs()
    assert content_pair in derived


def test_derivation_is_all_or_nothing_on_a_bad_input(monkeypatch):
    """A partial derivation would SHRINK the set and red-doctor a healthy box,
    so an unparseable source must return None (-> warn), never a subset."""
    from jasper.cli.doctor import audio_runtime

    monkeypatch.setattr(
        audio_runtime,
        "_FANIN_EXPECTED_ALOOP_INPUTS",
        [("spotify", "hw:Loopback,1,0"), ("airplay", "not-a-pcm")],
    )
    assert audio_runtime._derive_registered_pairs() is None


def test_derivation_rejects_a_non_loopback_card(monkeypatch):
    from jasper.cli.doctor import audio_runtime

    monkeypatch.setattr(
        audio_runtime, "_FANIN_EXPECTED_OUTPUT_PCM", "hw:SomeOtherCard,0,7"
    )
    assert audio_runtime._derive_registered_pairs() is None


def test_unparseable_content_pcm_constant_is_warn(proc_root, tmp_path, monkeypatch):
    """Pair 6's source is `_OUTPUTD_CONTENT_ALOOP_PCM` now, not a grouping
    constant — an unparseable value there, not in reconcile.py, is what
    degrades the full check to warn."""
    from jasper.cli.doctor import audio_runtime

    monkeypatch.setattr(audio_runtime, "_OUTPUTD_CONTENT_ALOOP_PCM", "not-a-pcm")
    proc_root(_make_card(tmp_path))
    result = audio_runtime.check_aloop_registered_substreams()
    assert result.status == "warn"
    assert "could not derive" in result.detail


def test_pair_five_is_not_registered():
    """P9-C DELETED pair 5, so no owner names it any more; re-registering it
    from any owning constant fails here — the deletion is what the guard
    protects."""
    derived, _ = audio_runtime._derive_registered_pairs()

    assert 5 not in derived


def test_registered_pairs_are_within_the_module_range():
    substreams = _modprobe_substreams()
    derived, _ = audio_runtime._derive_registered_pairs()
    for pair in derived:
        assert 0 <= pair < substreams, (
            f"registered pair {pair} is outside pcm_substreams={substreams}"
        )


# ===========================================================================
# fan-in coupling drift + the capture-type parser
# ===========================================================================

_RAWFILE_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: RawFile
    channels: 2
    filename: "/run/jasper-fanin/camilla.pipe"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-outputd/content.pipe"
    format: S16_LE
filters:
"""

_ALSA_CFG = """\
devices:
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
filters:
"""

_ALSA_LOCAL_PIPE_CFG = _ALSA_CFG.replace(
    "/run/jasper-snapserver/snapfifo",
    "/run/jasper-outputd/content.pipe",
)


def test_capture_parser_reads_rawfile(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(_RAWFILE_CFG)
    assert audio_runtime._loaded_capture_type(cfg) == "RawFile"
    assert audio_runtime._loaded_playback_type(cfg) == "File"
    assert (
        audio_runtime._loaded_playback_filename(cfg)
        == "/run/jasper-outputd/content.pipe"
    )


def test_capture_parser_reads_alsa_not_playback_file(tmp_path):
    # The playback File sink must NOT be misread as the capture type.
    cfg = tmp_path / "c.yml"
    cfg.write_text(_ALSA_CFG)
    assert audio_runtime._loaded_capture_type(cfg) == "Alsa"


def test_capture_parser_none_when_absent(tmp_path):
    assert audio_runtime._loaded_capture_type(tmp_path / "missing.yml") is None
    cfg = tmp_path / "c.yml"
    cfg.write_text("filters:\n  x: 1\n")
    assert audio_runtime._loaded_capture_type(cfg) is None


def _run_check(monkeypatch, *, coupling, cfg_text, tmp_path, outputd_env_text=""):
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(outputd_env_text)
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_OUTPUTD_ENV_PATH", str(outputd_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: coupling,
    )
    # _active_camilla_config_path returns (statefile, active_config_path|None) —
    # mock the REAL tuple shape (a str-only mock masked a production TypeError).
    monkeypatch.setattr(
        audio_runtime, "_active_camilla_config_path", lambda: (cfg.parent, str(cfg))
    )
    return audio_runtime.check_fanin_coupling()


def test_check_ok_when_loopback_matches_alsa(monkeypatch, tmp_path):
    res = _run_check(monkeypatch, coupling="loopback", cfg_text=_ALSA_CFG, tmp_path=tmp_path)
    assert res.status == "ok"


def test_check_warns_on_loopback_capture_with_stale_local_file_playback(
    monkeypatch, tmp_path
):
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_ALSA_LOCAL_PIPE_CFG,
        tmp_path=tmp_path,
    )

    assert res.status == "warn"
    assert "non-Snapcast File sink" in res.detail


def test_check_warns_on_dangerous_drift_loopback_intent_rawfile_loaded(
    monkeypatch, tmp_path
):
    # The crash-loop precursor: a retired persisted token with a RawFile config
    # live. The remedy names the ONE transport, never the intent it just read —
    # an intent this box has to be converged OFF, not re-applied.
    res = _run_check(monkeypatch, coupling="loopback", cfg_text=_RAWFILE_CFG, tmp_path=tmp_path)
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile shm_ring" in res.detail


def test_check_ok_when_no_loaded_capture(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch, coupling="loopback", cfg_text="filters:\n", tmp_path=tmp_path
    )
    assert res.status == "ok"


# --- check_fanin_coupling_value: persisted coupling value must be recognized --


def test_check_fanin_coupling_value_warns_on_removed_transport_pipe(monkeypatch, tmp_path):
    # A migrating box carrying the REMOVED transport_pipe coupling (or a typo) must
    # surface a warn until the reconciler converges it to loopback.
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("JASPER_FANIN_CAMILLA_COUPLING=transport_pipe\n")
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    res = audio_runtime.check_fanin_coupling_value()
    assert res.status == "warn"
    assert "transport_pipe" in res.detail
    assert "removed" in res.detail


def test_check_fanin_coupling_value_ok_on_recognized_coupling(monkeypatch, tmp_path):
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    res = audio_runtime.check_fanin_coupling_value()
    assert res.status == "ok"


# --- shm_ring coherence (Ring A + Ring B, P2) --------------------------------

_RING_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "jts_ring_capture"
    format: S16_LE
  playback:
    type: Alsa
    channels: 2
    device: "jts_ring_playback"
    format: S16_LE
filters:
"""

_RING_BRIDGE_ENV = "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"


def test_ring_ok_when_both_ends_ring_and_bridge_matches(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch,
        coupling="shm_ring",
        cfg_text=_RING_CFG,
        tmp_path=tmp_path,
        outputd_env_text=_RING_BRIDGE_ENV,
    )
    assert res.status == "ok"
    assert "jts_ring_capture" in res.detail and "jts_ring_playback" in res.detail


def test_ring_warns_on_partial_flip_bridge_missing(monkeypatch, tmp_path):
    # shm_ring intent but outputd bridge is direct -> partial flip warning.
    res = _run_check(
        monkeypatch,
        coupling="shm_ring",
        cfg_text=_RING_CFG,
        tmp_path=tmp_path,
        outputd_env_text="",  # bridge defaults to direct
    )
    assert res.status == "warn"
    assert "PARTIAL" in res.detail or "shm_ring" in res.detail


def test_loopback_warns_on_stale_ring_bridge(monkeypatch, tmp_path):
    # loopback intent but a stale shm_ring bridge remains -> partial flip warning.
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_ALSA_CFG,
        tmp_path=tmp_path,
        outputd_env_text=_RING_BRIDGE_ENV,
    )
    assert res.status == "warn"
    assert "stale" in res.detail.lower() and "shm_ring" in res.detail


def test_ring_warns_when_loaded_graph_reverted_to_loopback(monkeypatch, tmp_path):
    # THE finding-5 revert: env pair is coherent but the loaded config is the
    # loopback graph (a camilla restart re-seeded it) -> warn to re-arm.
    res = _run_check(
        monkeypatch,
        coupling="shm_ring",
        cfg_text=_ALSA_CFG,  # loopback capture device, NOT jts_ring_capture
        tmp_path=tmp_path,
        outputd_env_text=_RING_BRIDGE_ENV,
    )
    assert res.status == "warn"
    assert "ring config" in res.detail or "jts_ring" in res.detail


def test_loopback_warns_on_stale_ring_graph_with_clean_env(monkeypatch, tmp_path):
    # SF5 (the disarm-direction mirror of finding-5): a disarm's camilla step
    # FAILED, so the env pair reads clean (loopback intent, bridge=direct — the
    # earlier stale-bridge check does NOT fire) but the LOADED graph still names the
    # ring ioplug devices. CamillaDSP then captures a writer-dead Ring A (zero-fill
    # silence) while the box reads doctor-GREEN on a type-only capture==Alsa check.
    # The device-name check must catch this.
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_RING_CFG,  # stale ring devices, but capture.type is Alsa
        tmp_path=tmp_path,
        outputd_env_text="",  # bridge=direct -> env pair coherent with loopback
    )
    assert res.status == "warn"
    assert "ring ioplug device" in res.detail
    assert "jts_ring_capture" in res.detail
    assert "jasper-fanin-coupling-reconcile --auto" in res.detail


def test_loopback_ok_when_loaded_graph_is_plain_alsa(monkeypatch, tmp_path):
    # The guard must not false-positive: a clean loopback box (plug:jasper_capture,
    # snapfifo playback) stays OK.
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_ALSA_CFG,
        tmp_path=tmp_path,
        outputd_env_text="",
    )
    assert res.status == "ok"


# --- D-list survey finding 1 / wide-output-path PR-1: playback format check --
# Before this check, nothing read the CamillaDSP playback format back off a
# live config — a half-flip (emitter regenerated against one
# DEFAULT_PLAYBACK_FORMAT while the loaded file reflects another) was silent.

_S16_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
    format: S16_LE
filters:
"""

_S32_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
    format: S32_LE
filters:
"""


def _run_format_check(monkeypatch, tmp_path, cfg_text):
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
    monkeypatch.setattr(
        audio_runtime, "_active_camilla_config_path", lambda: (cfg.parent, str(cfg))
    )
    return audio_runtime.check_camilla_playback_format()


def test_playback_format_ok_when_alsa_lane_matches_the_wide_default(
    monkeypatch, tmp_path
):
    # Green on a flipped box: the ALSA content lane carries
    # DEFAULT_PLAYBACK_FORMAT, S32_LE since PR-6.
    res = _run_format_check(monkeypatch, tmp_path, _S32_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S32_LE" in res.detail


def test_playback_format_fails_on_a_half_flipped_narrow_alsa_lane(
    monkeypatch, tmp_path
):
    # Prove the check CAN STILL FAIL in the post-flip world (mutation rule): an
    # ALSA lane config left at S16_LE after the flip is a half-flipped box — a
    # stale generated file, or an emitter that regenerated against a different
    # constant — and on the raw active lane it is what makes outputd's open fail
    # rather than convert. Red doctor line instead of silence.
    res = _run_format_check(monkeypatch, tmp_path, _S16_PLAYBACK_CFG)
    assert res.status == "fail"
    assert "S16_LE" in res.detail
    assert "S32_LE" in res.detail
    assert "DEFAULT_PLAYBACK_FORMAT" in res.detail


def test_playback_format_ok_when_no_config_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        audio_runtime, "_active_camilla_config_path", lambda: (tmp_path, None)
    )
    res = audio_runtime.check_camilla_playback_format()
    assert res.status == "ok"


def test_playback_format_ok_when_config_has_no_format_field(monkeypatch, tmp_path):
    res = _run_format_check(monkeypatch, tmp_path, "filters:\n")
    assert res.status == "ok"


# --- NIT1 (PR-1 gate review): the check is lane-aware, keyed on playback type -

_S16_FILE_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
    format: S16_LE
filters:
"""

_S32_FILE_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
    format: S32_LE
filters:
"""


_S16_RING_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "jts_ring_capture"
    format: S16_LE
  playback:
    type: Alsa
    channels: 2
    device: "jts_ring_playback"
    format: S16_LE
filters:
"""

_S32_RING_PLAYBACK_CFG = _S16_RING_PLAYBACK_CFG.replace(
    '    device: "jts_ring_playback"\n    format: S16_LE',
    '    device: "jts_ring_playback"\n    format: S32_LE',
)


def _pin_ring_wire_narrow(monkeypatch, tmp_path):
    """Pin this box's ring wire to the NARROW token via the operator lever.

    ``JASPER_FANIN_RING_WIRE_FORMAT`` is the only way a box declares S16_LE
    since the resolver's default went WIDE (PR #2601) — nothing else in the
    repo writes it (``jasper.fanin_coupling.RING_WIRE_FORMAT_ENV_VAR``).
    Isolated to a tmp ``fanin.env``, the FIRST file the resolver's chain reads,
    so the pin neither leaks from nor needs the developer host's real
    ``/var/lib/jasper/fanin.env``.
    """
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR

    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=S16_LE\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )


def test_playback_format_ok_for_an_armed_ring_pinned_narrow_on_an_otherwise_wide_box(
    monkeypatch, tmp_path
):
    """AN ARMED RING IS ``type: Alsa`` — the File split alone does NOT cover it.

    Its width comes from resolve_ring_wire through the coupling's own kwargs (the
    PR-6 ring ruling), so a ring config at the ring's OWN resolved width is
    HEALTHY even when the general lane wants something else. Keyed
    on the ring's playback device, this must be green; keyed only on the File
    type, it red-lines every armed-ring box — including the certified-latency USB
    box, whose canary criterion is literally "doctor green" — with a remediation
    that regenerates the identical config.

    SINCE THE RING-WIRE DEFAULT FLIP, an UNDECLARED box's ring resolves S32_LE
    too — the same value as ``DEFAULT_PLAYBACK_FORMAT`` — so the two lanes no
    longer differ for free the way they did when narrow was the resolver's
    default. ``_pin_ring_wire_narrow`` declares the operator lever
    (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE``) so the ring resolves narrow
    while the general (loopback) lane stays wide, recreating the
    two-lanes-can-legitimately-differ shape this test exists to prove.
    """
    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT
    from jasper.fanin_coupling import RING_PLAYBACK_DEVICE, resolve_ring_wire

    _pin_ring_wire_narrow(monkeypatch, tmp_path)
    assert resolve_ring_wire().sample_format != DEFAULT_PLAYBACK_FORMAT
    assert RING_PLAYBACK_DEVICE in _S16_RING_PLAYBACK_CFG
    res = _run_format_check(monkeypatch, tmp_path, _S16_RING_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S16_LE" in res.detail
    assert "resolve_ring_wire" in res.detail


def test_playback_format_fails_on_a_ring_config_that_drifted_wide(
    monkeypatch, tmp_path
):
    """The ring split must not become "any ring device auto-passes": a config
    declaring a width the box's resolved ring wire does not carry is a genuinely
    broken box and stays red — even though S32 is what the loopback lane wants
    (and, since the ring-wire default flip, what an UNDECLARED box's ring
    resolves to as well), which is exactly the confusion the three-way split
    has to get right.

    This is the check that has to catch it, because the ring LAYOUT accepts both
    S16LE and S32LE: a config drifted to the other one is inside the accept-set,
    so the attach would not refuse it — the ends would simply be built to
    different widths.

    ``_pin_ring_wire_narrow`` declares this box's ring wire S16_LE (the
    operator lever) so the S32_LE config below is a genuine drift again — on an
    undeclared box the same config would simply match the new default and there
    would be nothing here to catch.
    """
    _pin_ring_wire_narrow(monkeypatch, tmp_path)
    res = _run_format_check(monkeypatch, tmp_path, _S32_RING_PLAYBACK_CFG)
    assert res.status == "fail"
    assert "S32_LE" in res.detail
    assert "S16_LE" in res.detail
    assert "resolve_ring_wire" in res.detail


def test_playback_format_ok_for_file_sink_pinned_narrow_while_the_lane_is_wide(
    monkeypatch, tmp_path
):
    # The bonded-leader pipe sink (and the active-speaker parked graph's
    # /dev/null sink) are pinned to DEFAULT_PIPE_SINK_FORMAT independently of
    # the general program lane (D4), so a File-type S16 config stays green while
    # the ALSA lane is S32 — the two constants now genuinely differ, no
    # monkeypatch needed. Without the lane split this would red-line every
    # healthy pipe-sink leader and parked box.
    from jasper.camilla_config_contract import (
        DEFAULT_PIPE_SINK_FORMAT,
        DEFAULT_PLAYBACK_FORMAT,
    )

    assert DEFAULT_PIPE_SINK_FORMAT != DEFAULT_PLAYBACK_FORMAT
    res = _run_format_check(monkeypatch, tmp_path, _S16_FILE_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S16_LE" in res.detail


def test_playback_format_fails_on_a_deliberately_wide_file_sink_config(
    monkeypatch, tmp_path
):
    # The lane split must not become "any File type auto-passes": a File
    # sink whose format has genuinely drifted off DEFAULT_PIPE_SINK_FORMAT
    # (S32 here) still fails — even though S32 is what the ALSA lane wants,
    # which is exactly the confusion the lane split has to get right.
    res = _run_format_check(monkeypatch, tmp_path, _S32_FILE_PLAYBACK_CFG)
    assert res.status == "fail"
    assert "S32_LE" in res.detail
    assert "S16_LE" in res.detail
    assert "DEFAULT_PIPE_SINK_FORMAT" in res.detail


_STALE_RING_CFG = """\
devices:
  capture:
    type: Alsa
    channels: 2
    device: "jts_ring_capture"
  playback:
    type: Alsa
    channels: 2
    device: "jts_ring_active_playback"
filters:
"""


def test_no_doctor_remedy_names_a_coupling_the_cli_rejects():
    """THE CLASS, not the three instances below.

    Eight of this module's remedies named `jasper-fanin-coupling-reconcile
    loopback` — a coupling ADR-0100 removed from the CLI's `choices`, so an
    operator who copied one got `exit 2` and an argparse error instead of a fix.
    A dead remedy is worse than no remedy: it spends the reader's trust in the
    rest of the line.

    DERIVED FROM BOTH VOCABULARIES, never from a list here. The tokens to judge
    are the coupling names `jasper.fanin_coupling` still spells (so the RETIRED
    one is judged, which is the whole point, and a future token is judged the
    day it is added); the verdict is the reconciler's OWN argparse. English that
    merely names the command in a sentence carries no coupling token and is not
    judged.
    """
    import re
    from pathlib import Path

    import jasper.fanin_coupling as fc
    import jasper.fanin.coupling_reconcile as cr

    couplings = {
        value
        for name, value in vars(fc).items()
        if name.startswith("COUPLING_") and isinstance(value, str)
    }
    assert "loopback" in couplings, (
        "the retired token stopped being spelled — this pin no longer judges it"
    )

    source = Path(audio_runtime.__file__).read_text(encoding="utf-8")
    # Same source line only: a remedy split across lines puts its verb on the
    # next one, and a comment that merely names the command carries none at all.
    named = {
        m.group(1)
        for m in re.finditer(
            r"jasper-fanin-coupling-reconcile[^\S\n]+([A-Za-z_][\w-]*)", source
        )
    } & couplings

    for token in sorted(named):
        accepted = True
        try:
            cr.main([token, "--help"])
        except SystemExit as exc:
            # 0 = --help printed (the token parsed); 2 = argparse rejected it.
            accepted = exc.code == 0
        assert accepted, (
            f"the doctor prints `jasper-fanin-coupling-reconcile {token}`, which "
            "the CLI rejects"
        )


def test_stale_ring_devices_under_loopback_send_a_roleful_box_up_the_ladder(
    monkeypatch, tmp_path
):
    """A ROLEFUL box gets the ARM LADDER, not a reconcile that converges nothing.

    This is the state PR #2514's residual describes: a ring-endpoint graph
    loaded while the persisted coupling still names the retired token. The
    check's remedy used to be `jasper-fanin-coupling-reconcile loopback`
    unconditionally — which on a roleful box moved nothing and reported SUCCESS.
    So the operator ran a command, was told it worked, and the warn stayed.

    The graph has to be moved by step 1 of the ladder. Asserted through the
    classification-free half of the message — the command spelling — because
    that is what an operator copies.

    #2285 P2 turned the ladder around. It was the ROLLBACK ladder
    (`baseline-reemit --endpoint aloop` -> reconcile -> `…-reconcile loopback`);
    both of those rungs are argparse errors now and the destination was the PARK
    for a roleful box, so the remedy converges FORWARD onto the ring instead.
    Both halves are asserted — the ring rungs present, the retired ones absent —
    so a partial re-point cannot pass.
    """
    monkeypatch.setattr(
        audio_runtime, "_requires_roleful_graph", lambda: True)
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_STALE_RING_CFG,
        tmp_path=tmp_path,
    )
    assert res.status == "warn"
    assert "jts_ring_active_playback" in res.detail
    assert "baseline-reemit --endpoint ring" in res.detail, res.detail
    assert "jasper-audio-hardware-reconcile" in res.detail, res.detail
    assert "jasper-fanin-coupling-reconcile shm_ring" in res.detail, res.detail
    # The retired rungs are asserted ABSENT, not merely unasserted: a remedy
    # naming a command argparse rejects, or naming the park as a destination,
    # is worse than no remedy at all.
    assert "--endpoint aloop" not in res.detail, res.detail
    assert "jasper-fanin-coupling-reconcile loopback" not in res.detail, res.detail


def test_stale_ring_devices_under_loopback_keep_the_plain_remedy_when_passive(
    monkeypatch, tmp_path
):
    """CONTROL: a PASSIVE box keeps exactly the one-command remedy.

    Without this the assertion above would also pass if the ladder text had been
    appended unconditionally — and on a passive box the reconciler's unattended
    pass genuinely is the whole fix, so sending one up a three-rung
    active-speaker ladder would be worse advice, not more of it.
    """
    monkeypatch.setattr(
        audio_runtime, "_requires_roleful_graph", lambda: False)
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_STALE_RING_CFG,
        tmp_path=tmp_path,
    )
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile --auto" in res.detail
    assert "baseline-reemit" not in res.detail, res.detail


# ===========================================================================
# check_ring_conf_floor_render
#
# Compares two facts, each read from its owner: the active DAC profile's
# DECLARED LatencyFloor (the DAC registry) and the period_frames the ring
# conf.d pins (the file). The ring slot IS one outputd DAC period, so a box
# whose DAC declares a floor should have that floor rendered into its conf.d
# by jasper-audio-hardware-reconcile. A DAC with no declared floor is ok by
# RULE, not by luck — the shipped conf.d default stands.
# ===========================================================================

SHIPPED_RING_CONF = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
)

# A registered profile that declares NO LatencyFloor, so the no-floor branches
# below exercise the real registry rather than a synthetic id. Asserted rather
# than assumed: declaring a floor for this profile must fail THIS line, not
# silently turn the two no-floor tests into vacuous passes against a branch
# they no longer reach. (That is exactly what a declared DAC8x floor did to
# them in R7a, when they were written against `hifiberry_dac8x`; the guard has
# now caught it a SECOND time, when the InnoMaker HiFi AMP Pro declared jts4's
# measured floor and this moved to the DAC8x Studio.)
NO_FLOOR_DAC_ID = HIFIBERRY_DAC8X_STUDIO_ID
assert latency_floor_for(NO_FLOOR_DAC_ID) is None, (
    f"{NO_FLOOR_DAC_ID} now declares a latency floor; pick another floorless "
    "profile for the no-floor doctor branches"
)


def _stage_floor_conf(monkeypatch, tmp_path, *, dac_id, conf_text=None):
    conf = tmp_path / "60-jts-ring.conf"
    if conf_text is None:
        conf.write_bytes(SHIPPED_RING_CONF.read_bytes())
    else:
        conf.write_text(conf_text, encoding="utf-8")
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(
        audio_runtime, "_active_audio_dac_id", lambda: dac_id)
    return conf


def _conf_text(period_frames):
    return (
        f"pcm.jts_ring_capture {{\n    period_frames {period_frames}\n"
        "    n_slots 2\n}\n"
        f"pcm.jts_ring_playback {{\n    period_frames {period_frames}\n"
        "    n_slots 2\n}\n"
    )


def _synthetic_floor(period_frames):
    return LatencyFloor(
        camilla_chunksize=256,
        camilla_target_level=1536,
        outputd_period_frames=period_frames,
        outputd_dac_buffer_frames=4 * period_frames,
    )


def test_ok_when_the_dac_declares_no_floor(monkeypatch, tmp_path):
    # State 1: nothing to render — the shipped default stands by rule.
    _stage_floor_conf(monkeypatch, tmp_path, dac_id=NO_FLOOR_DAC_ID)

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "no latency floor" in result.detail
    assert "shipped default" in result.detail


def test_ok_when_the_conf_matches_the_declared_floor(monkeypatch, tmp_path):
    # State 2: the golden Apple case — the declared floor IS the shipped 128.
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "128" in result.detail
    assert "apple_usb_c_dongle" in result.detail


def test_ok_when_the_floor_exceeds_the_fixed_ring_slot(monkeypatch, tmp_path):
    # State 4, the product boundary: Ring A's slot is fan-in's COMPILE-TIME
    # RING_SLOT_FRAMES, so a DAC whose floor is not exactly that never gets a
    # rendered conf.d — shm_ring is simply unavailable on it until #2147.
    # That is a documented boundary, NOT drift, so it reports ok.
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    monkeypatch.setattr(
        audio_runtime,
        "latency_floor_for",
        lambda _id: _synthetic_floor(2 * RING_SLOT_FRAMES),
    )
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="hifiberry_dac8x")

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "ok"
    # Honest: names both numbers, says what is unavailable and what still works.
    assert str(2 * RING_SLOT_FRAMES) in result.detail
    assert str(RING_SLOT_FRAMES) in result.detail
    assert "shm_ring" in result.detail
    assert "loopback" in result.detail.lower()
    assert "#2147" in result.detail


def test_warns_when_the_conf_diverges_from_the_declared_floor(
    monkeypatch, tmp_path
):
    # State 5, real drift: the Apple dongle's floor IS renderable (it equals
    # RING_SLOT_FRAMES) but this box's conf.d was never rendered to it. Warn,
    # not fail — the conf.d is inert unless shm_ring is armed, and the coupling
    # reconciler independently fail-closes to loopback on this mismatch.
    _stage_floor_conf(
        monkeypatch,
        tmp_path,
        dac_id="apple_usb_c_dongle",
        conf_text=_conf_text(1024),
    )

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "warn"
    # Names BOTH numbers and the remedy, not a bare "mismatch".
    assert "128" in result.detail
    assert "1024" in result.detail
    assert "jasper-audio-hardware-reconcile" in result.detail


@pytest.mark.parametrize(
    "conf_text",
    [
        # Torn: the two PCMs disagree, so there is no single geometry.
        "pcm.jts_ring_capture {\n    period_frames 128\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 1024\n}\n",
        # No period_frames line at all.
        "pcm.jts_ring_capture { type jts_ring }\n",
    ],
)
def test_warns_when_the_conf_period_is_indeterminate(
    monkeypatch, tmp_path, conf_text
):
    _stage_floor_conf(
        monkeypatch, tmp_path, dac_id="apple_usb_c_dongle", conf_text=conf_text
    )

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "no single period_frames" in result.detail
    assert "redeploy" in result.detail


def test_warns_when_the_conf_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(tmp_path / "missing.conf"))
    monkeypatch.setattr(
        audio_runtime, "_active_audio_dac_id", lambda: "apple_usb_c_dongle")

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "absent or torn" in result.detail


def test_check_is_registered_in_the_audio_doctor_group():
    from jasper.cli.doctor import audio as audio_group

    assert (
        audio_group.check_ring_conf_floor_render
        is audio_runtime.check_ring_conf_floor_render
    )
    assert "check_ring_conf_floor_render" in audio_group.__all__


# --- #2294: a floor-blocked box must SAY it cannot ring -----------------------


def test_no_floor_detail_names_both_routes_to_a_ring(monkeypatch, tmp_path):
    """`ok` is right, silence was not (issue #2294) — and neither was the old text.

    A DAC with no declared floor leaves outputd on its PACKAGED default period,
    which is not the fixed ring slot, so the conf.d has nothing to render. The
    conf.d is correct either way — hence `ok` — but the household's actual
    question is "why is this box on loopback?", and before #2294 the answer
    appeared on no surface.

    The answer it then gave was wrong. It said shm_ring was "unavailable on this
    box", which is a claim about outputd's RESOLVED period, and this check reads
    only the DECLARED floor. The two diverge through the operator seam: jts4
    (InnoMaker HiFi AMP Pro, floorless at the time) armed shm_ring on 2026-08-14
    with JASPER_OUTPUTD_PERIOD_FRAMES / _DAC_BUFFER_FRAMES hand-set in
    /etc/jasper/jasper.env. So the detail must name BOTH routes to a ring and
    claim no impossibility.
    """
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_PERIOD_FRAMES
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    _stage_floor_conf(monkeypatch, tmp_path, dac_id=NO_FLOOR_DAC_ID)

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert str(DEFAULT_OUTPUTD_PERIOD_FRAMES) in result.detail
    assert str(RING_SLOT_FRAMES) in result.detail
    assert "#2147" in result.detail
    # Route 1: the operator seam, named with the file that carries it.
    assert "JASPER_OUTPUTD_PERIOD_FRAMES" in result.detail
    assert "/etc/jasper/jasper.env" in result.detail
    # Route 2: a declared floor.
    assert "floor" in result.detail
    # And NO impossibility claim, on a check that cannot see the resolved period.
    assert "unavailable" not in result.detail


def test_a_floor_above_the_slot_does_not_claim_ineligibility(monkeypatch, tmp_path):
    """The same falsified claim lived in the floor-exceeds-slot branch.

    A DAC declaring, say, a 256-frame floor gets no rendered conf.d either — but
    an operator JASPER_OUTPUTD_PERIOD_FRAMES still outranks that floor and can
    bring the resolved period back to the slot, exactly as it did on jts4. So
    this branch may say the conf.d is not rendered and name the seam; it may not
    say the ring is unavailable.
    """
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    monkeypatch.setattr(
        audio_runtime,
        "latency_floor_for",
        lambda _id: _synthetic_floor(2 * RING_SLOT_FRAMES),
    )
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="hifiberry_dac8x")

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "unavailable" not in result.detail
    assert "JASPER_OUTPUTD_PERIOD_FRAMES" in result.detail


def test_a_matching_floor_does_not_claim_ineligibility(monkeypatch, tmp_path):
    # The mirror of the above: a box that CAN ring must not be told it cannot.
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    result = audio_runtime.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "unavailable" not in result.detail


# --- `_requires_roleful_graph` fail-soft DIRECTION ----------------------------


@pytest.mark.parametrize(
    "exc",
    [
        # The real-world case first. It subclasses ValueError, so the bare
        # ValueError below is the same except-arm — named separately because a
        # reader should not have to know the hierarchy to see it is covered.
        OutputTopologyError("topology has an unsupported shape"),
        OSError("topology unreadable"),
        ValueError("topology malformed"),
    ],
)
def test_an_unreadable_topology_fails_soft_to_not_roleful(monkeypatch, tmp_path, exc):
    """The documented direction, pinned — an unreadable topology asserts NOTHING.

    ``_requires_roleful_graph`` only ever SOFTENS a message or adds an
    eligibility sentence; it gates nothing, and every caller that acts on
    rolefulness reads the fail-CLOSED loaders instead. So its ``except`` arm must
    return False: a box whose topology cannot be read must keep the generic
    wording rather than be told, on no evidence, that it is an active-crossover
    box whose ring needs a proven graph before it can be armed. Failing soft to
    True would print a
    remediation ladder at every box with a torn topology file.

    Asserted at BOTH surfaces — the helper's own answer and the detail string it
    feeds — so flipping the arm cannot pass by only breaking the private half.
    """
    import jasper.output_topology as output_topology

    def _raise(*_a, **_kw):
        raise exc

    monkeypatch.setattr(output_topology, "load_output_topology_strict", _raise)
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    assert audio_runtime._requires_roleful_graph() is False
    assert "ROLEFUL" not in audio_runtime.check_ring_conf_floor_render().detail


def test_a_roleful_topology_is_reported_roleful(monkeypatch, tmp_path):
    """The other direction, so the fail-soft pin cannot be satisfied by a stub.

    Without this, ``return False`` unconditionally would pass the test above and
    the helper would be pinned to a constant. This runs the real classifier over
    a real roleful topology.
    """
    import jasper.output_topology as output_topology
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("mono", "active_2_way")
    monkeypatch.setattr(
        output_topology, "load_output_topology_strict", lambda *a, **kw: topology
    )
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    assert audio_runtime._requires_roleful_graph() is True
    assert "ROLEFUL" in audio_runtime.check_ring_conf_floor_render().detail


# ===========================================================================
# check_ring_geometry_coherence
#
# Ring-A geometry must agree across three axes when shm_ring is armed:
# fan-in's resolved JASPER_FANIN_RING_SLOTS through the systemd env chain, the
# conf.d jts_ring_capture, and the on-disk program.ring header. Against the
# header it compares every axis the ioplug attach compares (n_slots,
# period_frames, sample_format, channels); a mismatch on any of them is the
# 2026-07-05 crash-loop class (hw_params EINVAL + ioplug attach_fatal).
# ===========================================================================

def _write_conf(tmp_path, *, capture_n_slots=2):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        f"pcm.jts_ring_capture {{\n    period_frames 128\n    n_slots {capture_n_slots}\n}}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    return conf


def _write_ring(
    path,
    *,
    n_slots=2,
    period_frames=128,
    magic=0x4A52_494E,
    rate=48000,
    channels=2,
    sample_format=1,  # SAMPLE_FORMAT_S16LE
):
    hdr = bytearray(128)
    struct.pack_into("<I", hdr, 0, magic)
    struct.pack_into("<I", hdr, 4, 1)  # version
    struct.pack_into("<I", hdr, 8, rate)
    struct.pack_into("<I", hdr, 12, channels)
    struct.pack_into("<I", hdr, 16, sample_format)
    struct.pack_into("<I", hdr, 20, period_frames)
    struct.pack_into("<I", hdr, 24, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)


def _arm(monkeypatch):
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )


def _stage_ring_geometry(
    monkeypatch,
    tmp_path,
    *,
    fanin_env_text="",
    jasper_env_text="",
    capture_n_slots=2,
):
    conf = _write_conf(tmp_path, capture_n_slots=capture_n_slots)
    fanin_env = tmp_path / "fanin.env"
    jasper_env = tmp_path / "jasper.env"
    fanin_env.write_text(fanin_env_text, encoding="utf-8")
    jasper_env.write_text(jasper_env_text, encoding="utf-8")
    program = tmp_path / "program.ring"
    monkeypatch.setattr(audio_runtime.ring_assets, "RING_CONF_D", str(conf))
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio_runtime.ring_assets, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.JASPER_ENV_PATH", str(jasper_env)
    )
    return fanin_env, program


def test_skips_cleanly_when_not_armed(monkeypatch, tmp_path):
    # Default coupling (loopback) — the ring is inert, so a "mismatch" is not a live
    # defect. Must report ok/skip regardless of env or conf.d values.
    _stage_ring_geometry(monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=8\n")
    # No _arm(): read_persisted_coupling resolves loopback from the (empty) file.
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "ok"
    assert "skipped" in res.detail
    assert "not armed" in res.detail


def test_ok_when_all_three_axes_agree(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "n_slots=2" in res.detail


def test_ok_detail_reports_the_on_disk_wire(monkeypatch, tmp_path):
    """The ok detail names the wire it compared, not just the slot geometry.

    A reader that can see the whole header should say what it saw — otherwise
    "the Python layer can see the wire" is true only inside tests.
    """
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "S16_LE/2ch" in res.detail
    assert "48000 Hz" in res.detail


@pytest.mark.parametrize(
    ("kwargs", "axis"),
    [
        ({"sample_format": 2}, "sample_format"),
        ({"channels": 6}, "channels"),
    ],
)
def test_fail_when_the_on_disk_wire_shears(monkeypatch, tmp_path, kwargs, axis):
    """THE HOLE R5b CLOSED, and this test used to assert it stayed open.

    Its earlier form wrote a 44.1 kHz / 6-channel / S32_LE header — a wire the
    fleet does not run — and asserted ``ok``, because the wire axes were
    REPORTED and compared nowhere. That is exactly the silent hole: a ring file
    whose slots and period match while its format or channel count does not
    passed every Python guard, and the first symptom would have been CamillaDSP
    failing the ioplug attach and crash-looping. The inversion is deliberate.

    ``rate`` is NOT in the parametrize list: the conf.d declares no rate, so
    there is no expected value to compare against and the comparator skips that
    axis rather than guessing. It stays reported.
    """
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, **kwargs)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "fail", res.detail
    assert axis in res.detail
    assert "jasper-fanin-coupling-reconcile" in res.detail


def test_fail_when_env_disagrees_with_conf(monkeypatch, tmp_path):
    # Default migration class: stale JASPER_FANIN_RING_SLOTS=8 vs conf.d's 2.
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=8)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "2" in res.detail and "8" in res.detail
    assert "crash-loop" in res.detail.lower()


def test_fail_when_base_env_disagrees_with_conf(monkeypatch, tmp_path):
    # The effective env chain is /etc/jasper/jasper.env, then fanin.env. A stale
    # base-env value still controls the next fan-in start when fanin.env has no
    # override, so doctor must not report the product default.
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(
        monkeypatch,
        tmp_path,
        jasper_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=8)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "2" in res.detail and "8" in res.detail
    assert "crash-loop" in res.detail.lower()


def test_ok_when_fanin_env_overrides_stale_base_env(monkeypatch, tmp_path):
    # Later systemd EnvironmentFile wins. The reconciler's fix writes this exact
    # fanin.env override to neutralize stale /etc/jasper/jasper.env residue.
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(
        monkeypatch,
        tmp_path,
        fanin_env_text="JASPER_FANIN_RING_SLOTS=2\n",
        jasper_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=2)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "n_slots=2" in res.detail


def test_fail_when_on_disk_ring_disagrees(monkeypatch, tmp_path):
    # env + conf.d agree (both 2), but a stale on-disk ring carries 8 slots — the
    # ioplug attach still fails. Caught as the third axis.
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, n_slots=8)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "on-disk" in res.detail.lower()
    assert "2" in res.detail and "8" in res.detail


def test_fail_when_on_disk_ring_period_disagrees(monkeypatch, tmp_path):
    # Nit-7: env + conf.d + on-disk n_slots all agree (2), but the on-disk ring's
    # period_frames is stale (256 vs conf.d 128). The ioplug attach still fails on
    # the SECOND geometry axis — the slot-only check would miss it. Caught now.
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, period_frames=256)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "period_frames" in res.detail
    assert "256" in res.detail and "128" in res.detail


def test_ok_when_slots_and_period_both_agree(monkeypatch, tmp_path):
    # The positive: n_slots AND period_frames coherent across env + conf.d + on-disk.
    _arm(monkeypatch)
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, period_frames=128)
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "period_frames=128" in res.detail


def test_warn_when_ring_file_absent_but_env_conf_agree(monkeypatch, tmp_path):
    # Armed, env + conf.d agree, but no valid on-disk ring yet (fan-in restarting).
    # Not a hard failure — the next writer create will be coherent.
    _arm(monkeypatch)
    _stage_ring_geometry(monkeypatch, tmp_path)
    # No _write_ring — the program.ring path does not exist.
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "warn"
    assert "no valid ring header" in res.detail.lower()


def test_fail_when_env_value_invalid(monkeypatch, tmp_path):
    # An out-of-range JASPER_FANIN_RING_SLOTS is a hard failure while armed.
    _arm(monkeypatch)
    _stage_ring_geometry(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=99\n",
    )
    res = audio_runtime.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "invalid" in res.detail.lower()


# --- the outputd buffer-health check's RING-WIRE branches --------------------
#
# `_outputd_buffer_health` validates the shm_ring content geometry and then
# compares the wire outputd ATTACHED to (published in its top-level `shm_ring`
# block) against the wire this box's resolver answers. Those are two independent
# sources, so a disagreement is a real shear — outputd reading a geometry nobody
# declared — and it is a `fail`, not a detail line. Both axes are pinned because
# they fail for different reasons and print different remedies.


def _outputd_ring_status(*, fmt="S16_LE", channels=2, period=128, slots=2):
    """A STATUS payload for an ATTACHED shm_ring outputd, per rust state.rs."""
    return {
        "content": {
            "source": "shm_ring",
            "buffer_frames": period,
            "ring": {
                "slots": slots,
                "slot_frames": period,
                "capacity_frames": slots * period,
            },
        },
        "shm_ring": {
            "enabled": True,
            "attached": True,
            "slots": slots,
            "format": fmt,
            "channels": channels,
            "occupancy": 1,
        },
    }


def _buffer_health(data, *, period=128):
    return audio_runtime._outputd_buffer_health(
        data,
        data["content"],
        ring_mode=True,
        content_buffer=data["content"]["buffer_frames"],
        dac_buffer=period * 4,
        period_frames=period,
    )


def test_buffer_health_passes_when_the_attached_wire_matches():
    """POSITIVE CONTROL. On the box's default (undeclared) wire the comparison
    is silent, so the two failure pins below are proving a branch rather than a
    broken happy path.

    ``fmt="S32_LE"`` because the resolver's default went WIDE
    (``jasper.fanin_coupling.resolve_ring_wire_format``): an undeclared box —
    this test stubs neither the wire nor the env chain — now resolves S32_LE,
    not the C ioplug's compiled-in S16_LE.
    """
    result = _buffer_health(_outputd_ring_status(fmt="S32_LE"))
    assert isinstance(result, str), result
    assert "shm_ring_wire=S32_LE/2ch" in result
    assert "shm_ring_attached=True" in result


def test_buffer_health_fails_on_an_attached_format_the_box_does_not_declare():
    """outputd attached to a Ring B FORMAT nobody declared.

    S16_LE is the mismatching token now: the resolver's default went WIDE, so
    an undeclared box's wire IS S32_LE and S16_LE is what nobody declares.
    """
    result = _buffer_health(_outputd_ring_status(fmt="S16_LE"))
    assert not isinstance(result, str), "a wire shear must be a CheckResult, not detail"
    assert result.status == "fail"
    assert "shm_ring.format='S16_LE'" in result.detail
    assert "nobody declared" in result.detail
    # The remedy must name the reconcile that CLEARS the mismatched file — a bare
    # "redeploy" leaves the stale ring in place and the box parks again.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_buffer_health_fails_on_an_attached_channel_count_the_box_does_not_declare():
    """The channels axis has teeth independently of the format axis.

    ``fmt="S32_LE"`` pins the format to the box's default so that axis stays
    silent — an unpinned S16_LE default would (now) ALSO mismatch the format
    axis, and this test would no longer isolate the channels comparison.
    """
    result = _buffer_health(_outputd_ring_status(fmt="S32_LE", channels=6))
    assert not isinstance(result, str)
    assert result.status == "fail"
    assert "shm_ring.channels=6" in result.detail
    assert "nobody declared" in result.detail


def test_buffer_health_skips_the_wire_comparison_before_attach():
    """Only checked once ATTACHED.

    Before the attach outputd publishes its own DECLARATION, which proves
    nothing about a ring that does not exist yet. Comparing there would fail a
    box that is merely starting up — and it would do so with the shear remedy,
    sending an operator to clear a ring file that is not the problem.
    """
    data = _outputd_ring_status(fmt="S32_LE", channels=6)
    data["shm_ring"]["attached"] = False
    result = _buffer_health(data)
    assert isinstance(result, str), result
    assert "shm_ring_attached=False" in result


def test_buffer_health_resolves_the_wire_with_the_boxs_topology(monkeypatch):
    """The comparison asks the SAME question the reconciler's gates ask.

    ``ring_b_channels`` is the one per-topology axis in the wire, so resolving it
    without the topology answers the shipped stereo declaration and would report
    a shear on a box whose Ring B legitimately carries a different width — the
    doctor contradicting the reconciler that armed it.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.fanin.ring_health as rh
    import jasper.fanin_coupling as fc

    sentinel = object()
    monkeypatch.setattr(cr, "load_topology_for_wire", lambda: sentinel)
    monkeypatch.setattr(rh, "load_topology_for_wire", lambda: sentinel)
    seen: list[object] = []

    def _resolve(topology=None):
        seen.append(topology)
        return fc.RingWire(
            sample_format="S16_LE",
            ring_a_channels=2,
            ring_b_channels=6,
            period_frames=128,
        )

    monkeypatch.setattr(fc, "resolve_ring_wire", _resolve)
    # 6 channels is a SHEAR against the shipped resolver and a MATCH against this
    # topology-derived one, so a passing result proves the topology was threaded.
    result = _buffer_health(_outputd_ring_status(channels=6))
    assert seen == [sentinel], "the buffer-health wire must be topology-resolved"
    assert isinstance(result, str), result


# ===========================================================================
# check_ring_platform_assets
#
# The inert-phase contract: a MISSING asset is warn (loopback still carries
# audio), an INSTALLED-but-unusable ioplug is fail. The check never touches a
# live ring — the open probe is fully mocked.
# ===========================================================================

# A minimal but COMPLETE three-block conf.d: every PCM name resolves, and none
# declares `format`/`channels`, so the ioplug's own absent-key defaults
# (2ch/S16_LE) answer all three. Needed because the probe now reads its wire off
# THIS text (`_jts_ring_probe_wire` sources `ring_conf_format`/
# `ring_conf_channels`, not the resolver) — a stub declaring only
# `jts_ring_capture` left `jts_ring_playback` indeterminate.
_VALID_RING_CONF = (
    "pcm.jts_ring_capture {\n"
    "    type jts_ring\n"
    '    path "/dev/shm/jts-ring/program.ring"\n'
    "    period_frames 128\n"
    "    n_slots 2\n"
    "}\n"
    "\n"
    "pcm.jts_ring_playback {\n"
    "    type jts_ring\n"
    '    path "/dev/shm/jts-ring/content.ring"\n'
    "    period_frames 128\n"
    "    n_slots 2\n"
    "}\n"
    "\n"
    # The ACTIVE ring block. The probe walks every PCM in _JTS_RING_PCMS, so a
    # stub missing this one leaves it indeterminate exactly as a stub missing
    # jts_ring_playback used to.
    "pcm.jts_ring_active_playback {\n"
    "    type jts_ring\n"
    '    path "/dev/shm/jts-ring/active-content.ring"\n'
    "    period_frames 128\n"
    "    n_slots 2\n"
    "}\n"
)


def _stage_ring_conf(monkeypatch, tmp_path, text=_VALID_RING_CONF):
    """Point `_JTS_RING_CONF_D` at a tmp conf.d declaring every PCM block.

    Standalone helper for the probe-mechanics tests below, which don't stage
    the other P1 assets via `_stage_assets` but still need a readable conf.d
    now that the probe's wire lookup reads one.
    """
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(text, encoding="utf-8")
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(conf))
    return conf


def _stage_assets(monkeypatch, tmp_path, *, so=True, conf=True, shm=True):
    """Point the module constants at tmp paths and create/omit each asset."""
    plugin_dir = tmp_path / "alsa-lib"
    plugin_dir.mkdir()
    so_path = plugin_dir / audio_runtime._JTS_RING_IOPLUG_SO
    if so:
        so_path.write_bytes(b"\x7fELF fake so")
    conf_path = tmp_path / "60-jts-ring.conf"
    if conf:
        conf_path.write_text(_VALID_RING_CONF, encoding="utf-8")
    shm_dir = tmp_path / "jts-ring"
    if shm:
        shm_dir.mkdir()

    monkeypatch.setattr(
        audio_runtime, "_JTS_RING_ALSA_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(conf_path))
    monkeypatch.setattr(
        audio_runtime, "_JTS_RING_SHM_DIR", str(shm_dir))


# --- check_ring_platform_assets ---------------------------------------
#
# PRESENCE ONLY since ADR-0100: the ring is the only transport, so it is always
# load-bearing, there is no inert phase to open-probe, and a missing asset is a
# hard failure rather than a "loopback still carries audio" warn.


def _probe_must_not_run(monkeypatch):
    """Fail loudly if the check open-probes. It must never touch a live ring."""

    def _boom(pcm, tool):  # pragma: no cover - must never be called
        raise AssertionError("check_ring_platform_assets must not open-probe")

    monkeypatch.setattr(audio_runtime, "_jts_ring_pcm_resolves", _boom)


def test_ok_when_all_assets_present(monkeypatch, tmp_path):
    _stage_assets(monkeypatch, tmp_path)
    _probe_must_not_run(monkeypatch)
    res = audio_runtime.check_ring_platform_assets()
    assert res.status == "ok"


@pytest.mark.parametrize(
    "staged, expected_fragments",
    [
        ({"so": False}, ["ioplug .so absent", "redeploy"]),
        ({"conf": False}, ["conf.d absent", "redeploy"]),
        ({"shm": False}, ["absent", "redeploy"]),
        (
            {"so": False, "conf": False, "shm": False},
            ["ioplug .so absent", "conf.d absent", "redeploy"],
        ),
    ],
)
def test_any_missing_asset_is_a_hard_fail(
    monkeypatch, tmp_path, staged, expected_fragments
):
    """A missing asset FAILs whatever the persisted file says, and says redeploy.

    There is no second transport to degrade onto: the graph cannot resolve its
    ring devices at all, so there is no "loopback still carries audio" warn to
    fall back to. The verdict must not depend on read_persisted_coupling, so
    this never stubs it, and it must never open-probe a live ring.
    """
    _stage_assets(monkeypatch, tmp_path, **staged)
    _probe_must_not_run(monkeypatch)
    res = audio_runtime.check_ring_platform_assets()
    assert res.status == "fail"
    for fragment in expected_fragments:
        assert fragment in res.detail.lower() or fragment in res.detail


# --- _jts_ring_pcm_resolves (the open-probe helper) -------------------


def test_probe_ok_on_zero_exit(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(
        audio_runtime, "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ok, detail = audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is True and detail == "resolved"


def test_probe_reports_stderr_on_nonzero_exit(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(
        audio_runtime, "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(
            returncode=1, stdout="", stderr="ALSA lib: Unknown PCM jts_ring_playback"
        ),
    )
    ok, detail = audio_runtime._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert ok is False
    assert "Unknown PCM" in detail


def test_probe_fails_closed_when_tool_missing(monkeypatch):
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: None)
    ok, detail = audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is False
    assert "not found" in detail


def test_probe_fails_closed_when_conf_wire_is_indeterminate(monkeypatch, tmp_path):
    # No conf.d staged at all: `_JTS_RING_CONF_D` still points at its
    # production default, which is unreadable in the test environment. The
    # probe must refuse with a crisp reason rather than pass `None` to ALSA.
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(tmp_path / "missing.conf"))

    def _must_not_be_called(cmd, timeout=5.0):  # pragma: no cover - must never run
        raise AssertionError("probe ran with an indeterminate wire")

    monkeypatch.setattr(
        audio_runtime, "_run", _must_not_be_called)
    ok, detail = audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is False
    assert "indeterminate" in detail


def test_probe_reports_hang_on_timeout(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")

    def _timeout(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(
        audio_runtime, "_run", _timeout)
    ok, detail = audio_runtime._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert ok is False
    assert "hung" in detail


def test_probe_uses_devnull_for_capture_and_devzero_for_playback(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")
    seen = {}

    def _capture_cmd(cmd, timeout=5.0):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        audio_runtime, "_run", _capture_cmd)

    audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert seen["cmd"][0] == "arecord"
    assert seen["cmd"][-1] == "/dev/null"

    audio_runtime._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert seen["cmd"][0] == "aplay"
    assert seen["cmd"][-1] == "/dev/zero"


# --- residue cleanup (Finding 1: probe must not create a ring file) ---


def _probe_that_creates_the_ring(monkeypatch, tmp_path):
    """Repoint the SHM dir at tmp and make the mocked probe CREATE the ring
    file the ioplug's create-or-attach open would (O_CREAT|O_EXCL). Returns
    the ring Paths keyed by PCM name — one per block in _JTS_RING_PCMS."""
    shm_dir = tmp_path / "jts-ring"
    shm_dir.mkdir(exist_ok=True)  # _stage_assets may have created it already
    monkeypatch.setattr(
        audio_runtime, "_JTS_RING_SHM_DIR", str(shm_dir))
    # Re-stage the conf.d even when _stage_assets already did (same content,
    # so this is a no-op then): the probe now reads its wire off the conf.d
    # text, and the standalone callers below never call _stage_assets at all.
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")

    def _run_creates(cmd, timeout=5.0):
        # cmd == [tool, "-D", pcm, ...]; emulate the ioplug's O_CREAT|O_EXCL
        # open: create the ring only when absent, never truncate a file that
        # is already there (a live ring the real ioplug would attach to).
        pcm = cmd[2]
        ring = audio_runtime._jts_ring_path_for(pcm)
        if ring is not None and not os.path.exists(ring):
            open(ring, "wb").close()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        audio_runtime, "_run", _run_creates)
    # Derived from the module's own PCM table so a new ring cannot leave this
    # helper silently covering a subset of what the probe actually opens.
    return {
        pcm: shm_dir / basename for pcm, _tool, basename in audio_runtime._JTS_RING_PCMS
    }


def test_probe_unlinks_a_ring_it_created(monkeypatch, tmp_path):
    rings = _probe_that_creates_the_ring(monkeypatch, tmp_path)
    ok, detail = audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is True and detail == "resolved"
    # The probe created program.ring; residue cleanup must have removed it.
    assert not rings["jts_ring_capture"].exists(), (
        "probe left a ring file behind — violates P1 inertness"
    )


def test_full_check_leaves_no_ring_files(monkeypatch, tmp_path):
    # End-to-end: all assets present, the (mocked) open probe creates EVERY ring
    # file it opens, and after the check NONE of them exists on disk. Walking
    # the probe's own results rather than naming two files is what keeps this an
    # inertness proof for the whole set — including the ACTIVE ring, whose
    # accidental creation would poison the first real arm exactly as Ring A's
    # would.
    #
    # THREE is written out, not derived from the table under test. Comparing the
    # probe's coverage against `len(_JTS_RING_PCMS)` made the assertion
    # self-referential: deleting a ring from that table shrinks both sides
    # together and the test stays green while a shipped PCM goes unprobed. The
    # literal is the claim — a fourth ring must come here and say so.
    _stage_assets(monkeypatch, tmp_path)
    rings = _probe_that_creates_the_ring(monkeypatch, tmp_path)
    res = audio_runtime.check_ring_platform_assets()
    assert res.status == "ok", res.detail
    assert len(rings) == 3, (
        f"the ring conf.d ships three PCMs; the probe covered {sorted(rings)}"
    )
    for pcm, ring in rings.items():
        assert not ring.exists(), f"{pcm} left {ring} behind — violates inertness"


def test_probe_preserves_a_preexisting_live_ring(monkeypatch, tmp_path):
    # A live armed ring pre-exists. The probe (which here would EBUSY on real
    # hardware, but we mock a benign run) must NOT unlink it — only files the
    # probe itself created are removed.
    rings = _probe_that_creates_the_ring(monkeypatch, tmp_path)
    live = rings["jts_ring_capture"]
    live.write_bytes(b"live-armed-ring-magic")
    audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert live.exists(), "residue cleanup removed a pre-existing (live) ring"
    assert live.read_bytes() == b"live-armed-ring-magic"


def test_probe_unlinks_even_when_open_fails(monkeypatch, tmp_path):
    # An ioplug can create the ring FILE and then fail the open (nonzero exit).
    # The residue cleanup runs in a finally, so a failing probe still leaves
    # no ring file behind.
    shm_dir = tmp_path / "jts-ring"
    shm_dir.mkdir()
    monkeypatch.setattr(
        audio_runtime, "_JTS_RING_SHM_DIR", str(shm_dir))
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")
    ring = shm_dir / "program.ring"

    def _run_creates_then_fails(cmd, timeout=5.0):
        ring.write_bytes(b"half-baked")
        return SimpleNamespace(returncode=1, stdout="", stderr="some open error")

    monkeypatch.setattr(
        audio_runtime, "_run", _run_creates_then_fails)
    ok, _ = audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is False
    assert not ring.exists(), "residue left behind after a failed probe"


# --- The verdict is independent of the persisted token ----------------


def _arm_ring(monkeypatch):
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )


def test_a_missing_asset_fails_whatever_the_persisted_token_says(
    monkeypatch, tmp_path
):
    """The verdict does NOT turn on the coupling any more, and that is the fix.

    It used to: an armed box FAILED and an unarmed one merely WARNED "inert
    platform incomplete (loopback still active)". ADR-0100 retired that route,
    so on a box whose persisted token had not been rewritten yet the warn
    claimed a transport the box does not have — a speaker emitting nothing,
    reported as degraded-but-playing, which is exactly the reported-as-healthy
    case the transport parks exist to prevent.

    Both polarities are driven, because one alone would also pass against a
    check that still branched and happened to be tested on the branch that
    matches.
    """
    _stage_assets(monkeypatch, tmp_path, so=False)

    def _verdict(label):
        res = audio_runtime.check_ring_platform_assets()
        assert res.status == "fail", label
        assert "missing" in res.detail.lower(), label
        # The retired claim is asserted ABSENT: it is worse than silence.
        assert "loopback" not in res.detail.lower(), label
        assert "inert" not in res.detail.lower(), label

    _verdict("persisted token not rewritten yet")
    _arm_ring(monkeypatch)
    _verdict("persisted token names the ring")


# --- The open-probe asks for what the CONF.D DECLARES, never the resolver ----


def test_probe_sources_the_conf_declared_wire_not_the_resolver(monkeypatch, tmp_path):
    """The ioplug advertises EXACTLY the conf-declared format/channels as its
    hardware constraint, so the probe must read `ring_conf_format` /
    `ring_conf_channels` off THIS box's conf.d — never `resolve_ring_wire`.
    The two answer different questions ("what does the file say" vs. "what
    SHOULD this box declare") that are independently gated: conf rendering and
    ring-coupling arm are separate gates, so a box can carry a per-box-rendered
    Ring B conf.d while still sitting coupling-inert. Stage a conf.d rendered
    wide (S32_LE, Ring B 6ch) and make the resolver explode if touched; the
    probe argv must still follow the conf.d, per PCM."""
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        "pcm.jts_ring_capture {\n"
        "    type jts_ring\n"
        '    path "/dev/shm/jts-ring/program.ring"\n'
        "    period_frames 128\n"
        "    n_slots 2\n"
        "    format S32_LE\n"
        "}\n"
        "\n"
        "pcm.jts_ring_playback {\n"
        "    type jts_ring\n"
        '    path "/dev/shm/jts-ring/content.ring"\n'
        "    period_frames 128\n"
        "    n_slots 2\n"
        "    format S32_LE\n"
        "    channels 6\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio_runtime.shutil, "which", lambda t: f"/usr/bin/{t}")

    import jasper.fanin_coupling as fc

    def _must_not_be_called(*a, **k):  # pragma: no cover - must never run
        raise AssertionError(
            "the probe must never consult resolve_ring_wire — its answer is "
            "independently gated from what the conf.d actually declares"
        )

    monkeypatch.setattr(fc, "resolve_ring_wire", _must_not_be_called)

    seen = {}

    def _capture_cmd(cmd, timeout=5.0):
        seen[cmd[2]] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        audio_runtime, "_run", _capture_cmd)

    audio_runtime._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    audio_runtime._jts_ring_pcm_resolves("jts_ring_playback", "aplay")

    ring_a = seen["jts_ring_capture"]
    ring_b = seen["jts_ring_playback"]
    assert ring_a[ring_a.index("-f") + 1] == "S32_LE"
    assert ring_b[ring_b.index("-f") + 1] == "S32_LE"
    # Ring A declares no `channels` -> the ioplug default (2); Ring B's
    # explicit `channels 6` line must be honored.
    assert ring_a[ring_a.index("-c") + 1] == "2"
    assert ring_b[ring_b.index("-c") + 1] == "6"


def test_probe_asks_for_the_shipped_wire_today(monkeypatch, tmp_path):
    """On the REAL shipped (never-rendered) conf.d, the probe asks for 2
    channels / S32_LE.

    THE FORMAT AXIS NO LONGER REACHES THIS VIA DORMANCY. Every block now
    DECLARES ``format S32_LE`` EXPLICITLY (see
    ``deploy/alsa/conf.d/60-jts-ring.conf``'s own "WIRE FORMAT" header comment)
    — the probe reads a LITERAL in the file, not the ioplug's absent-key
    default. The shipped file changed to spell the token because the
    resolver's default went wide while the C ioplug's compiled-in default
    (mirrored by ``jasper.ring_assets.RING_CONF_DEFAULT_FORMAT``) stayed
    S16_LE: an omitted ``format`` key would now declare the OPPOSITE of what
    every other end of the ring resolves. That same disagreement is what makes
    the ioplug capability gate LIVE fleet-wide now (``ring_wire_caps_ready`` /
    ``ring_ioplug_wire_supported``) rather than dormant — see
    :data:`~jasper.ring_assets.RING_CONF_DEFAULT_FORMAT`'s own docstring.

    THE CHANNELS AXIS IS UNCHANGED: no block declares ``channels``, so it
    still answers via the ioplug's absent-key default (2), not a literal and
    not the resolver.

    Cross-checked against ``resolve_ring_wire``'s answer for the shipped
    topology, which still coincides today — both land on S32_LE/2ch/2ch now —
    a drift between the two independent policies would show up here as a
    failing cross-check, not as the probe's own source."""
    from jasper.fanin_coupling import resolve_ring_wire

    shipped = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
    )
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_bytes(shipped.read_bytes())
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(conf))

    wire = resolve_ring_wire()
    for pcm in ("jts_ring_capture", "jts_ring_playback"):
        channels, sample_format = audio_runtime._jts_ring_probe_wire(pcm)
        assert channels == 2
        assert sample_format == "S32_LE"
    assert (wire.sample_format, wire.ring_a_channels, wire.ring_b_channels) == (
        "S32_LE",
        2,
        2,
    )


# --- the ring writer-lock exclusivity guard (audio-graph consolidation #2285,
# --- P9-C). The C ioplug records the residual this closes verbatim: an flock's
# --- identity is the PATHNAME, not the inode, so unlinking `<ring>.writer.lock`
# --- while a writer holds it voids exclusivity SILENTLY and two live writers
# --- proceed with no log line between them. The guard reads the KERNEL's view
# --- (who holds an fd on the lock) because the shared header structurally
# --- cannot answer: `writer_pid` is a single slot a second attach overwrites.
#
# --- These build a synthetic /proc so the shapes are deterministic and the
# --- tests run on any host (macOS has no /proc at all). The same shapes were
# --- observed end-to-end against real processes on a real Linux box; the PR
# --- body carries that transcript.

_SHM = "/dev/shm/jts-ring"
_LOCK = f"{_SHM}/active-content.ring.writer.lock"


def _fake_proc(tmp_path, holders, *, unreadable_pids=()):
    """Build a /proc-shaped tree.

    `holders` maps pid -> list of fd targets (a target ending in " (deleted)"
    reproduces what the kernel shows for an fd on an unlinked file).
    """
    root = tmp_path / "proc"
    root.mkdir(parents=True)
    (root / "self").mkdir()  # a non-numeric entry, must be skipped
    for pid, targets in holders.items():
        fd_dir = root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        for i, target in enumerate(targets):
            os.symlink(target, fd_dir / str(i))
    for pid in unreadable_pids:
        fd_dir = root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        fd_dir.chmod(0o000)
    return root


def _run_guard(monkeypatch, root):
    monkeypatch.setattr(
        audio_runtime, "_PROC_ROOT", str(root))
    monkeypatch.setattr(
        audio_runtime, "_WRITER_LOCK_CONFIRM_DELAY_SEC", 0.0)
    return audio_runtime.check_ring_writer_lock_exclusivity()


def test_writer_lock_guard_ok_with_one_writer(monkeypatch, tmp_path):
    """NEGATIVE CONTROL: the normal armed box — one C writer holding one ring's
    writer lock, plus the Rust reader's mapping of the ring FILE itself — is
    `ok`. (Scanning maps for the ring file instead would call this a defect:
    the reader mmaps the same file by design.)"""
    root = _fake_proc(
        tmp_path,
        {
            41: [_LOCK, "/dev/null", f"{_SHM}/active-content.ring"],
            42: [f"{_SHM}/active-content.ring", "/dev/snd/pcmC0D0p"],
        },
    )

    result = _run_guard(monkeypatch, root)

    assert result.status == "ok"
    assert "no ring has more than one live writer" in result.detail


def test_writer_lock_guard_ok_when_nothing_holds_a_lock(monkeypatch, tmp_path):
    """An unarmed box holds no writer lock at all — `ok`, not a false alarm."""
    root = _fake_proc(tmp_path, {41: ["/dev/null"], 42: [f"{_SHM}/program.ring"]})

    result = _run_guard(monkeypatch, root)

    assert result.status == "ok"


def test_writer_lock_guard_fails_on_two_live_writers(monkeypatch, tmp_path):
    """POSITIVE CONTROL: the recorded residual. One incumbent holding the
    UNLINKED inode plus a fresh writer on a re-created file at the same
    pathname — two live writers, no log line between them — is `fail`, and the
    detail names both pids and which one is orphaned."""
    root = _fake_proc(
        tmp_path,
        {
            41: [f"{_LOCK} (deleted)"],
            42: [_LOCK],
        },
    )

    result = _run_guard(monkeypatch, root)

    assert result.status == "fail"
    assert "TWO LIVE WRITERS" in result.detail
    assert "pid 41 (lock file unlinked)" in result.detail
    assert "pid 42" in result.detail
    assert _LOCK in result.detail


def test_writer_lock_guard_fails_on_two_writers_without_an_unlink(
    monkeypatch, tmp_path
):
    """Two live holders of the SAME inode is equally a broken SPSC contract,
    so the guard keys on holder COUNT, not on the deleted marker alone."""
    root = _fake_proc(tmp_path, {41: [_LOCK], 42: [_LOCK]})

    result = _run_guard(monkeypatch, root)

    assert result.status == "fail"


def test_writer_lock_guard_ignores_a_contender_that_gave_up(monkeypatch, tmp_path):
    """`acquire_writer_lock` OPENS the lock file and only THEN spins on flock
    for up to JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS, so a healthy box legitimately
    shows two fd holders for up to that long. Only pids present in BOTH samples
    count: a contender that has gone by the confirm sample is not the defect."""
    first = _fake_proc(tmp_path, {41: [_LOCK], 42: [_LOCK]})
    second = _fake_proc(tmp_path / "after", {41: [_LOCK]})
    seen = []

    real = audio_runtime._ring_writer_lock_holders

    def sampling(**kwargs):
        seen.append(len(seen))
        root = first if len(seen) == 1 else second
        return real(proc_root=str(root), shm_dir=_SHM)

    monkeypatch.setattr(
        audio_runtime, "_ring_writer_lock_holders", sampling)
    monkeypatch.setattr(
        audio_runtime, "_PROC_ROOT", str(first))
    monkeypatch.setattr(
        audio_runtime, "_WRITER_LOCK_CONFIRM_DELAY_SEC", 0.0)

    result = audio_runtime.check_ring_writer_lock_exclusivity()

    assert len(seen) == 2, "a suspected two-writer read must be CONFIRMED"
    assert result.status == "ok"


def test_writer_lock_guard_warns_on_a_lone_orphaned_holder(monkeypatch, tmp_path):
    """One writer whose lock file was unlinked out from under it: exclusivity
    is ALREADY void (the next opener creates a fresh inode and is not
    excluded), so warn before the second writer arrives."""
    root = _fake_proc(tmp_path, {41: [f"{_LOCK} (deleted)"]})

    result = _run_guard(monkeypatch, root)

    assert result.status == "warn"
    assert "UNLINKED" in result.detail
    assert "pid 41" in result.detail


def test_writer_lock_guard_warns_when_proc_is_partially_unreadable(
    monkeypatch, tmp_path
):
    """A non-root sweep cannot read other users' /proc/<pid>/fd. That is a
    BLIND SPOT, so the guard says so rather than reporting a clean bill."""
    if os.geteuid() == 0:
        pytest.skip("root can read every /proc/<pid>/fd")
    root = _fake_proc(tmp_path, {41: ["/dev/null"]}, unreadable_pids=(77,))
    try:
        result = _run_guard(monkeypatch, root)
    finally:
        (root / "77" / "fd").chmod(0o755)

    assert result.status == "warn"
    assert "partially blind" in result.detail


def test_writer_lock_guard_ignores_locks_outside_the_ring_dir(monkeypatch, tmp_path):
    """Scoped to the ring tmpfs, and to the WRITER lock: some other subsystem's
    `.writer.lock`, and the ring's own `.open.lock` (a transaction lock BOTH
    C and Rust take), are none of this guard's business."""
    root = _fake_proc(
        tmp_path,
        {
            41: ["/var/lib/other/thing.writer.lock"],
            42: ["/var/lib/other/thing.writer.lock"],
            43: [f"{_SHM}/active-content.ring.open.lock"],
            44: [f"{_SHM}/active-content.ring.open.lock"],
        },
    )

    result = _run_guard(monkeypatch, root)

    assert result.status == "ok"


def test_writer_lock_guard_counts_one_pid_once(monkeypatch, tmp_path):
    """A single writer with several fds on one lock is still ONE writer."""
    root = _fake_proc(tmp_path, {41: [_LOCK, _LOCK, f"{_LOCK} (deleted)"]})

    result = _run_guard(monkeypatch, root)

    # Not a fail (one pid), but the unlinked fd still earns the warn.
    assert result.status == "warn"


# ===========================================================================
# check_active_ring_split_transport (#2285 P2, design §10.3)
#
# The state under test: the loaded CamillaDSP graph names the ACTIVE ring
# while the persisted coupling is loopback. Nothing consumes the ring, so the
# speaker is silent while every daemon is healthy. Deleting the aloop ACTIVE
# endpoint made that state QUIETER — it used to fail CamillaDSP's load and
# park loudly. Each conjunct is pinned separately: a two-term conjunction
# passes a single-conjunct test while still being wrong.
# ===========================================================================

SHM_RING = "shm_ring"
LOOPBACK = "loopback"


def _write_pair(tmp_path, name: str, playback_device: str | None):
    """Write a real statefile + the CamillaDSP config it points at.

    Returns the statefile path. ``playback_device=None`` writes a config with no
    ``playback`` device key at all — the program-bake / parked shape an active
    leader really keeps in its primary statefile.
    """
    config = tmp_path / f"{name}-config.yml"
    playback = (
        f"  playback:\n    type: Alsa\n    device: {playback_device}\n"
        if playback_device
        else "  playback:\n    type: File\n    filename: /dev/null\n"
    )
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  chunksize: 1024\n"
        "  capture:\n    type: Alsa\n    device: jts_ring_capture\n"
        + playback,
        encoding="utf-8",
    )
    statefile = tmp_path / f"{name}-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    return statefile


def _arrange(
    monkeypatch,
    tmp_path,
    *,
    coupling: str,
    playback_device: str | None,
    crossover_playback_device: str | None = None,
    primary_config_missing: bool = False,
) -> None:
    """Put the box in one (coupling, loaded-graph-playback) combination.

    REAL STATEFILES ON DISK, not stubs, and that is the point of this harness.
    The check resolves its evidence through BOTH the primary and the camilla#2
    crossover statefile (first recognized endpoint wins). Stubbing the resolved
    device would let the two halves of that union drift apart without any test
    noticing — which is exactly how the pre-#2285 gap between this check and
    `check_outputd_service`'s transport note survived unseen.
    """
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: coupling,
    )
    primary = _write_pair(tmp_path, "primary", playback_device)
    if primary_config_missing:
        # The statefile parses but names a config that is gone — evidence
        # carries an error string and the primary contributes nothing.
        primary.write_text(
            f"config_path: {tmp_path / 'deleted-config.yml'}\n", encoding="utf-8"
        )
    crossover = _write_pair(tmp_path, "crossover", crossover_playback_device)
    monkeypatch.setattr(
        audio_runtime, "_active_camilla_config_path", lambda: (str(primary), None)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(crossover)
    )


def test_the_split_state_fails_loudly(monkeypatch, tmp_path) -> None:
    """graph@ring AND coupling=loopback is the silent split — it must FAIL."""
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=RING_ACTIVE_PLAYBACK_DEVICE)

    result = audio_runtime.check_active_ring_split_transport()

    assert result.status == "fail", result
    # The remedy must be the EXPLICIT ARM command (the §4.2 mode-split
    # contract), not a rollback: there is no longer a rollback direction, and
    # for a roleful box `loopback` is the park rather than a destination.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail
    assert "loopback" not in result.detail.split("Complete the arm:")[1]


def test_the_known_mid_arm_transient_is_disclosed_to_the_operator(
    monkeypatch, tmp_path
) -> None:
    """The arm ladder moves the graph first, so this can FAIL mid-ladder.

    That is the ladder working. The detail has to say so, or an operator
    running doctor during an arm reads a correct FAIL as a fault.
    """
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=RING_ACTIVE_PLAYBACK_DEVICE)

    detail = audio_runtime.check_active_ring_split_transport().detail.lower()

    assert "transient" in detail
    assert "authoritative" in detail


# --- Per-conjunct pins. A two-term conjunction passes a single-conjunct test
# --- while still being wrong, so each term gets its own negative case.


def test_conjunct_one_the_coupling_term_alone_does_not_fire(monkeypatch, tmp_path) -> None:
    """coupling=loopback with a NON-ring graph is an ordinary loopback box."""
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device="outputd_content_playback")

    assert audio_runtime.check_active_ring_split_transport().status == "ok"


def test_conjunct_two_the_graph_term_alone_does_not_fire(monkeypatch, tmp_path) -> None:
    """graph@ring with coupling=shm_ring is a correctly ARMED box."""
    _arrange(
        monkeypatch, tmp_path, coupling=SHM_RING,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    assert audio_runtime.check_active_ring_split_transport().status == "ok"


def test_the_stereo_ring_is_not_the_active_ring(monkeypatch, tmp_path) -> None:
    """Only the ACTIVE ring is this check's subject.

    A flat box's graph on the stereo ring under loopback is a different
    condition owned by ``check_fanin_coupling``; claiming it here would report
    one fault twice and send a flat box the roleful arm remedy.
    """
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=RING_PLAYBACK_DEVICE)

    assert audio_runtime.check_active_ring_split_transport().status == "ok"


@pytest.mark.parametrize("missing", [None, ""])
def test_an_unreadable_graph_does_not_manufacture_a_fault(
    monkeypatch, tmp_path, missing
) -> None:
    """No loaded playback device is no evidence — never a FAIL.

    Fail-closed applies to arming, not to diagnosis: inventing a split from an
    absent reading would make a fresh or non-JTS box report a silent speaker it
    does not have.
    """
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=missing)

    assert audio_runtime.check_active_ring_split_transport().status == "ok"


# --- The union of BOTH statefiles (#2285 panel finding C1). These two shapes
# --- were measured going quiet from every doctor surface: the folded-away
# --- `check_outputd_service` transport note saw them (it read both statefiles),
# --- and this check did not (it read only the primary). The de-duplication was
# --- only correct once the survivor read what the deleted branch read.


def test_a_program_bake_in_the_primary_does_not_hide_the_ring_in_camilla2(
    monkeypatch, tmp_path
) -> None:
    """An active leader keeps a bake in the primary and its endpoint in camilla#2.

    The primary's graph names no registered output endpoint at all (a File sink
    — the program-bake / parked shape), so reading only the primary answers
    "(none)" and the box reads healthy while camilla#2 writes the ACTIVE ring
    under a loopback coupling: silent, with every daemon green.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        coupling=LOOPBACK,
        playback_device=None,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    result = audio_runtime.check_active_ring_split_transport()

    assert result.status == "fail", result
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_a_primary_statefile_pointing_at_a_deleted_config_does_not_hide_the_split(
    monkeypatch, tmp_path
) -> None:
    """The primary parses but its config is gone — evidence errors, not absence.

    The reader records "devices unavailable" for the primary and falls through
    to camilla#2, which names the ACTIVE ring. Before the union this returned
    the hardcoded `sound_current.yml` rescue's answer and went quiet.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        coupling=LOOPBACK,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        primary_config_missing=True,
    )

    assert audio_runtime.check_active_ring_split_transport().status == "fail"


def test_camilla2_evidence_still_respects_the_coupling_term(
    monkeypatch, tmp_path
) -> None:
    """The union widens the GRAPH term only — the conjunction still holds.

    Without this, widening the reader could have turned the check into a
    one-term alarm that reds every correctly-armed box.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        coupling=SHM_RING,
        playback_device=None,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    assert audio_runtime.check_active_ring_split_transport().status == "ok"


# ---------------------------------------------------------------------------
# The ENDPOINT rung — check_active_ring_path_projection.
#
# The sibling above owns the graph rung and returns ok the moment the coupling
# is `shm_ring`. These pin the other side of that partition: under the ring
# coupling, the ring PATH lagging its endpoint MARKER.
# ---------------------------------------------------------------------------


def _arrange_projection(monkeypatch, tmp_path, *, coupling: str, env_lines: str):
    """Put outputd.env and the persisted coupling on disk for the projection check.

    A real file, not a stubbed mapping: the check reads persisted evidence
    precisely BECAUSE outputd is not running in its target state, so what is
    under test includes the read itself.
    """
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: coupling,
    )
    env = tmp_path / "outputd.env"
    env.write_text(env_lines, encoding="utf-8")
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_OUTPUTD_ENV_PATH", str(env)
    )


@pytest.mark.parametrize(
    "marker,carried,derived",
    [
        # The first-arm lag (jts.local, 2026-08-21): marker armed, path Ring B.
        ("1", "/dev/shm/jts-ring/content.ring", "/dev/shm/jts-ring/active-content.ring"),
        # The disarm lag: marker cleared, path still the active ring.
        ("", "/dev/shm/jts-ring/active-content.ring", "/dev/shm/jts-ring/content.ring"),
    ],
)
def test_a_ring_path_lagging_its_marker_fails_with_the_runnable_remedy(
    monkeypatch, tmp_path, marker, carried, derived
) -> None:
    """The waypoint must produce a doctor line, and it must be actionable.

    This is the state ``check_outputd_service`` structurally cannot report:
    outputd refuses the crossed pair at startup, so that check returns its
    systemd failure before ever reaching the transport comparison. Nothing owned
    the finding, and the operator got "the unit is not running" with no cause.
    """
    _arrange_projection(
        monkeypatch,
        tmp_path,
        coupling=SHM_RING,
        env_lines=(
            "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
            f"JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT={marker}\n"
            f"JASPER_OUTPUTD_SHM_RING_PATH={carried}\n"
        ),
    )

    result = audio_runtime.check_active_ring_path_projection()

    assert result.status == "fail", result
    assert carried in result.detail
    assert derived in result.detail
    # Runnable, and the same pass the ladder's own next step runs.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_a_converged_ring_pair_is_ok(monkeypatch, tmp_path) -> None:
    """The negative control: an armed box on the active ring must not red."""
    _arrange_projection(
        monkeypatch,
        tmp_path,
        coupling=SHM_RING,
        env_lines=(
            "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
            "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1\n"
            "JASPER_OUTPUTD_SHM_RING_PATH=/dev/shm/jts-ring/active-content.ring\n"
        ),
    )

    assert audio_runtime.check_active_ring_path_projection().status == "ok"


def test_the_two_ladder_checks_partition_the_coupling_space(
    monkeypatch, tmp_path
) -> None:
    """Neither rung is unowned, and neither is double-reported.

    The split check returns ok as soon as the coupling is ``shm_ring``; the
    projection check returns ok as soon as it is not. Pinning the handoff means a
    later edit cannot leave a coupling value that both checks ignore — the gap
    that let the endpoint rung go unowned in the first place.
    """
    _arrange_projection(
        monkeypatch,
        tmp_path,
        coupling=LOOPBACK,
        env_lines=(
            "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1\n"
            "JASPER_OUTPUTD_SHM_RING_PATH=/dev/shm/jts-ring/content.ring\n"
        ),
    )
    # Under loopback outputd runs the `direct` bridge and never reads the ring
    # path, so the projection check stands down — and the split check is the one
    # that owns anything wrong on this side.
    assert audio_runtime.check_active_ring_path_projection().status == "ok"

    _arrange(monkeypatch, tmp_path, coupling=SHM_RING, playback_device=RING_PLAYBACK_DEVICE)
    assert audio_runtime.check_active_ring_split_transport().status == "ok"


# ===========================================================================
# check_fanin_binary_installed
# ===========================================================================

def test_check_fanin_binary_installed_reports_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "jasper-fanin"
    monkeypatch.setattr(audio_runtime, "Path", lambda _path: binary)

    result = audio_runtime.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == "fail"
    assert result.detail == (
        f"{binary} missing. Re-run install.sh; check cargo build "
        "output for compilation errors."
    )


def test_check_fanin_binary_installed_reports_nonexecutable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "jasper-fanin"
    binary.write_bytes(b"fan-in")
    binary.chmod(0o644)
    monkeypatch.setattr(audio_runtime, "Path", lambda _path: binary)

    result = audio_runtime.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == "fail"
    assert result.detail == (
        f"{binary} present but not executable. Run: sudo chmod +x {binary}"
    )


def test_check_fanin_binary_installed_reports_executable_size(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "jasper-fanin"
    binary.write_bytes(b"x" * 2500)
    binary.chmod(0o755)
    monkeypatch.setattr(audio_runtime, "Path", lambda _path: binary)

    result = audio_runtime.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == "ok"
    assert result.detail == f"{binary} (2 KB)"


# ===========================================================================
# _outputd_xrun_rate_warning — the outputd xrun-rate WARN tier
# ===========================================================================

def _xrun_section(rate_per_hour, last_xrun_age_ms):
    """Minimal outputd STATUS content/dac section for the xrun-rate helper."""
    return {
        "xrun_count": 0,
        "xrun_rate_per_hour": rate_per_hour,
        "last_xrun_age_ms": last_xrun_age_ms,
    }


def test_outputd_xrun_warning_none_when_no_recent_xrun():
    """last_xrun_age_ms=null (no xrun ever) → never warn, regardless of rate."""
    quiet = _xrun_section(rate_per_hour=0.0, last_xrun_age_ms=None)
    assert doctor.audio_runtime._outputd_xrun_rate_warning(quiet, quiet) is None


def test_outputd_xrun_warning_suppressed_for_stale_burst():
    """A high all-time rate whose last xrun is OLD (a cleared deploy-time
    burst) must NOT warn — the WARN is for a sustained, *current* problem."""
    stale = _xrun_section(
        rate_per_hour=50.0,
        last_xrun_age_ms=doctor.audio_runtime._OUTPUTD_XRUN_RECENT_AGE_MS + 1,
    )
    assert doctor.audio_runtime._outputd_xrun_rate_warning(stale, stale) is None


def test_outputd_xrun_warning_suppressed_for_recent_single_blip():
    """A recent xrun with a LOW sustained rate (one transient blip) must not
    warn — only a rate at/above the threshold qualifies."""
    blip = _xrun_section(
        rate_per_hour=doctor.audio_runtime._OUTPUTD_XRUN_RATE_WARN_PER_HOUR - 0.1,
        last_xrun_age_ms=1000,
    )
    assert doctor.audio_runtime._outputd_xrun_rate_warning(blip, blip) is None


def test_outputd_xrun_warning_fires_on_recent_sustained_rate():
    """Recent xrun AND a sustained rate at/above threshold → warn, naming the
    offending lane and both fields."""
    hot = _xrun_section(
        rate_per_hour=doctor.audio_runtime._OUTPUTD_XRUN_RATE_WARN_PER_HOUR,
        last_xrun_age_ms=2000,
    )
    quiet = _xrun_section(rate_per_hour=0.0, last_xrun_age_ms=None)
    reason = doctor.audio_runtime._outputd_xrun_rate_warning(quiet, hot)
    assert reason is not None
    assert "dac" in reason
    assert "xrun_rate_per_hour" in reason
    assert "last_xrun_age_ms" in reason


def test_outputd_xrun_warning_reports_worst_lane():
    """When both lanes qualify, the higher-rate lane is reported."""
    content = _xrun_section(rate_per_hour=8.0, last_xrun_age_ms=1000)
    dac = _xrun_section(rate_per_hour=40.0, last_xrun_age_ms=1000)
    reason = doctor.audio_runtime._outputd_xrun_rate_warning(content, dac)
    assert reason is not None
    assert reason.startswith("dac ")
