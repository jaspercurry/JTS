# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from types import MappingProxyType, ModuleType
from typing import Any

from . import home_assistant as _ha_env
from . import volume_persistence as _volume_persistence
from .accounts import legacy_cache_path, registry_path
from .camilla_config_contract import DEFAULT_CAMILLA_PORT
from .env_load import VOICE_PROVIDER_ENV_PATH, parse_bool_value
from .librespot_state import DEFAULT_PATH as DEFAULT_LIBRESPOT_STATE
from .location_state import (
    WEATHER_DEFAULT_LOCATION_ENV,
    WEATHER_DISPLAY_NAME_ENV,
    WEATHER_LAT_ENV,
    WEATHER_LON_ENV,
    WEATHER_UNITS_ENV,
    parse_transit_location,
)
from .mics.xvf3800 import CHIP_AEC_ENABLED_ENV
from .platform.status_socket import VOICE_CONTROL_SOCKET_PATH
from .assistant_loudness import (
    DEFAULT_PROFILE_PATH as DEFAULT_ASSISTANT_LOUDNESS_PROFILE_PATH,
)
from .identity.speaker_name import runtime_name as _speaker_runtime_name
from .google_creds import registry_path as google_registry_path
from .google_oauth import resolved_google_redirect_uri
from .identity.reader import resolve_hostname
from .spotify_oauth import resolved_spotify_redirect_uri
from .tts_routing import FANIN_TTS_SOCKET, VOICE_TTS_SOCKET_ENV
from .usage import (
    DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
    DEFAULT_DAILY_SPEND_CAP_USD,
    DEFAULT_USAGE_DB,
)
from .voice.catalog import (
    VALID_PROVIDER_IDS,
    default_extra_value,
    default_model_id,
    default_voice_id,
)
from .voice.input_policy import (
    normalize_openai_noise_reduction,
    validate_openai_noise_reduction,
)
from .wake_ports import DEFAULT_AEC_ON_PORT, DEFAULT_AEC_UDP_HOST
from .wake_events import (
    DEFAULT_MAX_AUDIO_BYTES as DEFAULT_WAKE_EVENTS_MAX_AUDIO_BYTES,
)
from jasper.paths import SOUNDS_DIR, WAKE_EVENTS_DIR


class VoiceConfigError(RuntimeError):
    """A config value the daemon cannot start on.

    The type is the routing: `daemon_main.main()` catches this, speaks the
    park cue and exits 78, which jasper-voice.service holds the unit down on.
    A bare RuntimeError tracebacks to exit 1 instead and climbs
    Restart=on-failure into StartLimitAction=reboot with nothing spoken
    (AGENTS.md non-negotiable 6). The restart remedy lives as a structured
    field at the park site (daemon_main.py), not here: str(exc) stays the
    bare message so the accessory-mic parser can mirror it verbatim.
    """


class VoiceProviderNotConfigured(VoiceConfigError):
    """Raised when first-time setup has not selected a voice provider."""


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise VoiceProviderNotConfigured(f"missing required env var: {name}")
    return val or ""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise VoiceConfigError(f"{name} must be a number") from e


def _env_optional_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as e:
        raise VoiceConfigError(f"{name} must be a number") from e


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise VoiceConfigError(f"{name} must be a number") from e


def env_bool(name: str, default: bool = False) -> bool:
    """Read a named env var through :func:`jasper.env_load.parse_bool_value`,
    falling back to ``default`` when it gives no answer."""
    value = parse_bool_value(os.environ.get(name))
    return default if value is None else value


def local_mic_present_from_env() -> bool | None:
    """``jasper-aec-reconcile``'s published local-microphone verdict.

    Public because the verdict has more than one reader and they must not
    disagree about the shape of the speaker: ``Config.local_mic_present``
    below is this call, and so is ``jasper-doctor``'s wake-leg check, which
    has to know a no-room-mic box arms zero wake legs *on purpose* before it
    can tell that apart from a bridge that died. One parse, two readers.

    ``None`` means the writer published no answer (unset, or the literal
    ``unknown`` it writes for a custom ``JASPER_MIC_DEVICE`` it deliberately
    does not manage) — never "absent". Only an explicit ``False`` may change
    behaviour anywhere.
    """
    return parse_bool_value(os.environ.get("JASPER_LOCAL_MIC_PRESENT"))


def _env_mapping(name: str, default: str) -> MappingProxyType[str, str]:
    raw = os.environ.get(name, default)
    result: dict[str, str] = {}
    if not raw or not raw.strip():
        return MappingProxyType(result)
    for part in raw.replace("\n", ",").split(","):
        item = part.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        if not sep or not key or not value:
            raise VoiceConfigError(
                f"{name} entries must be source_id=device, separated by commas"
            )
        if any(ch.isspace() for ch in key):
            raise VoiceConfigError(f"{name} source ids must not contain whitespace")
        if key in result:
            raise VoiceConfigError(f"{name} contains duplicate source id {key!r}")
        result[key] = value
    return MappingProxyType(result)


def _weather_defaults() -> tuple[str, float | None, float | None, str]:
    """``(location, lat, lon, display_name)`` for a weather question that names
    no place. With neither weather coordinate set, the saved transit location
    stands in (its display name too, when none is set); an unset display name
    falls back to ``location``."""
    location = _env(WEATHER_DEFAULT_LOCATION_ENV, "").strip()
    lat = _env_optional_float(WEATHER_LAT_ENV)
    lon = _env_optional_float(WEATHER_LON_ENV)
    display_name = _env(WEATHER_DISPLAY_NAME_ENV, "").strip()
    if lat is None and lon is None:
        transit = parse_transit_location(dict(os.environ))
        if transit is not None:
            lat, lon = transit.lat, transit.lon
            display_name = display_name or transit.display_name
    return location, lat, lon, display_name or location


def _parse_provider_env(
    gemini_key: str, openai_key: str, grok_key: str,
) -> dict[str, Any]:
    return dict(
        gemini_api_key=gemini_key,
        gemini_model=_env("JASPER_GEMINI_MODEL", default_model_id("gemini")),
        # Without a pinned voice, Gemini picks one per session.
        gemini_voice=_env("JASPER_GEMINI_VOICE", default_voice_id("gemini")),
        openai_api_key=openai_key,
        openai_live_model=_env("JASPER_OPENAI_LIVE_MODEL", default_model_id("openai_live")),
        openai_live_voice=_env("JASPER_OPENAI_LIVE_VOICE", default_voice_id("openai_live")),
        openai_live_backend_model=_env("JASPER_OPENAI_LIVE_BACKEND_MODEL", default_extra_value("openai_live", "backend_model")),
        openai_model=_env("JASPER_OPENAI_MODEL", default_model_id("openai")),
        openai_voice=_env("JASPER_OPENAI_VOICE", default_voice_id("openai")),
        # The adapter sends this only for `-2` models. `minimal` reduces
        # first-audio latency at the cost of multi-step answer coherence.
        openai_reasoning_effort=_env(
            "JASPER_OPENAI_REASONING_EFFORT",
            default_extra_value("openai", "reasoning_effort"),
        ),
        # Resolve "auto" from the active mic/AEC profile later to avoid
        # double-denoising streams already processed upstream.
        openai_noise_reduction=normalize_openai_noise_reduction(
            _env("JASPER_OPENAI_NOISE_REDUCTION", "auto"),
        ),
        grok_api_key=grok_key,
        grok_model=_env("JASPER_GROK_MODEL", default_model_id("grok")),
        grok_voice=_env("JASPER_GROK_VOICE", default_voice_id("grok")),
    )


def _parse_wake_input_env() -> dict[str, Any]:
    return dict(
        # See jasper/wake_models.py for the picker and install assets.
        # The fallback is a required hash-checked openWakeWord package asset.
        wake_model=_env("JASPER_WAKE_MODEL", "hey_jarvis"),
        wake_threshold=_env_float("JASPER_WAKE_THRESHOLD", 0.3),
        # PortAudio accepts an index or device-name substring; it rejects
        # ALSA "plughw:" syntax. Empty selects its default device.
        mic_device=_env("JASPER_MIC_DEVICE", "Array"),
        # Trust the reconciler verdict, not mic_device: a guessed False
        # could hide a broken room mic and silently disable wake response.
        local_mic_present=local_mic_present_from_env(),
        # Accessory reconcilers publish paired push-to-talk sources here.
        # Keys are /session/start source ids; values are make_mic_capture devices.
        manual_mic_sources=_env_mapping(
            "JASPER_MANUAL_MIC_SOURCES",
            "",
        ),
        mic_device_raw=_env("JASPER_MIC_DEVICE_RAW", ""),
        mic_device_dtln=_env("JASPER_MIC_DEVICE_DTLN", ""),
        # Optional XVF3800 150°/210° ASR beams use UDP 9887/9888.
        # The primary chip-AEC leg at :9876 does not imply these extra legs.
        mic_device_chip_aec_150=_env("JASPER_MIC_DEVICE_CHIP_AEC_150", ""),
        mic_device_chip_aec_210=_env("JASPER_MIC_DEVICE_CHIP_AEC_210", ""),
        aec_udp_port=_env_int("JASPER_AEC_UDP_PORT", DEFAULT_AEC_ON_PORT),
        aec_udp_host=_env("JASPER_AEC_UDP_HOST", DEFAULT_AEC_UDP_HOST),
        aec_chip_aec_enabled=env_bool(
            CHIP_AEC_ENABLED_ENV, False,
        ),
        # XVF3800 supports 16 kHz mono. UMIK-2 and other 44.1/48 kHz
        # devices need 48000/2; MicCapture downsamples to 16 kHz mono.
        mic_capture_rate=_env_int("JASPER_MIC_CAPTURE_RATE", 16000),
        mic_capture_channels=_env_int("JASPER_MIC_CAPTURE_CHANNELS", 1),
        # Per-leg WAVs cover 6 s; the audio ring evicts oldest first.
        wake_events_dir=_env(
            "JASPER_WAKE_EVENTS_DIR",
            WAKE_EVENTS_DIR,
        ),
        # See jasper/wake_events.py for retention-cap sizing.
        wake_events_max_audio_bytes=_env_int(
            "JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES",
            DEFAULT_WAKE_EVENTS_MAX_AUDIO_BYTES,
        ),
    )


def _parse_tts_env() -> dict[str, Any]:
    return dict(
        # Fan-in inserts TTS before Camilla crossover/protection; the
        # grouping reconciler routes bonded members to outputd instead.
        tts_outputd_socket=_env(
            VOICE_TTS_SOCKET_ENV, FANIN_TTS_SOCKET,
        ),
        # Python learns these profiles; the TTS IPC owner uses them for gain.
        assistant_loudness_profile_path=_env(
            "JASPER_ASSISTANT_LOUDNESS_PROFILE_PATH",
            DEFAULT_ASSISTANT_LOUDNESS_PROFILE_PATH,
        ),
        # Paid calibration requires explicit operator opt-in. Passive
        # measurement still learns from replies without making paid seed calls.
        assistant_loudness_auto_seed=env_bool(
            "JASPER_ASSISTANT_LOUDNESS_AUTO_SEED",
            False,
        ),
        # Apple dongle drain measured ~60–85 ms; leave a small margin.
        tts_drain_tail_sec=_env_float(
            "JASPER_TTS_DRAIN_TAIL_SEC", 0.085,
        ),
        # Silero default is 0.5; raise toward 0.7 for bleed false-triggers,
        # lower for missed interrupts. Used only when provider barge-in is
        # enabled; see jasper.voice.provider_state.read_barge_in_enabled.
        vad_barge_in_threshold=_env_float(
            "JASPER_VAD_BARGE_IN_THRESHOLD", 0.5,
        ),
    )


def _parse_camilla_env() -> dict[str, Any]:
    return dict(
        camilla_host=_env("JASPER_CAMILLA_HOST", "127.0.0.1"),
        camilla_port=_env_int("JASPER_CAMILLA_PORT", DEFAULT_CAMILLA_PORT),
    )


def _parse_session_env() -> dict[str, Any]:
    return dict(
        # Pre-response timeout measures progress, not socket activity:
        # audio, transcript deltas, tool calls, or turn_complete reset it.
        # Session bookkeeping/errors do not. Observed first audio took ~7.7 s.
        idle_timeout_sec=_env_int("JASPER_IDLE_TIMEOUT_SEC", 20),
        followup_timeout_sec=_env_float("JASPER_FOLLOWUP_TIMEOUT_SEC", 2.0),
        # Bounds stalled speech and turns that keep progressing without audio
        # (measured from end-of-input), so a wedged turn cannot keep music ducked.
        response_stall_timeout_sec=_env_int(
            "JASPER_RESPONSE_STALL_TIMEOUT_SEC",
            120,
        ),
        # Idle reset reopens the session, blocking wake for 1–6 s and losing
        # prompt-cache savings. OpenAI/Grok fully reconnect; Gemini drops its
        # resumption handle. Enable only for observed stale-context glitches.
        openai_context_reset_sec=_env_int(
            "JASPER_OPENAI_CONTEXT_RESET_SEC", 0,
        ),
        gemini_context_reset_sec=_env_int(
            "JASPER_GEMINI_CONTEXT_RESET_SEC", 0,
        ),
        grok_context_reset_sec=_env_int(
            "JASPER_GROK_CONTEXT_RESET_SEC", 0,
        ),
        # OpenAI has a 60-min cap; the buffer lets in-flight speech finish.
        # See openai_session._watchdog_delay_sec.
        openai_session_max_sec=_env_int(
            "JASPER_OPENAI_SESSION_MAX_SEC", 3600,
        ),
        openai_proactive_buffer_sec=_env_int(
            "JASPER_OPENAI_PROACTIVE_BUFFER_SEC", 300,
        ),
        # Grok publishes no hard cap; enable both knobs only if one is observed.
        grok_session_max_sec=_env_int(
            "JASPER_GROK_SESSION_MAX_SEC", 0,
        ),
        grok_proactive_buffer_sec=_env_int(
            "JASPER_GROK_PROACTIVE_BUFFER_SEC", 0,
        ),
    )


def _parse_usage_env() -> dict[str, Any]:
    return dict(
        daily_spend_cap_usd=_env_float(
            "JASPER_DAILY_SPEND_CAP_USD",
            DEFAULT_DAILY_SPEND_CAP_USD,
        ),
        daily_spend_cap_safety_multiplier=_env_float(
            "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER",
            DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
        ),
        usage_db=_env("JASPER_USAGE_DB", DEFAULT_USAGE_DB),
    )


def _parse_spotify_env(hostname: str) -> dict[str, Any]:
    return dict(
        spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
        # Manual mode uses http://127.0.0.1:8888/callback, Spotify's loopback exception.
        spotify_redirect_uri=resolved_spotify_redirect_uri(),
        # See jasper.accounts.maybe_migrate_legacy for the one-shot migration.
        spotify_cache_path=legacy_cache_path(),
        # The speaker wizard name also sets librespot's name; device matching
        # uses a case-insensitive substring of sp.devices()[].name.
        spotify_device_name=_speaker_runtime_name(),
        # See jasper.accounts for the household-account registry shape.
        spotify_accounts_path=registry_path(),
        # Override for a reverse proxy with a different hostname or path.
        spotify_setup_url=_env(
            "JASPER_SPOTIFY_SETUP_URL", f"http://{hostname}/spotify"
        ),
    )


def _parse_google_env(hostname: str) -> dict[str, Any]:
    return dict(
        # One OAuth client serves all household members; tokens stay per account.
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        google_redirect_uri=resolved_google_redirect_uri(),
        google_accounts_path=google_registry_path(),
        google_setup_url=_env(
            "JASPER_GOOGLE_SETUP_URL", f"http://{hostname}/assistant/google/",
        ),
    )


def _parse_local_services_env(hostname: str, timers: ModuleType) -> dict[str, Any]:
    return dict(
        # Cues use this hostname when directing users to a wake-blocking failure.
        management_url=_env(
            "JASPER_MANAGEMENT_URL", f"http://{hostname}",
        ),
        sounds_dir=_env(
            "JASPER_SOUNDS_DIR", SOUNDS_DIR,
        ),
        timer_db_path=_env(
            "JASPER_TIMER_DB", timers.DEFAULT_DB_PATH,
        ),
    )


def _parse_home_assistant_env() -> dict[str, Any]:
    return dict(
        # The HA wizard owns home_assistant.env; an empty URL or token disables
        # the tool. An empty agent id selects HA's configured default.
        ha_url=_env(_ha_env.ENV_URL, "").strip().rstrip("/"),
        ha_token=_env(_ha_env.ENV_TOKEN, "").strip(),
        ha_agent_id=_env(_ha_env.ENV_AGENT_ID, "").strip(),
        ha_verify_ssl=_ha_env.verify_ssl_from_state(os.environ),
    )


def _parse_volume_env() -> dict[str, Any]:
    return dict(
        volume_state_path=_volume_persistence.configured_path(),
        # Only old volume outside the safe band is regressed at boot;
        # recent restarts and in-band values retain continuity.
        volume_regress_after_sec=_env_float(
            "JASPER_VOLUME_REGRESS_AFTER_SEC",
            _volume_persistence.REGRESS_AFTER_SEC,
        ),
        volume_regress_safe_low_pct=_env_int(
            "JASPER_VOLUME_REGRESS_SAFE_LOW_PCT",
            _volume_persistence.REGRESS_SAFE_LOW_PCT,
        ),
        volume_regress_safe_high_pct=_env_int(
            "JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT",
            _volume_persistence.REGRESS_SAFE_HIGH_PCT,
        ),
        # Used when the persisted record is absent or corrupt.
        volume_first_boot_default_pct=_env_int(
            "JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT",
            _volume_persistence.FIRST_BOOT_DEFAULT_PCT,
        ),
    )


def _parse_control_env(
    mic_mute_persistence: ModuleType, peering_config: ModuleType,
) -> dict[str, Any]:
    return dict(
        # Restore at WakeLoop init so a restart cannot silently unmute.
        mic_mute_state_path=_env(
            "JASPER_MIC_MUTE_STATE_PATH",
            mic_mute_persistence.DEFAULT_PATH,
        ),
        # Systemd creates /run/jasper as RuntimeDirectory with mode 0750.
        voice_control_socket=_env(
            "JASPER_VOICE_CONTROL_SOCKET", VOICE_CONTROL_SOCKET_PATH,
        ),
        peering_enabled=env_bool("JASPER_PEERING", False),
        # jasper-control owns the server and its RuntimeDirectory.
        peering_uds_socket=_env(
            "JASPER_PEERING_UDS", peering_config.PEERING_UDS_PATH,
        ),
    )


def _validate(cfg: "Config") -> "Config":
    if not 0.0 <= cfg.wake_threshold <= 1.0:
        raise VoiceConfigError("JASPER_WAKE_THRESHOLD must be between 0.0 and 1.0")
    if not math.isfinite(cfg.followup_timeout_sec) or cfg.followup_timeout_sec < 0:
        raise VoiceConfigError("JASPER_FOLLOWUP_TIMEOUT_SEC must be finite and >= 0")
    if cfg.idle_timeout_sec <= 0:
        raise VoiceConfigError("JASPER_IDLE_TIMEOUT_SEC must be > 0")
    if cfg.response_stall_timeout_sec <= 0:
        raise VoiceConfigError("JASPER_RESPONSE_STALL_TIMEOUT_SEC must be > 0")
    try:
        validate_openai_noise_reduction(cfg.openai_noise_reduction)
    except RuntimeError as e:
        # jasper.voice.input_policy is imported by this module, so it cannot
        # name VoiceConfigError without a cycle; retyped so the park is cued.
        raise VoiceConfigError(str(e)) from e
    for name, value in [
        ("JASPER_OPENAI_CONTEXT_RESET_SEC", cfg.openai_context_reset_sec),
        ("JASPER_GEMINI_CONTEXT_RESET_SEC", cfg.gemini_context_reset_sec),
        ("JASPER_GROK_CONTEXT_RESET_SEC", cfg.grok_context_reset_sec),
        ("JASPER_OPENAI_SESSION_MAX_SEC", cfg.openai_session_max_sec),
        ("JASPER_OPENAI_PROACTIVE_BUFFER_SEC", cfg.openai_proactive_buffer_sec),
        ("JASPER_GROK_SESSION_MAX_SEC", cfg.grok_session_max_sec),
        ("JASPER_GROK_PROACTIVE_BUFFER_SEC", cfg.grok_proactive_buffer_sec),
    ]:
        if value < 0:
            raise VoiceConfigError(f"{name} must be >= 0 (0 = disabled)")
    if cfg.daily_spend_cap_usd < 0:
        raise VoiceConfigError("JASPER_DAILY_SPEND_CAP_USD must be >= 0")
    if cfg.daily_spend_cap_safety_multiplier < 1.0:
        raise VoiceConfigError(
            "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER must be >= 1.0 "
            "(1.0 = no padding; >1.0 = more conservative). A value below "
            "1.0 would weaken the cap; disable the cap with "
            "JASPER_DAILY_SPEND_CAP_USD=0 instead."
        )
    if (cfg.weather_default_lat is None) != (cfg.weather_default_lon is None):
        raise VoiceConfigError(
            "JASPER_WEATHER_LAT and JASPER_WEATHER_LON must be set together"
        )
    if cfg.weather_default_lat is not None and not -90 <= cfg.weather_default_lat <= 90:
        raise VoiceConfigError("JASPER_WEATHER_LAT must be between -90 and 90")
    if cfg.weather_default_lon is not None and not -180 <= cfg.weather_default_lon <= 180:
        raise VoiceConfigError("JASPER_WEATHER_LON must be between -180 and 180")
    if cfg.volume_regress_after_sec <= 0:
        raise VoiceConfigError("JASPER_VOLUME_REGRESS_AFTER_SEC must be > 0")
    for name, value in [
        ("JASPER_VOLUME_REGRESS_SAFE_LOW_PCT", cfg.volume_regress_safe_low_pct),
        ("JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT", cfg.volume_regress_safe_high_pct),
        ("JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT", cfg.volume_first_boot_default_pct),
    ]:
        if not 0 <= value <= 100:
            raise VoiceConfigError(f"{name} must be between 0 and 100 (got {value})")
    if cfg.volume_regress_safe_low_pct >= cfg.volume_regress_safe_high_pct:
        raise VoiceConfigError(
            "JASPER_VOLUME_REGRESS_SAFE_LOW_PCT must be < SAFE_HIGH_PCT"
        )
    return cfg


@dataclass(frozen=True)
class Config:
    # Voice provider: one of jasper.voice.catalog.VALID_PROVIDER_IDS. The
    # corresponding *_api_key + *_model + *_voice fields are read for
    # whichever provider is selected. Other providers' keys may be
    # blank and the daemon still starts — only the active provider's
    # credentials are required.
    voice_provider: str

    gemini_api_key: str = field(repr=False)
    gemini_model: str
    gemini_voice: str

    openai_api_key: str = field(repr=False)
    openai_model: str
    openai_live_model: str
    openai_live_voice: str
    openai_live_backend_model: str
    openai_voice: str
    openai_reasoning_effort: str
    openai_noise_reduction: str

    grok_api_key: str = field(repr=False)
    grok_model: str
    grok_voice: str

    wake_model: str
    wake_threshold: float
    mic_device: str
    # jasper-aec-reconcile's published half of "is there usable voice input":
    # True/False when it resolved local-microphone presence, None when it
    # deliberately did not (a custom JASPER_MIC_DEVICE) or never ran. Only an
    # explicit False lets the leg planner drop the primary wake leg — see
    # `configured_wake_legs`.
    local_mic_present: bool | None
    manual_mic_sources: MappingProxyType[str, str]
    mic_device_raw: str
    mic_device_dtln: str
    # Optional XVF3800 extra chip-AEC wake detector legs (the fixed 150°/210°
    # ASR beams the bridge may forward on UDP 9887/9888). Empty by default →
    # the leg is not built; the AEC reconciler sets these only from the
    # per-beam JASPER_WAKE_LEG_CHIP_AEC_150/_210 custom toggles.
    mic_device_chip_aec_150: str
    mic_device_chip_aec_210: str
    aec_chip_aec_enabled: bool
    aec_udp_port: int
    aec_udp_host: str
    mic_capture_rate: int
    mic_capture_channels: int
    wake_events_dir: str
    wake_events_max_audio_bytes: int
    tts_outputd_socket: str
    assistant_loudness_profile_path: str
    assistant_loudness_auto_seed: bool
    tts_drain_tail_sec: float
    vad_barge_in_threshold: float

    camilla_host: str
    camilla_port: int
    idle_timeout_sec: int
    followup_timeout_sec: float
    response_stall_timeout_sec: int
    # Per-provider idle context reset thresholds (seconds). 0 = disabled
    # (default). Without a reset, the persistent live session keeps
    # conversational context indefinitely; OpenAI Realtime auto-truncates
    # past 128K and caps sessions at 60 min, so unbounded growth is
    # impossible. Set a positive value (e.g. 21600 = 6 h) to force a
    # periodic fresh session as a safety hedge against stale-context
    # weirdness. Per-provider so e.g. Gemini's resumption-handle path
    # can be tuned separately from OpenAI's reconnect path.
    openai_context_reset_sec: int
    gemini_context_reset_sec: int
    grok_context_reset_sec: int

    # Proactive pre-cap reconnect for OpenAI Realtime / Grok. OpenAI
    # enforces a 60-min session cap with no resumption handle and no
    # pre-cap warning event; without proactive action, every cap hit
    # costs the user a ~3 s `cant_connect` cue on the next wake. The
    # watchdog tears down voluntarily at `(session_max_sec -
    # proactive_buffer_sec)` so the reconnect lands in an idle window.
    # Two values, not one, so OpenAI raising the cap only requires
    # bumping `session_max_sec` — the safety buffer ("how much margin
    # we want") stays correct. Set either to 0 to
    # disable. Gemini handles this server-side via GoAway + resumption
    # handle, so no equivalent knob is needed there.
    openai_session_max_sec: int
    openai_proactive_buffer_sec: int
    grok_session_max_sec: int
    grok_proactive_buffer_sec: int

    daily_spend_cap_usd: float
    # Multiplier applied to the rolling 24h spend before comparing to the
    # cap. Keeps the circuit breaker conservative without inflating the
    # dashboard's displayed (true-estimate) cost. See usage.SpendCap.
    daily_spend_cap_safety_multiplier: float
    usage_db: str

    # Path to the librespot state file written by the --onevent hook
    # (jasper-librespot-event). Read by mux, volume_observers, and
    # RendererClient. Default written by librespot.service via
    # systemd RuntimeDirectory.
    librespot_state_path: str

    # The speaker's mDNS hostname — what other devices on the LAN type
    # into their browser to reach the speaker. Default is `jts.local`
    # (canonical reference deployment). Override at install time if you
    # ran `hostnamectl set-hostname` to something else; the URLs below
    # default to `http://${hostname}` when not explicitly set.
    hostname: str

    spotify_client_id: str
    spotify_redirect_uri: str
    spotify_cache_path: str
    spotify_device_name: str
    spotify_accounts_path: str
    spotify_setup_url: str

    # Google integration: per-household-member Calendar + Gmail OAuth.
    # CLIENT_ID/SECRET come from a single Google Cloud Console OAuth
    # client (same shape as Spotify). Per-account refresh tokens live
    # under the registry path; the wizard at /assistant/google/ writes them.
    google_client_id: str
    google_client_secret: str = field(repr=False)
    google_redirect_uri: str
    google_accounts_path: str
    google_setup_url: str

    # Top-level URL for the speaker's management dashboard. Used by
    # the audio-cue subsystem to tell the user where to go when they
    # hit a wake-blocking failure (e.g., spend cap reached). The
    # spotify setup wizard at /spotify/ is the seed of what will
    # become a broader dashboard at /. The hostname (without scheme
    # or path) is what gets injected into cue templates.
    management_url: str

    # Where pre-rendered cue WAVs live. Path is in
    # ReadWritePaths=/var/lib/jasper of the systemd unit so the
    # daemon (and `jasper-cues regenerate`) can write here.
    sounds_dir: str

    weather_default_location: str
    weather_default_lat: float | None
    weather_default_lon: float | None
    weather_default_display_name: str
    weather_units: str

    # Transit (NYC subway / bus / Citi Bike, and future city packs) is NOT
    # a Config field: each provider under jasper.transit.providers parses its
    # OWN env keys in build_client(env), so adding a city/mode needs no edit
    # here. The doctor + voice-eval read the same env keys directly.

    # Home Assistant integration. The /ha wizard (PR 2) writes
    # /var/lib/jasper-intsecrets/home_assistant.env with these values; daemon picks
    # them up via systemd EnvironmentFile. URL is the base of the HA
    # install (e.g. "http://homeassistant.local:8123"); token is a
    # Long-Lived Access Token from HA's profile page; agent_id is an
    # optional override to route JTS to a specific conversation agent
    # (empty = use HA's default).
    ha_url: str
    ha_token: str = field(repr=False)
    ha_agent_id: str
    # When False, HAClient skips TLS verification — needed for HA
    # installs running HTTPS with a self-signed cert (a common
    # configuration HA users have). Wizard exposes a checkbox under
    # connection details that only renders when the URL is https://.
    ha_verify_ssl: bool

    volume_state_path: str
    volume_regress_after_sec: float
    volume_regress_safe_low_pct: int
    volume_regress_safe_high_pct: int
    volume_first_boot_default_pct: int

    mic_mute_state_path: str

    voice_control_socket: str

    # Multi-device peering (multi-Pi wake arbitration). Read once at
    # startup from the JASPER_PEERING env var which systemd merges in
    # from /var/lib/jasper/peering.env (written by /sound/pair/). When False
    # (the default), every peer-arbitrate code
    # path is a no-op — single-Pi installs pay zero cost. When True,
    # WakeLoop calls jasper-control's peering UDS on every wake event
    # to ask "should I take this turn?" — see jasper.peering for the
    # full design. Live-toggling requires a jasper-voice restart
    # (which the wizard performs).
    peering_enabled: bool
    peering_uds_socket: str

    # Timer persistence — SQLite DB tracking active kitchen timers
    # so a daemon restart doesn't lose the user's pending fire times.
    # Sits in the same /var/lib/jasper StateDirectory as everything
    # else under jasper-voice's systemd unit.
    timer_db_path: str

    # Gemini one-shot TTS model used by the cue subsystem when the
    # active voice provider is `gemini` (or a fallback path picks
    # Gemini). Defaults to 3.1 Flash TTS Preview; `gemini-2.5-flash-preview-tts`
    # returns `FinishReason.OTHER` with empty content for ~60 % of
    # calls in production, so it must not be the default — set
    # JASPER_GEMINI_TTS_MODEL=gemini-2.5-flash-preview-tts only to
    # reproduce that failure mode for testing.
    gemini_tts_model: str

    @classmethod
    def from_env(cls) -> "Config":
        from . import mic_mute_persistence, timers  # lazy: keep config imports light
        from .peering import config as peering_config  # lazy: keep config imports light

        # No default — the user MUST pick a provider via the wizard at
        # http://${JASPER_HOSTNAME}/assistant/voice/. Empty value here is a clear
        # signal that first-time setup hasn't happened yet, not a
        # silent "use gemini" fallback. The wizard writes
        # /var/lib/jasper/voice_provider.env which the systemd unit
        # sources after /etc/jasper/jasper.env; the wizard file is the
        # canonical source of truth for this variable.
        provider = _env("JASPER_VOICE_PROVIDER", "")
        if not provider:
            raise VoiceProviderNotConfigured(
                "JASPER_VOICE_PROVIDER is not set — visit "
                "http://jts.local/assistant/voice/ (or your speaker's "
                "hostname) and pick a provider. The wizard writes "
                f"{VOICE_PROVIDER_ENV_PATH} for you.",
            )
        if provider not in VALID_PROVIDER_IDS:
            raise VoiceProviderNotConfigured(
                f"unsupported JASPER_VOICE_PROVIDER={provider!r}; expected "
                f"one of: {', '.join(sorted(VALID_PROVIDER_IDS))}"
            )
        # Only the active provider's API key is required. Each provider
        # block's other env vars have sensible defaults, so the user
        # only needs to set the key + provider to switch backends.
        gemini_key = _env("GEMINI_API_KEY", required=(provider == "gemini"))
        openai_key = _env("OPENAI_API_KEY", required=(provider in {"openai", "openai_live"}))
        grok_key = _env("XAI_API_KEY", required=(provider == "grok"))
        # Speaker hostname is the single source of truth for "where do
        # other devices reach this speaker?" — read first so URL
        # defaults below can derive from it.
        hostname = resolve_hostname()
        weather_location, weather_lat, weather_lon, weather_name = _weather_defaults()
        return _validate(cls(
            voice_provider=provider,
            hostname=hostname,
            **_parse_provider_env(gemini_key, openai_key, grok_key),
            **_parse_wake_input_env(),
            **_parse_tts_env(),
            **_parse_camilla_env(),
            **_parse_session_env(),
            **_parse_usage_env(),
            librespot_state_path=_env(
                "JASPER_LIBRESPOT_STATE", DEFAULT_LIBRESPOT_STATE,
            ),
            **_parse_spotify_env(hostname),
            **_parse_google_env(hostname),
            **_parse_local_services_env(hostname, timers),
            gemini_tts_model=_env(
                "JASPER_GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview",
            ),
            weather_default_location=weather_location,
            weather_default_lat=weather_lat,
            weather_default_lon=weather_lon,
            weather_default_display_name=weather_name,
            weather_units=_env(WEATHER_UNITS_ENV, "celsius"),
            **_parse_home_assistant_env(),
            **_parse_volume_env(),
            **_parse_control_env(mic_mute_persistence, peering_config),
        ))

    @property
    def openai_live_api_key(self) -> str:
        return self.openai_api_key

    def voice_model_for(self, provider: str) -> str:
        """Model name this config resolves for ``provider``, or "" for an
        unknown provider id. The one provider→model mapping: a new
        provider's model resolution lives here and nowhere else.

        Reads THIS process's own environment, where a calling-shell
        export of e.g. ``JASPER_GEMINI_MODEL`` outranks both
        ``jasper.env`` and the wizard file (``env_load.load_env_files``
        uses ``setdefault``). Correct for ``jasper-voice`` itself, whose
        environment is always fresh (restarted on every relevant
        switch) — but a reader describing a DIFFERENT, possibly-polluted
        process (jasper-doctor's pricing check) should call
        ``jasper.voice.provider_state.read_active_model_from_env_files``
        instead, which merges the same files without touching
        ``os.environ`` (issue #3133)."""
        return {
            "gemini": self.gemini_model,
            "openai": self.openai_model,
            "openai_live": self.openai_live_model,
            "grok": self.grok_model,
        }.get(provider, "")

    @property
    def active_voice_model(self) -> str:
        """Model name configured for the active provider, or "" for an
        unset/unknown provider. Used by the daemon whose environment
        defines "active" — jasper-voice, restarted on every switch."""
        return self.voice_model_for(self.voice_provider)

    @property
    def weather_prompt_location(self) -> str:
        """Human-readable default location for the system addendum."""
        if self.weather_default_display_name:
            return self.weather_default_display_name
        if self.weather_default_location:
            return self.weather_default_location
        if self.weather_default_lat is not None and self.weather_default_lon is not None:
            return f"{self.weather_default_lat:.3f}, {self.weather_default_lon:.3f}"
        return ""

    @property
    def spotify_enabled(self) -> bool:
        # PKCE: only the client_id is needed; no secret. A client_id
        # alone is enough to authorize accounts and refresh their
        # tokens against Spotify.
        return bool(self.spotify_client_id)

    @property
    def google_enabled(self) -> bool:
        """True iff Google CLIENT_ID + CLIENT_SECRET are set. The voice
        tools also require at least one OAuthed account before they
        register — see `_build_registry`."""
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def ha_enabled(self) -> bool:
        """True iff Home Assistant URL + token are both set. The
        home_assistant tool is gated on this in `_build_registry`; when
        false, the model never sees the tool and handles smart-home
        requests conversationally ("smart-home control isn't set up
        yet — visit jts.local/assistant/ha/")."""
        return bool(self.ha_url and self.ha_token)
