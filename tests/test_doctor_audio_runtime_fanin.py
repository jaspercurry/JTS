# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the doctor's jasper-fanin and snd-aloop checks."""

import json
from pathlib import Path

import pytest

from jasper.cli import doctor
import re
from jasper.cli.doctor import (
    audio_runtime,
    audio_runtime_fanin,
    audio_runtime_ring,
)
from jasper.output_topology import OutputTopologyError

from ._doctor_audio_runtime_fixtures import (
    _FakeSocket,
    _patch_fanin_status_socket,
    _patch_fanin_systemctl,
    _stage_floor_conf,
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

    monkeypatch.setattr(doctor.audio_runtime_fanin, "Path", fake_path)


_FANIN_ASOUND = """
pcm.librespot_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,0"
        rate 48000
        channels 2
        format S32_LE
    }
}
pcm.shairport_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,1"
        rate 48000
        channels 2
        format S32_LE
    }
}
pcm.bluealsa_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,2"
        rate 48000
        channels 2
        format S32_LE
    }
}
pcm.correction_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,4"
        rate 48000
        channels 2
        format S32_LE
    }
}
"""


def test_fanin_asound_wiring_ok(monkeypatch, tmp_path):
    _patch_asound_conf(monkeypatch, _FANIN_ASOUND, tmp_path)
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "ok"
    assert "renderer/test lanes" in r.detail


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
    l0 = doctor.audio_runtime_fanin._host_clock_health_from_status(_host_clock_status())
    assert l0.status == "ok"
    assert "ladder=l0_locked" in l0.detail

    retry = doctor.audio_runtime_fanin._host_clock_health_from_status(
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
    result = doctor.audio_runtime_fanin._host_clock_health_from_status(
        _host_clock_status(ladder="l2_fallback", reason=reason)
    )
    assert result.status == "warn"
    assert f"fallback_reason={reason}" in result.detail


def test_host_clock_doctor_warns_on_unavailable_or_generation_mismatch():
    unavailable = doctor.audio_runtime_fanin._host_clock_health_from_status(
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

    mismatch = doctor.audio_runtime_fanin._host_clock_health_from_status(
        _host_clock_status(control_generation=3)
    )
    assert mismatch.status == "warn"
    assert "capture_generation=4" in mismatch.detail
    assert "control_generation=3" in mismatch.detail


def test_status_socket_strict_wrapper_and_lossy_caller_keep_decode_ownership(
    monkeypatch,
):
    strict = _FakeSocket(payload=b'{"note":"\xff"}')
    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **kw: strict)

    with pytest.raises(UnicodeDecodeError):
        doctor._shared._read_status_socket("/run/test.sock")

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
    monkeypatch.setattr(doctor.audio_runtime_fanin.time, "sleep", lambda _: None)
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


def test_check_fanin_service_ok_with_expected_status(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload())
    r = doctor.check_fanin_service()
    assert r.status == "ok"
    assert "transport=shm_ring" in r.detail
    assert "input_buffer_frames=4096" in r.detail
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
    fault = doctor.audio_runtime_fanin._assistant_gain_fault(loudness)
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


def test_check_fanin_service_fails_on_small_runtime_input_buffer(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_status_payload(input_buffer_frames=2048),
    )
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "input_buffer_frames=2048" in r.detail


def test_fanin_asound_wiring_fails_on_bare_renderer_lane(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            'slave {\n        pcm "hw:Loopback,0,1"\n        rate 48000\n        channels 2\n        format S32_LE\n    }',
            'slave.pcm "hw:Loopback,0,1"',
        ),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "shairport_substream" in r.detail


def test_fanin_asound_wiring_fails_when_the_lanes_shear_from_the_wire(
    monkeypatch, tmp_path
):
    """The renderer aliases are the PLAYBACK half of fan-in's aloop cables, and
    snd-aloop pins both halves to one format. A box pinned narrow through the
    rollback lever whose /etc/asound.conf still declares the wide wire cannot
    have both ends open, so the deployed file is judged against the wire the box
    actually resolves rather than a literal."""
    _patch_asound_conf(monkeypatch, _FANIN_ASOUND, tmp_path)
    monkeypatch.setattr(
        doctor.audio_runtime_fanin, "read_declared_ring_wire_format", lambda: "S16_LE"
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "S16_LE" in r.detail
    # A width shear is reported as a width shear: the lanes are wired correctly
    # and only their width disagrees, so calling them wrong slaves would send an
    # operator after the wrong fault.
    assert "wrong slave" not in r.detail


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
                "protocol_errors": 0,
                "dropped_commands": 0,
                "dropped_audio_frames": 0,
            }
        ),
    )
    r = doctor.check_fanin_tts_drops()
    assert r.status == "ok"
    assert "none since fan-in start" in r.detail


def test_check_fanin_tts_drops_warns_on_protocol_error(monkeypatch):
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_payload_with_tts(
            {
                "enabled": True,
                "protocol_errors": 1,
                "dropped_commands": 0,
                "dropped_audio_frames": 0,
            }
        ),
    )

    assert doctor.check_fanin_tts_drops().status == "warn"


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
    card = root / audio_runtime_fanin._ALOOP_CARD_ID
    for pcm_dir in audio_runtime_fanin._ALOOP_PCM_DIRS:
        for pair in range(audio_runtime_fanin._ALOOP_SUBSTREAMS):
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
        monkeypatch.setenv(audio_runtime_fanin._ALOOP_PROC_ROOT_ENV, str(root))

    return _set


#: The pairs that must be registered at this head, written as a LITERAL on
#: purpose. Deriving this expectation from the same constant the production
#: code derives from would make it move in lockstep with a regression — drop
#: pair 0 from `_FANIN_EXPECTED_ALOOP_INPUTS` and a derived expectation drops
#: it too, so nothing fails. A literal is what makes a source-constant
#: deletion detectable.
_EXPECTED_REGISTERED_PAIRS = (0, 1, 2, 3, 4)

#: The pairs whose owners are GONE, and which must therefore read as offenders.
#: Pair 5 lost its PCM definitions to #2285 P9-C; pairs 6 and 7 lost theirs to
#: ADR-0100, which moved outputd's passive content lane and fan-in's summed
#: music output onto SHM rings. deploy/alsa/asoundrc.jasper declares none of the
#: three, and deploy/modprobe.d/snd-aloop.conf records them as
#: reserved-not-reclaimed so no surviving pair renumbers.
_RETIRED_PAIRS = (5, 6, 7)


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

def test_no_aloop_card_is_ok(proc_root, tmp_path):
    """A box without snd-aloop has no remnant to police."""
    empty = tmp_path / "asound"
    empty.mkdir()
    proc_root(empty)
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "ok"
    assert "not loaded" in result.detail


def test_all_closed_is_ok(proc_root, tmp_path):
    proc_root(_make_card(tmp_path))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "ok"
    assert "no pair currently open" in result.detail
    # The remnant's size is REPORTED, not merely asserted — risk 5.1 in the
    # design is "the remnant becomes permanent by silence".
    assert (
        f"{len(_EXPECTED_REGISTERED_PAIRS)} of "
        f"{audio_runtime_fanin._ALOOP_SUBSTREAMS} pairs"
    ) in result.detail
    # The program path is off snd-aloop entirely (ADR-0100); the text must
    # not still name a content lane that no longer exists.
    assert "passive content lane" not in result.detail
    assert "pair 6" not in result.detail


def test_registered_open_pair_is_ok_jts4_shape(proc_root, tmp_path):
    """THE FALSE-POSITIVE REGRESSION GUARD.

    Observed on jts4, 2026-08-14: /proc/asound/Loopback/pcm1c/sub3 in
    `state: RUNNING`, owner cgroup jasper-fanin.service — the usbsink lane's
    idle-read fallback documented in deploy/modprobe.d/snd-aloop.conf. That
    box is HEALTHY. If this check ever fails it, the check is wrong.
    """
    proc_root(_make_card(tmp_path, {"pcm1c": [3]}))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "ok"
    assert "[3]" in result.detail


@pytest.mark.parametrize("pcm_dir", ["pcm0p", "pcm0c", "pcm1p", "pcm1c"])
@pytest.mark.parametrize("pair", _RETIRED_PAIRS)
def test_positive_control_foreign_substream_fails(
    proc_root, tmp_path, pcm_dir, pair
):
    """POSITIVE CONTROL — a deliberately-opened foreign substream trips it.

    Pairs 5, 6 and 7 are the foreign ones: P9-C deleted pair 5's PCM
    definitions and ADR-0100 deleted pairs 6 and 7's when the passive content
    lane and the summed music output both moved to SHM rings. A holder on any
    of them has resurrected a deleted lane — a rolled-back binary or a stale
    asoundrc — which is the regression this guard exists to catch, and which
    read as `ok` for as long as pairs 6 and 7 stayed in the registered set.
    Parametrised across all four PCM directions so a walker that only scanned
    the playback side would fail this.
    """
    proc_root(_make_card(tmp_path, {pcm_dir: [pair]}))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "fail"
    assert f"{pcm_dir}/sub{pair}" in result.detail
    assert "no registered purpose" in result.detail


def test_positive_control_names_the_offender(proc_root, tmp_path):
    """The FAIL names the offender — the design asks for pid/process."""
    proc_root(_make_card(tmp_path, {"pcm0p": [5]}))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "fail"
    assert "pid=4242" in result.detail
    # And it tells the operator what to do about it.
    assert "deploy-to-pi.sh" in result.detail


def test_offender_detail_is_bounded(proc_root, tmp_path, monkeypatch):
    """A pathological box cannot produce an unbounded doctor line.

    The registered set is shrunk to one pair, so every other pair reads as an
    offender and the offender count (28) genuinely exceeds the cap. An earlier
    version of this test opened only pair 5 across the four PCM dirs, giving
    exactly 4 offenders against a cap of 4; `[:cap]` and `[:]` were then
    indistinguishable and the bound was asserted but never proven.
    """
    monkeypatch.setattr(
        audio_runtime_fanin,
        "_derive_registered_pairs",
        lambda: {0: "fan-in input lane 'spotify'"},
    )
    proc_root(
        _make_card(
            tmp_path,
            {
                pcm: [p for p in range(audio_runtime_fanin._ALOOP_SUBSTREAMS) if p != 0]
                for pcm in audio_runtime_fanin._ALOOP_PCM_DIRS
            },
        )
    )
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "fail"
    cap = audio_runtime_fanin._ALOOP_OFFENDER_DETAIL_CAP
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
    card = root / audio_runtime_fanin._ALOOP_CARD_ID
    for pcm_dir in audio_runtime_fanin._ALOOP_PCM_DIRS:
        for pair in range(audio_runtime_fanin._ALOOP_SUBSTREAMS):
            (card / pcm_dir / f"sub{pair}" / "status").mkdir(parents=True)
    proc_root(root)
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "warn"
    assert "could not be verified" in result.detail


def test_missing_substreams_are_not_evidence(proc_root, tmp_path):
    """A narrower snd-aloop is not a fault — absence is not an offender."""
    root = tmp_path / "asound"
    card = root / audio_runtime_fanin._ALOOP_CARD_ID
    sub = card / "pcm0p" / "sub0"
    sub.mkdir(parents=True)
    (sub / "status").write_text("closed\n", encoding="utf-8")
    proc_root(root)
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "ok"


def test_check_never_raises_on_hostile_status(proc_root, tmp_path):
    """A garbage/empty status file must not crash the doctor."""
    root = _make_card(tmp_path)
    card = root / audio_runtime_fanin._ALOOP_CARD_ID
    (card / "pcm0p" / "sub0" / "status").write_text("", encoding="utf-8")
    (card / "pcm0p" / "sub1" / "status").write_text(
        "\x00\xff garbage", encoding="utf-8", errors="replace"
    )
    proc_root(root)
    result = audio_runtime_fanin.check_aloop_registered_substreams()
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
    assert audio_runtime_fanin._ALOOP_SUBSTREAMS == _modprobe_substreams()


# --------------------------------------------------------------------------
# Derivation — the registered set is READ from its owners, never restated
# --------------------------------------------------------------------------

def test_derived_set_matches_the_expected_allocation():
    """Dropping a pair from the owning constant changes this set."""
    derived = audio_runtime_fanin._derive_registered_pairs()
    assert tuple(sorted(derived)) == _EXPECTED_REGISTERED_PAIRS


@pytest.mark.parametrize("pair", _EXPECTED_REGISTERED_PAIRS)
def test_every_registered_pair_open_is_ok(proc_root, tmp_path, pair):
    """THE PER-ROW GUARD.

    One case per registered pair, each opening that pair and requiring `ok`.
    Dropping a pair from its owning constant makes that pair unregistered, and
    this test then FAILs it — which is the whole point: an unregistered pair
    that a real box holds open is a red doctor on healthy hardware.
    """
    proc_root(_make_card(tmp_path, {"pcm0p": [pair], "pcm1c": [pair]}))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "ok", f"pair {pair} open read as {result.detail}"


def test_derivation_is_all_or_nothing_on_a_bad_input(monkeypatch):
    """A partial derivation would SHRINK the set and red-doctor a healthy box,
    so an unparseable source must return None (-> warn), never a subset."""

    monkeypatch.setattr(
        audio_runtime_fanin,
        "_FANIN_EXPECTED_ALOOP_INPUTS",
        [("spotify", "hw:Loopback,1,0"), ("airplay", "not-a-pcm")],
    )
    assert audio_runtime_fanin._derive_registered_pairs() is None


def test_derivation_rejects_a_non_loopback_card(monkeypatch):
    monkeypatch.setattr(
        audio_runtime_fanin,
        "_FANIN_EXPECTED_ALOOP_INPUTS",
        [("spotify", "hw:SomeOtherCard,1,0")],
    )
    assert audio_runtime_fanin._derive_registered_pairs() is None


def test_unparseable_source_constant_is_warn(proc_root, tmp_path, monkeypatch):
    """An unparseable owner degrades the FULL check to warn, never to a
    shrunken set that red-doctors a healthy box."""
    monkeypatch.setattr(
        audio_runtime_fanin, "_FANIN_EXPECTED_ALOOP_INPUTS", [("spotify", "not-a-pcm")]
    )
    proc_root(_make_card(tmp_path))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "warn"
    assert "could not derive" in result.detail


@pytest.mark.parametrize("pair", _RETIRED_PAIRS)
def test_retired_pairs_are_not_registered(pair):
    """No owner names pairs 5-7 any more, so re-registering one from any
    owning constant fails here — the deletion is what the guard protects, and
    a registered pair is a pair whose resurrection reads as healthy.
    """
    derived = audio_runtime_fanin._derive_registered_pairs()

    assert pair not in derived


def test_registered_pairs_are_within_the_module_range():
    substreams = _modprobe_substreams()
    derived = audio_runtime_fanin._derive_registered_pairs()
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

# What a bonded LEADER actually loads: Ring A capture (its transport is the ring
# like every other box) into the Snapcast pipe (its post-DSP endpoint is the
# bond, not a local ring). `_ALSA_CFG` is the near-miss — same sink, a capture
# that reverted off the ring — and must keep warning.
_BONDED_LEADER_CFG = _ALSA_CFG.replace(
    'device: "plug:jasper_capture"',
    'device: "jts_ring_capture"',
)


def test_capture_parser_reads_rawfile(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(_RAWFILE_CFG)
    assert audio_runtime_fanin._loaded_capture_type(cfg) == "RawFile"
    assert audio_runtime._loaded_playback_type(cfg) == "File"


def test_capture_parser_reads_alsa_not_playback_file(tmp_path):
    # The playback File sink must NOT be misread as the capture type.
    cfg = tmp_path / "c.yml"
    cfg.write_text(_ALSA_CFG)
    assert audio_runtime_fanin._loaded_capture_type(cfg) == "Alsa"


def test_capture_parser_none_when_absent(tmp_path):
    assert audio_runtime_fanin._loaded_capture_type(tmp_path / "missing.yml") is None
    cfg = tmp_path / "c.yml"
    cfg.write_text("filters:\n  x: 1\n")
    assert audio_runtime_fanin._loaded_capture_type(cfg) is None


def _run_check(monkeypatch, *, cfg_text, tmp_path):
    """Put a loaded CamillaDSP config in front of ``check_fanin_coupling``.

    NO persisted coupling and NO outputd env: since ADR-0100 the check reads
    neither. Passing one here would let a test claim a gate the production check
    does not have.
    """
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
    # _active_camilla_config_path returns (statefile, active_config_path|None) —
    # mock the REAL tuple shape (a str-only mock masked a production TypeError).
    monkeypatch.setattr(
        audio_runtime_fanin, "_active_camilla_config_path", lambda: (cfg.parent, str(cfg))
    )
    return audio_runtime_fanin.check_fanin_coupling()


@pytest.mark.parametrize("cfg_text", [_ALSA_CFG, _ALSA_LOCAL_PIPE_CFG, _RAWFILE_CFG])
def test_a_graph_that_is_not_the_ring_graph_warns_with_the_ring_remedy(
    monkeypatch, tmp_path, cfg_text
):
    """Every non-ring graph shape is one fault with one remedy.

    The three used to split across an intent-keyed ladder (a File-sink branch, a
    RawFile crash-loop precursor, a plain-Alsa "clean loopback box" that read
    OK). Under one transport they are the same state — the loaded graph is not
    this box's ring graph — and the remedy names the only transport there is.
    """
    res = _run_check(monkeypatch, cfg_text=cfg_text, tmp_path=tmp_path)
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile shm_ring" in res.detail


def test_a_bonded_leader_feeding_the_snapcast_pipe_is_ok(monkeypatch, tmp_path):
    """THE FALSE WARN this endpoint restores.

    A bonded leader's camilla#1 captures Ring A and plays into the Snapcast
    pipe; it reaches no local ring at all, by design. Comparing the playback
    axis anyway read `(missing)` against a ring name and warned the box that is
    feeding the whole group.

    Its CONTROL is `_ALSA_CFG` in the parametrized warn above: the same sink
    with a capture that reverted off the ring still warns, so what is accepted
    here is the ENDPOINT and not any File sink.
    """
    res = _run_check(monkeypatch, cfg_text=_BONDED_LEADER_CFG, tmp_path=tmp_path)
    assert res.status == "ok"


def test_a_non_snapcast_file_sink_is_not_that_endpoint(monkeypatch, tmp_path):
    """A stale LOCAL pipe is a real fault — the filename is what tells them apart.

    Without comparing it, "playback is a File sink" would exempt every stale
    pipe the removed transport left behind.
    """
    cfg_text = _BONDED_LEADER_CFG.replace(
        "/run/jasper-snapserver/snapfifo", "/run/jasper-outputd/content.pipe"
    )
    res = _run_check(monkeypatch, cfg_text=cfg_text, tmp_path=tmp_path)
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile shm_ring" in res.detail


def test_check_ok_when_no_loaded_capture(monkeypatch, tmp_path):
    res = _run_check(monkeypatch, cfg_text="filters:\n", tmp_path=tmp_path)
    assert res.status == "ok"


# --- check_fanin_coupling_value: persisted coupling value must be recognized --


@pytest.mark.parametrize(
    "raw,status",
    [
        (None, "ok"),
        ("", "ok"),
        ("shm_ring", "ok"),
        ("transport_pipe", "warn"),
        ("loopback", "warn"),
    ],
    ids=["absent_key", "empty", "declared", "removed_token", "retired_token"],
)
def test_check_fanin_coupling_value_reads_the_shared_predicate(
    monkeypatch, tmp_path, raw, status
):
    """ADR-0100: only a value fan-in REFUSES is a finding.

    A migrating box carrying the removed ``transport_pipe`` token (or a typo)
    warns until the reconciler converges it. An ABSENT or empty key is not that
    state — fan-in serves the ring for it — so this surface must agree with the
    daemon rather than with the presence of a token (#3655).
    """
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        "" if raw is None else f"JASPER_FANIN_CAMILLA_COUPLING={raw}\n"
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    res = audio_runtime_fanin.check_fanin_coupling_value()
    assert res.status == status
    if status == "warn":
        assert raw in res.detail, "the operator needs the stale token named"


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

def test_ring_ok_when_the_loaded_graph_names_both_ring_ends(monkeypatch, tmp_path):
    res = _run_check(monkeypatch, cfg_text=_RING_CFG, tmp_path=tmp_path)
    assert res.status == "ok"
    assert "jts_ring_capture" in res.detail and "jts_ring_playback" in res.detail


def test_ring_warns_when_the_loaded_graph_reverted_off_the_ring(monkeypatch, tmp_path):
    # THE finding-5 revert: a camilla restart re-seeded a stale artifact whose
    # capture is not jts_ring_capture, so CamillaDSP sources a device fan-in is
    # not writing. Nothing about the persisted files can excuse it.
    res = _run_check(monkeypatch, cfg_text=_ALSA_CFG, tmp_path=tmp_path)
    assert res.status == "warn"
    assert "jts_ring_capture" in res.detail


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


def test_a_roleful_box_with_a_clear_marker_goes_up_the_ladder(monkeypatch, tmp_path):
    """A ROLEFUL box gets the ARM LADDER, not a reconcile that converges nothing.

    The state: a graph on the ACTIVE ring while this box's endpoint marker is
    CLEAR. The remedy used to be `jasper-fanin-coupling-reconcile loopback`
    unconditionally — which on a roleful box moved nothing and reported SUCCESS.
    So the operator ran a command, was told it worked, and the warn stayed.

    The graph has to be moved by step 1 of the ladder. Asserted through the
    classification-free half of the message — the command spelling — because
    that is what an operator copies. Both halves: the ring rungs present, the
    retired ones absent, so a partial re-point cannot pass.
    """
    monkeypatch.setattr(
        audio_runtime_fanin, "_requires_roleful_graph", lambda: True)
    res = _run_check(monkeypatch, cfg_text=_STALE_RING_CFG, tmp_path=tmp_path)
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


def test_a_passive_box_on_the_wrong_ring_keeps_the_plain_remedy(
    monkeypatch, tmp_path
):
    """CONTROL: a PASSIVE box keeps exactly the one-command remedy.

    Without this the assertion above would also pass if the ladder text had been
    appended unconditionally — and on a passive box the reconciler's own pass
    genuinely is the whole fix, so sending one up a three-rung active-speaker
    ladder would be worse advice, not more of it.
    """
    monkeypatch.setattr(
        audio_runtime_fanin, "_requires_roleful_graph", lambda: False)
    res = _run_check(monkeypatch, cfg_text=_STALE_RING_CFG, tmp_path=tmp_path)
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile shm_ring" in res.detail
    assert "baseline-reemit" not in res.detail, res.detail


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

    assert audio_runtime_fanin._requires_roleful_graph() is False
    assert "ROLEFUL" not in audio_runtime_ring.check_ring_conf_floor_render().detail


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

    assert audio_runtime_fanin._requires_roleful_graph() is True
    assert "ROLEFUL" in audio_runtime_ring.check_ring_conf_floor_render().detail


# ===========================================================================
# check_fanin_binary_installed
# ===========================================================================

def test_check_fanin_binary_installed_reports_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "jasper-fanin"
    monkeypatch.setattr(audio_runtime_fanin, "Path", lambda _path: binary)

    result = audio_runtime_fanin.check_fanin_binary_installed()

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
    monkeypatch.setattr(audio_runtime_fanin, "Path", lambda _path: binary)

    result = audio_runtime_fanin.check_fanin_binary_installed()

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
    monkeypatch.setattr(audio_runtime_fanin, "Path", lambda _path: binary)

    result = audio_runtime_fanin.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == "ok"
    assert result.detail == f"{binary} (2 KB)"
