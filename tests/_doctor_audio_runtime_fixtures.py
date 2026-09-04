# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures and payload builders shared by the audio-runtime test files."""

import json

from jasper import audio_runtime_plan
from jasper.cli import doctor
from jasper.cli.doctor import _evidence
from jasper.cli.doctor._evidence import evidence

#: Every unit the audio-runtime checks ask about, so a seeded run never falls
#: through to a real ``systemctl show`` for one of them.
_AUDIO_UNITS = (
    "jasper-fanin.service",
    "jasper-camilla.service",
    "jasper-outputd.service",
)


def _seed_units(*, enabled="enabled", active="active"):
    """Seed the run's ONE ``systemctl show`` (ADR-0233 rule 4).

    ``enabled="not-found"`` is systemd's answer for a unit it does not have,
    which the batched reader reports as ``load_state``, not as a file state.
    """
    evidence.seed(
        "units",
        {
            unit: (
                {
                    "unit": unit,
                    "load_state": "not-found",
                    "unit_file_state": None,
                    "active_state": "inactive",
                }
                if enabled == "not-found"
                else {
                    "unit": unit,
                    "load_state": "loaded",
                    "unit_file_state": enabled,
                    "active_state": active,
                }
            )
            for unit in _AUDIO_UNITS
        },
    )


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
    progress_age_ms: int = 2,
    transport: str = "shm_ring",
    ring: dict | None = _FANIN_RING_BLOCK,
) -> bytes:
    """A fan-in STATUS payload. Defaults to the ONLY shape a live daemon can
    report: transport=shm_ring with a ring block. Pass ``ring=None`` to build
    the malformed no-ring-block shape."""
    output = {
        "transport": transport,
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


def _patch_status_reader(monkeypatch, payload: bytes):
    """Answer this run's ONE read of each daemon STATUS socket with ``payload``.

    The seam is the evidence cache's reader, so several checks over one daemon
    still cost one read (ADR-0233 rule 4), and the reader's own contract —
    raise on unparseable bytes, raise on a non-object root — is preserved here
    so the callers' classification branches stay under test.
    """

    def fake_read(path, *, timeout):
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("STATUS response root is not an object")
        return parsed

    monkeypatch.setattr(_evidence, "read_status_socket", fake_read)
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
