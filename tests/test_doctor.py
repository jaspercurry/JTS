"""Unit tests for jasper-doctor's env loading, provider-aware key
check, and ALSA mic-card lookup. Hardware-side checks (sounddevice,
systemctl, arecord, etc) are exercised on the Pi via
``jasper-doctor`` itself; this file pins the pure-python helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jasper.cli import doctor
from jasper.config import Config


# ---------------------------------------------------------------- env loading


def test_parse_env_file_basic(tmp_path: Path):
    p = tmp_path / "jasper.env"
    p.write_text(
        "# comment line\n"
        "\n"
        "GEMINI_API_KEY=AIzaSyABC\n"
        "JASPER_VOICE_PROVIDER=openai\n"
        'OPENAI_API_KEY="sk-quoted"\n'
        "EMPTY=\n"
        "  WHITESPACE_KEY  =  trimmed  \n"
    )
    out = doctor._parse_env_file(str(p))
    assert out["GEMINI_API_KEY"] == "AIzaSyABC"
    assert out["JASPER_VOICE_PROVIDER"] == "openai"
    assert out["OPENAI_API_KEY"] == "sk-quoted"
    assert out["EMPTY"] == ""
    assert out["WHITESPACE_KEY"] == "trimmed"


def test_parse_env_file_missing_returns_empty(tmp_path: Path):
    out = doctor._parse_env_file(str(tmp_path / "does-not-exist"))
    assert out == {}


def test_load_env_files_wizard_overrides_operator(monkeypatch, tmp_path: Path):
    """`/var/lib/jasper/voice_provider.env` (wizard) must override
    `/etc/jasper/jasper.env` (operator) — same precedence as the
    systemd unit's `EnvironmentFile=` ordering. Verified via the
    explicit-paths form of `load_env_files` so test fixtures don't
    have to monkeypatch a module-level constant."""
    from jasper.env_load import load_env_files
    operator = tmp_path / "jasper.env"
    operator.write_text(
        "GEMINI_API_KEY=op-key\n"
        "JASPER_VOICE_PROVIDER=gemini\n"
    )
    wizard = tmp_path / "voice_provider.env"
    wizard.write_text(
        "OPENAI_API_KEY=wiz-key\n"
        "JASPER_VOICE_PROVIDER=openai\n"
    )
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "JASPER_VOICE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)

    load_env_files((str(operator), str(wizard)))

    assert os_environ_get("GEMINI_API_KEY") == "op-key"
    assert os_environ_get("OPENAI_API_KEY") == "wiz-key"
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def test_load_env_files_shell_wins_over_files(monkeypatch, tmp_path: Path):
    """A var already in the calling shell must NOT be overwritten by
    the env files. Lets an operator probe with `FOO=bar jasper-doctor`."""
    from jasper.env_load import load_env_files
    operator = tmp_path / "jasper.env"
    operator.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")

    load_env_files((str(operator),))
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def os_environ_get(name: str) -> str | None:
    import os
    return os.environ.get(name)


# -------------------------------------------------- provider-aware key check


def _fresh_cfg(monkeypatch, **vars_) -> Config:
    """Build a Config with only the requested env vars set."""
    drop = [
        "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
        "JASPER_VOICE_PROVIDER", "JASPER_GEMINI_MODEL",
        "SPOTIFY_CLIENT_ID",
    ]
    for v in drop:
        monkeypatch.delenv(v, raising=False)
    for k, v in vars_.items():
        monkeypatch.setenv(k, v)
    return Config.from_env()


def test_provider_key_gemini_ok(monkeypatch):
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaABCDEF12345")
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "GEMINI_API_KEY"


def test_provider_key_openai_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "OPENAI_API_KEY"


def test_provider_key_grok_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="grok",
        XAI_API_KEY="xai-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "XAI_API_KEY"


def test_provider_key_warns_on_wrong_prefix(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="WRONGPREFIX-1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "warn"


def test_provider_key_other_providers_keys_unchecked(monkeypatch):
    """Active=openai. GEMINI_API_KEY is intentionally unset; the doctor
    must NOT flag that as a problem — gemini is dormant."""
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-active1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"


# ------------------------------------------------ ALSA shorthand mic lookup


def test_extract_card_name_returns_none_for_shorthand():
    assert doctor._extract_card_name("hw:7,1") is None
    assert doctor._extract_card_name("plughw:0,0") is None


def test_extract_card_name_named_card_passthrough():
    assert doctor._extract_card_name("Array") == "Array"
    assert doctor._extract_card_name("plughw:CARD=Loopback") == "Loopback"


def test_check_arecord_l_card_device_match():
    """Mock arecord -l output for a 6-card system that includes the
    LoopbackAEC bridge target (card 7, device 1)."""
    fake_output = (
        "card 0: dongle [USB Audio], device 0: USB Audio [USB Audio]\n"
        "card 1: Array [XVF3800 Voice Capture], device 0: USB Audio\n"
        "card 6: Loopback [Loopback], device 0: Loopback PCM\n"
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    )
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        assert doctor._check_arecord_l_card_device(7, 1) is True
        assert doctor._check_arecord_l_card_device(7, 0) is True
        assert doctor._check_arecord_l_card_device(99, 0) is False


def test_check_arecord_l_does_not_match_wrong_card():
    """`device 1:` paired with card 6 must NOT satisfy a query for
    card 7 device 1 — both numbers must come from the same line."""
    fake_output = (
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
    )
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        assert doctor._check_arecord_l_card_device(7, 1) is False


def test_check_mic_card_routes_shorthand_through_arecord_l(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = (
        "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    )
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "ok"
    assert "card 7 device 1 present" in r.detail


def test_check_mic_capture_falls_back_to_daemon_active(monkeypatch):
    """When PortAudio refuses to open the mic AND jasper-voice is
    running, the check returns ok with a 'daemon holds device' note
    instead of a spurious fail. This is the snd-aloop / AEC bridge
    case where the daemon owns the capture handle exclusively."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    fake_sd = FakeSD()

    def fake_import(*args, **kwargs):
        if args and args[0] == "sounddevice":
            return fake_sd
        return __import__(*args, **kwargs)

    # Use a sd-stub by monkeypatching the import inside the function.
    # Easier: patch a wrapper. Instead, patch _jasper_voice_active and
    # mock sd.rec via injecting into sys.modules.
    import sys
    sys.modules["sounddevice"] = fake_sd
    try:
        with patch.object(doctor, "_jasper_voice_active", return_value=True):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "ok"
        assert "skipped" in r.detail
        assert "jasper-voice holds" in r.detail
    finally:
        del sys.modules["sounddevice"]


def test_check_mic_capture_fails_hard_when_daemon_inactive(monkeypatch):
    """If jasper-voice ISN'T running and the open still fails, the
    fail is real — the device is missing or misconfigured."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    import sys
    sys.modules["sounddevice"] = FakeSD()
    try:
        with patch.object(doctor, "_jasper_voice_active", return_value=False):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "fail"
    finally:
        del sys.modules["sounddevice"]


def test_check_mic_card_shorthand_failure_actionable(monkeypatch):
    """When the shorthand points at a card/device that's missing, the
    failure detail must mention the AEC bridge — that's the most
    common cause (bridge disabled but JASPER_MIC_DEVICE still set)."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = "card 0: dongle [USB Audio], device 0: USB Audio\n"
    with patch.object(
        doctor, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "fail"
    assert "AEC bridge" in r.detail
