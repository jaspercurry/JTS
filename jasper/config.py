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
    return cfg


@dataclass(frozen=True)
class Config:
    voice_provider: str
    gemini_api_key: str
    gemini_model: str

    wake_model: str
    wake_threshold: float
    mic_device: str
    tts_device: str

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

    @classmethod
    def from_env(cls) -> "Config":
        provider = _env("JASPER_VOICE_PROVIDER", "gemini")
        gemini_key = _env("GEMINI_API_KEY", required=(provider == "gemini"))
        return _validate(cls(
            voice_provider=provider,
            gemini_api_key=gemini_key,
            gemini_model=_env("JASPER_GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
            wake_model=_env("JASPER_WAKE_MODEL", "hey_jarvis"),
            wake_threshold=_env_float("JASPER_WAKE_THRESHOLD", 0.5),
            mic_device=_env("JASPER_MIC_DEVICE", "plughw:CARD=Array"),
            tts_device=_env("JASPER_TTS_DEVICE", "plug:jasper_dongle"),
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
        ))

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)
