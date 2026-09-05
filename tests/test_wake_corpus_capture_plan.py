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

from jasper.chip_aec.policy import ChipAecGate
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


# The serialized chip-AEC gate is a hash input: it rides
# dac_reference_fingerprint_source, whose digest becomes the stored plan's
# dac_reference fingerprint and, through it, the plan id. A session resumed
# after that digest moved is refused, so only facts that outlive a reconcile
# pass may reach it.
_BASE_GATE = ChipAecGate(
    dac_id="hifiberry_dac8x",
    status="approved",
    source="static",
    detail="HiFiBerry DAC8x is approved for production chip-AEC",
    auto_allowed=True,
).to_dict()


def _gate(**overrides: object) -> dict[str, object]:
    return {**_BASE_GATE, **overrides}


def _dac_reference_digest(gate: dict[str, object]) -> str:
    return bridge_session.fingerprint_mapping({
        "audio_dac_id": "hifiberry_dac8x",
        "dac": {"pcm": "outputd_dac", "backend": "alsa"},
        "reference": {"source": "outputd_udp"},
        "chip_gate": bridge_session.chip_gate_identity(gate),
    })


@pytest.mark.parametrize(
    "gate, same_digest",
    [
        # The reconciler's carry — same box, same verdict, resolver briefly
        # down — and the live outputd clock estimate it embeds in detail.
        (_gate(source="runtime_env_carried"), True),
        (_gate(detail="approved; outputd aec_clock chip_ref_sro_ppm=1.7"), True),
        # Derived policy, including permits-shaped booleans a later reshape
        # could add back.
        (_gate(auto_allowed=False), True),
        (_gate(recommended_action="fix_mic_profile_before_chip_aec"), True),
        (_gate(blockers=["dac"]), True),
        (_gate(permits_production=True, permits_testing=False), True),
        ({k: v for k, v in _BASE_GATE.items() if k != "auto_allowed"}, True),
        # Identity still moves it, so the subset is not hashing a constant.
        (_gate(dac_id="mystery_usb_audio"), False),
        (_gate(status="needs_calibration"), False),
    ],
    ids=[
        "carried_source", "live_clock_detail", "auto_allowed",
        "recommended_action", "blockers", "permits_shaped_keys", "dropped_key",
        "other_dac", "other_verdict",
    ],
)
def test_dac_reference_digest_tracks_only_dac_identity_and_verdict(
    gate, same_digest,
) -> None:
    """Only the physical DAC and its commissioning verdict may move the digest."""

    unchanged = _dac_reference_digest(gate) == _dac_reference_digest(_BASE_GATE)

    assert unchanged is same_digest


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
    kicks: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "_hand_chip_stack_to_reconciler",
        lambda *, reason: kicks.append(reason),
    )

    assert wake_corpus_setup.disable_bridge_corpus_outputs() is True

    assert not bridge_path.exists()
    assert restarts == [
        wake_corpus_setup.OUTPUTD_UNIT,
        wake_corpus_setup.AEC_INIT_UNIT,
        wake_corpus_setup.BRIDGE_UNIT,
    ]
    # A commissioned box converges on its own: nothing is handed to the
    # reconciler, so the #2254 park branch stays off the healthy path.
    assert kicks == []


def _chip_corpus_disable_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    stub_handoff: bool = True,
) -> tuple[Path, list[str], list[str]]:
    """A chip-corpus box whose aec-init restart fails on the way out.

    Returns (bridge env path, restart log, reconciler-kick log). Tests that
    exercise the real handoff pass ``stub_handoff=False``; the kick log stays
    empty for them.
    """
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
        ),
    )
    restarts: list[str] = []
    kicks: list[str] = []

    def fake_restart_unit(
        unit: str, timeout: float = wake_corpus_setup.BRIDGE_RESTART_TIMEOUT_SEC,
    ) -> None:
        restarts.append(unit)
        if unit == wake_corpus_setup.AEC_INIT_UNIT:
            raise subprocess.CalledProcessError(
                1,
                ["systemctl", "restart", unit],
                stderr="Job for jasper-aec-init.service failed",
            )

    monkeypatch.setattr(bridge_session, "restart_unit", fake_restart_unit)
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append(wake_corpus_setup.BRIDGE_UNIT),
    )
    if stub_handoff:
        monkeypatch.setattr(
            bridge_session,
            "_hand_chip_stack_to_reconciler",
            lambda *, reason: kicks.append(reason),
        )
    return bridge_path, restarts, kicks


def test_disable_on_a_box_with_no_corpus_overrides_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A box that was never in corpus mode restarts nothing at all.

    The #2254 branch lives inside the chip arm of the restart closure, so it
    cannot be reached from here — but pin the no-op rather than argue it.
    """
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
    kicks: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "_hand_chip_stack_to_reconciler",
        lambda *, reason: kicks.append(reason),
    )

    assert wake_corpus_setup.disable_bridge_corpus_outputs() is False

    assert restarts == []
    assert kicks == []
    assert not bridge_path.exists()


def test_corpus_exit_survives_the_designed_commissioning_park(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #2254: an exit intent is never rolled back by a designed park.

    Leaving a chip corpus profile puts aec-init on the production path, where
    an uncommissioned box parks (exit 2) and the unit fails. Treating that as a
    broken restart restored the corpus env and re-entered corpus mode — and the
    artifact the box lacks cannot be obtained from inside corpus mode, so the
    operator could never leave. The disable must stick.
    """
    bridge_path, restarts, kicks = _chip_corpus_disable_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge_session,
        "_aec_init_exec_main_status",
        lambda: 2,
    )

    with caplog.at_level(logging.WARNING):
        assert wake_corpus_setup.disable_bridge_corpus_outputs() is True

    # The corpus overrides are gone and stayed gone: no rollback write.
    assert not bridge_path.exists()
    # The bridge is left to the reconciler's park, which stops it.
    assert restarts == [
        wake_corpus_setup.OUTPUTD_UNIT,
        wake_corpus_setup.AEC_INIT_UNIT,
    ]
    # The park is loud and names an operator action, and its owner is asked to
    # converge rather than this surface deciding locally.
    assert any(
        "event=wake_corpus.corpus_exit_parked" in message
        and "jasper-aec-commission" in message
        and "not commissioned" in message
        for message in caplog.messages
    ), caplog.messages
    assert len(kicks) == 1


def test_corpus_exit_still_rolls_back_a_genuine_aec_init_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Only the designed park is exempt; a fault keeps the rollback."""
    bridge_path, restarts, kicks = _chip_corpus_disable_env(monkeypatch, tmp_path)
    original = bridge_path.read_text()
    monkeypatch.setattr(
        bridge_session,
        "_aec_init_exec_main_status",
        lambda: 1,
    )

    with pytest.raises(subprocess.CalledProcessError):
        wake_corpus_setup.disable_bridge_corpus_outputs()

    assert bridge_path.read_text() == original
    assert kicks == []
    # Rollback re-runs the same closure, so aec-init is attempted twice.
    assert restarts.count(wake_corpus_setup.AEC_INIT_UNIT) == 2


def test_corpus_exit_rolls_back_when_the_park_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Unreadable exit status is not evidence of a park — fail closed.

    We skip a rollback only on positive evidence, so an unavailable systemd
    keeps the pre-#2254 behaviour rather than silently swallowing a real fault.
    """
    bridge_path, _restarts, kicks = _chip_corpus_disable_env(monkeypatch, tmp_path)
    original = bridge_path.read_text()
    monkeypatch.setattr(
        bridge_session,
        "_aec_init_exec_main_status",
        lambda: None,
    )

    with pytest.raises(subprocess.CalledProcessError):
        wake_corpus_setup.disable_bridge_corpus_outputs()

    assert bridge_path.read_text() == original
    assert kicks == []


def test_park_detection_reads_the_exit_code_aec_init_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The predicate is bound to aec-init's own constant, not a local 2."""
    from jasper.cli.aec_init import COMMISSION_REQUIRED_EXIT

    monkeypatch.setattr(
        bridge_session,
        "_aec_init_exec_main_status",
        lambda: COMMISSION_REQUIRED_EXIT,
    )
    assert bridge_session._aec_init_parked_for_commissioning() is True

    monkeypatch.setattr(
        bridge_session,
        "_aec_init_exec_main_status",
        lambda: COMMISSION_REQUIRED_EXIT + 1,
    )
    assert bridge_session._aec_init_parked_for_commissioning() is False


def test_exec_main_status_parses_systemctl_show_and_fails_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="2\n", stderr="")

    monkeypatch.setattr(bridge_session.subprocess, "run", fake_run)
    assert bridge_session._aec_init_exec_main_status() == 2
    assert calls == [[
        "systemctl", "show", "-p", "ExecMainStatus", "--value",
        wake_corpus_setup.AEC_INIT_UNIT,
    ]]

    monkeypatch.setattr(
        bridge_session.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="[not set]\n", stderr="",
        ),
    )
    assert bridge_session._aec_init_exec_main_status() is None

    monkeypatch.setattr(
        bridge_session.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Failed to connect to bus",
        ),
    )
    assert bridge_session._aec_init_exec_main_status() is None

    def raise_timeout(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(argv, 1.5)

    monkeypatch.setattr(bridge_session.subprocess, "run", raise_timeout)
    assert bridge_session._aec_init_exec_main_status() is None


def test_reconciler_kick_is_non_blocking_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The exit already landed, so a failed handoff warns instead of raising.

    Non-blocking because the reconciler's own start budget (120 s) does not fit
    this module's 30 s restart timeout.
    """
    seen: list[dict[str, object]] = []

    def fake_manage_units(unit, **kwargs):  # type: ignore[no-untyped-def]
        seen.append({"unit": unit, **kwargs})
        return {"ok": False, "rc": 5}

    monkeypatch.setattr(
        bridge_session.restart_broker, "manage_units", fake_manage_units,
    )

    with caplog.at_level(logging.WARNING):
        bridge_session._hand_chip_stack_to_reconciler(reason="exit")

    assert seen[0]["unit"] == bridge_session.AEC_RECONCILE_UNIT
    assert seen[0]["verb"] == "start"
    assert seen[0]["no_block"] is True
    assert any(
        "event=wake_corpus.aec_reconcile_kick_failed" in message
        for message in caplog.messages
    ), caplog.messages


def test_a_broken_reconciler_kick_cannot_resurrect_the_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The handoff runs inside the restart closure, so it must not raise.

    An exception escaping the kick would be caught as a failed restart and roll
    the corpus env back — the #2254 trap through a second door.
    """
    # The real handoff, not the fixture's stub: the point is what the live
    # function does when the broker call under it explodes.
    bridge_path, _restarts, _kicks = _chip_corpus_disable_env(
        monkeypatch, tmp_path, stub_handoff=False,
    )
    monkeypatch.setattr(
        bridge_session, "_aec_init_exec_main_status", lambda: 2,
    )

    def exploding_manage_units(unit, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("broker socket vanished")

    monkeypatch.setattr(
        bridge_session.restart_broker, "manage_units", exploding_manage_units,
    )

    assert wake_corpus_setup.disable_bridge_corpus_outputs() is True
    assert not bridge_path.exists()


def test_reconcile_unit_is_brokerable_from_the_wizard_process() -> None:
    """jasper-web asks the broker; a unit outside its allowlist can't be kicked."""
    from jasper.control import restart_broker

    assert bridge_session.AEC_RECONCILE_UNIT in restart_broker.MANAGED_UNITS


def test_session_configure_keeps_rollback_when_leaving_a_chip_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Deliberate non-change: only the EXIT direction drops the rollback.

    Switching corpus profiles hits the same aec-init park, but rolling that
    back returns the operator to a working corpus state they can still leave —
    it is a refusal, not the #2254 trap. Configuring a session that cannot run
    must keep failing loudly rather than reporting a session it did not apply.
    """
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
        ),
    )
    original = bridge_path.read_text()

    def fake_restart_unit(
        unit: str, timeout: float = wake_corpus_setup.BRIDGE_RESTART_TIMEOUT_SEC,
    ) -> None:
        if unit == wake_corpus_setup.AEC_INIT_UNIT:
            raise subprocess.CalledProcessError(
                1, ["systemctl", "restart", unit],
            )

    monkeypatch.setattr(bridge_session, "restart_unit", fake_restart_unit)
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)
    monkeypatch.setattr(
        bridge_session, "_aec_init_exec_main_status", lambda: 2,
    )

    with pytest.raises(subprocess.CalledProcessError):
        wake_corpus_setup.set_bridge_outputs_for_session(
            corpus_profile=wake_corpus_setup.PROFILE_STANDARD,
            include_dtln=False,
            include_usb_mic=False,
            include_usb_dtln=False,
        )

    assert bridge_path.read_text() == original


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
