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

from pathlib import Path

import pytest

from jasper.control import server


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
    }


def test_read_aec_state_parses_all_three_keys(aec_mode_file):
    aec_mode_file.write_text(
        "JASPER_AEC_MODE=disabled\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=1\n"
    )
    state = server._read_aec_state()
    assert state == {
        "mode": "disabled",
        "leg_raw": False,
        "leg_dtln": True,
    }


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


def test_write_aec_leg_creates_file_when_missing(aec_mode_file):
    """A POST that fires before the reconciler has had a chance to
    seed the file must still succeed — write_env_file creates the
    file path's parent if needed."""
    assert not aec_mode_file.exists()
    server._write_aec_leg("raw", False)
    state = server._read_aec_state()
    assert state["leg_raw"] is False


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


def test_toggle_to_token_maps_to_real_wake_input_legs():
    """Every operator toggle name maps to a real wake_input leg token in
    the registry — and "raw" specifically resolves to the chip-direct
    "off" leg on UDP 9877, NOT the "raw0" corpus leg (the easy-to-confuse
    footgun). Guards _TOGGLE_TO_TOKEN against drifting onto the wrong leg
    if the registry is ever reorganized."""
    from jasper.wake_legs import by_token, wake_input_legs
    wake_tokens = {leg.token for leg in wake_input_legs()}
    for toggle, token in server._TOGGLE_TO_TOKEN.items():
        assert token in wake_tokens, (
            f"toggle {toggle!r} -> {token!r} is not a wake_input leg"
        )
    # The collision the map exists to document: operator "raw" == "off".
    assert server._TOGGLE_TO_TOKEN["raw"] == "off"
    assert by_token("off").udp_port == 9877


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
    daemon's compiled-in default (0.5) so users see what's actually
    live, not a misleading 0."""
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    assert not wake_model_file.exists()
    assert server._read_wake_threshold() == 0.5


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


# ---------- _aec_full_status -----------------------------------------------


def test_aec_full_status_includes_legs_and_threshold(
    aec_mode_file, wake_model_file, monkeypatch,
):
    """The /system Wake detection card polls this every 3s. All
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
    status = server._aec_full_status()
    assert status["mode"] == "auto"
    assert status["bridge_active"] is True
    assert status["legs"]["raw"]["configured"] is True
    assert status["legs"]["dtln"]["configured"] is True
    assert status["threshold"] == 0.40


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
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    status = server._aec_full_status()
    assert status == {
        "mode": "disabled",
        "bridge_active": False,
        "legs": {
            "raw": {"configured": True},   # boolean stays even with mode=disabled
            "dtln": {"configured": False},
        },
        "threshold": 0.5,
    }
