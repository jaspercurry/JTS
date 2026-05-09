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
    monkeypatch.setenv("JASPER_SOUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("JASPER_MANAGEMENT_URL", "https://test.local")
    monkeypatch.setenv("JASPER_GEMINI_VOICE", "Aoede")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("JASPER_GEMINI_API_KEY", raising=False)
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
