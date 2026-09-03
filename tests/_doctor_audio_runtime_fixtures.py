# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures and payload builders shared by the audio-runtime test files."""

import json
from pathlib import Path

from jasper import audio_runtime_plan
from jasper.cli import doctor
from jasper.cli.doctor import (
    audio_runtime_ring,
)

from .doctor_test_support import record_active_dac


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


def _fake_systemctl(enabled: str, active: str):
    def fake_run(cmd, *args, **kwargs):
        stdout = ""
        if cmd[:2] == ["systemctl", "is-enabled"]:
            stdout = enabled + "\n"
        elif cmd[:2] == ["systemctl", "is-active"]:
            stdout = active + "\n"
        return type("P", (), {"stdout": stdout, "stderr": "", "returncode": 0})()

    return fake_run


def _patch_fanin_systemctl(monkeypatch, *, enabled="enabled", active="active"):
    """Answer systemctl for every check module that reads a unit's state."""
    for module in (doctor.audio_runtime_fanin, doctor.audio_runtime_outputd):
        monkeypatch.setattr(module, "_run", _fake_systemctl(enabled, active))


def _outputd_status_payload(
    *,
    backend: str = "alsa",
    sink_mode: str = "single_alsa",
    dac_pcm: str = doctor._OUTPUTD_EXPECTED_DAC_PCM,
    # Period-sized, because that is the honest synthetic every box publishes:
    # outputd opens no content ALSA PCM at all, so there is no negotiated
    # content buffer behind this number (ADR-0100).
    content_buffer_frames: int = 1024,
    dac_buffer_frames: int = 3072,
    period_frames: int = 1024,
    progress_age_ms: int = 2,
    dual_apple_status: dict | None = None,
    # THE default, because it is the only source a started daemon reports:
    # outputd attaches the ring or parks (ADR-0100). `alsa` stays reachable as
    # an argument so the checks that own the stale-env and split-transport
    # states can still stage a daemon that came up on the retired route.
    content_source: str = "shm_ring",
    shm_ring_slots: int = 2,
    shm_ring_slot_frames: int | None = None,
    shm_ring_capacity_frames: int | None = None,
    shm_ring_occupancy: int = 0,
) -> bytes:
    content = {
        "source": content_source,
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
        # `mode` alone: the fill/ppm/lock counters beside it belonged to the
        # rate_match bridge, which was deleted with its lane. Tied to
        # `content_source` so this payload cannot describe a daemon that reads
        # one transport and reports another.
        "content_bridge": {
            "mode": "shm_ring" if content_source == "shm_ring" else "direct",
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
    if content.get("source") == "shm_ring":
        playback_device = "jts_ring_playback"
        capture_device = "jts_ring_capture"
    else:
        playback_device = "outputd_content_playback"
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


def _stage_floor_conf(monkeypatch, tmp_path, *, dac_id, conf_text=None, status="ready"):
    conf = tmp_path / "60-jts-ring.conf"
    if conf_text is None:
        conf.write_bytes(SHIPPED_RING_CONF.read_bytes())
    else:
        conf.write_text(conf_text, encoding="utf-8")
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(conf))
    record_active_dac(dac_id, status=status)
    return conf
