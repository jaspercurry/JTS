from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from jasper.cues import cli
from jasper.cues.generator import TTSResult


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesise(self, text: str) -> TTSResult:
        self.calls.append(text)
        return TTSResult(pcm_24k=b"\x00\x00" * 240)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Standard env for CLI tests: writable sounds dir, deterministic
    hostname/voice, fake API key (so the factory builds a backend),
    and monkey-patched factory that doesn't hit the network."""
    monkeypatch.setenv("JASPER_SOUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("JASPER_MANAGEMENT_URL", "https://test.local")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("JASPER_GEMINI_VOICE", "Aoede")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    fake = _FakeBackend()
    # The CLI builds its backend via `build_cue_tts_backend(cfg)` —
    # patch that to return our deterministic fake regardless of
    # provider so tests don't hit any provider's network.
    monkeypatch.setattr(
        cli, "build_cue_tts_backend", lambda cfg: (fake, "Aoede"),
    )
    return tmp_path, fake


# --- list ---


def test_list_returns_1_when_cues_missing(cli_env, capsys):
    code = cli.main(["list"])
    assert code == 1  # all cues missing on a fresh sounds dir
    captured = capsys.readouterr()
    assert "spend_cap_reached" in captured.out
    assert "cant_connect" in captured.out
    assert "MISSING" in captured.out


def test_list_returns_0_after_regen(cli_env, capsys):
    cli.main(["regenerate"])
    code = cli.main(["list"])
    assert code == 0
    captured = capsys.readouterr()
    assert "MISSING" not in captured.out
    assert "cached" in captured.out


# --- regenerate ---


def test_regenerate_writes_all_cues_by_default(cli_env, capsys):
    code = cli.main(["regenerate"])
    assert code == 0
    captured = capsys.readouterr()
    assert "wrote spend_cap_reached" in captured.out
    assert "wrote cant_connect" in captured.out


def test_regenerate_idempotent_second_run(cli_env, capsys):
    cli.main(["regenerate"])
    capsys.readouterr()  # discard
    code = cli.main(["regenerate"])
    assert code == 0
    captured = capsys.readouterr()
    assert "all cues already cached" in captured.out


def test_regenerate_force_re_renders_cached(cli_env, capsys):
    _, fake = cli_env
    cli.main(["regenerate"])
    fake.calls.clear()
    capsys.readouterr()
    code = cli.main(["regenerate", "--force"])
    assert code == 0
    assert len(fake.calls) >= 2  # both cues re-rendered


def test_regenerate_single_cue(cli_env, capsys):
    code = cli.main(["regenerate", "--cue", "spend_cap_reached"])
    assert code == 0
    captured = capsys.readouterr()
    assert "wrote spend_cap_reached" in captured.out
    assert "wrote cant_connect" not in captured.out


def test_regenerate_unknown_cue_returns_error(cli_env, capsys):
    code = cli.main(["regenerate", "--cue", "definitely_not_a_real_cue"])
    assert code == 2  # ValueError mapped to exit 2
    captured = capsys.readouterr()
    assert "definitely_not_a_real_cue" in captured.err


def test_regenerate_without_api_key_reports_runtime_error(tmp_path, monkeypatch, capsys):
    """With no provider key configured anywhere, the CLI degrades
    gracefully (no backend, regen exits non-zero with explanation)
    rather than crashing on a NoneType attribute access."""
    monkeypatch.setenv("JASPER_SOUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("JASPER_MANAGEMENT_URL", "https://test.local")
    monkeypatch.setenv("JASPER_GEMINI_VOICE", "Aoede")
    # All three provider keys must be absent for the factory to
    # return None — otherwise the fallback path picks one of them.
    for key in (
        "GEMINI_API_KEY", "JASPER_GEMINI_API_KEY",
        "OPENAI_API_KEY", "XAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    # Also keep the file-loader from picking up real /etc/jasper
    # creds on a developer machine that has them.
    monkeypatch.setattr(
        "jasper.cues.cli.load_env_files", lambda *_: None,
    )
    code = cli.main(["regenerate"])
    assert code == 3  # RuntimeError mapped to exit 3
    captured = capsys.readouterr()
    assert "TTS backend" in captured.err or "no TTS backend" in captured.err


# --- play ---


def test_play_unknown_slug_returns_2(cli_env, capsys):
    code = cli.main(["play", "definitely_not_a_real_cue"])
    assert code == 2
    captured = capsys.readouterr()
    assert "unknown" in captured.err.lower()


class _FakeResponse:
    """Minimal context-manager stand-in for urllib's HTTP response."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_play_valid_slug_routes_to_control_endpoint(cli_env, capsys):
    """Happy path: a valid slug must POST to jasper-control and report
    success — exercising the code past find_cue() that used to raise
    NameError on the undefined `_env`. Mocks the HTTP POST so no
    network is touched."""
    captured_url = {}

    def _fake_urlopen(req, timeout=None):
        captured_url["url"] = req.full_url
        captured_url["data"] = req.data
        return _FakeResponse(b'{"result": "ok"}')

    with patch("urllib.request.urlopen", _fake_urlopen):
        code = cli.main(["play", "cant_connect"])

    assert code == 0
    out = capsys.readouterr().out
    assert "played cant_connect" in out
    # Default control host/port came from os.environ.get fallbacks.
    assert captured_url["url"] == "http://127.0.0.1:8780/cue/play"
    assert b"cant_connect" in captured_url["data"]


def test_play_valid_slug_honors_control_env_overrides(cli_env, monkeypatch):
    """The os.environ.get fallbacks are overridable — confirms the
    env lookups (formerly the broken `_env` calls) read the live
    environment."""
    monkeypatch.setenv("JASPER_CONTROL_HOST", "10.0.0.5")
    monkeypatch.setenv("JASPER_CONTROL_PORT", "9999")
    captured_url = {}

    def _fake_urlopen(req, timeout=None):
        captured_url["url"] = req.full_url
        return _FakeResponse(b'{"result": "ok"}')

    with patch("urllib.request.urlopen", _fake_urlopen):
        code = cli.main(["play", "cant_connect"])

    assert code == 0
    assert captured_url["url"] == "http://10.0.0.5:9999/cue/play"
