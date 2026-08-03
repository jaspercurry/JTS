# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Wake-corpus capture-plan, bridge-output, and capture-health tests."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from jasper.wake_corpus import bridge_session
from jasper.web import wake_corpus_setup

from tests.wake_corpus_setup_fixtures import (
    _backend_fixture,
    _patch_udp,
    _session_metadata,
    _use_tmp_bridge_env,
)

_IMPORTED_FIXTURES = (_backend_fixture, _patch_udp)

# ---------------------------------------------------------------------------
# Raw mic 0 leg — 4th capture leg, opt-in per session
# ---------------------------------------------------------------------------


def test_legs_includes_raw0_in_tuple() -> None:
    """The LEGS tuple must include raw0 so downstream tools that
    iterate over it pick up the new quadrant directories."""
    assert "raw0" in wake_corpus_setup.LEGS
    assert wake_corpus_setup.BASE_LEGS == ("on", "off")
    assert wake_corpus_setup.DTLN_LEG == "dtln"


def test_default_aec_raw0_port_constant_exposed() -> None:
    """Recorder re-exports the shared default port so socket-activation
    + CLI both see the same number."""
    from jasper.cli.wake_enroll import DEFAULT_AEC_RAW0_PORT
    assert DEFAULT_AEC_RAW0_PORT == 9879


def test_default_ports_dict_includes_all_four_legs(tmp_path: Path) -> None:
    """A backend constructed without explicit ports defaults to all
    known leg ports (recorder subscribes to a session-selected subset)."""
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    assert set(b._ports.keys()) == {
        "on", "off", "dtln", "raw0", "ref", "usb_raw",
        "usb_webrtc", "usb_dtln",
        "chip_aec_150", "chip_aec_210",
        "xvf_raw0_webrtc_aec3", "xvf_raw0_dtln",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    }


def test_build_ports_keeps_raw0_when_dtln_disabled() -> None:
    """Low-RAM installs can skip DTLN without losing the raw0 corpus leg."""
    ports = wake_corpus_setup.build_ports(
        aec_on_port=1111,
        aec_off_port=2222,
        aec_dtln_port=3333,
        aec_raw0_port=4444,
        aec_ref_port=5555,
        aec_usb_raw_port=6666,
        aec_usb_webrtc_port=7777,
        aec_usb_dtln_port=8888,
        include_chip_corpus=False,
        include_dtln=False,
    )
    assert ports == {
        "on": 1111,
        "off": 2222,
        "raw0": 4444,
        "ref": 5555,
        "usb_raw": 6666,
        "usb_webrtc": 7777,
        "usb_dtln": 8888,
        **wake_corpus_setup.DEFAULT_AEC3_SWEEP_PORTS,
    }


def test_combined_web_entrypoint_includes_raw0_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The socket-activated jasper-web path must also pass raw0.

    Regression guard for the production path: RecordingBackend defaults
    included raw0, but jasper.web.__main__ supplied an explicit 3-leg
    map, so raw0-enabled sessions could silently produce only 3 WAVs.
    """
    from jasper.web import __main__ as web_main

    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_ON_PORT", "1100")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_OFF_PORT", "2200")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_DTLN_PORT", "3300")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_RAW0_PORT", "4400")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_REF_PORT", "5500")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_USB_RAW_PORT", "6600")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_USB_WEBRTC_PORT", "7700")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_USB_DTLN_PORT", "8800")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_CHIP_AEC_150_PORT", "8810")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_CHIP_AEC_210_PORT", "8820")
    monkeypatch.setenv(
        "JASPER_WAKE_CORPUS_AEC_XVF_RAW0_WEBRTC_AEC3_PORT",
        "8890",
    )
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_XVF_RAW0_DTLN_PORT", "8900")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC3_SWEEP_AEC3_VARIANT_1_PORT", "9901")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC3_SWEEP_AEC3_VARIANT_2_PORT", "9902")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC3_SWEEP_AEC3_VARIANT_3_PORT", "9903")

    assert web_main._wake_corpus_ports_from_env() == {
        "on": 1100,
        "off": 2200,
        "dtln": 3300,
        "raw0": 4400,
        "ref": 5500,
        "usb_raw": 6600,
        "usb_webrtc": 7700,
        "usb_dtln": 8800,
        "chip_aec_150": 8810,
        "chip_aec_210": 8820,
        "xvf_raw0_webrtc_aec3": 8890,
        "xvf_raw0_dtln": 8900,
        "aec3_variant_1": 9901,
        "aec3_variant_2": 9902,
        "aec3_variant_3": 9903,
    }


def test_combined_web_entrypoint_keeps_raw0_when_dtln_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jasper.web import __main__ as web_main

    monkeypatch.setenv("JASPER_WAKE_CORPUS_DTLN", "0")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_RAW0_PORT", "4400")

    ports = web_main._wake_corpus_ports_from_env()
    assert "dtln" not in ports
    assert ports["raw0"] == 4400
    assert ports["ref"] == wake_corpus_setup.DEFAULT_AEC_REF_PORT
    assert ports["usb_dtln"] == wake_corpus_setup.DEFAULT_AEC_USB_DTLN_PORT
    assert ports["aec3_variant_1"] == wake_corpus_setup.DEFAULT_AEC3_SWEEP_PORTS[
        "aec3_variant_1"
    ]


def test_combined_web_lazy_wake_corpus_serves_after_first_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy loader must not overwrite BaseHTTPRequestHandler.handle.

    Regression guard for the production 502: the first request loaded
    wake_corpus_setup successfully, then later requests accepted and
    immediately closed because the lazy class had copied socketserver's
    no-op BaseRequestHandler.handle onto itself.
    """
    import http.client

    from jasper.web import __main__ as web_main

    monkeypatch.setattr(bridge_session, "voice_daemon_active", lambda: False)
    monkeypatch.setattr(
        bridge_session,
        "bridge_output_status",
        lambda: {
            "dtln": True,
            "ref": False,
            "usb": False,
            "usb_dtln": False,
            "env_path": str(tmp_path / "wake_corpus_bridge.env"),
        },
    )

    server = web_main._make_lazy_wake_corpus_server(
        ("127.0.0.1", 0),
        output_dir=tmp_path / "out",
        ports={"on": 9876, "off": 9877},
        csrf_token="test-token",
    )
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    def get(path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    try:
        status, body = get("/")
        assert status == 200
        assert b"Wake-word corpus" in body

        status, body = get("/api/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["voice_daemon_active"] is False

        status, body = get("/api/sessions")
        assert status == 200
        assert json.loads(body) == {"sessions": []}
    finally:
        backend_obj = getattr(server.RequestHandlerClass, "backend", None)
        server.shutdown()
        server.server_close()
        if backend_obj is not None:
            backend_obj.shutdown()
        th.join(timeout=2)


def test_begin_session_default_excludes_raw0(backend) -> None:
    """Default begin_session() does NOT opt into raw0 — historical
    pre-flag sessions shouldn't suddenly start capturing 4 legs."""
    backend.begin_session("jasper")
    assert backend.include_raw_mic_0() is False
    assert backend.include_dtln() is True


def test_begin_session_can_disable_dtln(backend, tmp_path: Path) -> None:
    """XVF DTLN is session-selectable so low-RAM corpus runs can stay
    on the two cheap production legs."""
    backend.begin_session("jasper", include_dtln=False)
    assert backend.include_dtln() is False
    assert backend.enabled_legs() == ("on", "off")

    backend.start_recording("quiet", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    assert (out / "aec_on_nomusic").is_dir()
    assert (out / "aec_off_nomusic").is_dir()
    assert not (out / "aec_dtln_nomusic").exists()
    assert set(clip.files.keys()) == {"on", "off"}


def test_begin_session_with_raw0_records_4_legs(
    backend, tmp_path: Path,
) -> None:
    """A session opened with include_raw_mic_0=True captures all 4
    legs per clip into aec_<leg>_<condition_dir>/ quadrants."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    assert backend.include_raw_mic_0() is True
    backend.start_recording("ambient", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    # All 4 quadrants exist with 1 file each
    for leg in ("on", "off", "dtln", "raw0"):
        d = out / f"aec_{leg}_ambient"
        assert d.is_dir(), f"missing dir: {d}"
        wavs = list(d.glob("*.aec-*.wav"))
        assert len(wavs) == 1, f"expected 1 wav in {d}, got {len(wavs)}"
    # ClipMetadata.files maps all 4 legs
    assert set(clip.files.keys()) == {"on", "off", "dtln", "raw0"}


def test_begin_session_without_raw0_records_3_legs(
    backend, tmp_path: Path,
) -> None:
    """Without the flag, only the 3 base legs are captured — the
    raw0 quadrant directories should NOT be created (keeps the
    on-disk layout clean for non-raw0 sessions)."""
    backend.begin_session("jasper", include_raw_mic_0=False)
    backend.start_recording("quiet", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in ("on", "off", "dtln"):
        assert (out / f"aec_{leg}_nomusic").is_dir()
    # raw0 dir absent
    assert not (out / "aec_raw0_nomusic").exists()
    # ClipMetadata.files has 3 keys
    assert set(clip.files.keys()) == {"on", "off", "dtln"}


def test_begin_session_with_usb_mic_records_corpus_experiment_legs(
    backend, tmp_path: Path,
) -> None:
    """USB/ref opt-in adds the corpus-only cheap-mic legs without
    needing to change the production base leg set."""
    backend.begin_session("jasper", include_usb_mic=True)
    assert backend.include_usb_mic() is True
    assert set(backend.enabled_legs()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
    }
    backend.start_recording("ambient", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in ("ref", "usb_raw", "usb_webrtc"):
        d = out / f"aec_{leg}_ambient"
        assert d.is_dir(), f"missing dir: {d}"
        assert len(list(d.glob("*.aec-*.wav"))) == 1
    assert set(clip.files.keys()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
    }


def test_begin_session_with_usb_dtln_records_companion_legs(
    backend, tmp_path: Path,
) -> None:
    """USB DTLN can be tested independently of USB WebRTC, but it
    still records ref + USB raw so the comparison is interpretable."""
    backend.begin_session("jasper", include_usb_dtln=True)
    assert backend.include_usb_mic() is False
    assert backend.include_usb_dtln() is True
    assert set(backend.enabled_legs()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_dtln",
    }

    backend.start_recording("ambient", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in ("ref", "usb_raw", "usb_dtln"):
        d = out / f"aec_{leg}_ambient"
        assert d.is_dir(), f"missing dir: {d}"
        assert len(list(d.glob("*.aec-*.wav"))) == 1
    assert set(clip.files.keys()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_dtln",
    }
    assert "usb_webrtc" not in clip.files


def test_begin_session_with_xvf_raw0_dtln_records_companion_legs(backend) -> None:
    backend.begin_session(
        "jasper",
        include_raw_mic_0=False,
        include_xvf_raw0_dtln=True,
    )

    assert backend.include_raw_mic_0() is True
    assert backend.include_xvf_raw0_dtln() is True
    assert set(backend.enabled_legs()) == {
        "on", "off", "dtln", "raw0", "xvf_raw0_dtln",
    }


def test_begin_session_chip_profile_records_comparison_legs(backend) -> None:
    backend.begin_session(
        "jasper",
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=True,  # ignored here; this is not the raw0 DTLN path.
        include_raw_mic_0=False,  # forced by the chip profile.
        include_usb_mic=False,
        include_usb_dtln=False,
        include_xvf_raw0_dtln=True,
        include_aec3_sweep=True,  # incompatible pilot sweep is parked.
    )

    assert backend.corpus_profile() == wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON
    assert backend.include_raw_mic_0() is True
    assert backend.include_usb_mic() is False
    assert backend.include_aec3_sweep() is False
    assert backend.enabled_legs() == (
        "chip_aec_150",
        "chip_aec_210",
        "raw0",
        "xvf_raw0_webrtc_aec3",
        "ref",
        "xvf_raw0_dtln",
    )


def test_begin_session_chip_profile_records_usb_legs_when_requested(
    backend,
) -> None:
    backend.begin_session(
        "jasper",
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=True,
        include_usb_dtln=True,
    )

    assert backend.include_usb_mic() is True
    assert backend.enabled_legs() == (
        "chip_aec_150",
        "chip_aec_210",
        "raw0",
        "xvf_raw0_webrtc_aec3",
        "ref",
        "usb_raw",
        "usb_webrtc",
        "usb_dtln",
    )


def test_capture_plan_describes_chip_profile_layers(backend) -> None:
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=False,
        include_usb_dtln=False,
        include_bridge_readiness=False,
    )

    assert plan["schema_version"] == wake_corpus_setup.CAPTURE_PLAN_SCHEMA_VERSION
    assert plan["recipe"] == "chip_aec_comparison"
    assert plan["selected_physical_mics"] == ["xvf3800"]
    by_token = {leg["token"]: leg for leg in plan["legs"]}
    assert by_token["chip_aec_150"]["processing"] == "hardware_aec"
    assert by_token["xvf_raw0_webrtc_aec3"]["native_stream"] == "raw_mic_0"
    assert by_token["xvf_raw0_webrtc_aec3"]["processing"] == "webrtc_aec3"
    assert by_token["ref"]["device_id"] == "speaker_reference"


def test_capture_plan_chip_profile_is_canonical_bridge_contract(backend) -> None:
    snapshot = {
        "system_env": {"JASPER_XVF_ALSA_CARD": "Array"},
        "merged_env": {"JASPER_AEC_USB_MIC_DEVICE": "Studio Mic"},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
        "fingerprint_sources": {
            "mic": {"variant_id": "xvf3800_legacy_square_6ch"},
            "dac_reference": {"audio_dac_id": "apple_usb_c_dongle"},
        },
    }

    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_bridge_readiness=True,
        runtime_snapshot=snapshot,
    )

    assert plan["plan_id"]
    assert plan["selected_legs"] == [
        "chip_aec_150", "chip_aec_210", "raw0",
        "xvf_raw0_webrtc_aec3", "ref",
    ]
    assert plan["expected_emitted_legs"] == plan["selected_legs"]
    assert plan["required_bridge_outputs"] == [
        "ref", "chip_aec", "xvf_raw0_webrtc_aec3", "outputd_ref",
    ]
    env = plan["required_bridge_env"]
    assert env["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"
    assert env["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] == "1"
    assert env["JASPER_AEC_REF_SOURCE"] == "outputd_udp"
    assert env["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] == (
        wake_corpus_setup.OUTPUTD_REF_UDP_TARGET
    )
    assert plan["fingerprints"] == {"mic": "mic-a", "dac_reference": "dac-a"}


def test_capture_plan_id_is_stable_and_changes_with_hardware(
    backend,
) -> None:
    base_snapshot = {
        "system_env": {},
        "merged_env": {},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
    }
    plan1 = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        include_dtln=False,
        runtime_snapshot=base_snapshot,
    )
    plan2 = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        include_dtln=False,
        runtime_snapshot=dict(base_snapshot),
    )
    changed = {
        **base_snapshot,
        "fingerprints": {"mic": "mic-b", "dac_reference": "dac-a"},
    }
    plan3 = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        include_dtln=False,
        runtime_snapshot=changed,
    )

    assert plan1["plan_id"] == plan2["plan_id"]
    assert plan1["plan_id"] != plan3["plan_id"]


def test_chip_capture_plan_id_is_stable_across_its_own_env_apply(backend) -> None:
    """Recorder-owned reference env must not change the plan that requested it."""
    mic_source = {
        "family": "xvf3800",
        "variant_id": "xvf3800_legacy_square_6ch",
        "selected_xvf_mic_device": "Array",
        "selected_usb_mic_device": "Studio Mic",
        "chip_primary_leg": "chip_aec_150",
    }
    dac_source = {
        "audio_dac_id": "apple_usb_c_dongle",
        "dac": {
            "pcm": "outputd_dac",
            "backend": "alsa",
            "control_socket": "/run/jasper-outputd/control.sock",
        },
        "reference": {
            "source": "alsa",
            "outputd_chip_ref_pcm": "",
            "outputd_reference_udp_target": "",
            "outputd_chip_ref_sample_rate": 48000,
            "outputd_chip_ref_period_frames": 256,
            "outputd_chip_ref_buffer_frames": 1024,
            "bridge_output_enabled": False,
        },
        "chip_gate": {"allowed": True},
    }
    before = {
        "identity_recomputable": True,
        "system_env": {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
        "merged_env": {
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_USB_MIC_DEVICE": "Studio Mic",
            "JASPER_AEC_CHIP_AEC_PRIMARY_LEG": "chip_aec_150",
        },
        "bridge_outputs": {"outputd_ref": False},
        "dac_reference": {"validation": {"status": "unknown"}},
        "fingerprint_sources": {
            "mic": mic_source,
            "dac_reference": dac_source,
        },
        "fingerprints": {
            "mic": bridge_session.fingerprint_mapping(mic_source),
            "dac_reference": bridge_session.fingerprint_mapping(dac_source),
        },
    }

    planned = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        runtime_snapshot=before,
    )
    after = {
        **before,
        "merged_env": {
            **before["merged_env"],
            **planned["required_bridge_env"],
        },
        "bridge_outputs": {
            **before["bridge_outputs"],
            "ref": True,
            "chip_aec": True,
            "xvf_raw0_webrtc_aec3": True,
            "outputd_ref": True,
        },
    }
    observed = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        runtime_snapshot=after,
    )

    assert observed["plan_id"] == planned["plan_id"]
    assert observed["fingerprints"] == planned["fingerprints"]


def test_validate_active_capture_plan_refuses_missing_promised_leg(
    backend,
) -> None:
    snapshot = {
        "system_env": {},
        "merged_env": {},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
    }
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        runtime_snapshot=snapshot,
    )
    stats = {
        "counters": {},
        "active_capture_plan": {
            "wake_corpus_plan_id": plan["plan_id"],
            "emitted_legs": [
                "chip_aec_150", "raw0", "xvf_raw0_webrtc_aec3", "ref",
            ],
        },
    }

    result = wake_corpus_setup.validate_active_capture_plan(
        plan,
        bridge_stats=stats,
        runtime_snapshot=snapshot,
    )

    assert result.ok is False
    assert result.missing_emitted_legs == ["chip_aec_210"]
    assert "chip_aec_210" in result.errors[0]


def test_validate_active_capture_plan_refuses_mic_fingerprint_change(
    backend,
) -> None:
    snapshot = {
        "system_env": {},
        "merged_env": {},
        "bridge_outputs": {},
        "fingerprints": {"mic": "mic-a", "dac_reference": "dac-a"},
    }
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        include_dtln=False,
        runtime_snapshot=snapshot,
    )
    stats = {
        "counters": {},
        "active_capture_plan": {
            "wake_corpus_plan_id": plan["plan_id"],
            "emitted_legs": plan["expected_emitted_legs"],
        },
    }
    changed_runtime = {
        **snapshot,
        "fingerprints": {"mic": "mic-b", "dac_reference": "dac-a"},
    }

    result = wake_corpus_setup.validate_active_capture_plan(
        plan,
        bridge_stats=stats,
        runtime_snapshot=changed_runtime,
    )

    assert result.ok is False
    assert result.fingerprint_mismatches == ["mic"]


@pytest.mark.parametrize(
    "active_profile",
    ["xvf_chip_aec", "xvf_chip_aec_testing"],
)
def test_capture_plan_describes_on_leg_runtime_overlay(
    backend,
    active_profile: str,
) -> None:
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        include_dtln=False,
        include_bridge_readiness=False,
        active_audio_profile={
            "requested": active_profile,
            "active": active_profile,
            "state": "active",
        },
        runtime_audio_env={"chip_primary_leg": "chip_aec_210"},
    )

    by_token = {leg["token"]: leg for leg in plan["legs"]}
    assert by_token["on"]["label"] == "Chip AEC ASR 210 primary"
    assert by_token["on"]["kind"] == "hardware_aec"
    assert by_token["on"]["processing"] == "hardware_aec"
    assert by_token["on"]["source_channel"] == "fixed_beam_210"
    assert by_token["on"]["runtime_role"] == "production_primary"
    assert "on" not in plan["software_transforms"]["webrtc_aec3"]


def test_capture_plan_warns_for_heavy_two_mic_dtln(backend) -> None:
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_xvf_raw0_dtln=True,
        include_bridge_readiness=False,
    )

    assert plan["recipe"] == "chip_aec_comparison_extended"
    assert set(plan["selected_physical_mics"]) == {"xvf3800", "usb_mic"}
    assert plan["resource"]["level"] in {"high", "unsafe"}
    assert any("Multiple DTLN legs" in warning for warning in plan["warnings"])


def test_begin_session_with_aec3_sweep_records_variant_legs(
    backend, tmp_path: Path,
) -> None:
    """AEC3 sweep captures XVF reference plus USB baseline/variants."""
    backend.begin_session(
        "jasper", include_dtln=False, include_aec3_sweep=True,
    )
    assert backend.include_aec3_sweep() is True
    assert backend.enabled_legs() == (
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    )

    backend.start_recording("music", "far")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in (
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    ):
        d = out / f"aec_{leg}_music"
        assert d.is_dir(), f"missing dir: {d}"
        assert len(list(d.glob("*.aec-*.wav"))) == 1
    assert set(clip.files.keys()) == {
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    }
    assert "dtln" not in clip.files


def test_missing_bridge_outputs_detects_disabled_usb_and_dtln(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _use_tmp_bridge_env(monkeypatch, tmp_path)

    assert wake_corpus_setup.missing_bridge_outputs_for_session(
        include_dtln=True,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_aec3_sweep=True,
    ) == ["dtln", "ref", "usb", "usb_dtln", "aec3_sweep"]


def test_missing_bridge_outputs_honors_overlay_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The /var/lib corpus env wins over /etc, matching systemd's
    later EnvironmentFile precedence."""
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env=(
            "JASPER_AEC_DTLN_ENABLED=0\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=0\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=0\n"
        ),
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb\n"
        ),
    )

    assert wake_corpus_setup.missing_bridge_outputs_for_session(
        include_dtln=True,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_aec3_sweep=True,
    ) == []


def test_parse_amixer_bool_accepts_common_forms() -> None:
    assert wake_corpus_setup._parse_amixer_bool("Mono: Capture [on]") is True
    assert wake_corpus_setup._parse_amixer_bool(": values=off") is False
    assert wake_corpus_setup._parse_amixer_bool("no boolean here") is None


def test_usb_mic_status_reports_hardware_agc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_USB_MIC_DEVICE=USB PnP Sound Device\n"
            "JASPER_AEC_USB_MIXER_CARD=4\n"
        ),
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Mono: Capture [on]\n", stderr="",
        )

    monkeypatch.setattr(wake_corpus_setup.subprocess, "run", fake_run)

    status = wake_corpus_setup.usb_mic_status()

    assert status["device"] == "USB PnP Sound Device"
    assert status["hardware_agc"]["mixer_card"] == "4"
    assert status["hardware_agc"]["control"] == "Auto Gain Control"
    assert status["hardware_agc"]["available"] is True
    assert status["hardware_agc"]["enabled"] is True
    assert calls == [["amixer", "-c", "4", "get", "Auto Gain Control"]]


def test_restart_aec_bridge_resets_start_limit_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WS1 Phase 3: restart_aec_bridge asks jasper-control's restart broker
    # (reset-failed to clear any start-limit lockout, then restart) instead of
    # shelling out to systemctl, so the wake-corpus flow needs no privilege of
    # its own once jasper-web drops to a non-root service user.
    from jasper.control import restart_broker

    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_manage(*units: str, **kwargs: object):
        calls.append((units, kwargs.get("verb")))
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage)

    wake_corpus_setup.restart_aec_bridge()

    assert calls == [
        ((wake_corpus_setup.BRIDGE_UNIT,), "reset-failed"),
        ((wake_corpus_setup.BRIDGE_UNIT,), "restart"),
    ]


def test_set_bridge_outputs_matches_selected_session_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1\n"
            "JASPER_AEC_USB_MIC_DEVICE=Studio Mic\n"
        ),
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        include_dtln=False,
        include_usb_mic=True,
        include_usb_dtln=False,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert "JASPER_AEC_DTLN_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED" not in values
    assert values["JASPER_AEC_CORPUS_REF_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_ENABLED"] == "1"
    assert values["JASPER_AEC_USB_MIC_DEVICE"] == "Studio Mic"
    assert restarts == ["restart"]


def test_set_bridge_outputs_enables_aec3_sweep_and_parks_dtln(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        include_dtln=False,
        include_usb_mic=False,
        include_usb_dtln=False,
        include_aec3_sweep=True,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_DTLN_ENABLED"] == "0"
    assert values["JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED"] == "1"


def test_set_bridge_outputs_enables_chip_profile_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_unit",
        lambda unit, timeout=wake_corpus_setup.BRIDGE_RESTART_TIMEOUT_SEC: (
            restarts.append(unit)
        ),
    )
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append(wake_corpus_setup.BRIDGE_UNIT),
    )

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=False,
        include_usb_mic=False,
        include_usb_dtln=True,
        include_xvf_raw0_dtln=True,
        include_aec3_sweep=True,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_CORPUS_REF_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_REF_SOURCE"] == "outputd_udp"
    assert values["JASPER_OUTPUTD_CHIP_REF_PCM"] == wake_corpus_setup.DEFAULT_CHIP_REF_PCM
    assert values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] == wake_corpus_setup.OUTPUTD_REF_UDP_TARGET
    assert (
        values["JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE"]
        == wake_corpus_setup.DEFAULT_CHIP_REF_SAMPLE_RATE
    )
    assert (
        values["JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES"]
        == wake_corpus_setup.DEFAULT_CHIP_REF_PERIOD_FRAMES
    )
    assert (
        values["JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES"]
        == wake_corpus_setup.DEFAULT_CHIP_REF_BUFFER_FRAMES
    )
    assert "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED" not in values
    assert restarts == [
        wake_corpus_setup.OUTPUTD_UNIT,
        wake_corpus_setup.AEC_INIT_UNIT,
        wake_corpus_setup.BRIDGE_UNIT,
    ]


def test_chip_ref_pcm_prefers_resolved_xvf_card() -> None:
    assert bridge_session.chip_ref_pcm_for_env(
        {
            "JASPER_XVF_ALSA_CARD": "L16K6Ch",
            "JASPER_AEC_MIC_DEVICE": "Array",
        }
    ) == "plughw:CARD=L16K6Ch,DEV=0"


def test_set_bridge_outputs_chip_profile_without_usb_enables_ref_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_unit",
        lambda unit, timeout=wake_corpus_setup.BRIDGE_RESTART_TIMEOUT_SEC: (
            restarts.append(unit)
        ),
    )
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append(wake_corpus_setup.BRIDGE_UNIT),
    )

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=False,
        include_usb_mic=False,
        include_usb_dtln=False,
        include_xvf_raw0_dtln=False,
        include_aec3_sweep=False,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_CORPUS_REF_ENABLED"] == "1"
    assert "JASPER_AEC_CORPUS_USB_ENABLED" not in values
    assert "JASPER_AEC_USB_MIC_DEVICE" not in values
    assert values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] == "1"
    assert values["JASPER_AEC_REF_SOURCE"] == "outputd_udp"
    assert values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] == (
        wake_corpus_setup.OUTPUTD_REF_UDP_TARGET
    )
    assert restarts == [
        wake_corpus_setup.OUTPUTD_UNIT,
        wake_corpus_setup.AEC_INIT_UNIT,
        wake_corpus_setup.BRIDGE_UNIT,
    ]


def test_set_bridge_outputs_chip_profile_parks_production_dtln(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    monkeypatch.setattr(bridge_session, "restart_unit", lambda *args, **kwargs: None)
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=False,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_xvf_raw0_dtln=True,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_DTLN_ENABLED"] == "0"
    assert values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"


def test_set_bridge_outputs_rolls_back_when_restart_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_DTLN_ENABLED=0\n",
    )
    attempts = 0

    def fake_restart() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(
                1, ["systemctl", "restart", "jasper-aec-bridge.service"],
                stderr="USB corpus mic unavailable",
            )

    monkeypatch.setattr(
        bridge_session, "restart_aec_bridge", fake_restart,
    )

    with pytest.raises(subprocess.CalledProcessError):
        wake_corpus_setup.set_bridge_outputs_for_session(
            include_dtln=True,
            include_usb_mic=True,
            include_usb_dtln=True,
        )

    assert bridge_path.read_text() == "JASPER_AEC_DTLN_ENABLED=0\n"
    assert attempts == 2  # failed new config, then restarted rollback config


def test_disable_bridge_outputs_rolls_back_when_restart_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_USB_MIC_DEVICE=Studio Mic\n"
        ),
    )
    original = bridge_path.read_text()
    attempts = 0

    def fake_restart() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("bridge unavailable")

    monkeypatch.setattr(bridge_session, "restart_aec_bridge", fake_restart)

    with pytest.raises(OSError, match="bridge unavailable"):
        wake_corpus_setup.disable_bridge_corpus_outputs()

    assert bridge_path.read_text() == original
    assert attempts == 2


def test_bridge_env_rollback_deletes_new_file_and_logs_one_restart_failure(
    caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:
    env_path = tmp_path / "new-corpus.env"
    attempts = 0

    def restart() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("initial bridge restart failed")
        raise subprocess.TimeoutExpired(["systemctl", "restart"], 1.0)

    with caplog.at_level(logging.WARNING), pytest.raises(
        OSError, match="initial bridge restart failed"
    ):
        bridge_session._write_env_and_restart_with_rollback(
            env_path=str(env_path),
            existed=False,
            old_values={},
            values={"JASPER_AEC_CORPUS_REF_ENABLED": "1"},
            restart=restart,
            failure_context="configure",
        )

    assert not env_path.exists()
    assert attempts == 2
    assert caplog.messages == [
        "event=wake_corpus.bridge_rollback_restart_failed "
        "failure_context=configure error=\"Command '['systemctl', 'restart']' "
        "timed out after 1.0 seconds\""
    ]


def test_disable_bridge_outputs_restarts_chip_stack_in_safe_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
        ),
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_unit",
        lambda unit, timeout=wake_corpus_setup.BRIDGE_RESTART_TIMEOUT_SEC: (
            restarts.append(unit)
        ),
    )
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append(wake_corpus_setup.BRIDGE_UNIT),
    )

    assert wake_corpus_setup.disable_bridge_corpus_outputs() is True

    assert not bridge_path.exists()
    assert restarts == [
        wake_corpus_setup.OUTPUTD_UNIT,
        wake_corpus_setup.AEC_INIT_UNIT,
        wake_corpus_setup.BRIDGE_UNIT,
    ]


def test_disable_bridge_outputs_removes_overrides_and_preserves_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
            "JASPER_AEC_USB_MIC_DEVICE=Studio Mic\n"
        ),
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )

    wake_corpus_setup.disable_bridge_corpus_outputs()

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert "JASPER_AEC_DTLN_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_REF_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_USB_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED" not in values
    assert values["JASPER_AEC_USB_MIC_DEVICE"] == "Studio Mic"
    assert restarts == ["restart"]


def test_disable_bridge_outputs_restores_system_dtln_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=0\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
        ),
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    before = wake_corpus_setup.bridge_output_status()
    wake_corpus_setup.disable_bridge_corpus_outputs()
    after = wake_corpus_setup.bridge_output_status()

    assert before["active"] is True
    assert before["dtln"] is False
    assert after["active"] is False
    assert after["dtln"] is True


def test_build_capture_health_marks_bridge_drop_compromised() -> None:
    frame = np.zeros(1280, dtype=np.int16)
    start = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 2.0,
        "counters": {
            "frames_processed": 10,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 0, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {"on": 0},
            "packets_sent_by_leg": {"on": 0},
        },
    }
    stop = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 3.0,
        "counters": {
            "frames_processed": 20,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 1, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {"on": 0},
            "packets_sent_by_leg": {"on": 1},
        },
    }

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={"on": [frame]},
        bridge_start=start,
        bridge_stop=stop,
    )

    assert health["status"] == "compromised"
    assert health["bridge_delta"]["queue_drops"]["mic"] == 1
    assert health["legs"]["on"]["status"] == "compromised"
    assert health["legs"]["on"]["bridge_drop_counts"]["mic_queue_full"] == 1


def test_build_capture_health_marks_aec3_sweep_bridge_drops() -> None:
    """AEC3 sweep legs use the same XVF mic/ref frames as the baseline
    AEC leg, so their per-leg health must inherit mic/ref bridge drops."""
    leg = wake_corpus_setup.AEC3_SWEEP_LEGS[0]
    frame = np.zeros(1280, dtype=np.int16)
    start = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 2.0,
        "counters": {
            "frames_processed": 10,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 0, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 0},
        },
    }
    stop = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 3.0,
        "counters": {
            "frames_processed": 20,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 1, "raw0": 0, "usb": 0, "ref": 2},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 1},
        },
    }

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={leg: [frame]},
        bridge_start=start,
        bridge_stop=stop,
    )

    drop_counts = health["legs"][leg]["bridge_drop_counts"]
    assert health["status"] == "compromised"
    assert health["legs"][leg]["status"] == "compromised"
    assert drop_counts["mic_queue_full"] == 1
    assert drop_counts["ref_queue_full"] == 2


def test_build_capture_health_marks_usb_aec3_sweep_bridge_drops() -> None:
    """When the sweep source is USB, variant legs inherit USB/ref drops
    instead of XVF mic drops."""
    leg = wake_corpus_setup.AEC3_SWEEP_LEGS[0]
    frame = np.zeros(1280, dtype=np.int16)
    start = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 2.0,
        "counters": {
            "frames_processed": 10,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 0, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 0},
        },
    }
    stop = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 3.0,
        "counters": {
            "frames_processed": 20,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 4, "raw0": 0, "usb": 1, "ref": 2},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 1},
        },
    }

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={leg: [frame]},
        bridge_start=start,
        bridge_stop=stop,
        aec3_sweep_source="usb",
    )

    drop_counts = health["legs"][leg]["bridge_drop_counts"]
    assert health["status"] == "compromised"
    assert "mic_queue_full" not in drop_counts
    assert drop_counts["usb_queue_full"] == 1
    assert drop_counts["ref_queue_full"] == 2


def test_build_capture_health_unknown_without_bridge_stats() -> None:
    frame = np.zeros(1280, dtype=np.int16)

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={"on": [frame]},
        bridge_start=None,
        bridge_stop=None,
    )

    assert health["status"] == "unknown"
    assert health["legs"]["on"]["packets"] == 1
    assert health["legs"]["on"]["audio_duration_sec"] == pytest.approx(0.08)


def test_metadata_persists_include_raw_mic_0_flag(
    backend, tmp_path: Path,
) -> None:
    """The session JSON sidecar must persist include_raw_mic_0 so
    recovery + list_sessions can show it."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    _, data = _session_metadata(tmp_path)
    assert data["include_raw_mic_0"] is True
    assert data["include_dtln"] is True
    assert data["enabled_legs"] == ["on", "off", "dtln", "raw0"]


def test_metadata_persists_include_usb_mic_flag(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper", include_usb_mic=True)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    _, data = _session_metadata(tmp_path)
    assert data["include_usb_mic"] is True
    assert data["include_usb_dtln"] is False
    assert data["enabled_legs"] == [
        "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
    ]


def test_metadata_persists_dtln_session_flags(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session(
        "jasper", include_dtln=False, include_usb_dtln=True,
    )
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    _, data = _session_metadata(tmp_path)
    assert data["include_dtln"] is False
    assert data["include_usb_dtln"] is True
    assert data["enabled_legs"] == ["on", "off", "ref", "usb_raw", "usb_dtln"]


def test_metadata_persists_aec3_sweep_flags(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session(
        "jasper", include_dtln=False, include_aec3_sweep=True,
    )
    backend.start_recording("music", "far")
    time.sleep(0.05)
    backend.stop_recording()

    _, data = _session_metadata(tmp_path)
    assert data["include_aec3_sweep"] is True
    assert data["include_usb_mic"] is True
    assert data["aec3_sweep_source"] == "usb"
    assert data["enabled_legs"] == [
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    ]
    assert data["aec3_sweep_variants"] == wake_corpus_setup.variant_metadata(
        input_source="usb",
    )
    assert data["aec3_sweep_config"]["input_source"] == "usb"


def test_loaded_aec3_sweep_session_refreshes_current_variant_legs() -> None:
    """A loaded pilot session should use the current sweep registry even
    if its saved metadata names an older retired variant."""
    ports = {
        "on": 9876,
        "off": 9877,
        **{
            leg: 9884 + index
            for index, leg in enumerate(wake_corpus_setup.AEC3_SWEEP_LEGS)
        },
    }
    data = {
        "include_aec3_sweep": True,
        "enabled_legs": [
            "on",
            "aec3_hf_relaxed",
            "aec3_nearend_fast",
            "aec3_slow_attack",
            "off",
        ],
    }

    assert wake_corpus_setup._enabled_legs_from_metadata(data, ports) == (
        "on", *wake_corpus_setup.AEC3_SWEEP_LEGS, "off",
    )


def test_recovery_restores_include_raw_mic_0_flag(tmp_path: Path) -> None:
    """A recovered session must restore the include_raw_mic_0 flag
    so a follow-up clip inherits the original session's leg set
    (not silently degraded to the 3-base default)."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_x.json").write_text(json.dumps({
        "session_id": "x", "member": "jasper",
        "ports": {"on": 9876, "off": 9877, "dtln": 9878, "raw0": 9879},
        "include_raw_mic_0": True,
        "clips": [],
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "x",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.include_raw_mic_0() is True
        assert b.include_dtln() is True
    finally:
        b.shutdown()


def test_recovery_restores_usb_dtln_flag(tmp_path: Path) -> None:
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_x.json").write_text(json.dumps({
        "session_id": "x", "member": "jasper",
        "ports": {
            "on": 9876, "off": 9877, "dtln": 9878,
            "ref": 9880, "usb_raw": 9881, "usb_dtln": 9883,
        },
        "include_dtln": False,
        "include_usb_dtln": True,
        "clips": [],
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "x",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.include_dtln() is False
        assert b.include_usb_dtln() is True
        assert b.enabled_legs() == ("on", "off", "ref", "usb_raw", "usb_dtln")
    finally:
        b.shutdown()


def test_recovery_handles_pre_raw0_session_metadata(tmp_path: Path) -> None:
    """Sessions recorded BEFORE this feature don't have the
    include_raw_mic_0 key. Recovery must treat the missing key as
    False (backward compat with existing on-disk corpora)."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_old.json").write_text(json.dumps({
        "session_id": "old", "member": "jasper",
        "ports": {"on": 9876, "off": 9877, "dtln": 9878},
        "clips": [],
        # NO include_raw_mic_0 key
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "old",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.include_raw_mic_0() is False
    finally:
        b.shutdown()


def test_recovery_handles_pre_audio_context_session_metadata(tmp_path: Path) -> None:
    """Older sidecars do not have audio_context or per-clip selected_legs."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_old.json").write_text(json.dumps({
        "session_id": "old", "member": "jasper",
        "ports": {"on": 9876, "off": 9877, "dtln": 9878},
        "include_dtln": True,
        "clips": [
            {"clip_id": "1", "member": "jasper", "condition": "quiet",
             "distance": "near", "session_id": "old", "seq": 1,
             "start_ts": "x", "stop_ts": "y", "duration_sec": 1.0,
             "files": {}, "deleted": False, "auto_stopped": False, "notes": ""},
        ],
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "old",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.audio_context() is None
        clips = b.list_clips(include_deleted=True)
        assert len(clips) == 1
        assert clips[0].selected_legs == []
        assert clips[0].audio_context == {}
    finally:
        b.shutdown()


# ---------------------------------------------------------------------------
# Sessions management — list / load / delete
# ---------------------------------------------------------------------------
