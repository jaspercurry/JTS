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
    assert cfg.live_context_reset_sec == 300
    assert cfg.daily_spend_cap_usd == 1.0
    # ALSA defaults must match the templates in /root/.asoundrc and the
    # post-install /etc/jasper/jasper.env. If these drift, first-boot fails.
    assert cfg.mic_device == "Array"
    assert cfg.mic_capture_rate == 16000
    assert cfg.mic_capture_channels == 1
    assert cfg.tts_device == "jasper_out"
    assert cfg.tts_output_rate == 48000
    assert cfg.tts_gain_db == -8.0
    assert cfg.tts_music_headroom_db == 12.0
    assert cfg.tts_silence_threshold_dbfs == -50.0
    assert cfg.tts_music_window_sec == 8.0
    assert cfg.volume_state_path == "/var/lib/jasper/speaker_volume.json"
    assert cfg.volume_regress_after_sec == 1800.0
    assert cfg.volume_regress_safe_low_pct == 20
    assert cfg.volume_regress_safe_high_pct == 70
    assert cfg.volume_first_boot_default_pct == 50
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
        ("JASPER_LIVE_CONTEXT_RESET_SEC", "0", "JASPER_LIVE_CONTEXT_RESET_SEC"),
        ("JASPER_DAILY_SPEND_CAP_USD", "-1", "JASPER_DAILY_SPEND_CAP_USD"),
        ("JASPER_TTS_GAIN_DB", "8", "JASPER_TTS_GAIN_DB"),
        ("JASPER_TTS_GAIN_DB", "0.5", "JASPER_TTS_GAIN_DB"),
        ("JASPER_TTS_SILENCE_THRESHOLD_DBFS", "0", "JASPER_TTS_SILENCE_THRESHOLD_DBFS"),
        ("JASPER_TTS_SILENCE_THRESHOLD_DBFS", "5", "JASPER_TTS_SILENCE_THRESHOLD_DBFS"),
        ("JASPER_TTS_MUSIC_WINDOW_SEC", "0", "JASPER_TTS_MUSIC_WINDOW_SEC"),
        ("JASPER_TTS_MUSIC_WINDOW_SEC", "-5", "JASPER_TTS_MUSIC_WINDOW_SEC"),
        ("JASPER_VOLUME_REGRESS_AFTER_SEC", "0", "JASPER_VOLUME_REGRESS_AFTER_SEC"),
        ("JASPER_VOLUME_REGRESS_SAFE_LOW_PCT", "150", "JASPER_VOLUME_REGRESS_SAFE_LOW_PCT"),
        ("JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT", "-1", "JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT"),
        ("JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT", "200", "JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT"),
    ],
)
def test_invalid_numeric_env_values_raise(monkeypatch, name, value, expected):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=expected):
        Config.from_env()


def test_tts_gain_db_zero_is_allowed(monkeypatch):
    """The boundary: zero offset is fine (TTS at master level), only
    positive values risk pushing TTS above the user's master."""
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_TTS_GAIN_DB", "0")
    cfg = Config.from_env()
    assert cfg.tts_gain_db == 0.0
