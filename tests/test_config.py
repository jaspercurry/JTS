from __future__ import annotations

from pathlib import Path

import pytest

from jasper.config import Config, VoiceProviderNotConfigured
from jasper.voice import catalog

_ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def _parse_env_example():
    """Parse KEY=VALUE lines out of .env.example, ignoring comments/blanks."""
    values: dict[str, str] = {}
    for raw in _ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.fixture(autouse=True)
def _default_voice_provider(monkeypatch):
    """Set JASPER_VOICE_PROVIDER=gemini for every test in this module.

    The Config no longer has an implicit "gemini" default — production
    forces the wizard to write the value to
    /var/lib/jasper/voice_provider.env. Tests that explicitly delenv
    or setenv `JASPER_VOICE_PROVIDER` override this fixture, which is
    what the "unset" assertion tests want."""
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")


def test_defaults_with_only_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for var in [
        "JASPER_GEMINI_MODEL", "JASPER_GEMINI_VOICE",
        "JASPER_OPENAI_MODEL", "JASPER_OPENAI_VOICE",
        "JASPER_OPENAI_REASONING_EFFORT", "JASPER_OPENAI_NOISE_REDUCTION",
        "JASPER_GROK_MODEL", "JASPER_GROK_VOICE",
        "JASPER_WAKE_MODEL",
        "JASPER_DUCK_DB", "JASPER_DUCK_TRANSPORT",
        "JASPER_DAILY_SPEND_CAP_USD",
        "JASPER_MIC_DEVICE", "JASPER_TTS_DEVICE",
        "JASPER_SPEAKER_NAME",
        "JASPER_DEFAULT_LOCATION", "JASPER_WEATHER_LAT",
        "JASPER_WEATHER_LON", "JASPER_WEATHER_DISPLAY_NAME",
        "JASPER_WEATHER_UNITS",
        "JASPER_TRANSIT_LAT", "JASPER_TRANSIT_LON",
        "JASPER_TRANSIT_DISPLAY_NAME",
        "JASPER_SUBWAY_STATION_ID", "JASPER_SUBWAY_DEFAULT_DIRECTION",
        "SPOTIFY_CLIENT_ID",
    ]:
        monkeypatch.delenv(var, raising=False)

    cfg = Config.from_env()
    assert cfg.voice_provider == "gemini"
    assert cfg.gemini_api_key == "test-key"
    assert cfg.gemini_model == catalog.default_model_id("gemini")
    assert cfg.gemini_voice == catalog.default_voice_id("gemini")
    assert cfg.openai_model == catalog.default_model_id("openai")
    assert cfg.openai_voice == catalog.default_voice_id("openai")
    assert cfg.openai_reasoning_effort == catalog.default_extra_value(
        "openai", "reasoning_effort",
    )
    assert cfg.openai_noise_reduction == "auto"
    assert cfg.grok_model == catalog.default_model_id("grok")
    assert cfg.grok_voice == catalog.default_voice_id("grok")
    assert cfg.wake_model == "hey_jarvis"
    assert cfg.duck_db == -25.0
    assert cfg.duck_transport == "fanin"
    # Idle context reset is opt-in (0 = disabled). Per-provider so the
    # cost/race tradeoffs can be tuned separately.
    assert cfg.openai_context_reset_sec == 0
    assert cfg.gemini_context_reset_sec == 0
    assert cfg.grok_context_reset_sec == 0
    # Proactive pre-cap reconnect: OpenAI fires the watchdog at 55 min
    # (3600 cap − 300 buffer); Grok is disabled until xAI publishes
    # a cap.
    assert cfg.openai_session_max_sec == 3600
    assert cfg.openai_proactive_buffer_sec == 300
    assert cfg.grok_session_max_sec == 0
    assert cfg.grok_proactive_buffer_sec == 0
    assert cfg.daily_spend_cap_usd == 1.0
    # ALSA defaults must match the templates in /etc/asound.conf and the
    # post-install /etc/jasper/jasper.env. If these drift, first-boot fails.
    assert cfg.mic_device == "Array"
    assert cfg.mic_capture_rate == 16000
    assert cfg.mic_capture_channels == 1
    assert cfg.aec_chip_aec_enabled is False
    assert cfg.tts_device == "jasper_out"
    assert cfg.tts_transport == "outputd"
    assert cfg.tts_outputd_socket == "/run/jasper-fanin/tts.sock"
    assert cfg.tts_output_rate == 48000
    assert cfg.assistant_loudness_profile_path == (
        "/var/lib/jasper/assistant_loudness_profiles.json"
    )
    assert cfg.assistant_loudness_auto_seed is False
    assert cfg.volume_state_path == "/var/lib/jasper/speaker_volume.json"
    assert cfg.volume_regress_after_sec == 1800.0
    assert cfg.volume_regress_safe_low_pct == 20
    assert cfg.volume_regress_safe_high_pct == 70
    assert cfg.volume_first_boot_default_pct == 50
    assert cfg.gemini_voice == "Aoede"
    assert cfg.vad_barge_in_threshold == 0.5
    assert cfg.server_vad_enabled is False
    assert cfg.spotify_device_name == "JTS"
    assert cfg.weather_default_location == ""
    assert cfg.weather_default_lat is None
    assert cfg.weather_default_lon is None
    assert cfg.weather_default_display_name == ""
    assert cfg.weather_prompt_location == ""
    assert cfg.weather_units == "celsius"
    assert cfg.subway_station_id == ""
    # Empty default direction means "both directions" at query time —
    # set by the /transit/ wizard's "Both" radio.
    assert cfg.subway_default_direction == ""
    assert cfg.subway_enabled is False
    assert cfg.bus_stops == ()
    assert cfg.bus_enabled is False
    assert cfg.spotify_enabled is False


def test_openai_noise_reduction_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_OPENAI_NOISE_REDUCTION", "off")

    cfg = Config.from_env()

    assert cfg.openai_noise_reduction == "off"


def test_invalid_openai_noise_reduction_env_rejected(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_OPENAI_NOISE_REDUCTION", "potato")

    with pytest.raises(RuntimeError, match="JASPER_OPENAI_NOISE_REDUCTION"):
        Config.from_env()


def test_missing_voice_provider_raises_setup_exception(monkeypatch):
    monkeypatch.delenv("JASPER_VOICE_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    with pytest.raises(VoiceProviderNotConfigured, match="JASPER_VOICE_PROVIDER"):
        Config.from_env()


@pytest.mark.parametrize("provider_id", sorted(catalog.VALID_PROVIDER_IDS))
def test_config_accepts_catalog_provider_ids(provider_id, monkeypatch):
    provider = catalog.provider_by_id(provider_id)
    assert provider is not None
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", provider_id)
    monkeypatch.setenv(provider.key_env, "test-key")

    cfg = Config.from_env()

    assert cfg.voice_provider == provider_id


def test_assistant_loudness_profile_path_override(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv(
        "JASPER_ASSISTANT_LOUDNESS_PROFILE_PATH",
        "/tmp/jasper-loudness.json",
    )
    monkeypatch.setenv("JASPER_ASSISTANT_LOUDNESS_AUTO_SEED", "1")

    cfg = Config.from_env()

    assert cfg.assistant_loudness_profile_path == "/tmp/jasper-loudness.json"
    assert cfg.assistant_loudness_auto_seed is True


def test_weather_default_coordinates_from_weather_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_WEATHER_LAT", "40.653")
    monkeypatch.setenv("JASPER_WEATHER_LON", "-74.007")
    monkeypatch.setenv("JASPER_WEATHER_DISPLAY_NAME", "Sunset Park, Brooklyn")
    monkeypatch.setenv("JASPER_WEATHER_UNITS", "fahrenheit")
    cfg = Config.from_env()
    assert cfg.weather_default_lat == 40.653
    assert cfg.weather_default_lon == -74.007
    assert cfg.weather_default_display_name == "Sunset Park, Brooklyn"
    assert cfg.weather_prompt_location == "Sunset Park, Brooklyn"
    assert cfg.weather_units == "fahrenheit"


def test_weather_default_falls_back_to_transit_coords(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("JASPER_WEATHER_LAT", raising=False)
    monkeypatch.delenv("JASPER_WEATHER_LON", raising=False)
    monkeypatch.setenv("JASPER_TRANSIT_LAT", "40.653")
    monkeypatch.setenv("JASPER_TRANSIT_LON", "-74.007")
    monkeypatch.setenv(
        "JASPER_TRANSIT_DISPLAY_NAME",
        "341, 39th Street, Brooklyn",
    )
    cfg = Config.from_env()
    assert cfg.weather_default_lat == 40.653
    assert cfg.weather_default_lon == -74.007
    assert cfg.weather_default_display_name == "341, 39th Street, Brooklyn"
    assert cfg.weather_prompt_location == "341, 39th Street, Brooklyn"


def test_weather_coordinate_pair_must_be_complete(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_WEATHER_LAT", "40.653")
    monkeypatch.delenv("JASPER_WEATHER_LON", raising=False)
    with pytest.raises(RuntimeError, match="JASPER_WEATHER_LAT"):
        Config.from_env()


def test_bus_stops_preserves_labels_with_spaces(monkeypatch):
    """Regression: JASPER_BUS_STOPS labels can contain spaces (MTA
    name + direction, e.g. "4 Av/39 St eastbound"). Earlier the
    config used `.replace(",", " ").split()` which shredded the
    label into separate "stops" — saw it in production with
    "MTA_302680|39 ST/4 AV SE" parsing into four bogus stops."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv(
        "JASPER_BUS_STOPS",
        "MTA_302680|39 ST/4 AV SE,MTA_302682|39 ST/4 AV NW",
    )
    cfg = Config.from_env()
    assert cfg.bus_stops == (
        ("MTA_302680", "39 ST/4 AV SE"),
        ("MTA_302682", "39 ST/4 AV NW"),
    )


def test_bus_stops_bare_id_no_label(monkeypatch):
    """Stops without a `|label` suffix still parse — empty label."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_BUS_STOPS", "MTA_302680")
    cfg = Config.from_env()
    assert cfg.bus_stops == (("MTA_302680", ""),)


def test_missing_gemini_key_raises_when_provider_is_gemini(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        Config.from_env()


def test_spotify_enabled_when_client_id_present(monkeypatch):
    """PKCE: only the client_id is needed. The Client Secret is no
    longer used — the wizard pastes neither."""
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "abc")
    cfg = Config.from_env()
    assert cfg.spotify_enabled is True


def test_blank_spotify_redirect_uri_uses_hostname_default(monkeypatch):
    """A stale empty /etc/jasper value must not suppress the default."""
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "")
    cfg = Config.from_env()
    assert cfg.spotify_redirect_uri == (
        "https://jaspercurry.github.io/spotify-oauth-callback/?host=jts3.local"
    )


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("JASPER_WAKE_THRESHOLD", "1.2", "JASPER_WAKE_THRESHOLD"),
        ("JASPER_IDLE_TIMEOUT_SEC", "0", "JASPER_IDLE_TIMEOUT_SEC"),
        # 0 is now valid (= disabled); negative is rejected.
        ("JASPER_OPENAI_CONTEXT_RESET_SEC", "-1", "JASPER_OPENAI_CONTEXT_RESET_SEC"),
        ("JASPER_GEMINI_CONTEXT_RESET_SEC", "-1", "JASPER_GEMINI_CONTEXT_RESET_SEC"),
        ("JASPER_GROK_CONTEXT_RESET_SEC", "-1", "JASPER_GROK_CONTEXT_RESET_SEC"),
        ("JASPER_DAILY_SPEND_CAP_USD", "-1", "JASPER_DAILY_SPEND_CAP_USD"),
        ("JASPER_TTS_TRANSPORT", "pipewire", "JASPER_TTS_TRANSPORT"),
        ("JASPER_TTS_TRANSPORT", "sounddevice", "pre-outputd revision"),
        ("JASPER_VOLUME_REGRESS_AFTER_SEC", "0", "JASPER_VOLUME_REGRESS_AFTER_SEC"),
        ("JASPER_VOLUME_REGRESS_SAFE_LOW_PCT", "150", "JASPER_VOLUME_REGRESS_SAFE_LOW_PCT"),
        ("JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT", "-1", "JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT"),
        ("JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT", "200", "JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT"),
        ("JASPER_DUCK_TRANSPORT", "sidechain", "JASPER_DUCK_TRANSPORT"),
    ],
)
def test_invalid_env_values_raise(monkeypatch, name, value, expected):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=expected):
        Config.from_env()


def test_tts_outputd_transport_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_TTS_TRANSPORT", "outputd")
    monkeypatch.setenv("JASPER_TTS_OUTPUTD_SOCKET", "/tmp/jasper-outputd.sock")
    cfg = Config.from_env()
    assert cfg.tts_transport == "outputd"
    assert cfg.tts_outputd_socket == "/tmp/jasper-outputd.sock"


def test_duck_transport_env_accepts_fanin(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_DUCK_TRANSPORT", "fanin")

    cfg = Config.from_env()

    assert cfg.duck_transport == "fanin"


def test_fanin_tts_socket_requires_fanin_duck_transport(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_TTS_OUTPUTD_SOCKET", "/run/jasper-fanin/tts.sock")
    monkeypatch.setenv("JASPER_DUCK_TRANSPORT", "camilla")

    with pytest.raises(RuntimeError, match="JASPER_DUCK_TRANSPORT=fanin"):
        Config.from_env()


def test_spend_cap_safety_multiplier_below_one_raises(monkeypatch):
    """A safety multiplier < 1.0 would weaken the cap — reject it loudly
    at startup so a typo (or 0, which would otherwise silently disable
    the breaker) surfaces instead of quietly degrading spend protection.
    Disabling the cap is solely JASPER_DAILY_SPEND_CAP_USD=0's job."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER", "0")
    with pytest.raises(RuntimeError, match="SAFETY_MULTIPLIER"):
        Config.from_env()


def test_active_voice_model_resolves_for_active_provider(monkeypatch):
    """Single source for the active provider's model — shared by the daemon
    (_active_model) and jasper-doctor (check_pricing)."""
    # Provider defaults to gemini via the module autouse fixture.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    assert (
        Config.from_env().active_voice_model
        == "gemini-3.1-flash-live-preview"
    )
    # Switch the active provider — resolution follows it.
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JASPER_OPENAI_MODEL", "gpt-realtime-2")
    assert Config.from_env().active_voice_model == "gpt-realtime-2"


def test_wake_threshold_default_matches_env_example(monkeypatch):
    """install.sh seeds /etc/jasper/jasper.env from .env.example as the FIRST
    EnvironmentFile, so a code default that diverges from .env.example means
    production silently runs the .env.example value while code+docs claim the
    code one. Guard the load-bearing wake threshold against that drift —
    codify, don't memorise."""
    for var in ["GEMINI_API_KEY", "JASPER_WAKE_THRESHOLD"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    env_value = float(_parse_env_example()["JASPER_WAKE_THRESHOLD"])
    assert Config.from_env().wake_threshold == env_value
