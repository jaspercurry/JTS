from __future__ import annotations

import pytest

from jasper.config import Config


def test_defaults_with_only_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for var in [
        "JASPER_VOICE_PROVIDER", "JASPER_GEMINI_MODEL", "JASPER_WAKE_MODEL",
        "JASPER_DUCK_DB", "JASPER_DAILY_SPEND_CAP_USD",
        "JASPER_MIC_DEVICE", "JASPER_TTS_DEVICE",
        "JASPER_SPOTIFY_DEVICE_NAME",
        "JASPER_DEFAULT_LOCATION", "JASPER_WEATHER_UNITS",
        "JASPER_SUBWAY_STATION_ID", "JASPER_SUBWAY_DEFAULT_DIRECTION",
        "JASPER_SUBWAY_LINES",
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
    # ALSA defaults must match the templates in /root/.asoundrc and the
    # post-install /etc/jasper/jasper.env. If these drift, first-boot fails.
    assert cfg.mic_device == "Array"
    assert cfg.mic_capture_rate == 16000
    assert cfg.mic_capture_channels == 1
    assert cfg.tts_device == "jasper_xvf"
    assert cfg.tts_output_rate == 24000
    assert cfg.tts_gain_db == -8.0
    assert cfg.aec_mode == "hardware"
    assert cfg.gemini_voice == "Aoede"
    assert cfg.vad_barge_in_threshold == 0.5
    assert cfg.spotify_device_name == "moode"
    assert cfg.weather_default_location == ""
    assert cfg.weather_units == "celsius"
    assert cfg.subway_station_id == ""
    assert cfg.subway_default_direction == "uptown"
    assert cfg.subway_lines == ()
    assert cfg.subway_enabled is False
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


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("JASPER_WAKE_THRESHOLD", "1.2", "JASPER_WAKE_THRESHOLD"),
        ("JASPER_IDLE_TIMEOUT_SEC", "0", "JASPER_IDLE_TIMEOUT_SEC"),
        ("JASPER_DAILY_SPEND_CAP_USD", "-1", "JASPER_DAILY_SPEND_CAP_USD"),
    ],
)
def test_invalid_numeric_env_values_raise(monkeypatch, name, value, expected):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=expected):
        Config.from_env()
