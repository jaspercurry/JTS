# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor jasper-fanin checks."""

import json
import re
from pathlib import Path

import pytest

from jasper.cli.doctor import _evidence, audio_runtime_fanin, audio_runtime_ring
from jasper.cli.doctor._evidence import evidence
from jasper.output_topology import OutputTopologyError
from jasper.route_latency.status_socket import FANIN_STATUS_SOCKET

from ._doctor_audio_runtime_fixtures import (
    _fanin_status_payload,
    _patch_status_reader,
    _seed_units,
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
    real_path_cls = Path

    def fake_path(arg):
        if arg == "/etc/asound.conf":
            return target
        if arg == "/var/lib/jasper/audio_topology.env":
            return stale
        return real_path_cls(arg)

    monkeypatch.setattr(audio_runtime_fanin, "Path", fake_path)


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
    r = audio_runtime_fanin.check_fanin_asound_wiring()
    assert r.status == "ok"


# The healthy Ring A block a running fan-in always publishes (ADR-0100 — the
# ring is the only transport, so there is no ring-less live STATUS).


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
    """A locked ladder is a plain ok; a bounded acquisition says it is probing."""
    l0 = audio_runtime_fanin._host_clock_health_from_status(_host_clock_status())
    assert l0.status == "ok"
    assert l0.reason == ""

    retry = audio_runtime_fanin._host_clock_health_from_status(
        _host_clock_status(ladder="probing", phase="retry_wait", attempt=2, retries=1)
    )
    assert retry.status == "ok"
    assert retry.reason == audio_runtime_fanin.REASON_HOST_CLOCK_PROBING


def test_host_clock_doctor_probing_exemption_precedes_the_actuator_fail():
    """A fresh session in `probing` with the ctl device still opening
    (`ready=False`, bounded per the docstring) must read as recovering, not
    hard-fail on the actuator-unavailable branch."""
    result = audio_runtime_fanin._host_clock_health_from_status(
        _host_clock_status(ladder="probing", ready=False, phase="await_lock", attempt=1)
    )
    assert result.status == "ok"
    assert result.reason == audio_runtime_fanin.REASON_HOST_CLOCK_PROBING


def test_host_clock_doctor_warns_on_a_persistent_l2_fallback():
    result = audio_runtime_fanin._host_clock_health_from_status(
        _host_clock_status(ladder="l2_fallback", reason="probe_noncompliant")
    )
    assert result.status == "warn"
    assert result.reason == audio_runtime_fanin.REASON_HOST_CLOCK_L2_FALLBACK


@pytest.mark.parametrize(
    "status_kwargs",
    [
        # actuator never became ready
        {
            "ladder": "l2_fallback",
            "reason": "actuator_unavailable",
            "ready": False,
            "control_generation": None,
        },
        # ready, but the control generation lags the capture generation
        {"control_generation": 3},
    ],
)
def test_host_clock_doctor_fails_on_unavailable_or_generation_mismatch(status_kwargs):
    """A dead steering actuator is a drift FAULT, not an advisory: nothing is
    correcting the host clock and the box is on the direct resampler."""
    result = audio_runtime_fanin._host_clock_health_from_status(
        _host_clock_status(**status_kwargs)
    )
    assert result.status == "fail"
    assert result.reason == audio_runtime_fanin.REASON_HOST_CLOCK_ACTUATOR_UNAVAILABLE


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({}, audio_runtime_fanin.REASON_HOST_CLOCK_TELEMETRY_MISSING),
        (
            {"host_clock": {"enabled": True, "probe": {}}},
            audio_runtime_fanin.REASON_HOST_CLOCK_ACTUATOR_TELEMETRY_MISSING,
        ),
        (
            {"host_clock": {"enabled": True, "actuator": {}}},
            audio_runtime_fanin.REASON_HOST_CLOCK_PROBE_TELEMETRY_MISSING,
        ),
    ],
    ids=["no-host-clock", "no-actuator", "no-probe"],
)
def test_host_clock_telemetry_a_build_does_not_publish_is_skipped(payload, reason):
    """Nothing observed is `skipped`, never `warn` (ADR-0233 rule 3)."""
    result = audio_runtime_fanin._host_clock_health_from_status(payload)
    assert result.status == "skipped"
    assert result.reason == reason


def _patch_unreachable_status(monkeypatch):
    """A daemon whose STATUS socket cannot be reached at all — the OSError the
    evidence cache classifies as ``StatusRead.unreachable``."""

    def refused(path, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(_evidence, "read_status_socket", refused)


def test_one_doctor_pass_opens_the_fanin_status_socket_once(monkeypatch):
    """Every fan-in STATUS consumer in this module shares ONE read.

    The five checks below used to open ``/run/jasper-fanin/control.sock`` five
    times per run; the evidence cache is what makes that one (ADR-0233 rule 4).
    """
    opens: list[str] = []

    def counting_read(path, *, timeout):
        opens.append(path)
        return json.loads(_fanin_status_payload().decode("utf-8"))

    monkeypatch.setattr(_evidence, "read_status_socket", counting_read)
    monkeypatch.setattr(
        "jasper.renderer_lanes.read_armed_labels", lambda *a, **k: ["librespot"]
    )
    _seed_units()

    for check in (
        audio_runtime_fanin.check_fanin_service,
        audio_runtime_fanin.check_fanin_host_clock,
        audio_runtime_fanin.check_fanin_tts_drops,
        audio_runtime_ring.check_ring_reader_stall,
        audio_runtime_ring.check_renderer_ring_lanes,
    ):
        check()

    assert opens == [FANIN_STATUS_SOCKET]


def test_check_fanin_service_ok_with_expected_status(monkeypatch):
    _seed_units()
    _patch_status_reader(monkeypatch, _fanin_status_payload())
    r = audio_runtime_fanin.check_fanin_service()
    assert r.status == "ok"
    assert r.reason == ""


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
    _seed_units()
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda: persisted,
    )
    _patch_status_reader(monkeypatch, _fanin_status_payload())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "ok"


def test_check_fanin_service_fails_on_a_non_ring_live_transport(monkeypatch):
    _seed_units()
    _patch_status_reader(
        monkeypatch, _fanin_status_payload(transport="loopback")
    )

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "fail"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_TRANSPORT_NOT_RING
    assert r.speaker_silent is True


def test_check_fanin_service_fails_when_status_carries_no_ring_block(monkeypatch):
    _seed_units()
    _patch_status_reader(monkeypatch, _fanin_status_payload(ring=None))

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "fail"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_STATUS_MISSING_RING
    assert r.speaker_silent is True


def test_check_fanin_service_fails_when_status_carries_no_output_block(monkeypatch):
    payload = json.loads(_fanin_status_payload().decode())
    payload["output"] = []
    _seed_units()
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "fail"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_STATUS_MISSING_OUTPUT
    assert r.speaker_silent is True


def test_check_fanin_service_reports_pre_dsp_tts_loudness(monkeypatch):
    _seed_units()
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
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "ok"
    assert r.reason == ""


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
    fault = audio_runtime_fanin._assistant_gain_fault(loudness)
    assert (fault is not None) is faulty, fault


def test_check_fanin_service_ok_with_peak_capped_positive_gain(monkeypatch):
    """#2345: a peak-capped positive gain is the contract, not a warning."""
    _seed_units()
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
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "ok"
    assert r.reason == ""


def test_check_fanin_service_warns_when_gain_exceeds_the_peak_cap(monkeypatch):
    _seed_units()
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
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "warn"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_ASSISTANT_GAIN_OFF_CONTRACT


def test_check_fanin_service_warns_on_malformed_pre_dsp_tts_loudness(monkeypatch):
    _seed_units()
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
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "warn"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_ASSISTANT_GAIN_NOT_NUMERIC


def test_check_fanin_service_is_ok_when_loudness_telemetry_is_absent(monkeypatch):
    """An older fan-in publishes no assistant_loudness: nothing was observed
    about the gain, and the SERVICE is still active and responding."""
    _seed_units()
    payload = json.loads(_fanin_status_payload().decode())
    del payload["tts"]["assistant_loudness"]
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == "ok"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_LOUDNESS_TELEMETRY_MISSING


def test_check_fanin_service_fails_on_invalid_status_json(monkeypatch):
    _seed_units()
    _patch_status_reader(monkeypatch, b"not-json")
    r = audio_runtime_fanin.check_fanin_service()
    assert r.status == "fail"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_STATUS_MALFORMED


def test_check_fanin_service_fails_when_status_socket_unreachable(monkeypatch):
    _seed_units()
    _patch_unreachable_status(monkeypatch)
    r = audio_runtime_fanin.check_fanin_service()
    assert r.status == "fail"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_STATUS_UNREACHABLE
    assert r.speaker_silent is True


def test_check_fanin_service_warns_on_small_runtime_input_buffer(monkeypatch):
    """4096 is where AirPlay burst absorption was validated, not where audio
    breaks: a smaller buffer is a WARN on a speaker that still plays."""
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _fanin_status_payload(input_buffer_frames=2048),
    )
    r = audio_runtime_fanin.check_fanin_service()
    assert r.status == "warn"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_INPUT_BUFFER_UNDERSIZED


def _reorder_inputs(payload: dict) -> None:
    payload["inputs"].reverse()


def _drift_one_input(payload: dict) -> None:
    payload["inputs"][0]["pcm"] = "hw:Loopback,1,7"


def _duplicate_one_input(payload: dict) -> None:
    payload["inputs"].append(dict(payload["inputs"][0]))


def _drift_the_roster_and_wedge_the_work_loop(payload: dict) -> None:
    payload["inputs"][0]["pcm"] = "hw:Loopback,1,7"
    payload["input_buffer_frames"] = 2048
    payload["watchdog"]["last_progress_age_ms"] = 60_000


@pytest.mark.parametrize(
    "mutator, status, reason",
    [
        (_reorder_inputs, "ok", ""),
        (_drift_one_input, "warn", audio_runtime_fanin.REASON_FANIN_INPUTS_DRIFTED),
        (_duplicate_one_input, "warn", audio_runtime_fanin.REASON_FANIN_INPUTS_DRIFTED),
        (
            _drift_the_roster_and_wedge_the_work_loop,
            "warn",
            audio_runtime_fanin.REASON_FANIN_PROGRESS_STALE,
        ),
    ],
    ids=[
        "reordered-roster-is-not-drift",
        "drifted-lane-warns",
        "duplicated-lane-warns",
        "wedged-work-loop-is-not-masked-by-roster-or-buffer-faults",
    ],
)
def test_check_fanin_service_input_roster_compare(monkeypatch, mutator, status, reason):
    """The roster compare is order-insensitive but multiplicity-preserving: a
    lane-map REORDERING is not drift, but a duplicated lane must still be
    caught — a `set` compare would forgive it by collapsing the duplicate.
    The roster and buffer branches must not mask a wedged work loop: a box
    with all three defects reports the watchdog fault, not the cosmetic
    ones."""
    _seed_units()
    payload = json.loads(_fanin_status_payload().decode())
    mutator(payload)
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = audio_runtime_fanin.check_fanin_service()

    assert r.status == status
    assert r.reason == reason


def test_fanin_asound_wiring_fails_on_bare_renderer_lane(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            'slave {\n        pcm "hw:Loopback,0,1"\n        rate 48000\n        channels 2\n        format S32_LE\n    }',
            'slave.pcm "hw:Loopback,0,1"',
        ),
        tmp_path,
    )
    r = audio_runtime_fanin.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert r.reason == audio_runtime_fanin.REASON_ASOUND_LANE_WRONG_SLAVE


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
        audio_runtime_fanin, "read_declared_ring_wire_format", lambda: "S16_LE"
    )
    r = audio_runtime_fanin.check_fanin_asound_wiring()
    assert r.status == "fail"
    # A width shear is reported as a width shear: the lanes are wired correctly
    # and only their width disagrees, so calling them wrong slaves would send an
    # operator after the wrong fault.
    assert r.reason == audio_runtime_fanin.REASON_ASOUND_LANE_WIDTH_SHEAR


def test_fanin_asound_wiring_fails_on_legacy_renderer_dmix(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND + "\npcm.jasper_renderer_mix {\n    type dmix\n}\n",
        tmp_path,
    )
    r = audio_runtime_fanin.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert r.reason == audio_runtime_fanin.REASON_ASOUND_LEGACY_RENDERER_BLOCK


def test_fanin_asound_wiring_warns_on_stale_topology_env(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND,
        tmp_path,
        stale_topology_env=True,
    )
    r = audio_runtime_fanin.check_fanin_asound_wiring()
    assert r.status == "warn"
    assert r.reason == audio_runtime_fanin.REASON_ASOUND_STALE_TOPOLOGY_STATE


# ---------------------------------------------------------------------------
# check_fanin_tts_drops — dropped TTS audio at the pending budget means the
# user heard garbled/"fast-forward" replies (the 2026-06-11 JTS3 incident).
# ---------------------------------------------------------------------------


def _fanin_payload_with_tts(tts: dict) -> bytes:
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = tts
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    "tts, reason",
    [
        (
            {
                "enabled": True,
                "pending_frames": 0,
                "budget_frames": 96000,
                "protocol_errors": 0,
                "dropped_commands": 0,
                "dropped_audio_frames": 0,
            },
            "",
        ),
        (
            {
                "enabled": True,
                "protocol_errors": 1,
                "dropped_commands": 0,
                "dropped_audio_frames": 0,
            },
            audio_runtime_fanin.REASON_FANIN_TTS_PROTOCOL_ERRORS,
        ),
        # 82 dropped commands / 523200 frames ≈ 10.9 s at 48 kHz — the real
        # incident's order of magnitude.
        (
            {
                "enabled": True,
                "pending_frames": 89216,
                "budget_frames": 96000,
                "dropped_commands": 82,
                "dropped_audio_frames": 523200,
            },
            audio_runtime_fanin.REASON_FANIN_TTS_AUDIO_DROPPED,
        ),
        ({"enabled": False}, audio_runtime_fanin.REASON_FANIN_TTS_LANE_DISABLED),
    ],
    ids=["quiet", "protocol-error", "dropped-audio", "lane-disabled"],
)
def test_check_fanin_tts_counters_never_latch(monkeypatch, tts, reason):
    """Cumulative-since-start counters are reported, never warned on: one drop
    at boot would otherwise stay red until fan-in restarts, and a healer that
    restarts fan-in to clear it costs the household more audio."""
    _patch_status_reader(monkeypatch, _fanin_payload_with_tts(tts))

    r = audio_runtime_fanin.check_fanin_tts_drops()

    assert r.status == "ok"
    assert r.reason == reason


def test_check_fanin_tts_drops_skips_when_status_unreachable(monkeypatch):
    # Reachability is the 'jasper-fanin service' check's job; this check
    # must not double-report a down daemon.
    _patch_unreachable_status(monkeypatch)
    r = audio_runtime_fanin.check_fanin_tts_drops()
    assert r.status == "skipped"
    assert r.reason == audio_runtime_fanin.REASON_FANIN_TTS_STATUS_NOT_PROBED


# ===========================================================================
# check_aloop_registered_substreams — the snd-aloop remnant guard
#
# Every OPEN snd-aloop substream must have a purpose registered by one of the
# constants that own the pair allocation; anything holding a substream with no
# such purpose (pair 5, whose PCM definitions are gone) is a FAIL that
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
#: Pair 5 lost its PCM definitions; pairs 6 and 7 lost theirs to
#: ADR-0100, which moved outputd's passive content lane and fan-in's summed
#: music output onto SHM rings. deploy/alsa/asoundrc.jasper declares none of the
#: three, and deploy/modprobe.d/snd-aloop.conf records them as
#: reserved-not-reclaimed so no surviving pair renumbers.
_RETIRED_PAIRS = (5, 6, 7)


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

def test_no_aloop_card_is_skipped(proc_root, tmp_path):
    """A box without snd-aloop has no remnant to police."""
    empty = tmp_path / "asound"
    empty.mkdir()
    proc_root(empty)
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "skipped"
    assert result.reason == audio_runtime_fanin.REASON_ALOOP_NOT_LOADED


def test_all_closed_is_ok(proc_root, tmp_path):
    proc_root(_make_card(tmp_path))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "ok"
    assert result.reason == ""


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
    assert result.reason == ""


@pytest.mark.parametrize("pcm_dir", ["pcm0p", "pcm0c", "pcm1p", "pcm1c"])
@pytest.mark.parametrize("pair", _RETIRED_PAIRS)
def test_positive_control_foreign_substream_warns(
    proc_root, tmp_path, pcm_dir, pair
):
    """POSITIVE CONTROL — a deliberately-opened foreign substream trips it.

    Pairs 5, 6 and 7 are the foreign ones: pair 5's PCM definitions are gone
    and ADR-0100 deleted pairs 6 and 7's when the passive content
    lane and the summed music output both moved to SHM rings. A holder on any
    of them has resurrected a deleted lane — a rolled-back binary or a stale
    asoundrc — which is the regression this guard exists to catch, and which
    read as `ok` for as long as pairs 6 and 7 stayed in the registered set.
    Parametrised across all four PCM directions so a walker that only scanned
    the playback side would fail this.
    """
    proc_root(_make_card(tmp_path, {pcm_dir: [pair]}))
    result = audio_runtime_fanin.check_aloop_registered_substreams()
    assert result.status == "warn"
    assert result.reason == audio_runtime_fanin.REASON_ALOOP_UNREGISTERED_SUBSTREAM_OPEN


def test_offender_owner_line_names_the_pid():
    """The offender line the row carries names the holder.

    Asserted on the owner helper, whose whole output IS that string; the
    check's own verdict is pinned on status + reason above.
    """
    assert audio_runtime_fanin._aloop_substream_owner(_OPEN_STATUS).startswith("pid=4242")
    assert audio_runtime_fanin._aloop_substream_owner("state: RUNNING\n") == ""


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
    assert result.status == "warn"
    cap = audio_runtime_fanin._ALOOP_OFFENDER_DETAIL_CAP
    shown = result.detail.count("/sub")
    assert shown <= cap, f"listed {shown} offenders, cap is {cap}"


def test_unreadable_proc_is_skipped(proc_root, tmp_path):
    """'I could not look' is `skipped` — nothing was observed (ADR-0233).

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
    assert result.status == "skipped"
    assert result.reason == audio_runtime_fanin.REASON_ALOOP_PROC_UNREADABLE


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
    """Dropping a pair from the owning constant changes this set.

    Also THE structural pin that every entry of `_FANIN_EXPECTED_ALOOP_INPUTS`
    parses as an snd-aloop triple: an entry that does not would silently shrink
    the registered set, and the module derives it rather than guarding for it
    at runtime.
    """
    derived = audio_runtime_fanin._derive_registered_pairs()
    assert tuple(sorted(derived)) == _EXPECTED_REGISTERED_PAIRS
    assert len(derived) == len(audio_runtime_fanin._FANIN_EXPECTED_ALOOP_INPUTS)
    assert audio_runtime_fanin._pair_from_loopback_pcm("hw:OtherCard,1,0") is None


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


def _run_check(monkeypatch, *, cfg_text, tmp_path):
    """Put a loaded CamillaDSP config in front of ``check_fanin_coupling``.

    NO persisted coupling and NO outputd env: since ADR-0100 the check reads
    neither. Passing one here would let a test claim a gate the production check
    does not have.
    """
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
    # The evidence memo holds the REAL tuple shape, (statefile,
    # active_config_path|None) — a str-only seed masked a production TypeError.
    evidence.seed("camilla_config", (cfg.parent, str(cfg)))
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
    assert res.reason == audio_runtime_fanin.REASON_COUPLING_GRAPH_NOT_RING


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
    assert res.reason == audio_runtime_fanin.REASON_COUPLING_GRAPH_NOT_RING


@pytest.mark.parametrize(
    "cfg_text",
    [
        # Two top-level `devices:` keys — the subset parser refuses to guess
        # which one is live.
        "devices:\n  capture:\n    type: Alsa\ndevices:\n  capture:\n    type: File\n",
        # A graph with no `devices:` block at all.
        "filters:\n  flat:\n    type: Gain\n",
    ],
    ids=("duplicate-devices", "no-devices-block"),
)
def test_a_loaded_config_whose_devices_block_does_not_parse_warns(
    monkeypatch, tmp_path, cfg_text
):
    """A config IS loaded and no axis can be judged: that is a warn, not the
    "no config yet" skip a fresh box gets."""
    res = _run_check(monkeypatch, cfg_text=cfg_text, tmp_path=tmp_path)
    assert res.status == "warn"
    assert res.reason == audio_runtime_fanin.REASON_COUPLING_DEVICES_UNPARSED


def test_check_skipped_when_no_loaded_capture(monkeypatch, tmp_path):
    """A devices block that parses but declares no capture lane: nothing to
    compare, and nothing wrong with the file."""
    res = _run_check(
        monkeypatch, cfg_text="devices:\n  samplerate: 48000\n", tmp_path=tmp_path
    )
    assert res.status == "skipped"
    assert res.reason == audio_runtime_fanin.REASON_COUPLING_NO_LOADED_CAPTURE


# --- check_fanin_coupling_value: persisted coupling value must be recognized --


@pytest.mark.parametrize(
    "raw,status,reason",
    [
        (None, "ok", audio_runtime_fanin.REASON_COUPLING_FILE_ABSENT),
        ("", "ok", ""),
        ("shm_ring", "ok", ""),
        ("transport_pipe", "warn", audio_runtime_fanin.REASON_COUPLING_TOKEN_UNKNOWN),
        ("loopback", "warn", audio_runtime_fanin.REASON_COUPLING_TOKEN_UNKNOWN),
    ],
    ids=["absent_file", "absent_key", "declared", "removed_token", "retired_token"],
)
def test_check_fanin_coupling_value_reads_the_shared_predicate(
    monkeypatch, tmp_path, raw, status, reason
):
    """ADR-0100: only a value fan-in REFUSES is a finding.

    A migrating box carrying the removed ``transport_pipe`` token (or a typo)
    warns until the reconciler converges it. An ABSENT or empty key is not that
    state — fan-in serves the ring for it — so this surface must agree with the
    daemon rather than with the presence of a token (#3655).
    """
    fanin_env = tmp_path / "fanin.env"
    if raw is not None:
        fanin_env.write_text(f"JASPER_FANIN_CAMILLA_COUPLING={raw}\n")
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    res = audio_runtime_fanin.check_fanin_coupling_value()
    assert res.status == status
    assert res.reason == reason


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
    assert res.reason == ""


def test_ring_warns_when_the_loaded_graph_reverted_off_the_ring(monkeypatch, tmp_path):
    # A camilla restart re-seeded a stale artifact whose capture is not
    # jts_ring_capture, so CamillaDSP sources a device fan-in is not writing.
    # Nothing about the persisted files can excuse it.
    res = _run_check(monkeypatch, cfg_text=_ALSA_CFG, tmp_path=tmp_path)
    assert res.status == "warn"
    assert res.reason == audio_runtime_fanin.REASON_COUPLING_GRAPH_NOT_RING


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

# Same mid-ladder playback, but capture also broken — a second, independent
# fault the ladder does not excuse.
_STALE_RING_CAPTURE_BROKEN_CFG = _STALE_RING_CFG.replace(
    'device: "jts_ring_capture"', 'device: "plug:jasper_capture"',
)


@pytest.mark.parametrize(
    "cfg_text, roleful, status, reason",
    [
        # A ROLEFUL box on a graph its CLEAR endpoint marker does not name is
        # mid-ladder: the graph moves first, the marker second, so this is
        # the ladder in flight, not a fault — ok, with the ladder's remedy.
        (_STALE_RING_CFG, True, "ok",
         audio_runtime_fanin.REASON_COUPLING_ACTIVE_LADDER_PENDING),
        # CONTROL: on a PASSIVE box there is no ladder in flight, so the
        # mismatch is a real fault — the reconciler's own pass is the fix.
        (_STALE_RING_CFG, False, "warn", audio_runtime_fanin.REASON_COUPLING_GRAPH_NOT_RING),
        # Roleful and mid-ladder on playback, but capture is ALSO broken: a
        # real fault the ladder does not excuse, so it must not be ok.
        (_STALE_RING_CAPTURE_BROKEN_CFG, True, "warn",
         audio_runtime_fanin.REASON_COUPLING_GRAPH_NOT_RING),
    ],
    ids=["pending-ladder", "passive-mismatch", "capture-mismatch-while-roleful"],
)
def test_a_roleful_box_with_a_clear_marker_is_its_own_state(
    monkeypatch, tmp_path, cfg_text, roleful, status, reason
):
    monkeypatch.setattr(audio_runtime_fanin, "_requires_roleful_graph", lambda: roleful)

    res = _run_check(monkeypatch, cfg_text=cfg_text, tmp_path=tmp_path)

    assert res.status == status
    assert res.reason == reason


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
    """
    import jasper.output_topology as output_topology

    def _raise(*_a, **_kw):
        raise exc

    monkeypatch.setattr(output_topology, "load_output_topology_strict", _raise)

    assert audio_runtime_fanin._requires_roleful_graph() is False


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

    assert audio_runtime_fanin._requires_roleful_graph() is True


# ===========================================================================
# check_fanin_binary_installed
# ===========================================================================

def _stage_fanin_binary(monkeypatch, tmp_path, *, present, mode=0o755):
    binary = tmp_path / "jasper-fanin"
    if present:
        binary.write_bytes(b"x" * 2500)
        binary.chmod(mode)
    monkeypatch.setattr(audio_runtime_fanin, "Path", lambda _path: binary)


@pytest.mark.parametrize(
    "stage_kwargs, status, reason",
    [
        ({"present": False}, "fail", audio_runtime_fanin.REASON_FANIN_BINARY_MISSING),
        (
            {"present": True, "mode": 0o644},
            "fail",
            audio_runtime_fanin.REASON_FANIN_BINARY_NOT_EXECUTABLE,
        ),
        ({"present": True}, "ok", ""),
    ],
    ids=["missing", "not_executable", "installed"],
)
def test_check_fanin_binary_installed(
    monkeypatch, tmp_path: Path, stage_kwargs, status, reason
) -> None:
    _stage_fanin_binary(monkeypatch, tmp_path, **stage_kwargs)

    result = audio_runtime_fanin.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == status
    assert result.reason == reason
