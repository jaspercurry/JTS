from __future__ import annotations

import pytest

from jasper.config import Config


def test_defaults_with_only_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for var in [
        "JASPER_VOICE_PROVIDER", "JASPER_GEMINI_MODEL", "JASPER_WAKE_MODEL",
        "JASPER_DUCK_DB", "JASPER_DAILY_SPEND_CAP_USD",
        "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
    ]:
        monkeypatch.delenv(var, raising=False)

    cfg = Config.from_env()
    assert cfg.voice_provider == "gemini"
    assert cfg.gemini_api_key == "test-key"
    assert cfg.gemini_model == "gemini-3.1-flash-live-preview"
    assert cfg.wake_model == "hey_jarvis"
    assert cfg.duck_db == -15.0
    assert cfg.daily_spend_cap_usd == 1.0
    assert cfg.spotify_enabled is False


def test_missing_gemini_key_raises_when_provider_is_gemini(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        Config.from_env()


def test_spotify_enabled_when_both_creds_present(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "abc")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "def")
    cfg = Config.from_env()
    assert cfg.spotify_enabled is True
