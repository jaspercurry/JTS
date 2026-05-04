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
    if cfg.daily_spend_cap_usd < 0:
        raise RuntimeError("JASPER_DAILY_SPEND_CAP_USD must be >= 0")
    if cfg.aec_mode not in ("hardware", "software"):
        raise RuntimeError(
            f"JASPER_AEC_MODE must be 'hardware' or 'software', got "
            f"'{cfg.aec_mode}'"
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
    aec_mode: str
    vad_barge_in_threshold: float

    camilla_host: str
    camilla_port: int
    duck_db: float
    idle_timeout_sec: int

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

    weather_default_location: str
    weather_units: str

    subway_station_id: str
    subway_default_direction: str
    subway_lines: tuple[str, ...]

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
            # enumerated by PortAudio). Pick `jasper_xvf` for hardware
            # AEC mode (XVF3800 jack), `jasper_dongle` for software AEC
            # mode (Apple dongle).
            tts_device=_env("JASPER_TTS_DEVICE", "jasper_xvf"),
            # 24 kHz = Gemini's native output. With the `jasper_xvf`
            # plug wrapper this works directly (plug downsamples to the
            # 16 kHz dmix slave). For `jasper_dongle` (48 kHz dmix), set
            # JASPER_TTS_OUTPUT_RATE=48000 so TtsPlayout polyphase-
            # upsamples 24 → 48 kHz before writing (must be integer
            # multiple of 24000).
            tts_output_rate=_env_int("JASPER_TTS_OUTPUT_RATE", 24000),
            # Static attenuation applied to TTS PCM before write. Gemini
            # outputs raw PCM at consistent level (peaks ~-3 dBFS); with
            # no gain stage between Gemini and the dongle this comes out
            # quite loud vs. user's music volume. -8 dB is a comfortable
            # default that's audible above ducked music but doesn't
            # dominate. Long-term fix: route TTS through CamillaDSP so
            # it tracks user's master_gain (TODO).
            tts_gain_db=_env_float("JASPER_TTS_GAIN_DB", -8.0),
            # JASPER_AEC_MODE — how echo cancellation is achieved:
            #   hardware: XVF3800 chip's built-in AEC. Speakers plug
            #             into the XVF3800's 3.5mm jack; CamillaDSP
            #             playback → jasper_xvf; chip subtracts the
            #             playback signal from the mic capture using
            #             USB-IN as the AEC reference. No software
            #             gating needed; real barge-in works.
            #   software: NOT YET IMPLEMENTED — will gate mic-to-Gemini
            #             via local Silero VAD so any mic (UMIK-2,
            #             cheap USB mic) can be used. Speakers plug
            #             into the Apple dongle (or any DAC).
            aec_mode=_env("JASPER_AEC_MODE", "hardware"),
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
            daily_spend_cap_usd=_env_float("JASPER_DAILY_SPEND_CAP_USD", 1.0),
            usage_db=_env("JASPER_USAGE_DB", "/var/lib/jasper/usage.db"),
            moode_base_url=_env("MOODE_BASE_URL", "http://127.0.0.1"),
            mpd_host=_env("MPD_HOST", "127.0.0.1"),
            mpd_port=_env_int("MPD_PORT", 6600),
            spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
            spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET"),
            spotify_redirect_uri=_env(
                "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8765/callback"
            ),
            spotify_cache_path=_env(
                "SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache"
            ),
            # Substring (case-insensitive) matched against
            # `sp.devices()[].name` to find the Pi's librespot endpoint.
            # moOde defaults to "Moode <hostname>". Change if you renamed
            # your moOde Spotify Connect device.
            spotify_device_name=_env("JASPER_SPOTIFY_DEVICE_NAME", "moode"),
            # Default location for "Hey Jarvis, what's the weather?" with
            # no city specified. Empty = require explicit location each time.
            weather_default_location=_env("JASPER_DEFAULT_LOCATION", ""),
            weather_units=_env("JASPER_WEATHER_UNITS", "celsius"),
            # NYC MTA subway. Empty station_id disables the tool.
            # Find your stop_id at data.ny.gov/dataset/...subway-stations
            # (column: "GTFS Stop ID"). 9 Av on the West End line is "B16".
            subway_station_id=_env("JASPER_SUBWAY_STATION_ID", ""),
            subway_default_direction=_env(
                "JASPER_SUBWAY_DEFAULT_DIRECTION", "uptown",
            ),
            subway_lines=tuple(
                t for t in _env("JASPER_SUBWAY_LINES", "").replace(",", " ").split()
            ),
        ))

    @property
    def subway_enabled(self) -> bool:
        return bool(self.subway_station_id)

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)
