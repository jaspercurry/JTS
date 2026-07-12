# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the per-leg + threshold helpers in
jasper.control.server. These are the Python-side counterpart to
tests/test_aec_reconcile.py (which covers the bash mapping from
the same aec_mode.env state file to /etc/jasper/jasper.env).

The HTTP endpoints (/aec, /aec/leg, /aec/threshold) are exercised
end-to-end by test_control_server.py via the live ThreadingHTTPServer
fixture — that file is the right place to add route-level tests if
the endpoint behavior changes. Here we pin the helpers themselves
so a regression in parse/write logic surfaces fast.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from jasper import atomic_io
from jasper.audio_profile_state import MicProbe
from jasper.chip_aec_policy import (
    ACTION_FIX_MIC_PROFILE,
    BLOCKER_DAC,
    BLOCKER_MIC,
    CHIP_AEC_BLOCKER_CODES,
)
from jasper.control import aec_endpoints
from jasper.control import server
from jasper.mics import xvf3800
from jasper.web import wake_setup


@pytest.fixture
def aec_mode_file(tmp_path: Path, monkeypatch):
    """Redirect _AEC_MODE_FILE at a tmp path so the helpers don't
    touch the real /var/lib/jasper/aec_mode.env."""
    path = tmp_path / "aec_mode.env"
    monkeypatch.setattr(server, "_AEC_MODE_FILE", str(path))
    return path


@pytest.fixture
def wake_model_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "wake_model.env"
    monkeypatch.setattr(server, "_WAKE_MODEL_FILE", str(path))
    return path


def _stub_xvf_runtime(
    monkeypatch,
    *,
    variant: xvf3800.FirmwareVariant | None = xvf3800.VARIANT_6CH,
    present: bool = True,
    channels: int | None = 6,
) -> None:
    plan = xvf3800.chip_beam_plan_for_variant(variant)
    card = variant.alsa_card_name if variant else xvf3800.ALSA_CARD_NAME
    monkeypatch.setattr(
        "jasper.mics.xvf3800.detect_runtime_profile",
        lambda: xvf3800.RuntimeProfile(
            present=present,
            variant=variant,
            alsa_card_name=card,
            capture_channels=channels,
            chip_beam_plan=plan,
            reason="test profile",
        ),
    )


# ---------- _parse_env_bool ------------------------------------------------


def test_parse_env_bool_truthy_variants():
    """Operators editing aec_mode.env by hand might write yes/on/true
    instead of 1; mirror the bash reconciler's normalize_bool."""
    for raw in ("1", "on", "true", "yes", "y", "enabled", "ON", "True"):
        assert server._parse_env_bool(raw, default=False), f"{raw!r} should parse True"


def test_parse_env_bool_falsy_variants():
    for raw in ("0", "off", "false", "no", "n", "disabled", "", "  "):
        assert not server._parse_env_bool(raw, default=True), f"{raw!r} should parse False"


def test_parse_env_bool_unknown_falls_through_to_default():
    """Garbage values fall through to the caller's default — silent
    drop matches the bash reconciler. The doctor's check_wake_legs
    surfaces the configured state so unknown values become visible
    via UI even if the parse silently defaults."""
    assert server._parse_env_bool("garbage", default=True)
    assert not server._parse_env_bool("garbage", default=False)


def test_parse_env_bool_strips_quotes_and_whitespace():
    assert server._parse_env_bool("'1'", default=False)
    assert server._parse_env_bool('"yes"', default=False)
    assert not server._parse_env_bool(" 0 ", default=True)


# ---------- _read_aec_state ------------------------------------------------


def test_read_aec_state_defaults_when_file_missing(aec_mode_file):
    """Pre-reconciler-seed state: file doesn't exist yet. Helpers
    must return the documented defaults (matches install.sh +
    bash reconciler seeds — see test_aec_reconcile.py)."""
    assert not aec_mode_file.exists()
    state = server._read_aec_state()
    assert state == {
        "mode": "auto",
        "leg_raw": True,
        "leg_dtln": False,
        "leg_chip_aec": False,
        "leg_chip_aec_150": False,
        "leg_chip_aec_210": False,
        "profile": "auto",
    }


def test_read_aec_state_parses_all_leg_keys(aec_mode_file):
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=disabled\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=1\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
        "JASPER_WAKE_LEG_CHIP_AEC_150=1\n"
        "JASPER_WAKE_LEG_CHIP_AEC_210=0\n"
        "JASPER_AUDIO_INPUT_PROFILE=custom\n"
    )
    state = server._read_aec_state()
    assert state == {
        "mode": "disabled",
        "leg_raw": False,
        "leg_dtln": True,
        "leg_chip_aec": True,
        "leg_chip_aec_150": True,
        "leg_chip_aec_210": False,
        "profile": "custom",
    }


def test_read_aec_state_chip_aec_defaults_off_when_absent(aec_mode_file):
    """Pre-chip-AEC deploys lack JASPER_WAKE_LEG_CHIP_AEC; the helper
    surfaces the default (off) so the /wake/ UI doesn't show a stale or
    accidentally-on chip toggle before the reconciler appends the key."""
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
    )
    assert server._read_aec_state()["leg_chip_aec"] is False
    assert server._read_aec_state()["profile"] == "xvf_software_aec3"


def test_read_aec_state_partial_file_uses_defaults_for_missing(aec_mode_file):
    """Pre-leg-toggle deploys have only JASPER_AEC_MODE in the file.
    The helper must surface defaults for absent leg keys — otherwise
    the /system UI would show stale "off" for raw until the
    reconciler's next ensure_mode_file appends the keys."""
    aec_mode_file.write_text("JASPER_AEC_MODE=auto\n")
    state = server._read_aec_state()
    assert state["mode"] == "auto"
    assert state["leg_raw"] is True   # default
    assert state["leg_dtln"] is False  # default
    assert state["profile"] == "xvf_software_aec3"


def test_read_aec_state_ignores_comments_and_blanks(aec_mode_file):
    aec_mode_file.write_text(
        "# operator notes\n"
        "\n"
        "JASPER_AEC_MODE=auto\n"
        "  # JASPER_WAKE_LEG_RAW=1  ← commented out\n"
        "JASPER_WAKE_LEG_DTLN=1\n"
    )
    state = server._read_aec_state()
    assert state["mode"] == "auto"
    assert state["leg_raw"] is True   # default (commented line ignored)
    assert state["leg_dtln"] is True
    assert state["profile"] == "custom"


# ---------- _write_aec_leg --------------------------------------------------


def test_write_aec_leg_preserves_other_keys(aec_mode_file):
    """The slider's POST writes one leg; mode + the other leg must
    stay untouched (RMW pattern, not atomic replace)."""
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=disabled\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
    )
    server._write_aec_leg("dtln", True)
    body = aec_mode_file.read_text()
    assert "JASPER_AEC_MODE=disabled" in body
    assert "JASPER_WAKE_LEG_RAW=1" in body
    assert "JASPER_WAKE_LEG_DTLN=1" in body
    assert "JASPER_AUDIO_INPUT_PROFILE=custom" in body


def test_write_aec_leg_creates_file_when_missing(aec_mode_file):
    """A POST that fires before the reconciler has had a chance to
    seed the file must still succeed — write_env_file creates the
    file path's parent if needed."""
    assert not aec_mode_file.exists()
    server._write_aec_leg("raw", False)
    state = server._read_aec_state()
    assert state["leg_raw"] is False
    assert state["profile"] == "custom"


def test_write_aec_leg_rejects_invalid_leg(aec_mode_file):
    with pytest.raises(ValueError, match="invalid leg"):
        server._write_aec_leg("not-a-leg", True)


def test_write_aec_leg_writes_zero_for_off(aec_mode_file):
    """Off must be the literal string '0', not 'False' or '' — the
    bash reconciler's normalize_bool reads these on the next pass
    and "" would silently default to the type default rather than
    the operator's explicit choice."""
    server._write_aec_leg("dtln", False)
    body = aec_mode_file.read_text()
    assert "JASPER_WAKE_LEG_DTLN=0" in body
    assert "JASPER_AUDIO_INPUT_PROFILE=custom" in body


def test_write_audio_input_profile_writes_profile_and_legacy_keys(aec_mode_file):
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=1\n"
    )
    server._write_audio_input_profile("xvf_chip_aec")
    body = aec_mode_file.read_text()
    assert "JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec" in body
    assert "JASPER_AEC_MODE=auto" in body
    assert "JASPER_WAKE_LEG_RAW=0" in body
    assert "JASPER_WAKE_LEG_DTLN=0" in body
    assert "JASPER_WAKE_LEG_CHIP_AEC=1" in body
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=0" in body
    assert "JASPER_WAKE_LEG_CHIP_AEC_210=0" in body


def test_write_audio_input_profile_rejects_custom(aec_mode_file):
    with pytest.raises(ValueError, match="invalid profile"):
        server._write_audio_input_profile("custom")


def test_aec_mode_interleaved_writers_preserve_each_others_keys(
    aec_mode_file, monkeypatch,
):
    """Two HTTP workers can toggle different AEC legs at once. The env
    read-modify-write must be flocked so the later writer sees and preserves
    the earlier writer's keys."""
    from jasper.web import _common as web_common

    real_atomic_write = atomic_io.atomic_write_text
    real_web_write = web_common.write_env_file
    first_write_paused = threading.Event()
    release_first_write = threading.Event()
    errors: list[BaseException] = []

    def should_pause(text: str) -> bool:
        return (
            "JASPER_WAKE_LEG_RAW=0" in text
            and not first_write_paused.is_set()
        )

    def pausing_atomic_write(path, text, *, mode=0o644):
        if should_pause(text):
            first_write_paused.set()
            assert release_first_write.wait(timeout=2)
        return real_atomic_write(path, text, mode=mode)

    def pausing_web_write(path, values, *, mode=0o644):
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        if should_pause(text):
            first_write_paused.set()
            assert release_first_write.wait(timeout=2)
        return real_web_write(path, values, mode=mode)

    def write_raw_off():
        try:
            server._write_aec_leg("raw", False)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def write_dtln_on():
        try:
            server._write_aec_leg("dtln", True)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    monkeypatch.setattr(atomic_io, "atomic_write_text", pausing_atomic_write)
    monkeypatch.setattr(web_common, "write_env_file", pausing_web_write)
    raw_thread = threading.Thread(target=write_raw_off)
    raw_thread.start()
    assert first_write_paused.wait(timeout=2)

    dtln_thread = threading.Thread(target=write_dtln_on)
    dtln_thread.start()
    release_first_write.set()

    raw_thread.join(timeout=2)
    dtln_thread.join(timeout=2)

    assert not raw_thread.is_alive()
    assert not dtln_thread.is_alive()
    assert not errors
    body = aec_mode_file.read_text()
    assert "JASPER_WAKE_LEG_RAW=0" in body
    assert "JASPER_WAKE_LEG_DTLN=1" in body
    assert "JASPER_AUDIO_INPUT_PROFILE=custom" in body


def test_wake_model_and_threshold_interleaved_writers_preserve_both_keys(
    wake_model_file, monkeypatch,
):
    """The /wake model form and jasper-control sensitivity endpoint both
    read-modify-write wake_model.env. If the model writer reads first and the
    threshold writer lands before it publishes, the final file must still keep
    both keys."""
    real_atomic_write = atomic_io.atomic_write_text
    model_write_paused = threading.Event()
    release_model_write = threading.Event()
    errors: list[BaseException] = []

    def pausing_atomic_write(path, text, *, mode=0o644):
        if "JASPER_WAKE_MODEL=alexa" in text and not model_write_paused.is_set():
            model_write_paused.set()
            assert release_model_write.wait(timeout=2)
        return real_atomic_write(path, text, mode=mode)

    def write_model():
        try:
            wake_setup.locked_update_env_file(
                str(wake_model_file),
                {"JASPER_WAKE_MODEL": "alexa"},
                mode=0o644,
            )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def write_threshold():
        try:
            server._write_wake_threshold(0.42)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    monkeypatch.setattr(atomic_io, "atomic_write_text", pausing_atomic_write)
    model_thread = threading.Thread(target=write_model)
    model_thread.start()
    assert model_write_paused.wait(timeout=2)

    threshold_thread = threading.Thread(target=write_threshold)
    threshold_thread.start()
    release_model_write.set()

    model_thread.join(timeout=2)
    threshold_thread.join(timeout=2)

    assert not model_thread.is_alive()
    assert not threshold_thread.is_alive()
    assert not errors
    assert wake_setup._load_state(str(wake_model_file)) == {
        "JASPER_WAKE_MODEL": "alexa",
        "JASPER_WAKE_THRESHOLD": "0.42",
    }


def test_toggle_to_token_maps_to_real_wake_input_legs():
    """Every operator toggle name maps to one-or-more real wake_input leg
    tokens in the registry — and "raw" specifically resolves to the
    chip-direct "off" leg on UDP 9877, NOT the "raw0" corpus leg (the
    easy-to-confuse footgun). Guards _TOGGLE_TO_TOKEN against drifting onto
    the wrong leg if the registry is ever reorganized. Values are tuples
    because one UI affordance can eventually map to several concrete legs."""
    from jasper.wake_legs import by_token, wake_input_legs
    wake_tokens = {leg.token for leg in wake_input_legs()}
    for toggle, tokens in server._TOGGLE_TO_TOKEN.items():
        for token in tokens:
            assert token in wake_tokens, (
                f"toggle {toggle!r} -> {token!r} is not a wake_input leg"
            )
    # The collision the map exists to document: operator "raw" == "off".
    assert server._TOGGLE_TO_TOKEN["raw"] == ("off",)
    assert by_token("off").udp_port == 9877
    assert server._TOGGLE_TO_TOKEN["chip_aec_150"] == ("chip_aec_150",)
    assert server._TOGGLE_TO_TOKEN["chip_aec_210"] == ("chip_aec_210",)


# ---------- _write_wake_threshold ------------------------------------------


def test_write_wake_threshold_preserves_model(wake_model_file):
    """The slider on /system writes the same file as the /wake/
    wizard. A threshold write must preserve any JASPER_WAKE_MODEL
    in place — otherwise picking a new sensitivity would silently
    revert the user's wake-word choice."""
    wake_model_file.write_text(
        "JASPER_WAKE_MODEL=/var/lib/jasper/wake/jarvis_v2.onnx\n"
        "JASPER_WAKE_THRESHOLD=0.50\n"
    )
    server._write_wake_threshold(0.65)
    body = wake_model_file.read_text()
    assert "JASPER_WAKE_MODEL=/var/lib/jasper/wake/jarvis_v2.onnx" in body
    assert "JASPER_WAKE_THRESHOLD=0.65" in body


def test_write_wake_threshold_normalises_to_two_decimals(wake_model_file):
    """Browsers can ship value="0.5000000001" after JSON roundtrip.
    Match the slider step granularity (0.05) by formatting to two
    decimal places — keeps wake_model.env clean and diffable."""
    server._write_wake_threshold(0.5000000001)
    body = wake_model_file.read_text()
    assert "JASPER_WAKE_THRESHOLD=0.50" in body


def test_write_wake_threshold_rejects_out_of_range(wake_model_file):
    for bad in (-0.1, 1.1, 5.0, -1.0):
        with pytest.raises(ValueError, match="threshold out of range"):
            server._write_wake_threshold(bad)


def test_read_wake_threshold_default_when_file_missing(wake_model_file, monkeypatch):
    """Fresh install: no wake_model.env yet. Slider must show the
    daemon's compiled-in default (0.3, per Config.wake_threshold in
    jasper/config.py and .env.example) so users see what's actually live
    — not a misleading
    0, and not a higher value that a Save would silently raise the real
    threshold to."""
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    assert not wake_model_file.exists()
    assert server._read_wake_threshold() == 0.3


def test_read_wake_threshold_reads_persisted_value(wake_model_file, monkeypatch):
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    wake_model_file.write_text("JASPER_WAKE_THRESHOLD=0.35\n")
    assert server._read_wake_threshold() == 0.35


def test_read_wake_threshold_falls_back_to_env(wake_model_file, monkeypatch):
    """If no wizard file but process env has the var (operator set
    in /etc/jasper/jasper.env), use that. Matches daemon precedence."""
    monkeypatch.setenv("JASPER_WAKE_THRESHOLD", "0.42")
    assert not wake_model_file.exists()
    assert server._read_wake_threshold() == 0.42


def test_read_wake_threshold_default_matches_daemon_config(wake_model_file, monkeypatch):
    """The control-plane unconfigured fallback MUST equal the daemon's
    compiled-in default. If they drift, /wake/'s slider and
    /state.aec.threshold show a value higher than what jasper-voice is
    actually running, and a Save at the displayed value silently RAISES
    the live threshold (the bug this guards). Assert against the daemon
    Config so the two can't diverge again rather than re-hardcoding the
    literal."""
    from jasper.config import Config

    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    # Config.from_env() requires a provider + key to construct; supply
    # the minimum so we can read its compiled-in wake_threshold default.
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert not wake_model_file.exists()
    assert server._read_wake_threshold() == Config.from_env().wake_threshold


# ---------- _aec_full_status -----------------------------------------------


def test_aec_full_status_includes_legs_and_threshold(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """The /wake/ microphone settings view polls this every 3s. All
    fields must be present in the response shape so the JS doesn't
    have to null-check across deploy boundaries."""
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=1\n"
    )
    wake_model_file.write_text("JASPER_WAKE_THRESHOLD=0.40\n")
    # Stub the systemctl call — we don't want this unit test to
    # depend on a live jasper-aec-bridge.service.
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    # Stub the mic profile probe so the chip leg's `available` flag is
    # deterministic off-device (no /proc/asound/Array here).
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_MIC_DEVICE_RAW": "udp:9877",
            "JASPER_MIC_DEVICE_DTLN": "udp:9878",
            "JASPER_AEC_DTLN_ENABLED": "1",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
        },
    )
    validation_filters = []

    def fake_validation_summary(**kwargs):
        validation_filters.append(kwargs)
        return {"state": "current", "status": "pass"}

    monkeypatch.setattr(server, "_audio_validation_summary", fake_validation_summary)
    status = server._aec_full_status()
    assert status["mode"] == "auto"
    assert status["bridge_active"] is True
    assert status["bridge_role"] == "software_aec3"
    assert status["software_aec3"] == {
        "configured": True,
        "active": True,
        "bypassed": False,
        "reason": "Software AEC3 bridge is active.",
    }
    assert status["legs"]["raw"]["configured"] is True
    assert status["legs"]["dtln"]["configured"] is True
    assert status["legs"]["chip_aec"]["configured"] is False
    assert status["legs"]["chip_aec"]["available"] is True
    assert status["chip_aec_gate"]["status"] == "approved"
    assert status["threshold"] == 0.40
    assert status["audio_profile"]["requested"] == "xvf_software_aec3"
    assert status["audio_profile"]["active"] == "xvf_software_aec3"
    assert status["audio_profile"]["state"] == "active"
    assert status["mic_settings"]["schema_version"] == 1
    assert status["mic_settings"]["echo"]["mode"] == "software_aec3"
    assert status["mic_settings"]["mic"]["kind"] == "xvf3800"
    assert status["microphone"]["detected"] is True
    assert status["microphone"]["firmware"]["state"] == "ok"
    assert status["microphone"]["processing_mode"] == "Software AEC3"
    assert status["microphone"]["session_source"] == "WebRTC AEC3 via :9876"
    assert status["microphone"]["wake_legs"] == ["AEC3", "Chip-direct raw", "DTLN"]
    assert status["validation"] == {"state": "current", "status": "pass"}
    assert validation_filters == [{
        "requested_profile": "xvf_software_aec3",
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }]
    assert status["wake_word"]["label"]


def test_aec_full_status_with_disabled_aec(aec_mode_file, wake_model_file, monkeypatch):
    """Fresh install with no slider edit and no AEC enable yet. The
    UI relies on this shape being identical regardless of state so
    the row-disable logic can key off `mode`."""
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=disabled\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: False)
    _stub_xvf_runtime(monkeypatch, variant=None, present=False, channels=None)
    monkeypatch.setattr(server, "_fresh_jasper_env", lambda: {})
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    status = server._aec_full_status()
    assert status["mode"] == "disabled"
    assert status["profile"] == "direct_mic"
    assert status["bridge_active"] is False
    assert status["bridge_role"] == "off"
    assert status["software_aec3"] == {
        "configured": False,
        "active": False,
        "bypassed": False,
        "reason": "AEC bridge is disabled by the direct-mic profile.",
    }
    assert status["legs"]["raw"]["configured"] is False
    assert status["legs"]["raw"]["available"] is False
    assert status["legs"]["dtln"]["configured"] is False
    assert status["legs"]["dtln"]["available"] is False
    assert status["legs"]["chip_aec"]["configured"] is False
    assert status["legs"]["chip_aec"]["available"] is False
    assert status["raw_intent"]["leg_raw"] is True
    # Unconfigured threshold falls back to the daemon default (0.3), not
    # a higher value the slider would otherwise misrepresent as live.
    assert status["threshold"] == 0.3
    assert status["microphone"]["processing_mode"] == "Direct mic"
    assert status["microphone"]["firmware"]["state"] == "absent"
    assert status["mic_settings"]["echo"]["mode"] == "no_mic"
    assert all(
        not choice["enabled"]
        for choice in status["mic_settings"]["echo"]["choices"]
    )


def test_aec_full_status_chip_available_tracks_firmware(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """The chip-AEC leg's `available` flag mirrors the detected mic beam
    plan, so the /wake/ toggle can grey out on unsupported firmware or
    geometry. Configured state is independent of available."""
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(
        monkeypatch,
        variant=xvf3800.VARIANT_2CH,
        present=True,
        channels=2,
    )
    monkeypatch.setattr(server, "_fresh_jasper_env", lambda: {})
    status = server._aec_full_status()
    # Applied leg state reflects reconciler output; raw_intent preserves
    # the operator's unavailable chip request.
    assert status["raw_intent"]["leg_chip_aec"] is True
    assert status["legs"]["chip_aec"]["configured"] is False
    assert status["legs"]["chip_aec"]["available"] is False
    assert "Chip-AEC needs" in " ".join(status["microphone"]["warnings"])

    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )
    assert server._aec_full_status()["legs"]["chip_aec"]["available"] is True


def test_aec_full_status_rejects_unvalidated_beam_plan_from_one_probe(
    aec_mode_file,
    wake_model_file,
    monkeypatch,
):
    plan = xvf3800.ChipBeamPlan(
        plan_id="experimental_unvalidated",
        display_name="Experimental unvalidated",
        geometry="square",
        description="test-only research plan",
        legs=xvf3800.SQUARE_FIXED_150_210_PLAN.legs,
        production_validated=False,
    )
    monkeypatch.setitem(xvf3800.CHIP_BEAM_PLANS, plan.plan_id, plan)
    probe_calls = 0

    def probe() -> MicProbe:
        nonlocal probe_calls
        probe_calls += 1
        return MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            display_name="Experimental XVF",
            alsa_card_name=xvf3800.ALSA_CARD_NAME,
            variant_id="experimental_variant",
            geometry="square",
            chip_beam_plan=plan.plan_id,
            probe_error=None,
        )

    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=auto\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
    )
    monkeypatch.setattr(aec_endpoints, "_xvf_mic_probe", probe)
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        },
    )

    status = server._aec_full_status()

    assert probe_calls == 1
    assert status["microphone"]["detected"] is True
    assert status["legs"]["chip_aec"]["available"] is False
    assert status["audio_profile"]["requested"] == "xvf_software_aec3"
    assert status["bridge_role"] == "software_aec3"


def test_aec_full_status_surfaces_required_xvf_firmware_update(
    aec_mode_file, wake_model_file, monkeypatch,
):
    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=auto\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: False)
    monkeypatch.setattr(aec_endpoints, "_unit_active", lambda unit: False)
    monkeypatch.setattr(aec_endpoints, "_read_xvf_firmware_update_state", lambda: {})
    _stub_xvf_runtime(
        monkeypatch,
        variant=xvf3800.VARIANT_2CH,
        present=True,
        channels=2,
    )
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )

    status = server._aec_full_status()

    assert status["firmware_update"]["state"] == "update_required"
    assert status["firmware_update"]["required"] is True
    assert status["firmware_update"]["action"]["enabled"] is True
    assert status["firmware_update"]["target"]["id"] == "legacy_square_6ch"
    assert status["firmware_update"]["target"]["sha256"] == (
        xvf3800.FIRMWARE_KNOWN_GOOD_SHA256
    )
    assert status["mic_settings"]["mic"]["firmware_update"]["state"] == (
        "update_required"
    )


def test_aec_full_status_auto_profile_resolves_chip_when_available(
    aec_mode_file, wake_model_file, monkeypatch,
):
    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=auto\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
        },
    )

    status = server._aec_full_status()

    assert status["profile"] == "auto"
    assert status["bridge_role"] == "chip_aec_carrier"
    assert status["software_aec3"] == {
        "configured": False,
        "active": False,
        "bypassed": True,
        "reason": (
            "Chip-AEC profile selected; WebRTC AEC3 is bypassed while "
            "the bridge carries the chip beam to voice."
        ),
    }
    assert status["legs"]["raw"]["configured"] is False
    assert status["legs"]["chip_aec"]["configured"] is True
    assert status["legs"]["chip_aec_150"]["configured"] is False
    assert status["legs"]["chip_aec_150"]["active"] is False
    assert status["legs"]["chip_aec_210"]["configured"] is False
    assert status["legs"]["chip_aec_210"]["active"] is False
    assert status["raw_intent"]["leg_raw"] is True
    assert status["audio_profile"]["selection"] == "auto"
    assert status["audio_profile"]["requested"] == "xvf_chip_aec"
    assert status["audio_profile"]["validation_profile"] == "xvf_chip_aec"
    assert status["mic_settings"]["echo"]["mode"] == "hardware_chip_aec"
    assert status["mic_settings"]["echo"]["software_aec3"]["bypassed"] is True
    assert status["chip_aec_gate"]["production_available"] is True


def test_custom_chip_beam_toggle_uses_saved_intent_until_reconcile(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """After an advanced toggle POST, aec_mode.env is already saved while
    jasper-aec-reconcile restarts asynchronously. The /wake/ checkbox must
    reflect saved intent, not briefly flip back to the old runtime state."""

    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=custom\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
        "JASPER_WAKE_LEG_CHIP_AEC_150=1\n"
        "JASPER_WAKE_LEG_CHIP_AEC_210=0\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            # Reconciler has not yet published the optional 150 beam device.
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
        },
    )

    status = server._aec_full_status()
    toggles = {
        toggle["id"]: toggle
        for toggle in status["mic_settings"]["fusion"]["toggles"]
    }

    assert status["raw_intent"]["leg_chip_aec_150"] is True
    assert status["legs"]["chip_aec_150"]["configured"] is False
    assert toggles["chip_aec_150"]["checked"] is True
    assert toggles["chip_aec_150"]["applied"] is False
    assert toggles["chip_aec_150"]["status"] == "starting"


def test_aec_full_status_testing_profile_allows_unapproved_dac_testing(
    aec_mode_file, wake_model_file, monkeypatch,
):
    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec_testing\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AUDIO_DAC_ID": "mystery_usb_audio",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_AEC_CHIP_AEC_DAC_STATUS": "testing",
            "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "explicit_testing",
            "JASPER_AEC_CHIP_AEC_DAC_DETAIL": "operator validation",
        },
    )

    status = server._aec_full_status()

    assert status["profile"] == "xvf_chip_aec_testing"
    assert status["audio_profile"]["requested"] == "xvf_chip_aec_testing"
    assert status["audio_profile"]["active"] == "xvf_chip_aec_testing"
    assert status["audio_profile"]["validation_profile"] == "xvf_chip_aec"
    assert status["chip_aec_gate"]["status"] == "testing"
    assert status["chip_aec_gate"]["auto_allowed"] is False
    assert status["chip_aec_gate"]["testing_available"] is True
    assert status["legs"]["chip_aec"]["available"] is True
    assert status["legs"]["chip_aec"]["production_available"] is False


def test_aec_full_status_flex_linear_auto_resolves_software_aec3(
    aec_mode_file, wake_model_file, monkeypatch,
):
    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=auto\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch, variant=xvf3800.VARIANT_FLEX_LINEAR_6CH)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
            "JASPER_MIC_DEVICE_RAW": "udp:9877",
            "JASPER_XVF_VARIANT": "xvf3800_flex_linear_6ch",
            "JASPER_XVF_GEOMETRY": "linear",
            "JASPER_XVF_CHIP_AEC_SUPPORTED": "0",
        },
    )

    status = server._aec_full_status()

    assert status["legs"]["chip_aec"]["available"] is False
    assert status["audio_profile"]["requested"] == "xvf_software_aec3"
    assert status["microphone"]["variant_id"] == "xvf3800_flex_linear_6ch"
    assert status["microphone"]["geometry"] == "linear"


def test_aec_full_status_chip_aec_request_shows_runtime_software_until_applied(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """The status card must not present intent as applied runtime truth."""
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_MIC_DEVICE_RAW": "udp:9877",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
        },
    )

    status = server._aec_full_status()

    assert status["raw_intent"]["leg_chip_aec"] is True
    assert status["legs"]["chip_aec"]["configured"] is False
    assert status["legs"]["raw"]["configured"] is True
    assert status["bridge_role"] == "software_aec3"
    assert status["software_aec3"]["active"] is True
    assert status["software_aec3"]["bypassed"] is False
    assert status["audio_profile"]["selection"] == "xvf_chip_aec"
    assert status["audio_profile"]["requested"] == "xvf_chip_aec"
    assert status["audio_profile"]["active"] == "xvf_software_aec3"
    assert status["audio_profile"]["state"] == "pending"
    assert status["microphone"]["processing_mode"] == "Software AEC3"
    assert status["mic_settings"]["echo"]["mode"] == "software_aec3"
    assert "not applied" in " ".join(status["microphone"]["warnings"])


def test_aec_full_status_explicit_chip_fallback_reports_software_aec3(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """Unsupported explicit hardware-AEC request must show active fallback.

    The reconciler fail-closes to software AEC3. `/aec` must report that
    applied runtime truth instead of claiming WebRTC AEC3 is bypassed.
    """
    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AUDIO_DAC_ID": "dual_apple_usb_c_dac_4ch",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_MIC_DEVICE_RAW": "udp:9877",
            "JASPER_AEC_CHIP_AEC_DAC_ID": "dual_apple_usb_c_dac_4ch",
            "JASPER_AEC_CHIP_AEC_DAC_STATUS": "needs_calibration",
            "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "static",
            "JASPER_AEC_CHIP_AEC_DAC_DETAIL": (
                "Dual Apple USB-C DAC measured-sync contract needs validation"
            ),
        },
    )

    status = server._aec_full_status()

    assert status["bridge_role"] == "software_aec3"
    assert status["software_aec3"] == {
        "configured": True,
        "active": True,
        "bypassed": False,
        "reason": "Software AEC3 bridge is active.",
    }
    assert status["raw_intent"]["leg_chip_aec"] is True
    assert status["legs"]["chip_aec"]["configured"] is False
    assert status["legs"]["raw"]["configured"] is True
    assert status["audio_profile"]["selection"] == "xvf_chip_aec"
    assert status["audio_profile"]["requested"] == "xvf_chip_aec"
    assert status["audio_profile"]["active"] == "xvf_software_aec3"
    assert status["audio_profile"]["state"] == "fallback"
    assert status["mic_settings"]["echo"]["mode"] == "software_aec3"
    hardware = next(
        choice
        for choice in status["mic_settings"]["echo"]["choices"]
        if choice["profile"] == "xvf_chip_aec"
    )
    assert hardware["selected"] is True
    assert hardware["enabled"] is False
    assert hardware["status"] == "needs calibration"


def test_aec_full_status_names_stale_saved_aec_card(
    aec_mode_file, wake_model_file, monkeypatch,
):
    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: False)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        },
    )

    status = server._aec_full_status()

    assert status["audio_profile"]["state"] == "waiting_bridge"
    assert "configured AEC mic L16K6Ch" in status["audio_profile"]["reason"]
    warnings = " ".join(status["microphone"]["warnings"])
    assert "Configured AEC mic L16K6Ch" in warnings
    assert "detected XVF card Array" in warnings


def test_aec_full_status_stale_aec_card_does_not_report_software_active(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """A stale runtime env is not an active software-AEC3 path.

    The bridge service can briefly remain active while the reconciler is
    correcting a mic-card change. `/aec` must surface that as pending instead
    of treating "bridge process is up" as "WebRTC AEC3 is running on the
    detected mic."
    """
    aec_mode_file.write_text(
        "JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
            "JASPER_MIC_DEVICE_RAW": "udp:9877",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        },
    )

    status = server._aec_full_status()

    assert status["bridge_active"] is True
    assert status["bridge_role"] == "pending"
    assert status["audio_profile"]["active"] is None
    assert status["audio_profile"]["state"] == "pending"
    assert "configured AEC mic L16K6Ch" in status["audio_profile"]["reason"]
    assert status["software_aec3"] == {
        "configured": False,
        "active": False,
        "bypassed": False,
        "reason": status["audio_profile"]["reason"],
    }
    assert status["legs"]["raw"]["configured"] is False
    assert status["legs"]["dtln"]["configured"] is False
    assert status["legs"]["chip_aec"]["configured"] is False
    assert status["mic_settings"]["echo"]["mode"] == "hardware_chip_aec_pending"


def test_aec_full_status_chip_aec_applied_requires_runtime_env(
    aec_mode_file, wake_model_file, monkeypatch,
):
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n"
    )
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: True)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    _stub_xvf_runtime(monkeypatch)
    monkeypatch.setattr(
        server,
        "_fresh_jasper_env",
        lambda: {
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
        },
    )

    status = server._aec_full_status()

    assert status["microphone"]["processing_mode"] == "Chip-AEC"
    assert status["microphone"]["session_source"] == "Chip AEC 150 beam via :9876"
    assert status["microphone"]["warnings"] == []


def test_aec_full_status_survives_firmware_probe_error(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """A failing firmware probe must never 500 the status GET the /wake/
    page polls every 3 s — it degrades to available=False."""
    aec_mode_file.write_text("JASPER_AEC_MODE=auto\n")
    monkeypatch.setattr(server, "_aec_bridge_active", lambda: False)
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)

    def _boom():
        raise RuntimeError("proc read blew up")

    monkeypatch.setattr(
        "jasper.mics.xvf3800.detect_runtime_profile", _boom,
    )
    status = server._aec_full_status()
    assert status["legs"]["chip_aec"]["available"] is False
    assert "microphone" in status


def test_write_aec_leg_chip_aec_150_writes_boolean(aec_mode_file):
    """The /aec/leg POST for chip_aec_150 writes its per-beam boolean,
    preserving the other leg keys (RMW)."""
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
    )
    server._write_aec_leg("chip_aec_150", True)
    body = aec_mode_file.read_text()
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=1" in body
    assert "JASPER_WAKE_LEG_RAW=1" in body   # preserved
    assert "JASPER_AUDIO_INPUT_PROFILE=custom" in body
    assert server._read_aec_state()["leg_chip_aec_150"] is True


# ---------- chip_aec_gate blockers: one canonical vocabulary ----------------
#
# `chip_aec_gate.blockers` once carried two vocabularies on one JSON field:
# the ChipAecGate dataclass emitted "mic"/"dac" (and recommended_action
# switched on them), while jasper-control's _chip_aec_gate overwrote the list
# with a parallel "mic_beam_plan"/"dac_gate" scheme. A consumer could not
# reliably switch on the field. These tests pin the unified vocabulary
# (CHIP_AEC_BLOCKER_CODES) and fail if a mixed/foreign value is reintroduced.

# An approved DAC needs no calibration (no "dac" blocker); an unapproved one
# does. Pulled from the registry so a profile rename can't silently rot these.
_APPROVED_DAC_ID = "hifiberry_dac8x"
_UNAPPROVED_DAC_ID = "mystery_usb_audio_not_in_registry"


def _gate_state() -> dict:
    """Minimal aec_mode.env-shaped state selecting the chip-AEC profile."""
    return {
        "mode": "auto",
        "leg_raw": False,
        "leg_dtln": False,
        "leg_chip_aec": True,
        "profile": "xvf_chip_aec",
    }


@pytest.mark.parametrize(
    ("mic_available", "dac_id", "expected"),
    [
        # mic present + approved DAC -> nothing blocks
        (True, _APPROVED_DAC_ID, set()),
        # mic absent + approved DAC -> only the mic blocks
        (False, _APPROVED_DAC_ID, {BLOCKER_MIC}),
        # mic present + unapproved DAC -> only the DAC blocks
        (True, _UNAPPROVED_DAC_ID, {BLOCKER_DAC}),
        # mic absent + unapproved DAC -> both block
        (False, _UNAPPROVED_DAC_ID, {BLOCKER_MIC, BLOCKER_DAC}),
    ],
)
def test_chip_aec_gate_blockers_use_canonical_vocabulary(
    mic_available, dac_id, expected,
):
    """Every blocker _chip_aec_gate emits is a canonical code, in every
    mic/DAC permutation — and matches the exact expected set so the
    gating itself is pinned alongside the vocabulary."""
    payload = aec_endpoints._chip_aec_gate(
        {"JASPER_AUDIO_DAC_ID": dac_id},
        _gate_state(),
        mic_available=mic_available,
    )
    blockers = payload["blockers"]
    assert set(blockers) == expected
    # The whole point of the fix: a consumer can switch on these codes.
    assert set(blockers) <= CHIP_AEC_BLOCKER_CODES
    # Mutation guard — the retired foreign vocabulary must never come back.
    assert "mic_beam_plan" not in blockers
    assert "dac_gate" not in blockers


def test_chip_aec_gate_blockers_never_emit_foreign_codes():
    """Exhaustive scan: across the mic x DAC matrix, the union of every
    code _chip_aec_gate can put on `blockers` is exactly the canonical
    set's subset — reintroducing a second vocabulary fails here."""
    emitted: set[str] = set()
    for mic_available in (True, False):
        for dac_id in (_APPROVED_DAC_ID, _UNAPPROVED_DAC_ID):
            payload = aec_endpoints._chip_aec_gate(
                {"JASPER_AUDIO_DAC_ID": dac_id},
                _gate_state(),
                mic_available=mic_available,
            )
            emitted.update(payload["blockers"])
    assert emitted == {BLOCKER_MIC, BLOCKER_DAC}
    assert emitted <= CHIP_AEC_BLOCKER_CODES


def test_chip_aec_gate_recommended_action_reflects_mic_blocker():
    """recommended_action reads the same vocabulary the gate emits: a
    missing mic routes the operator to fix the mic first. This was dead
    before unification because the endpoint's foreign 'mic_beam_plan'
    code never reached the dataclass that recommended_action inspects."""
    payload = aec_endpoints._chip_aec_gate(
        {"JASPER_AUDIO_DAC_ID": _APPROVED_DAC_ID},
        _gate_state(),
        mic_available=False,
    )
    assert BLOCKER_MIC in payload["blockers"]
    assert payload["recommended_action"] == ACTION_FIX_MIC_PROFILE
