# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor jasper-outputd checks."""

import json
from pathlib import Path

import pytest

from jasper import audio_runtime_plan
from jasper.cli import doctor
from jasper.cli.doctor import audio_runtime_outputd
from jasper.control import audio_health

from ._doctor_audio_runtime_fixtures import (
    _fanin_status_payload,
    _outputd_status_payload,
    _patch_status_reader,
    _seed_units,
)
from .active_speaker_fixtures import (
    PASSIVE_ONLY_DAC_ID,
    PASSIVE_ONLY_DAC_LABEL,
    register_passive_only_dac,
)

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
    model. The reconciler no longer states ``JASPER_OUTPUTD_CONTENT_PCM`` at all
    — it REMOVES the key, so ABSENT is the converged steady state for every box
    shape — and ``Config::from_env`` refuses a composite sink on the DIRECT
    bridge outright (EX_CONFIG), so a composite box that is running at all is a
    ring box. ``active_endpoint=True`` adds the reconciler's endpoint marker,
    which is what selects the ACTIVE-ring transport shape rather than the
    full-range stereo Ring B — the marker, never the observed device.

    Call this LAST, after ``_patch_status_reader``: that one points the
    endpoint evidence at the stereo ring, so an earlier call is silently undone.
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


def test_outputd_service_fails_when_disabled(monkeypatch):
    _seed_units(enabled="disabled")
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_UNIT_NOT_ENABLED


def test_outputd_service_ok_with_expected_status(monkeypatch):
    _seed_units()
    _patch_status_reader(monkeypatch, _outputd_status_payload())
    r = doctor.check_outputd_service()
    assert r.status == "ok", r.detail
    assert r.reason == ""


def test_the_doctor_reads_outputd_env_through_the_units_own_layering(
    monkeypatch, tmp_path
):
    """The LAST EnvironmentFile wins, exactly as it does for outputd itself.

    jasper-outputd.service names outputd.env then grouping-outputd.env, so a
    bonded box's grouping pin overrides the base file. Reading only the base
    layer reported such a box on an env it is not running — which is what made
    the transport checks contradict the grouping doctor.
    """
    from jasper.fanin_coupling import OUTPUTD_CONTENT_BRIDGE_ENV_VAR

    base = tmp_path / "outputd.env"
    base.write_text(f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring\n", encoding="utf-8")
    grouping = tmp_path / "grouping-outputd.env"
    grouping.write_text(f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=direct\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(base))
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping)
    )

    env = audio_runtime_outputd._outputd_reconciled_env()

    assert env[OUTPUTD_CONTENT_BRIDGE_ENV_VAR] == "direct"


def test_outputd_content_bridge_detail_reports_every_mode():
    """The surviving `/state.content_bridge` readout is a plain mode string.

    The rate-matched bridge's fill/ppm/lock counters were deleted with it, so
    this is all the doctor reports for the axis now. Pinned across all three
    branches because `direct` alone would pass even if the function hardcoded
    it: `shm_ring` proves it reads the payload, and the two malformed shapes
    prove it degrades to `missing` rather than raising inside a doctor check.
    """
    from jasper.cli.doctor.audio_runtime_outputd import _outputd_content_bridge_detail

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
    _seed_units()
    _patch_status_reader(
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
    assert r.reason == ""


def test_outputd_service_fails_shm_ring_missing_ring_geometry(monkeypatch, tmp_path):
    """shm_ring with no content.ring geometry contract fails loud (a
    pre-honesty-fix outputd binary, or a corrupt STATUS)."""
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _seed_units()
    payload = json.loads(
        _outputd_status_payload(
            content_source="shm_ring",
            content_buffer_frames=1024,
            period_frames=1024,
        ).decode()
    )
    del payload["content"]["ring"]
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_RING_CONTRACT_MISSING


def test_outputd_service_fails_shm_ring_slot_frames_mismatch(monkeypatch, tmp_path):
    """The shm_ring branch keeps its teeth: a ring slot that does not match the
    DAC period is a real geometry break, not an exempted synthetic."""
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _seed_units()
    _patch_status_reader(
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
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_RING_SLOT_FRAMES_MISMATCH


def test_outputd_service_fails_shm_ring_capacity_incoherent(monkeypatch, tmp_path):
    """content.ring.capacity_frames must equal n_slots*slot_frames — a mismatch
    is dishonest STATUS and fails loud."""
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _seed_units()
    _patch_status_reader(
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
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_RING_CAPACITY_INCOHERENT


def test_outputd_service_fails_when_the_daemon_lags_its_own_env(
    monkeypatch, tmp_path
):
    """The check keeps its teeth: a ring bridge in outputd's env with outputd
    still live on the ALSA content lane (it missed the flip restart) fails with
    the reconcile remedy.

    The expectation comes from outputd's OWN env, not from
    ``JASPER_FANIN_CAMILLA_COUPLING``: that file selects nothing under ADR-0100,
    so it cannot predict what outputd opened.
    """
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_status_payload(content_source="alsa"),
    )

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_CONTENT_SOURCE_MISMATCH


def test_outputd_service_ok_with_single_alsa_active_lane(monkeypatch, tmp_path):
    """An ACTIVE single-ALSA box is healthy on the ring, declaring NO content PCM.

    The width readout is the assertion that matters, and it comes from the env,
    independently of the transport.
    """
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_status_payload(dac_pcm=doctor._OUTPUTD_EXPECTED_DAC_PCM),
    )
    _patch_ring_coupled_box(
        monkeypatch, tmp_path, active_endpoint=True, active_channels=2
    )

    r = doctor.check_outputd_service()

    assert r.status == "ok", r.detail
    assert r.reason == ""


def _patch_disconnected_post_dsp_route(monkeypatch, tmp_path) -> None:
    """Camilla on the retired ACTIVE snd-aloop lane, outputd off the ring.

    The bridge is STATED, because the unpaired-route error this stages lives in
    the non-ring plan and an absent key resolves to the ring. The daemon in the
    STATUS payload is the one that env starts, so it reports the retired source
    and its negotiated ALSA buffer.
    """
    env = tmp_path / "outputd-disconnected.env"
    env.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=direct\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env))
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_status_payload(content_source="alsa", content_buffer_frames=4096),
    )
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
    _patch_disconnected_post_dsp_route(monkeypatch, tmp_path)

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_TRANSPORT_ROUTE_UNPAIRED


def test_route_disconnect_remedy_does_not_recommend_an_impossible_reconcile(
    monkeypatch,
    tmp_path,
):
    """When the saved layout needs a lane the DAC does not have, running the
    reconciler cannot help — it is already resolving passive correctly. The
    remedy must say what actually clears it instead of sending the operator
    into a loop.

    Asserted on the remedy STRING because that string is this helper's whole
    output; the check itself is pinned on status + reason above.
    """
    register_passive_only_dac(monkeypatch)
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))
    _write_no_lane_active_topology(topo_path)

    remedy = audio_runtime_outputd._transport_route_remedy()

    assert PASSIVE_ONLY_DAC_LABEL in remedy
    assert "/sound/setup/" in remedy
    assert "audio-hardware-reconcile" not in remedy
    # Passive is not a free remedy: it sends full-range into every assigned
    # output, which on an actively-wired cabinet reaches a bare tweeter. An
    # operator following doctor's advice must be told that.
    assert "full-range audio to every output" in remedy
    assert "built-in passive crossover" in remedy
    assert "attach an active-capable DAC" in remedy


def test_outputd_service_warns_when_transport_evidence_is_unavailable(monkeypatch):
    _seed_units()
    _patch_status_reader(monkeypatch, _outputd_status_payload())
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
            devices=None,
            errors=("statefile unavailable",),
        ),
    )

    r = doctor.check_outputd_service()

    assert r.status == "warn"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_TRANSPORT_EVIDENCE_UNKNOWN


def test_outputd_service_ok_when_loudness_is_owned_by_fanin(monkeypatch):
    payload = json.loads(_outputd_status_payload().decode())
    payload.pop("assistant_loudness", None)
    _seed_units()
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "ok"
    assert r.reason == ""


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
    _seed_units()
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "warn"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_ASSISTANT_GAIN_OFF_CONTRACT


def test_outputd_service_fails_when_dual_apple_status_missing(monkeypatch, tmp_path):
    _seed_units()
    payload = json.loads(
        _outputd_status_payload(
            sink_mode="dual_apple",
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
        ).decode()
    )
    payload.pop("dual_apple", None)
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())
    _patch_ring_coupled_box(monkeypatch, tmp_path, active_endpoint=True)

    r = doctor.check_outputd_service()
    assert r.status == "fail", r.detail
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_DUAL_APPLE_STATUS_MISSING


def test_outputd_service_warns_when_dual_apple_pcm_link_missing(monkeypatch, tmp_path):
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_status_payload(
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
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_DUAL_APPLE_NOT_LINKED


def test_outputd_service_ok_with_dual_apple_status(monkeypatch, tmp_path):
    """The armed composite box on the ring — jts.local's own shape.

    #2285 P2 moved the three dual_apple tests off the direct bridge. A composite
    sink declaring no content PCM is REFUSED at parse on the direct bridge
    (``Config::from_env``, EX_CONFIG), and the only other direct-bridge shape —
    a composite naming the passive lane — is the 4ch-over-a-2ch-slave reuse this
    PR rejected as hearing-adjacent. A running composite box is a ring box.
    """
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_status_payload(
            sink_mode="dual_apple",
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
        ),
    )
    _patch_ring_coupled_box(monkeypatch, tmp_path, active_endpoint=True)
    r = doctor.check_outputd_service()
    assert r.status == "ok", r.detail
    assert r.reason == ""


def test_outputd_service_fails_on_fake_backend(monkeypatch):
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_status_payload(backend="fake"),
    )
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_BACKEND_NOT_ALSA


def test_outputd_service_fails_on_small_runtime_buffers(monkeypatch):
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_status_payload(dac_buffer_frames=1024),
    )
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_DAC_BUFFER_UNDERSIZED


def test_outputd_service_fails_when_reference_contract_missing(monkeypatch):
    payload = json.loads(_outputd_status_payload().decode())
    payload["reference_outputs"] = {}
    _seed_units()
    _patch_status_reader(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert r.reason == audio_runtime_outputd.REASON_OUTPUTD_REFERENCE_SOURCE_UNEXPECTED


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


@pytest.mark.parametrize(
    "aec_clock",
    [
        _aec_clock_block(verdict="coherent", status="locked", ppm=1.2),
        _aec_clock_block(verdict="compensable", status="locked", ppm=42.0),
        # Observe mode: the chip-ref writer is armed purely to MEASURE drift on
        # the software-AEC3 path.
        _aec_clock_block(
            verdict="compensable", status="locked", ppm=42.0, observe=True
        ),
        # The initial lock window. Warning here would cry wolf on every boot,
        # before the estimator has enough samples.
        _aec_clock_block(verdict="fallback", status="observing", ppm=None),
    ],
)
def test_aec_clock_drift_ok_for_every_healthy_estimator_state(monkeypatch, aec_clock):
    _seed_units()
    _patch_status_reader(
        monkeypatch, _outputd_aec_clock_payload(aec_clock=aec_clock)
    )

    r = doctor.check_aec_clock_drift()

    assert r.status == "ok"
    assert r.reason == ""


def test_aec_clock_drift_warns_when_untrusted(monkeypatch):
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(verdict="fallback", status="untrusted", ppm=None)
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "warn"
    assert r.reason == audio_runtime_outputd.REASON_AEC_CLOCK_UNTRUSTED


def test_aec_clock_drift_warns_when_optional_chip_reference_is_unavailable(
    monkeypatch,
):
    _seed_units()
    _patch_status_reader(
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
    assert r.reason == audio_runtime_outputd.REASON_AEC_CLOCK_CHIP_REF_UNAVAILABLE


@pytest.mark.parametrize(
    "payload_kwargs, reason",
    [
        (
            {"chip_ref_pcm": None},
            audio_runtime_outputd.REASON_AEC_CLOCK_CHIP_REF_NOT_CONFIGURED,
        ),
        # A chip-ref is present but the outputd build has no aec_clock block.
        ({"aec_clock": None}, audio_runtime_outputd.REASON_AEC_CLOCK_BLOCK_ABSENT),
    ],
)
def test_aec_clock_drift_stands_down_without_an_estimate(
    monkeypatch, payload_kwargs, reason
):
    _seed_units()
    _patch_status_reader(
        monkeypatch, _outputd_aec_clock_payload(**payload_kwargs)
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "skipped"
    assert r.reason == reason


def test_aec_clock_drift_skips_when_outputd_disabled(monkeypatch):
    _seed_units(enabled="disabled")
    r = doctor.check_aec_clock_drift()
    assert r.status == "skipped"
    assert r.reason == audio_runtime_outputd.REASON_AEC_CLOCK_OUTPUTD_NOT_ENABLED


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


def _buffer_health(data, *, period=128, content_hop=None):
    from jasper.fanin_coupling import COUPLING_SHM_RING as TRANSPORT_SHM_RING

    return audio_runtime_outputd._outputd_buffer_health(
        data,
        data["content"],
        content_hop=content_hop or TRANSPORT_SHM_RING,
        content_buffer=data["content"]["buffer_frames"],
        dac_buffer=period * 4,
        period_frames=period,
    )


@pytest.mark.parametrize(
    "shape,expect_fail",
    [
        ("off_ring", True),
        ("dac_content_ring", False),
    ],
)
def test_the_alsa_jitter_floor_applies_to_exactly_the_alsa_class(shape, expect_fail):
    """The content hop's CLASS decides the floor, not a second marker read.

    outputd seeds `content.buffer_frames` to the period and negotiates no
    content PCM, so a period-sized buffer is the honest value for every SHM hop
    — including a bonded member's return ring. Only the ALSA class owes the
    ">= 2x period" jitter margin.
    """
    from jasper.cli.doctor._shared import CheckResult

    result = audio_runtime_outputd._outputd_buffer_health(
        {},
        {"source": "alsa"},
        content_hop=shape,
        content_buffer=128,
        dac_buffer=512,
        period_frames=128,
    )
    assert isinstance(result, CheckResult) is expect_fail, result


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


@pytest.mark.parametrize(
    "status_kwargs, reason",
    [
        # S16_LE is the mismatching FORMAT token: the resolver's default went
        # WIDE, so an undeclared box's wire IS S32_LE.
        ({"fmt": "S16_LE"}, audio_runtime_outputd.REASON_OUTPUTD_RING_FORMAT_SHEAR),
        # The channels axis has teeth independently of the format axis, so the
        # format is pinned to the box's default to isolate it.
        (
            {"fmt": "S32_LE", "channels": 6},
            audio_runtime_outputd.REASON_OUTPUTD_RING_CHANNELS_SHEAR,
        ),
    ],
    ids=["format", "channels"],
)
def test_buffer_health_fails_on_an_attached_wire_the_box_does_not_declare(
    status_kwargs, reason
):
    result = _buffer_health(_outputd_ring_status(**status_kwargs))

    assert not isinstance(result, str), "a wire shear must be a CheckResult, not detail"
    assert result.status == "fail"
    assert result.reason == reason


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
    import jasper.fanin.ring_health as rh
    import jasper.fanin_coupling as fc

    sentinel = object()
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
    assert doctor.audio_runtime_outputd._outputd_xrun_rate_warning(quiet, quiet) is None


def test_outputd_xrun_warning_suppressed_for_stale_burst():
    """A high all-time rate whose last xrun is OLD (a cleared deploy-time
    burst) must NOT warn — the WARN is for a sustained, *current* problem."""
    stale = _xrun_section(
        rate_per_hour=50.0,
        last_xrun_age_ms=doctor.audio_runtime_outputd._OUTPUTD_XRUN_RECENT_AGE_MS + 1,
    )
    assert doctor.audio_runtime_outputd._outputd_xrun_rate_warning(stale, stale) is None


def test_outputd_xrun_warning_suppressed_for_recent_single_blip():
    """A recent xrun with a LOW sustained rate (one transient blip) must not
    warn — only a rate at/above the threshold qualifies."""
    blip = _xrun_section(
        rate_per_hour=doctor.audio_runtime_outputd._OUTPUTD_XRUN_RATE_WARN_PER_HOUR - 0.1,
        last_xrun_age_ms=1000,
    )
    assert doctor.audio_runtime_outputd._outputd_xrun_rate_warning(blip, blip) is None


def test_outputd_xrun_warning_fires_on_recent_sustained_rate():
    """Recent xrun AND a sustained rate at/above threshold → warn, naming the
    offending lane and both fields."""
    hot = _xrun_section(
        rate_per_hour=doctor.audio_runtime_outputd._OUTPUTD_XRUN_RATE_WARN_PER_HOUR,
        last_xrun_age_ms=2000,
    )
    quiet = _xrun_section(rate_per_hour=0.0, last_xrun_age_ms=None)
    reason = doctor.audio_runtime_outputd._outputd_xrun_rate_warning(quiet, hot)
    assert reason is not None
    assert "dac" in reason
    assert "xrun_rate_per_hour" in reason
    assert "last_xrun_age_ms" in reason


def test_outputd_xrun_warning_reports_worst_lane():
    """When both lanes qualify, the higher-rate lane is reported."""
    content = _xrun_section(rate_per_hour=8.0, last_xrun_age_ms=1000)
    dac = _xrun_section(rate_per_hour=40.0, last_xrun_age_ms=1000)
    reason = doctor.audio_runtime_outputd._outputd_xrun_rate_warning(content, dac)
    assert reason is not None
    assert reason.startswith("dac ")


# --- the dac-content marker reaches the doctor's content-source expectation --


def _transport_health(env: dict[str, str], *, content_source: str):
    """Run the doctor's transport-health helper over one env + STATUS pairing.

    Returns the helper's own value: a ``CheckResult`` when it refused, or its
    4-tuple when it got through. Nothing here reads a message string — the
    discrimination is the RETURN SHAPE, which is what the caller branches on.
    """
    payload = json.loads(_outputd_status_payload(content_source=content_source))
    return doctor.audio_runtime_outputd._outputd_transport_health(
        payload,
        payload["content"],
        payload["dac"],
        outputd_env=env,
        sink_mode=payload["sink_mode"],
        active_channels=None,
        expected_dac_pcm=payload["dac"]["pcm"],
    )


_LANE_ARMED = {"JASPER_OUTPUTD_DAC_CONTENT_LANE": "1"}


def test_a_marker_armed_member_expects_the_source_outputd_publishes():
    """THE doctor half of the cutover: a bonded member must not FAIL.

    Its bridge key is absent, which alone reads as the central ring and derives
    `content.source == "shm_ring"`. But outputd resolved the dac-content return
    ring, left `shm_ring` unattached, and therefore publishes `alsa` — so the
    expectation has to be derived with the marker in hand, or every healthy
    bonded speaker fails this check.
    """
    from jasper.cli.doctor._shared import CheckResult

    assert not isinstance(
        _transport_health(_LANE_ARMED, content_source="alsa"), CheckResult
    )


def test_a_marker_armed_member_still_refuses_a_central_ring_source():
    """The narrowing is not a blanket pass: the marker names ONE expectation.

    A daemon reporting `shm_ring` under an armed marker is running an env older
    than the file it was given — the exact staleness this check exists to
    catch — so it must still refuse.
    """
    from jasper.cli.doctor._shared import CheckResult

    result = _transport_health(_LANE_ARMED, content_source="shm_ring")
    assert isinstance(result, CheckResult)
    assert result.status == "fail"


def test_an_unbonded_box_keeps_expecting_the_central_ring():
    """The shipped verdict where no marker is armed, both ways round."""
    from jasper.cli.doctor._shared import CheckResult

    assert not isinstance(
        _transport_health({}, content_source="shm_ring"), CheckResult
    )
    stale = _transport_health({}, content_source="alsa")
    assert isinstance(stale, CheckResult)
    assert stale.status == "fail"


def test_outputd_service_ok_on_a_marker_armed_member(monkeypatch, tmp_path):
    """END TO END: a healthy bonded member must come out `ok`, not `fail`.

    Three of this check's steps read the content hop, and all three had to learn
    the marker together or the box fails on one of them: the content.source
    expectation (`alsa`, not `shm_ring`), the transport coherence report (the
    dac-content shape, not an off-ring one), and the buffer geometry — outputd
    seeds `content.buffer_frames` to the period and negotiates no content PCM,
    so the ALSA ">= 2x period" jitter floor would fail every such box.
    """
    grouping_env = tmp_path / "grouping-outputd.env"
    grouping_env.write_text("JASPER_OUTPUTD_DAC_CONTENT_LANE=1\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping_env)
    )
    _seed_units()
    _patch_status_reader(
        monkeypatch,
        # What outputd publishes with no CENTRAL ring attached: `alsa`, and a
        # period-sized content buffer with no content.ring sub-block.
        _outputd_status_payload(
            content_source="alsa", content_buffer_frames=1024, period_frames=1024
        ),
    )
    # RING A IS STILL LIVE on a bonded member — fan-in serves it here like
    # anywhere else — so the graph captures it. (The status-socket helper's own
    # default pairs an `alsa` content source with the snd-aloop tap, which is
    # the genuinely off-ring box, not this one.)
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE

    monkeypatch.setattr(
        audio_runtime_plan,
        "output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
            devices={
                "playback_device": RING_PLAYBACK_DEVICE,
                "capture_device": RING_CAPTURE_DEVICE,
            }
        ),
    )

    r = doctor.check_outputd_service()

    assert r.status == "ok", r.detail
    assert r.reason == ""


@pytest.mark.parametrize(
    "stale_ms,payload,check",
    [
        (
            audio_health.FANIN_STALE_MS,
            _fanin_status_payload,
            lambda: doctor.check_fanin_service(),
        ),
        (
            audio_health.OUTPUTD_STALE_MS,
            _outputd_status_payload,
            lambda: doctor.check_outputd_service(),
        ),
    ],
    ids=["fanin", "outputd"],
)
@pytest.mark.parametrize("over", [False, True])
def test_doctor_stale_warning_uses_the_household_threshold(
    monkeypatch, stale_ms, payload, check, over
) -> None:
    """One tolerance for "the transport stopped moving": the doctor must not
    warn about an age the /system household card still calls healthy."""
    _seed_units()
    _patch_status_reader(
        monkeypatch, payload(progress_age_ms=stale_ms + (1 if over else 0))
    )

    assert check().status == ("warn" if over else "ok")
