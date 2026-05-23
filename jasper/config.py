from __future__ import annotations

import os
from dataclasses import dataclass

from . import home_assistant as _ha_env
from .bus import parse_bus_stops
from .citibike import parse_saved_stations as _parse_citibike_stations


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {name}")
    return val or ""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _validate(cfg: "Config") -> "Config":
    if not 0.0 <= cfg.wake_threshold <= 1.0:
        raise RuntimeError("JASPER_WAKE_THRESHOLD must be between 0.0 and 1.0")
    if cfg.idle_timeout_sec <= 0:
        raise RuntimeError("JASPER_IDLE_TIMEOUT_SEC must be > 0")
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
            raise RuntimeError(f"{name} must be >= 0 (0 = disabled)")
    if cfg.daily_spend_cap_usd < 0:
        raise RuntimeError("JASPER_DAILY_SPEND_CAP_USD must be >= 0")
    # Hearing-safety: TTS gain is now an OFFSET applied on top of
    # CamillaDSP's main_volume (negative attenuates from master,
    # zero matches it). A positive value would push TTS above master
    # and risk loud/clipping output. Refuse at startup rather than
    # discover the bug at speaker-blasting time.
    if cfg.tts_gain_db > 0.0:
        raise RuntimeError(
            f"JASPER_TTS_GAIN_DB must be <= 0 (got {cfg.tts_gain_db}); "
            "it is now an offset relative to main_volume — positive "
            "values would push TTS above the user's master and risk "
            "blasting the speaker"
        )
    # Silence threshold must sit somewhere in "no music" territory.
    # 0 dBFS or higher is meaningless (nothing is louder than full-scale).
    if cfg.tts_silence_threshold_dbfs >= 0.0:
        raise RuntimeError(
            "JASPER_TTS_SILENCE_THRESHOLD_DBFS must be < 0 dBFS"
        )
    if cfg.tts_music_window_sec <= 0:
        raise RuntimeError("JASPER_TTS_MUSIC_WINDOW_SEC must be > 0")
    if cfg.volume_regress_after_sec <= 0:
        raise RuntimeError("JASPER_VOLUME_REGRESS_AFTER_SEC must be > 0")
    for name, value in [
        ("JASPER_VOLUME_REGRESS_SAFE_LOW_PCT", cfg.volume_regress_safe_low_pct),
        ("JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT", cfg.volume_regress_safe_high_pct),
        ("JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT", cfg.volume_first_boot_default_pct),
    ]:
        if not 0 <= value <= 100:
            raise RuntimeError(f"{name} must be between 0 and 100 (got {value})")
    if cfg.volume_regress_safe_low_pct >= cfg.volume_regress_safe_high_pct:
        raise RuntimeError(
            "JASPER_VOLUME_REGRESS_SAFE_LOW_PCT must be < SAFE_HIGH_PCT"
        )
    return cfg


@dataclass(frozen=True)
class Config:
    # Voice provider: "gemini" (default) | "openai" | "grok". The
    # corresponding *_api_key + *_model + *_voice fields are read for
    # whichever provider is selected. Other providers' keys may be
    # blank and the daemon still starts — only the active provider's
    # credentials are required.
    voice_provider: str

    gemini_api_key: str
    gemini_model: str
    gemini_voice: str

    openai_api_key: str
    openai_model: str
    openai_voice: str
    openai_reasoning_effort: str

    grok_api_key: str
    grok_model: str
    grok_voice: str

    wake_model: str
    wake_threshold: float
    mic_device: str
    mic_device_raw: str
    mic_device_dtln: str
    mic_capture_rate: int
    mic_capture_channels: int
    wake_events_dir: str
    wake_events_max_audio_bytes: int
    tts_device: str
    tts_output_rate: int
    tts_gain_db: float
    tts_music_headroom_db: float
    tts_silence_threshold_dbfs: float
    tts_music_window_sec: float
    vad_barge_in_threshold: float

    camilla_host: str
    camilla_port: int
    duck_db: float
    idle_timeout_sec: int
    # Per-provider idle context reset thresholds (seconds). 0 = disabled
    # (default). Without a reset, the persistent live session keeps
    # conversational context indefinitely; OpenAI Realtime auto-truncates
    # past 128K and caps sessions at 60 min, so unbounded growth is
    # impossible. Set a positive value (e.g. 21600 = 6 h) to force a
    # periodic fresh session as a safety hedge against stale-context
    # weirdness. Per-provider so e.g. Gemini's resumption-handle path
    # can be tuned separately from OpenAI's reconnect path. Falls back
    # to the legacy `JASPER_LIVE_CONTEXT_RESET_SEC` if set, for
    # backwards-compat with existing /etc/jasper/jasper.env files.
    openai_context_reset_sec: int
    gemini_context_reset_sec: int
    grok_context_reset_sec: int

    # Proactive pre-cap reconnect for OpenAI Realtime / Grok. OpenAI
    # enforces a 60-min session cap with no resumption handle and no
    # pre-cap warning event; without proactive action, every cap hit
    # costs the user a ~3 s `cant_connect` cue on the next wake. The
    # watchdog tears down voluntarily at `(session_max_sec -
    # proactive_buffer_sec)` so the reconnect lands in an idle window.
    # Two values, not one, so OpenAI raising the cap (it went 30→60 in
    # 2025) only requires bumping `session_max_sec` — the safety buffer
    # ("how much margin we want") stays correct. Set either to 0 to
    # disable. Gemini handles this server-side via GoAway + resumption
    # handle, so no equivalent knob is needed there.
    openai_session_max_sec: int
    openai_proactive_buffer_sec: int
    grok_session_max_sec: int
    grok_proactive_buffer_sec: int

    daily_spend_cap_usd: float
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
    spotify_web_bind_host: str
    spotify_web_bind_port: int

    # Google integration: per-household-member Calendar + Gmail OAuth.
    # CLIENT_ID/SECRET come from a single Google Cloud Console OAuth
    # client (same shape as Spotify). Per-account refresh tokens live
    # under the registry path; the wizard at /google/ writes them.
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    google_accounts_path: str
    google_setup_url: str
    google_web_bind_host: str
    google_web_bind_port: int

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
    weather_units: str

    subway_station_id: str
    # Configured default direction for subway queries. "uptown" /
    # "downtown" set a specific default; "" or "both" → answer in both
    # directions when the voice query doesn't specify one.
    subway_default_direction: str

    # MTA BusTime. `bus_stops` is a tuple of (stop_id, label) pairs
    # parsed from the wizard's JASPER_BUS_STOPS env var.
    mta_bustime_key: str
    bus_stops: tuple[tuple[str, str], ...]

    # Citi Bike (NYC + Jersey City + Hoboken). Tuple of (station_id, label)
    # pairs parsed from the wizard's JASPER_CITIBIKE_STATIONS env var.
    # `citibike_ebike_only` is a household-wide preference flag: when
    # true, voice answers suppress classic-bike counts and only mention
    # e-bike availability. Per-station overrides were considered (a
    # household might only need e-bikes at the far station but accept
    # classic at the near one) but global was explicitly requested for
    # simplicity. See jasper.citibike for the GBFS data layer.
    citibike_stations: tuple[tuple[str, str], ...]
    citibike_ebike_only: bool

    # Home Assistant integration. The /ha wizard (PR 2) writes
    # /var/lib/jasper/home_assistant.env with these values; daemon picks
    # them up via systemd EnvironmentFile. URL is the base of the HA
    # install (e.g. "http://homeassistant.local:8123"); token is a
    # Long-Lived Access Token from HA's profile page; agent_id is an
    # optional override to route JTS to a specific conversation agent
    # (empty = use HA's default). See docs/HANDOFF-homeassistant.md.
    ha_url: str
    ha_token: str
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
    # from /var/lib/jasper/peering.env (written by the /peers/ web
    # wizard). When False (the default), every peer-arbitrate code
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
    # Gemini). Defaults to 3.1 Flash TTS Preview (released
    # 2026-04-15); the older `gemini-2.5-flash-preview-tts`
    # returned `FinishReason.OTHER` with empty content for ~60 % of
    # calls in production, so it's no longer the default — set
    # JASPER_GEMINI_TTS_MODEL=gemini-2.5-flash-preview-tts to keep
    # the legacy path on for testing.
    gemini_tts_model: str

    @classmethod
    def from_env(cls) -> "Config":
        # No default — the user MUST pick a provider via the wizard at
        # http://${JASPER_HOSTNAME}/voice. Empty value here is a clear
        # signal that first-time setup hasn't happened yet, not a
        # silent "use gemini" fallback. The wizard writes
        # /var/lib/jasper/voice_provider.env which the systemd unit
        # sources after /etc/jasper/jasper.env; the wizard file is the
        # canonical source of truth for this variable.
        provider = _env("JASPER_VOICE_PROVIDER", "")
        if not provider:
            raise RuntimeError(
                "JASPER_VOICE_PROVIDER is not set — visit "
                "http://jts.local/voice (or your speaker's hostname) "
                "and pick a provider. The wizard will write "
                "/var/lib/jasper/voice_provider.env and restart "
                "jasper-voice.",
            )
        if provider not in {"gemini", "openai", "grok"}:
            raise RuntimeError(
                f"unsupported JASPER_VOICE_PROVIDER={provider!r}; expected "
                "one of: gemini, openai, grok"
            )
        # Only the active provider's API key is required. Each provider
        # block's other env vars have sensible defaults, so the user
        # only needs to set the key + provider to switch backends.
        gemini_key = _env("GEMINI_API_KEY", required=(provider == "gemini"))
        openai_key = _env("OPENAI_API_KEY", required=(provider == "openai"))
        grok_key = _env("XAI_API_KEY", required=(provider == "grok"))
        # Speaker hostname is the single source of truth for "where do
        # other devices reach this speaker?" — read first so URL
        # defaults below can derive from it.
        hostname = _env("JASPER_HOSTNAME", "jts.local")
        return _validate(cls(
            voice_provider=provider,
            hostname=hostname,
            gemini_api_key=gemini_key,
            gemini_model=_env("JASPER_GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
            # Pin the TTS voice so it's consistent across sessions.
            # Available prebuilt voices on Gemini 3.1 Live Preview
            # include Aoede, Charon, Fenrir, Kore, Puck, Leda, Orus,
            # Zephyr. Without this, the server picks one per session.
            gemini_voice=_env("JASPER_GEMINI_VOICE", "Aoede"),
            openai_api_key=openai_key,
            # Default model is the post-2026-05-07 reasoning-capable
            # GA: gpt-realtime-2 ($32 / $64 / $0.40 per 1M audio tokens
            # in / out / cached). For the cheaper non-reasoning sibling
            # set JASPER_OPENAI_MODEL=gpt-realtime-mini ($10 / $20 /
            # $0.30) — same wire format, no `reasoning.effort` field.
            openai_model=_env("JASPER_OPENAI_MODEL", "gpt-realtime-2"),
            # OpenAI Realtime voices include marin, cedar, alloy, ash,
            # ballad, coral, echo, sage, shimmer, verse. `marin` is the
            # default in the post-GA SDK quickstarts.
            openai_voice=_env("JASPER_OPENAI_VOICE", "marin"),
            # Reasoning effort for gpt-realtime-2: minimal | low |
            # medium | high | xhigh. Default `low` matches the SDK
            # default and is the right choice for short voice queries.
            # Ignored on non-`-2` models (the openai_session adapter
            # only includes the field when the model name carries
            # "-2"). `minimal` cuts ~1 second of TTFA at the cost of
            # less coherent multi-step answers.
            openai_reasoning_effort=_env("JASPER_OPENAI_REASONING_EFFORT", "low"),
            grok_api_key=grok_key,
            # xAI Grok Voice Agent. The `grok-voice-think-fast-1.0`
            # model claims sub-second latency and is OpenAI-Realtime-
            # protocol-compatible per xAI's docs (we run it through the
            # same adapter as OpenAI with a base-URL override).
            grok_model=_env("JASPER_GROK_MODEL", "grok-voice-think-fast-1.0"),
            # Grok voice list is disjoint from OpenAI's: eve, ara, rex,
            # sal, leo. Default is `eve` per xAI docs.
            grok_voice=_env("JASPER_GROK_VOICE", "eve"),
            # `JASPER_WAKE_MODEL` is either a bundled openWakeWord name
            # (e.g. "hey_jarvis", "alexa") or an absolute path to a
            # .onnx file under /var/lib/jasper/wake/. The /wake/ wizard
            # writes /var/lib/jasper/wake_model.env to set it; the
            # curated picker rows + install-time download list live in
            # jasper/wake_models.py. The compiled-in fallback below is
            # "hey_jarvis" because it's the openWakeWord-bundled model
            # that is always present (downloaded at install time via
            # `openwakeword.utils.download_models()`), so dev/test runs
            # without a seeded env file still load something.
            wake_model=_env("JASPER_WAKE_MODEL", "hey_jarvis"),
            wake_threshold=_env_float("JASPER_WAKE_THRESHOLD", 0.5),
            # JASPER_MIC_DEVICE is a sounddevice/PortAudio identifier, not
            # an ALSA pcm string — PortAudio rejects "plughw:" syntax.
            # Accepts an integer index (`sd.query_devices()`), or a
            # substring of the PortAudio device name (e.g. "Array" matches
            # the XVF3800's "Array: USB Audio (hw:N,0)"; "UMIK-2" matches
            # the MiniDSP UMIK-2). Empty/absent → PortAudio default.
            mic_device=_env("JASPER_MIC_DEVICE", "Array"),
            # JASPER_MIC_DEVICE_RAW: optional second mic source for
            # dual-stream wake detection. When set (typically to
            # `udp:9877` paired with the bridge's chip-direct stream
            # introduced in the wake-telemetry PR 1), the WakeLoop
            # opens a second mic + a second WakeWordDetector and
            # OR-gates fires across both legs — recovering the union
            # of post-AEC and chip-direct detections.
            #
            # Empty / absent → single-stream behaviour (the existing
            # production default while PR 2 rolls out). Accepts the
            # same forms as JASPER_MIC_DEVICE (`udp:PORT`,
            # `udp://HOST:PORT`, or a PortAudio device string for
            # hypothetical hardware-second-mic configurations).
            #
            # See docs/HANDOFF-wake-telemetry.md for the architecture
            # and the empirical case for OR-gating.
            mic_device_raw=_env("JASPER_MIC_DEVICE_RAW", ""),
            # JASPER_MIC_DEVICE_DTLN: optional third mic source for
            # triple-stream wake detection (raw + AEC3-BEST_A + DTLN).
            # When set (typically `udp:9878` paired with the bridge's
            # DTLN-aec parallel output added in Phase 1.2 of the
            # triple-stream rollout), the WakeLoop spawns a third
            # WakeWordDetector and OR-gates fires across all three legs.
            # See docs/HANDOFF-mic-quality-v2.md "Triple-stream
            # architecture plan" for context.
            mic_device_dtln=_env("JASPER_MIC_DEVICE_DTLN", ""),
            # The XVF3800 supports 16 kHz mono natively, so 16000/1 is the
            # default. Mics that only do 44.1 / 48 kHz (UMIK-2 et al.) need
            # JASPER_MIC_CAPTURE_RATE=48000 and JASPER_MIC_CAPTURE_CHANNELS=2;
            # MicCapture polyphase-downsamples to 16 kHz mono internally.
            mic_capture_rate=_env_int("JASPER_MIC_CAPTURE_RATE", 16000),
            mic_capture_channels=_env_int("JASPER_MIC_CAPTURE_CHANNELS", 1),
            # Wake-event telemetry (HANDOFF-wake-telemetry.md PR 3).
            # Directory holds wake-events.sqlite3 + per-event WAV
            # files (one per leg, 6 s window). DB rows kept forever;
            # audio ring rolls oldest-first when the byte cap is hit.
            # install.sh creates this dir at mode 0755 owned by pi:pi.
            wake_events_dir=_env(
                "JASPER_WAKE_EVENTS_DIR",
                "/var/lib/jasper/wake-events",
            ),
            # 1 GB default. Each event captures 3 WAVs (one per leg:
            # AEC ON, AEC OFF, DTLN) at ~192 KB each = ~576 KB/event.
            # 1 GB ≈ 1740 events; at ~30-50 events/day that's ~5-7
            # weeks of retention. Was 500 MB pre-triple-stream (one
            # WAV per event); bumped to 1 GB on 2026-05-23 with the
            # third-leg capture so retention stays in the same ballpark.
            wake_events_max_audio_bytes=_env_int(
                "JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES",
                1024 * 1024 * 1024,
            ),
            # JASPER_TTS_DEVICE: PortAudio device name (bare ALSA pcm
            # name from /root/.asoundrc — `plug:` aliases aren't
            # enumerated by PortAudio). `jasper_out` is the fan-out PCM
            # that duplicates writes to BOTH the Apple dongle (speaker)
            # AND the XVF3800 USB-IN (AEC reference).
            tts_device=_env("JASPER_TTS_DEVICE", "jasper_out"),
            # Top-level pcm.jasper_out runs at 48 kHz (matches the
            # dongle's native rate and CamillaDSP's chunk rate).
            # TtsPlayout polyphase-upsamples Gemini's 24 kHz → 48 kHz
            # before write (factor 2, exact integer ratio).
            tts_output_rate=_env_int("JASPER_TTS_OUTPUT_RATE", 48000),
            # OFFSET (dB) applied on top of CamillaDSP's main_volume —
            # a SECONDARY ceiling alongside the TtsVolumeTracker's
            # tracker-and-headroom formula (below) and the absolute
            # hearing-safety cap MAX_TTS_GAIN_DB in audio_io.py. With
            # music ducking ~25 dB during TTS, voice and music don't
            # overlap perceptually, so this offset's main historical
            # role (keep TTS quieter than concurrent music) is mostly
            # moot. Default 0 lets the tracker drive the level; the
            # safety cap at -6 still bounds the maximum. Stays useful
            # as a master-volume-tracking ceiling when no music has
            # played to update the loudness anchor (sudden master
            # change with stale anchor → ceiling kicks in proportional
            # to master). MUST be <= 0 — validator enforces.
            tts_gain_db=_env_float("JASPER_TTS_GAIN_DB", 0.0),
            # When music is playing, TtsVolumeTracker sizes TTS to a
            # headroom above the windowed RMS of CamillaDSP's playback
            # signal — so TTS scales with whatever music is actually
            # coming out of the speaker, accounting for renderer-side
            # volume sliders (AirPlay sender, Spotify Connect, etc.)
            # that don't touch CamillaDSP's main_volume.
            # Headroom is added on top of the windowed music RMS to
            # produce the TTS effective output peak target. With music
            # ducking ~25 dB during TTS, the comparison the user
            # actually feels is "TTS during duck" vs "music when not
            # ducked", so we want TTS *slightly louder* than the
            # music level. 16 dB headroom + voice's ~9-12 dB crest
            # factor ≈ TTS RMS lands ~4-7 dB above music RMS, which
            # reads as "a touch louder than music" without being
            # shouty. Higher → more dominance over music; clamped to
            # -6 dB max gain by audio_io's hearing-safety cap.
            tts_music_headroom_db=_env_float(
                "JASPER_TTS_MUSIC_HEADROOM_DB", 16.0,
            ),
            # Below this windowed RMS, the tracker treats the room as
            # silent and falls back to the legacy "main_volume +
            # tts_gain_db" formula. Camilla reports very negative
            # dBFS during silence (we've measured -53 dBFS noise
            # floor); -50 dBFS is comfortably above that and well
            # below any audible music level.
            tts_silence_threshold_dbfs=_env_float(
                "JASPER_TTS_SILENCE_THRESHOLD_DBFS", -50.0,
            ),
            # Seconds of playback RMS to keep in the windowed-peak
            # buffer. Long enough to ride through quiet passages
            # and inter-track silences without flapping back to
            # the silence-fallback (a typical pop song has 2-3 s
            # quiet intros / outros); short enough that pause →
            # ask Jarvis a question feels responsive and TTS
            # actually gets quieter.
            tts_music_window_sec=_env_float(
                "JASPER_TTS_MUSIC_WINDOW_SEC", 8.0,
            ),
            # Silero VAD probability threshold for barge-in gating.
            # While the model is producing TTS, mic frames are only
            # forwarded to Gemini if Silero says speech_prob >= this.
            # 0.5 = standard Silero default; raise to 0.7 if music
            # bleed false-triggers barge-in, lower if real speech
            # is being missed.
            vad_barge_in_threshold=_env_float(
                "JASPER_VAD_BARGE_IN_THRESHOLD", 0.5,
            ),
            camilla_host=_env("JASPER_CAMILLA_HOST", "127.0.0.1"),
            camilla_port=_env_int("JASPER_CAMILLA_PORT", 1234),
            duck_db=_env_float("JASPER_DUCK_DB", -25.0),
            # Pre-response idle watchdog: closes the turn after this
            # many seconds of pure model silence (no audio chunk
            # received, server hasn't sent turn_complete, no
            # intermediate events like tool dispatches — those reset
            # the anchor via ``_note_activity()``). The chosen 20 s
            # sits comfortably above the worst observed OpenAI
            # Realtime first-chunk latency (~7.7 s in 2026-05-21
            # production logs) while keeping recovery from a genuine
            # API hang under half a minute. See
            # docs/HANDOFF-voice-providers.md "Idle anchor + tool
            # rounds" for the full rationale.
            idle_timeout_sec=_env_int("JASPER_IDLE_TIMEOUT_SEC", 20),
            # Idle context reset is OFF by default. Each turn pays full
            # uncached price for the system prompt + tool defs on the
            # first turn after a reset (OpenAI: ~$0.008 vs $0.001
            # cached), and the reset itself blocks the wake event for
            # 1-6 s while the session reopens. Worth it only if you
            # actually observe stale-context glitches. Per-provider
            # because the cost/race tradeoffs differ:
            #   - OpenAI: no resumption handle, full reconnect, prompt
            #     cache busted. Most expensive.
            #   - Gemini: drops resumption handle, similar reconnect
            #     cost but cheaper baseline pricing.
            #   - Grok: inherits OpenAI implementation.
            # Legacy JASPER_LIVE_CONTEXT_RESET_SEC, if set, supplies a
            # global default for any provider whose specific var is
            # unset.
            openai_context_reset_sec=_env_int(
                "JASPER_OPENAI_CONTEXT_RESET_SEC",
                _env_int("JASPER_LIVE_CONTEXT_RESET_SEC", 0),
            ),
            gemini_context_reset_sec=_env_int(
                "JASPER_GEMINI_CONTEXT_RESET_SEC",
                _env_int("JASPER_LIVE_CONTEXT_RESET_SEC", 0),
            ),
            grok_context_reset_sec=_env_int(
                "JASPER_GROK_CONTEXT_RESET_SEC",
                _env_int("JASPER_LIVE_CONTEXT_RESET_SEC", 0),
            ),
            # OpenAI Realtime: 60-min hard cap (verified against
            # developers.openai.com/api/docs/guides/realtime-conversations
            # as of 2026-05). 5-min buffer leaves comfortable headroom
            # for an in-flight turn to finish before the proactive
            # tear-down fires. See `_proactive_reconnect_watchdog`.
            openai_session_max_sec=_env_int(
                "JASPER_OPENAI_SESSION_MAX_SEC", 3600,
            ),
            openai_proactive_buffer_sec=_env_int(
                "JASPER_OPENAI_PROACTIVE_BUFFER_SEC", 300,
            ),
            # xAI Grok Voice Agent doesn't publish a hard cap; defaults
            # off. Enable by setting both knobs if a cap is observed.
            grok_session_max_sec=_env_int(
                "JASPER_GROK_SESSION_MAX_SEC", 0,
            ),
            grok_proactive_buffer_sec=_env_int(
                "JASPER_GROK_PROACTIVE_BUFFER_SEC", 0,
            ),
            daily_spend_cap_usd=_env_float("JASPER_DAILY_SPEND_CAP_USD", 1.0),
            usage_db=_env("JASPER_USAGE_DB", "/var/lib/jasper/usage.db"),
            librespot_state_path=_env(
                "JASPER_LIBRESPOT_STATE", "/run/librespot/state.json",
            ),
            spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
            # The redirect URI is the URL Spotify bounces the OAuth
            # code back to. It must be an exact match for one of the
            # URIs registered in the user's Spotify Developer App.
            # Default is the canonical bounce page on GitHub Pages
            # (separate public repo `jaspercurry/spotify-oauth-callback`),
            # with `?host=` carrying the speaker's hostname so a single
            # hosted page works for any speaker. For `manual` mode (no
            # external infrastructure), override to
            # "http://127.0.0.1:8888/callback" — the loopback exception
            # Spotify still allows.
            spotify_redirect_uri=_env(
                "SPOTIFY_REDIRECT_URI",
                f"https://jaspercurry.github.io/spotify-oauth-callback/?host={hostname}",
            ),
            # Legacy single-user cache. Read once at startup for the
            # one-shot migration into the new multi-account layout
            # (see jasper.accounts.maybe_migrate_legacy); after the
            # migration runs once, this path is no longer touched.
            spotify_cache_path=_env(
                "SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache"
            ),
            # Substring (case-insensitive) matched against
            # `sp.devices()[].name` to find the Pi's librespot endpoint.
            # Default "JTS" matches `--name JTS` in deploy/systemd/
            # librespot.service. Change if you renamed the device.
            spotify_device_name=_env("JASPER_SPOTIFY_DEVICE_NAME", "JTS"),
            # Multi-account registry: one record per household member,
            # mapping AirPlay ClientName patterns to per-user OAuth
            # caches. See jasper.accounts module-doc for shape.
            spotify_accounts_path=_env(
                "JASPER_SPOTIFY_ACCOUNTS_PATH",
                "/var/lib/jasper/spotify/accounts.json",
            ),
            # Public URL household members visit to add their Spotify
            # account. Surfaced in error messages so the voice
            # assistant can tell unrecognized users where to go.
            # Defaults to http://${hostname}/spotify; override only if
            # the speaker is reverse-proxied behind a different
            # hostname or path.
            spotify_setup_url=_env(
                "JASPER_SPOTIFY_SETUP_URL", f"http://{hostname}/spotify"
            ),
            # Where the jasper-web service listens. Reverse-proxied
            # from nginx's port 80 — the public surface stays at
            # jts.local/spotify regardless.
            spotify_web_bind_host=_env(
                "JASPER_SPOTIFY_WEB_HOST", "127.0.0.1"
            ),
            spotify_web_bind_port=_env_int(
                "JASPER_SPOTIFY_WEB_PORT", 8765
            ),
            # Google OAuth client (Calendar + Gmail). One Google Cloud
            # Console OAuth client serves every household member; per-
            # member refresh tokens are stored under google_accounts_path.
            google_client_id=_env("GOOGLE_CLIENT_ID"),
            google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
            google_redirect_uri=_env(
                # Bounce page (jaspercurry/google-oauth-callback) — see
                # jasper.web.google_setup.default_redirect_uri for why.
                "GOOGLE_REDIRECT_URI",
                "https://jaspercurry.github.io/google-oauth-callback/?host="
                + _env("JASPER_HOSTNAME", "jts.local"),
            ),
            google_accounts_path=_env(
                "JASPER_GOOGLE_ACCOUNTS_PATH",
                "/var/lib/jasper/google/accounts.json",
            ),
            google_setup_url=_env(
                "JASPER_GOOGLE_SETUP_URL", "http://jts.local/google",
            ),
            google_web_bind_host=_env(
                "JASPER_GOOGLE_WEB_HOST", "127.0.0.1",
            ),
            google_web_bind_port=_env_int(
                "JASPER_GOOGLE_WEB_PORT", 8768,
            ),
            # Speaker management dashboard URL. Audio cues extract the
            # hostname from this and tell the user "visit <hostname>"
            # when something blocks normal voice response (spend cap,
            # connection failure). Defaults to http://${hostname}; the
            # speaker no longer ships an HTTPS cert.
            management_url=_env(
                "JASPER_MANAGEMENT_URL", f"http://{hostname}",
            ),
            sounds_dir=_env(
                "JASPER_SOUNDS_DIR", "/var/lib/jasper/sounds",
            ),
            timer_db_path=_env(
                "JASPER_TIMER_DB", "/var/lib/jasper/timers.db",
            ),
            gemini_tts_model=_env(
                "JASPER_GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview",
            ),
            # Default location for "Hey Jarvis, what's the weather?" with
            # no city specified. Empty = require explicit location each time.
            weather_default_location=_env("JASPER_DEFAULT_LOCATION", ""),
            weather_units=_env("JASPER_WEATHER_UNITS", "celsius"),
            # NYC MTA subway. Empty station_id disables the tool.
            # Find your stop_id at data.ny.gov/dataset/...subway-stations
            # (column: "GTFS Stop ID"). 9 Av on the West End line is "B12".
            subway_station_id=_env("JASPER_SUBWAY_STATION_ID", ""),
            # No fallback default — empty means "both directions" at
            # query time. The wizard's "Both" radio writes empty here.
            subway_default_direction=_env(
                "JASPER_SUBWAY_DEFAULT_DIRECTION", "",
            ),
            # NYC MTA bus (BusTime SIRI API). Configured through the
            # /transit/ wizard, which discovers nearby stops via OBA
            # `stops-for-location` and SIRI-probes their live routes.
            # Empty key OR empty stops disables the tool.
            mta_bustime_key=_env("JASPER_MTA_BUSTIME_KEY", ""),
            # JASPER_BUS_STOPS is "id|label,id|label" — labels can
            # contain spaces (e.g. "4 Av/39 St eastbound"), so a
            # naive `.replace(",", " ").split()` like other list-
            # shaped vars use would shred the labels into separate
            # entries. Hand off to the canonical parser.
            bus_stops=tuple(parse_bus_stops(_env("JASPER_BUS_STOPS", ""))),
            # Citi Bike (NYC + JC + Hoboken). Same pipe-list format as
            # JASPER_BUS_STOPS — see jasper.citibike.parse_saved_stations
            # for the canonical parser. Empty list disables the tool.
            citibike_stations=tuple(
                _parse_citibike_stations(_env("JASPER_CITIBIKE_STATIONS", "")),
            ),
            # Household-wide e-bike-only preference. "1" / "true" / "yes"
            # (case-insensitive) → True; anything else (empty, "0",
            # "false") → False. Default False so a fresh install reports
            # both kinds until the household opts in via the wizard.
            citibike_ebike_only=_env(
                "JASPER_CITIBIKE_EBIKE_ONLY", "",
            ).strip().lower() in {"1", "true", "yes"},
            # Home Assistant. Empty url OR empty token disables the tool
            # (cfg.ha_enabled gates registration). The /ha
            # wizard (PR 2) writes these to /var/lib/jasper/home_assistant.env;
            # operators can also set them directly in /etc/jasper/jasper.env
            # for headless / CI imaging. agent_id is optional — empty
            # means "let HA pick the default" (its UI-configured choice).
            ha_url=_env(_ha_env.ENV_URL, "").strip().rstrip("/"),
            ha_token=_env(_ha_env.ENV_TOKEN, "").strip(),
            ha_agent_id=_env(_ha_env.ENV_AGENT_ID, "").strip(),
            # Default to verifying. Wizard writes "0" only when the
            # household explicitly opts into self-signed-cert mode.
            ha_verify_ssl=_env(_ha_env.ENV_VERIFY_SSL, "1").strip() not in ("0", "false", "no"),
            # Persistent speaker-volume file. Read at boot to restore
            # CamillaDSP main_volume, written on every change.
            volume_state_path=_env(
                "JASPER_VOLUME_STATE_PATH",
                "/var/lib/jasper/speaker_volume.json",
            ),
            # If the persisted volume is older than this at boot AND
            # outside [safe_low, safe_high], clamp it into that band.
            # Within-session restarts (deploys, fast crash recovery)
            # preserve continuity. Yesterday's late-night 90% gets
            # clamped to safe_high so the morning isn't a blast.
            volume_regress_after_sec=_env_float(
                "JASPER_VOLUME_REGRESS_AFTER_SEC", 1800.0,
            ),
            # Hard floors and ceilings used by the boot-time regression.
            # Inside [safe_low, safe_high], the saved value is preserved
            # regardless of age — only "extreme" values get nudged.
            volume_regress_safe_low_pct=_env_int(
                "JASPER_VOLUME_REGRESS_SAFE_LOW_PCT", 20,
            ),
            volume_regress_safe_high_pct=_env_int(
                "JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT", 70,
            ),
            # Used when no persisted record exists (first boot, or the
            # state file got deleted / corrupted).
            volume_first_boot_default_pct=_env_int(
                "JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT", 50,
            ),
            # Persistent mic-mute file. Restored at WakeLoop init so a
            # daemon restart (deploy, web-wizard save, watchdog) doesn't
            # silently un-mute. Default lives under StateDirectory=jasper.
            mic_mute_state_path=_env(
                "JASPER_MIC_MUTE_STATE_PATH",
                "/var/lib/jasper/mic_mute.env",
            ),
            # Unix-domain socket where voice_daemon listens for external
            # session triggers (dial hold-to-talk via jasper-control).
            # systemd's RuntimeDirectory=jasper auto-creates /run/jasper
            # at service start with mode 0750.
            voice_control_socket=_env(
                "JASPER_VOICE_CONTROL_SOCKET", "/run/jasper/voice.sock",
            ),
            # Multi-device peering — read JASPER_PEERING the same way
            # the peering daemon does. Anything other than "on" / "true"
            # / "1" / "yes" / "enabled" resolves to off (fail-safe;
            # peering is off by default, and a typo in the env file
            # should never accidentally enable it).
            peering_enabled=_env(
                "JASPER_PEERING", "off",
            ).strip().lower() in ("on", "true", "1", "yes", "enabled"),
            # The UDS where jasper-control's peering daemon listens.
            # Matches PEERING_UDS_PATH in jasper.peering.config —
            # duplicated here so voice_daemon doesn't have to import
            # the peering package just to know where to connect.
            peering_uds_socket=_env(
                "JASPER_PEERING_UDS", "/run/jasper/peering.sock",
            ),
        ))

    @property
    def subway_enabled(self) -> bool:
        return bool(self.subway_station_id)

    @property
    def bus_enabled(self) -> bool:
        return bool(self.bus_stops and self.mta_bustime_key)

    @property
    def citibike_enabled(self) -> bool:
        # GBFS is keyless, so the only gating condition is whether
        # the household has saved any stations.
        return bool(self.citibike_stations)

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
        yet — visit jts.local/ha")."""
        return bool(self.ha_url and self.ha_token)
