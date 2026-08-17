# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor audio-runtime domain."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from jasper import audio_runtime_plan, audio_validation
from jasper.cli import doctor
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
)

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
pcm.jasper_capture {
    type dsnoop
    slave {
        pcm "hw:Loopback,1,7"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.jasper_ref {
    type plug
    slave.pcm "jasper_capture"
}
"""


def test_fanin_asound_wiring_ok(monkeypatch, tmp_path):
    _patch_asound_conf(monkeypatch, _FANIN_ASOUND, tmp_path)
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "ok"
    assert "substream 7" in r.detail


def test_fanin_asound_wiring_fails_on_legacy_capture(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace('pcm "hw:Loopback,1,7"', 'pcm "hw:Loopback,1,0"'),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "substream 0" in r.detail
    assert "EBUSY" in r.detail


def test_fanin_asound_wiring_fails_without_jasper_ref(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            'pcm.jasper_ref {\n    type plug\n    slave.pcm "jasper_capture"\n}\n',
            "",
        ),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "pcm.jasper_ref missing" in r.detail


def test_fanin_asound_wiring_fails_when_capture_shape_unpinned(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            '        pcm "hw:Loopback,1,7"\n        rate 48000\n',
            '        pcm "hw:Loopback,1,7"\n',
        ),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "48 kHz stereo S16_LE" in r.detail


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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda: "loopback",
    )


def _fanin_status_payload(
    *,
    input_buffer_frames: int = 4096,
    output_buffer_frames: int = 1024,
    progress_age_ms: int = 2,
    transport: str = "loopback",
) -> bytes:
    output = {
        "pcm": doctor._FANIN_EXPECTED_OUTPUT_PCM,
        "transport": transport,
        "buffer_frames": output_buffer_frames,
        "frames_written": 1234,
        "xrun_count": 0,
    }
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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
    assert "transport=loopback" in r.detail
    assert "input_buffer_frames=4096" in r.detail
    assert "output_buffer_frames=1024" in r.detail
    assert "tts_enabled=true" in r.detail
    assert "assistant_loudness_decision=False" in r.detail


def test_check_fanin_service_fails_on_live_transport_mismatch(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda: "shm_ring",
    )
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload())

    r = doctor.check_fanin_service()

    assert r.status == "fail"
    assert "output.transport='loopback'" in r.detail
    assert "expected 'shm_ring'" in r.detail


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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
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
    plan = audio_runtime_plan.build_audio_runtime_plan(
        fanin_env={"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"},
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring"},
        route_mode="active_leader",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_audio_runtime_plan()

    assert r.status == "fail"
    assert "shm_ring is not supported while" in r.detail


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


def test_check_fanin_ring_stall_ok_when_loopback(monkeypatch):
    # Default loopback coupling: STATUS carries no ring block → skip-if-loopback.
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload())
    r = doctor.check_fanin_ring_stall()
    assert r.status == "ok"
    assert "loopback" in r.detail


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
