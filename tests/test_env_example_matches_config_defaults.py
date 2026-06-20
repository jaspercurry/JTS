"""Guard: .env.example literals must equal the jasper/config.py defaults.

install.sh seeds /etc/jasper/jasper.env from .env.example on a fresh
install and never re-syncs it; jasper.env is the FIRST EnvironmentFile
systemd loads for the daemons, so any literal in .env.example permanently
overrides the matching `Config` default on every existing Pi. A code
default that diverges from .env.example therefore ships a lie: production
runs the (frozen) .env.example value while code + docs claim the code one.
This has twice silently hidden a code-default fix — a HEADROOM volume value
and the wake threshold (see MEMORY: "Pi jasper.env is a frozen first-install
seed"). `tests/test_config.py` guards the wake threshold one key at a time;
this file generalizes that guard across the load-bearing tunables.

Scope: the JASPER_* (and SPOTIFY_*) tunables that have BOTH a line in
.env.example AND a real default in `Config.from_env`. EXCLUDED, because they
legitimately have no safe code default (an empty / template value the
operator or a wizard must fill in): API keys, the hostname, weather
coordinates / location, transit station ids, and the provider selection
(which isn't in .env.example at all). Daemon-owned knobs read elsewhere
(JASPER_AEC_*, JASPER_OUTPUTD_*, JASPER_CONTROL_*, JASPER_DIAL_*, ...) are
out of scope here — they have no `Config` field to compare against.

Defaults are read by constructing `Config.from_env()` under a cleaned
environment (no JASPER_* / *_API_KEY set) rather than regex-parsing
jasper/config.py, so the comparison tracks the value the daemon actually
resolves at startup.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.config import Config

_ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Truthy set used by Config._env_bool. Kept
# local so a coercion mismatch here surfaces as a real divergence rather
# than importing config internals that could mask one.
_TRUTHY = {"1", "true", "yes", "on", "enabled"}


def _parse_env_example() -> dict[str, str]:
    """Parse KEY=VALUE lines out of .env.example, ignoring comments/blanks.

    Mirrors the parser in tests/test_config.py so both guards read the
    template the same way.
    """
    values: dict[str, str] = {}
    for raw in _ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# (env var, Config attribute, coercion) for each in-scope tunable.
#
# Coercion turns the .env.example string literal into the Python value the
# matching `Config` field holds, applying exactly the rule `from_env` uses
# for that field's type, so the comparison is value equality not string
# equality (e.g. "-25" == -25.0, "1.00" == 1.0, "0" == False):
#   str:   identity — the literal is compared as-is.
#   float: float(literal)  — every float field reads via _env_float.
#   int:   int(literal)    — every int field reads via _env_int.
#   bool:  literal.strip().lower() in _TRUTHY — Config._env_bool's parse.
_CASES: tuple[tuple[str, str, str], ...] = (
    # Active-provider model / voice / tuning selectors (safe shipped
    # defaults; the wizard overrides per provider).
    ("JASPER_GEMINI_MODEL", "gemini_model", "str"),
    ("JASPER_GEMINI_VOICE", "gemini_voice", "str"),
    ("JASPER_OPENAI_MODEL", "openai_model", "str"),
    ("JASPER_OPENAI_VOICE", "openai_voice", "str"),
    ("JASPER_OPENAI_REASONING_EFFORT", "openai_reasoning_effort", "str"),
    ("JASPER_OPENAI_NOISE_REDUCTION", "openai_noise_reduction", "str"),
    ("JASPER_GROK_MODEL", "grok_model", "str"),
    ("JASPER_GROK_VOICE", "grok_voice", "str"),
    # Wake detection.
    ("JASPER_WAKE_MODEL", "wake_model", "str"),
    ("JASPER_WAKE_THRESHOLD", "wake_threshold", "float"),
    ("JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES", "wake_events_max_audio_bytes", "int"),
    # Mic capture.
    ("JASPER_MIC_DEVICE", "mic_device", "str"),
    ("JASPER_MIC_CAPTURE_RATE", "mic_capture_rate", "int"),
    ("JASPER_MIC_CAPTURE_CHANNELS", "mic_capture_channels", "int"),
    # TTS / output path.
    ("JASPER_TTS_DEVICE", "tts_device", "str"),
    ("JASPER_TTS_TRANSPORT", "tts_transport", "str"),
    ("JASPER_TTS_OUTPUTD_SOCKET", "tts_outputd_socket", "str"),
    ("JASPER_TTS_OUTPUT_RATE", "tts_output_rate", "int"),
    ("JASPER_ASSISTANT_LOUDNESS_PROFILE_PATH", "assistant_loudness_profile_path", "str"),
    # Barge-in + server VAD.
    ("JASPER_VAD_BARGE_IN_THRESHOLD", "vad_barge_in_threshold", "float"),
    ("JASPER_SERVER_VAD_ENABLED", "server_vad_enabled", "bool"),
    ("JASPER_SERVER_VAD_THRESHOLD", "server_vad_threshold", "float"),
    ("JASPER_SERVER_VAD_SILENCE_MS", "server_vad_silence_ms", "int"),
    ("JASPER_SERVER_VAD_PREFIX_MS", "server_vad_prefix_ms", "int"),
    # Ducking / CamillaDSP reach.
    ("JASPER_CAMILLA_HOST", "camilla_host", "str"),
    ("JASPER_CAMILLA_PORT", "camilla_port", "int"),
    ("JASPER_DUCK_DB", "duck_db", "float"),
    # Timeouts / idle context reset.
    ("JASPER_IDLE_TIMEOUT_SEC", "idle_timeout_sec", "int"),
    ("JASPER_OPENAI_CONTEXT_RESET_SEC", "openai_context_reset_sec", "int"),
    ("JASPER_GEMINI_CONTEXT_RESET_SEC", "gemini_context_reset_sec", "int"),
    ("JASPER_GROK_CONTEXT_RESET_SEC", "grok_context_reset_sec", "int"),
    # Spend cap.
    ("JASPER_DAILY_SPEND_CAP_USD", "daily_spend_cap_usd", "float"),
    ("JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER", "daily_spend_cap_safety_multiplier", "float"),
    ("JASPER_USAGE_DB", "usage_db", "str"),
    # Conversation history.
    ("JASPER_CONVERSATION_DB", "conversation_db_path", "str"),
    # Async research scheduler.
    ("JASPER_RESEARCH_DB", "research_db_path", "str"),
    ("JASPER_RESEARCH_MAX_RUNTIME_SEC", "research_max_runtime_sec", "float"),
    ("JASPER_RESEARCH_CONCURRENCY", "research_concurrency", "int"),
    ("JASPER_RESEARCH_MAX_RESULT_CHARS", "research_max_result_chars", "int"),
    # Misc paths / feature defaults with a real shipped value.
    ("JASPER_WEATHER_UNITS", "weather_units", "str"),
    ("JASPER_VOICE_CONTROL_SOCKET", "voice_control_socket", "str"),
    ("SPOTIFY_CACHE_PATH", "spotify_cache_path", "str"),
)

# Env vars that, if present in the real test environment, would perturb a
# default we compare. Cleared before constructing Config so the resolved
# value is the code default, not a leaked override. The two construction
# requirements (provider + its key) are set after, and are themselves
# excluded from the comparison (secret / provider-selection).
_CLEARED = tuple(env for env, _attr, _kind in _CASES) + (
    "JASPER_VOICE_PROVIDER",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "JASPER_LIVE_CONTEXT_RESET_SEC",  # legacy global fallback for *_CONTEXT_RESET_SEC
    "JASPER_TRANSIT_LAT",
    "JASPER_TRANSIT_LON",
)


@pytest.fixture()
def default_config(monkeypatch) -> Config:
    """Config built under a cleaned environment so each field is its code
    default. gemini + a throwaway key are the minimum to construct (both
    excluded from the comparison)."""
    for var in _CLEARED:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return Config.from_env()


def test_all_in_scope_keys_present_in_env_example() -> None:
    """Every key we assert on must actually exist in .env.example — a
    typo or a removed line would otherwise silently skip its guard."""
    example = _parse_env_example()
    missing = [env for env, _attr, _kind in _CASES if env not in example]
    assert not missing, f"in-scope keys absent from .env.example: {missing}"


@pytest.mark.parametrize(
    ("env_name", "attr", "kind"),
    _CASES,
    ids=[env for env, _attr, _kind in _CASES],
)
def test_env_example_literal_matches_config_default(
    env_name: str, attr: str, kind: str, default_config: Config,
) -> None:
    literal = _parse_env_example()[env_name]
    actual = getattr(default_config, attr)

    if kind == "str":
        expected: object = literal
    elif kind == "float":
        expected = float(literal)
    elif kind == "int":
        expected = int(literal)
    elif kind == "bool":
        expected = literal.strip().lower() in _TRUTHY
    else:  # pragma: no cover - guards the table against a bad kind string
        raise AssertionError(f"unknown coercion kind {kind!r} for {env_name}")

    assert actual == expected, (
        f"{env_name} in .env.example is {literal!r} but Config.{attr} "
        f"defaults to {actual!r}. install.sh freezes .env.example into "
        f"/etc/jasper/jasper.env (first EnvironmentFile), so existing Pis "
        f"would run {expected!r} while code claims {actual!r}. Reconcile "
        f"the two intentionally — do not just edit this test."
    )
