from __future__ import annotations

import os
from dataclasses import dataclass


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
    if cfg.live_context_reset_sec <= 0:
        raise RuntimeError("JASPER_LIVE_CONTEXT_RESET_SEC must be > 0")
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
    voice_provider: str
    gemini_api_key: str
    gemini_model: str
    gemini_voice: str

    wake_model: str
    wake_threshold: float
    mic_device: str
    mic_capture_rate: int
    mic_capture_channels: int
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
    live_context_reset_sec: int

    daily_spend_cap_usd: float
    usage_db: str

    moode_base_url: str
    mpd_host: str
    mpd_port: int

    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str
    spotify_cache_path: str
    spotify_device_name: str
    spotify_accounts_path: str
    spotify_setup_url: str
    spotify_web_bind_host: str
    spotify_web_bind_port: int

    weather_default_location: str
    weather_units: str

    subway_station_id: str
    subway_default_direction: str
    subway_lines: tuple[str, ...]

    volume_state_path: str
    volume_regress_after_sec: float
    volume_regress_safe_low_pct: int
    volume_regress_safe_high_pct: int
    volume_first_boot_default_pct: int

    @classmethod
    def from_env(cls) -> "Config":
        provider = _env("JASPER_VOICE_PROVIDER", "gemini")
        gemini_key = _env("GEMINI_API_KEY", required=(provider == "gemini"))
        return _validate(cls(
            voice_provider=provider,
            gemini_api_key=gemini_key,
            gemini_model=_env("JASPER_GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
            # Pin the TTS voice so it's consistent across sessions.
            # Available prebuilt voices on Gemini 3.1 Live Preview
            # include Aoede, Charon, Fenrir, Kore, Puck, Leda, Orus,
            # Zephyr. Without this, the server picks one per session.
            gemini_voice=_env("JASPER_GEMINI_VOICE", "Aoede"),
            wake_model=_env("JASPER_WAKE_MODEL", "hey_jarvis"),
            wake_threshold=_env_float("JASPER_WAKE_THRESHOLD", 0.5),
            # JASPER_MIC_DEVICE is a sounddevice/PortAudio identifier, not
            # an ALSA pcm string — PortAudio rejects "plughw:" syntax.
            # Accepts an integer index (`sd.query_devices()`), or a
            # substring of the PortAudio device name (e.g. "Array" matches
            # the XVF3800's "Array: USB Audio (hw:N,0)"; "UMIK-2" matches
            # the MiniDSP UMIK-2). Empty/absent → PortAudio default.
            mic_device=_env("JASPER_MIC_DEVICE", "Array"),
            # The XVF3800 supports 16 kHz mono natively, so 16000/1 is the
            # default. Mics that only do 44.1 / 48 kHz (UMIK-2 et al.) need
            # JASPER_MIC_CAPTURE_RATE=48000 and JASPER_MIC_CAPTURE_CHANNELS=2;
            # MicCapture polyphase-downsamples to 16 kHz mono internally.
            mic_capture_rate=_env_int("JASPER_MIC_CAPTURE_RATE", 16000),
            mic_capture_channels=_env_int("JASPER_MIC_CAPTURE_CHANNELS", 1),
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
            # OFFSET (dB) applied on top of CamillaDSP's main_volume
            # to set the TTS playback level. TTS bypasses CamillaDSP
            # — a background tracker in voice_daemon polls main_volume
            # every 0.5 s and updates TtsPlayout's gain to
            # `main_volume + tts_gain_db`, clamped to TtsPlayout's
            # MIN/MAX safety bounds. Negative values keep TTS quieter
            # than music at the same nominal level (helps intelligibility
            # over louder source material); -8 dB is the previous fixed
            # default and remains a sensible offset. MUST be <= 0 — the
            # config validator enforces this so a fat-fingered "+8"
            # cannot blast the speaker.
            tts_gain_db=_env_float("JASPER_TTS_GAIN_DB", -8.0),
            # When music is playing, TtsVolumeTracker sizes TTS to a
            # headroom above the windowed RMS of CamillaDSP's playback
            # signal — so TTS scales with whatever music is actually
            # coming out of the speaker, accounting for renderer-side
            # volume sliders (AirPlay sender, Spotify Connect, MPD,
            # etc.) that don't touch CamillaDSP's main_volume.
            # Headroom is added on top of the windowed music RMS to
            # produce the TTS effective output peak target. Bigger →
            # TTS dominates the music more clearly. 12 dB ≈ TTS source
            # peaks land around music's typical peak level (since
            # music peak-to-RMS is ~12-15 dB for typical content),
            # which is "intelligible above music without being shouty".
            tts_music_headroom_db=_env_float(
                "JASPER_TTS_MUSIC_HEADROOM_DB", 12.0,
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
            duck_db=_env_float("JASPER_DUCK_DB", -15.0),
            idle_timeout_sec=_env_int("JASPER_IDLE_TIMEOUT_SEC", 60),
            # After this many seconds with no turns, the persistent live
            # connection drops its sessionResumption handle and reopens
            # with a fresh session — so conversational context from a
            # query hours earlier doesn't leak into the next one
            # ("what's the weather" at 9am should NOT influence "what
            # time is it" at 5pm). 5 min default = long enough to keep
            # multi-turn dialogues coherent, short enough to feel fresh.
            live_context_reset_sec=_env_int("JASPER_LIVE_CONTEXT_RESET_SEC", 300),
            daily_spend_cap_usd=_env_float("JASPER_DAILY_SPEND_CAP_USD", 1.0),
            usage_db=_env("JASPER_USAGE_DB", "/var/lib/jasper/usage.db"),
            moode_base_url=_env("MOODE_BASE_URL", "http://127.0.0.1"),
            mpd_host=_env("MPD_HOST", "127.0.0.1"),
            mpd_port=_env_int("MPD_PORT", 6600),
            spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
            spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET"),
            # The redirect URI is the URL Spotify bounces the OAuth
            # code back to. It must be an exact match for one of the
            # URIs registered in the user's Spotify Developer App.
            # In multi-user installs this is the public-facing URL
            # (jasper.local/spotify/callback) reverse-proxied by nginx
            # to the local jasper-web service.
            spotify_redirect_uri=_env(
                "SPOTIFY_REDIRECT_URI", "https://jasper.local/spotify/callback"
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
            # moOde defaults to "Moode <hostname>". Change if you renamed
            # your moOde Spotify Connect device.
            spotify_device_name=_env("JASPER_SPOTIFY_DEVICE_NAME", "moode"),
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
            spotify_setup_url=_env(
                "JASPER_SPOTIFY_SETUP_URL", "https://jasper.local/spotify"
            ),
            # Where the jasper-web service listens. Reverse-proxied
            # from nginx's port 80 — the public surface stays at
            # jasper.local/spotify regardless.
            spotify_web_bind_host=_env(
                "JASPER_SPOTIFY_WEB_HOST", "127.0.0.1"
            ),
            spotify_web_bind_port=_env_int(
                "JASPER_SPOTIFY_WEB_PORT", 8765
            ),
            # Default location for "Hey Jarvis, what's the weather?" with
            # no city specified. Empty = require explicit location each time.
            weather_default_location=_env("JASPER_DEFAULT_LOCATION", ""),
            weather_units=_env("JASPER_WEATHER_UNITS", "celsius"),
            # NYC MTA subway. Empty station_id disables the tool.
            # Find your stop_id at data.ny.gov/dataset/...subway-stations
            # (column: "GTFS Stop ID"). 9 Av on the West End line is "B12".
            subway_station_id=_env("JASPER_SUBWAY_STATION_ID", ""),
            subway_default_direction=_env(
                "JASPER_SUBWAY_DEFAULT_DIRECTION", "uptown",
            ),
            subway_lines=tuple(
                t for t in _env("JASPER_SUBWAY_LINES", "").replace(",", " ").split()
            ),
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
        ))

    @property
    def subway_enabled(self) -> bool:
        return bool(self.subway_station_id)

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)
