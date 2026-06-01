"""TTS generation + content-addressable caching for audio cues.

Lifecycle:
  - `cue_hash(cue, hostname, voice)` derives the expected filename
    component from everything that should bust the cache (template
    text, hostname substitution, voice, model, audio format).
  - `write_cue(...)` calls the generator, writes a 24 kHz WAV at
    `<sounds_dir>/<slug>-<hash>.wav`, and returns the path.

A TTS backend (`GeminiTTSGenerator` / `OpenAITTSGenerator` /
`GrokTTSGenerator`) is an injectable interface so tests can swap in
a deterministic fake without hitting the network. The factory at
`jasper.voice_daemon._build_cues_manager` picks one to match the
active `JASPER_VOICE_PROVIDER` so cue audio comes from the same
provider that drives the live conversation — no Gemini round-trips
when the user is on OpenAI Realtime, and vice versa.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import wave
from dataclasses import dataclass
from typing import Protocol

from .registry import CueDef

logger = logging.getLogger(__name__)


# --- Cache key inputs ---
#
# Bump GENERATOR_VERSION if you change generation semantics in a way
# that should invalidate every cached file (e.g., switching the
# WAV format). Editing a template string OR changing the
# hostname / voice / model is handled automatically by the hash.
#
# WAV files are written at 24kHz mono — same shape as the live
# audio every supported provider streams (Gemini Live, OpenAI
# Realtime, Grok Voice). TtsPlayout assumes 24kHz input and
# upsamples to its output_rate (48kHz on the dongle); writing WAVs
# at 48kHz here would be double-upsampled and play at half speed
# at the output. So: keep the file at 24kHz and let TtsPlayout
# handle the conversion the same way it does for Live.
GENERATOR_VERSION = "3"  # v3: provider-aware backends (Gemini/OpenAI/Grok)

# Provider-default TTS model identifiers. These flow into the cache
# hash so swapping JASPER_VOICE_PROVIDER auto-invalidates cached
# cues into a fresh re-bake in the new provider's voice.
GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
# xAI's TTS endpoint doesn't take an explicit model parameter — the
# voice_id selects everything. We still record a stable identifier
# in the cache hash so future xAI model changes (when they expose
# them) bust the cache cleanly.
GROK_TTS_MODEL = "grok-tts-1"

# Legacy default kept as a constant for tests / opt-in users — the
# old preview-TTS model (2.5) returned `FinishReason.OTHER` with
# empty content for ~60 % of calls in production, which is why we
# moved off it. Pinned to the exported name so callers that import
# `TTS_MODEL` (older external code) get the new sensible default.
TTS_MODEL = GEMINI_TTS_MODEL

WAV_RATE = 24000           # 24 kHz — what every supported provider returns
WAV_CHANNELS = 1
WAV_SAMPLE_WIDTH = 2       # 16-bit signed little-endian

# Per-attempt synthesis retries. Some provider endpoints
# intermittently return "successful" HTTP 200 responses with no
# audio payload (Gemini's preview TTS is the worst offender —
# `FinishReason.OTHER` with `content=None` for a meaningful
# fraction of requests, even on innocuous text). 5 retries with
# brief backoff turn a 60 %-success-per-call model into a >99 %
# overall-success rate at the cost of up to ~5–8 s of latency on
# the unlucky paths. Pre-rendering at set_timer time hides that
# latency from the user for normal timer flows; the retry is the
# safety net.
TTS_MAX_ATTEMPTS = 5
TTS_RETRY_BACKOFF_SEC = 0.4


def render_template(cue: CueDef, hostname: str) -> str:
    """Substitute the {hostname} placeholder in the cue's template."""
    return cue.template.format(hostname=hostname)


def cue_hash(
    cue: CueDef, hostname: str, voice: str, model: str = TTS_MODEL,
) -> str:
    """Short content-addressable cache key. Encoded into the cached
    filename so a mismatch on any input naturally invalidates the
    cache (the manager looks for the new filename, doesn't find it,
    regenerates)."""
    text = render_template(cue, hostname)
    payload = (
        f"v={GENERATOR_VERSION}|model={model}|voice={voice}"
        f"|rate={WAV_RATE}|sw={WAV_SAMPLE_WIDTH}"
        f"|ch={WAV_CHANNELS}|text={text}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def cue_filename(cue: CueDef, hostname: str, voice: str) -> str:
    return f"{cue.slug}-{cue_hash(cue, hostname, voice)}.wav"


def cue_path(sounds_dir: str, cue: CueDef, hostname: str, voice: str) -> str:
    return os.path.join(sounds_dir, cue_filename(cue, hostname, voice))


# --- WAV write ---


def _write_wav_atomic(path: str, pcm_24k_bytes: bytes) -> None:
    """Write a 16-bit mono PCM 24kHz WAV file atomically (write
    `.tmp` first, then rename). Standard WAV (not raw PCM) so cached
    files are playable with `aplay` / `afplay` for debugging — those
    tools read the rate from the WAV header and produce correct
    playback regardless of the speaker's TtsPlayout configuration."""
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as f:
        f.setnchannels(WAV_CHANNELS)
        f.setsampwidth(WAV_SAMPLE_WIDTH)
        f.setframerate(WAV_RATE)
        f.writeframes(pcm_24k_bytes)
    os.replace(tmp, path)


# --- Generator interface ---


@dataclass
class TTSResult:
    """What a TTS backend hands back to write_cue."""
    pcm_24k: bytes


class TTSBackend(Protocol):
    """Tiny interface so tests can swap in a fake generator."""
    def synthesise(self, text: str) -> TTSResult: ...


class GeminiTTSGenerator:
    """One-shot TTS via Gemini's audio-modal `generate_content`. The
    Live API isn't used here — it's a streaming bidirectional
    protocol, overkill for baking a few short messages.

    Default model is `gemini-3.1-flash-tts-preview` (released
    2026-04-15). The older `gemini-2.5-flash-preview-tts` returned
    `FinishReason.OTHER` with empty content for a meaningful
    fraction of requests — kept reachable via the `model=` kwarg
    for opt-in compatibility but no longer the default.
    """

    def __init__(
        self,
        api_key: str,
        voice: str,
        model: str = GEMINI_TTS_MODEL,
        *,
        max_attempts: int = TTS_MAX_ATTEMPTS,
        retry_backoff_sec: float = TTS_RETRY_BACKOFF_SEC,
    ):
        if not api_key:
            raise ValueError("GeminiTTSGenerator requires an api_key")
        if not voice:
            raise ValueError("GeminiTTSGenerator requires a voice name")
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._max_attempts = max(1, int(max_attempts))
        self._retry_backoff_sec = max(0.0, float(retry_backoff_sec))

    @property
    def model(self) -> str:
        return self._model

    def synthesise(self, text: str) -> TTSResult:
        last_status: str | None = None
        for attempt in range(self._max_attempts):
            status, result = self._attempt(text)
            if result is not None:
                return result
            last_status = status
            if attempt + 1 >= self._max_attempts:
                break
            logger.warning(
                "Gemini TTS empty response on attempt %d/%d (%s); retrying",
                attempt + 1, self._max_attempts, status,
            )
            time.sleep(self._retry_backoff_sec * (attempt + 1))
        raise RuntimeError(
            f"Gemini TTS returned no audio after {self._max_attempts} "
            f"attempts (last status={last_status!r}, text={text!r})"
        )

    def _attempt(self, text: str) -> "tuple[str, TTSResult | None]":
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        response = client.models.generate_content(
            model=self._model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=self._voice,
                        ),
                    ),
                ),
            ),
        )
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return "no_candidates", None
        candidate = candidates[0]
        finish_reason = getattr(candidate, "finish_reason", None)
        content = getattr(candidate, "content", None)
        if content is None:
            return f"finish={finish_reason}_content=None", None
        parts = getattr(content, "parts", None) or []
        audio_part = next(
            (p for p in parts if getattr(p, "inline_data", None)), None,
        )
        if audio_part is None:
            return f"finish={finish_reason}_no_inline_audio", None
        data = audio_part.inline_data.data
        if not data:
            return f"finish={finish_reason}_empty_data", None
        return "ok", TTSResult(pcm_24k=data)


class OpenAITTSGenerator:
    """One-shot TTS via OpenAI's `audio.speech.create` endpoint.

    Returns 24 kHz mono 16-bit signed little-endian PCM (no header)
    when `response_format="pcm"`, which slots directly into
    TtsPlayout. The Realtime API isn't used — same reasoning as
    Gemini; one-shot caching of short messages doesn't need a
    bidirectional streaming connection.

    Default model is `gpt-4o-mini-tts` (per OpenAI's recommendation
    for new integrations); voice catalog overlaps with Realtime
    (marin / cedar / alloy / ash / ballad / coral / echo / fable /
    nova / onyx / sage / shimmer / verse).
    """

    def __init__(
        self,
        api_key: str,
        voice: str,
        model: str = OPENAI_TTS_MODEL,
        base_url: str | None = None,
        max_attempts: int = TTS_MAX_ATTEMPTS,
        retry_backoff_sec: float = TTS_RETRY_BACKOFF_SEC,
    ):
        if not api_key:
            raise ValueError("OpenAITTSGenerator requires an api_key")
        if not voice:
            raise ValueError("OpenAITTSGenerator requires a voice name")
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._base_url = base_url
        self._max_attempts = max(1, int(max_attempts))
        self._retry_backoff_sec = max(0.0, float(retry_backoff_sec))

    @property
    def model(self) -> str:
        return self._model

    def synthesise(self, text: str) -> TTSResult:
        last_err: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._attempt(text)
            except _RetryableTTSError as e:
                last_err = e
                if attempt + 1 >= self._max_attempts:
                    break
                logger.warning(
                    "OpenAI TTS empty response on attempt %d/%d (%s); "
                    "retrying", attempt + 1, self._max_attempts, e,
                )
                time.sleep(self._retry_backoff_sec * (attempt + 1))
        raise RuntimeError(
            f"OpenAI TTS returned no audio after {self._max_attempts} "
            f"attempts (last err={last_err!r}, text={text!r})"
        )

    def _attempt(self, text: str) -> TTSResult:
        from openai import OpenAI

        kwargs: dict = {"api_key": self._api_key}
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        client = OpenAI(**kwargs)
        with client.audio.speech.with_streaming_response.create(
            model=self._model,
            voice=self._voice,
            input=text,
            response_format="pcm",  # 24 kHz mono int16, no header
        ) as response:
            data = response.read()
        if not data:
            raise _RetryableTTSError("openai_empty_pcm")
        return TTSResult(pcm_24k=data)


class GrokTTSGenerator:
    """One-shot TTS via xAI's standalone TTS endpoint at
    `https://api.x.ai/v1/tts`.

    Not OpenAI-SDK compatible — requires a direct HTTP POST. The
    response with `output_format.codec="pcm"` and
    `output_format.sample_rate=24000` is 24 kHz mono 16-bit signed
    little-endian PCM with no header, matching our existing
    pipeline. Voice catalog: eve / ara / rex / sal / leo.
    """

    DEFAULT_ENDPOINT = "https://api.x.ai/v1/tts"

    def __init__(
        self,
        api_key: str,
        voice: str,
        model: str = GROK_TTS_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        language: str = "auto",
        max_attempts: int = TTS_MAX_ATTEMPTS,
        retry_backoff_sec: float = TTS_RETRY_BACKOFF_SEC,
    ):
        if not api_key:
            raise ValueError("GrokTTSGenerator requires an api_key")
        if not voice:
            raise ValueError("GrokTTSGenerator requires a voice name")
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._endpoint = endpoint
        self._language = language
        self._max_attempts = max(1, int(max_attempts))
        self._retry_backoff_sec = max(0.0, float(retry_backoff_sec))

    @property
    def model(self) -> str:
        return self._model

    def synthesise(self, text: str) -> TTSResult:
        import urllib.error
        import urllib.request

        body = json.dumps({
            "text": text,
            "voice_id": self._voice,
            "language": self._language,
            "output_format": {"codec": "pcm", "sample_rate": WAV_RATE},
        }).encode()
        last_err: Exception | None = None
        for attempt in range(self._max_attempts):
            req = urllib.request.Request(
                self._endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "audio/pcm",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    data = response.read()
            except urllib.error.HTTPError as e:
                # 4xx is unrecoverable — bad voice / bad auth /
                # bad text — don't burn retries on it.
                if 400 <= e.code < 500:
                    raise RuntimeError(
                        f"Grok TTS HTTP {e.code} (text={text!r}): "
                        f"{e.read()[:200]!r}"
                    ) from e
                last_err = e
            except urllib.error.URLError as e:
                last_err = e
            else:
                if data:
                    return TTSResult(pcm_24k=data)
                last_err = RuntimeError("grok_empty_pcm")
            if attempt + 1 >= self._max_attempts:
                break
            logger.warning(
                "Grok TTS empty/failed on attempt %d/%d (%s); retrying",
                attempt + 1, self._max_attempts, last_err,
            )
            time.sleep(self._retry_backoff_sec * (attempt + 1))
        raise RuntimeError(
            f"Grok TTS failed after {self._max_attempts} attempts "
            f"(last err={last_err!r}, text={text!r})"
        )


class _RetryableTTSError(Exception):
    """Marker class for "the call returned but with no audio" — the
    retry loop catches this and tries again. Other exception types
    (HTTP 4xx, network unreachable) propagate up immediately."""


# --- Public write entry point ---


def write_cue(
    cue: CueDef,
    hostname: str,
    voice: str,
    sounds_dir: str,
    backend: TTSBackend,
) -> str:
    """Render `cue`'s template, call the TTS backend, resample to 48k,
    write a WAV at `<sounds_dir>/<slug>-<hash>.wav`. Returns the
    absolute path. Idempotent: safe to call when the file already
    exists (will just rewrite the same content)."""
    text = render_template(cue, hostname)
    path = cue_path(sounds_dir, cue, hostname, voice)
    os.makedirs(sounds_dir, exist_ok=True)
    logger.info(
        "cue: synthesising %s (text=%r, voice=%s, hash=%s)",
        cue.slug, text, voice, cue_hash(cue, hostname, voice),
    )
    result = backend.synthesise(text)
    _write_wav_atomic(path, result.pcm_24k)
    logger.info("cue: wrote %s (%d bytes pcm @ 24kHz)", path, len(result.pcm_24k))
    return path


def dynamic_text_hash(text: str, voice: str, model: str = TTS_MODEL) -> str:
    """Cache key for `speak_text(...)` — analogous to `cue_hash` but
    for arbitrary text not tied to a static CueDef. Uses the same
    GENERATOR_VERSION + audio-format inputs so a generator change
    invalidates dynamic and static cues together."""
    payload = (
        f"v={GENERATOR_VERSION}|model={model}|voice={voice}"
        f"|rate={WAV_RATE}|sw={WAV_SAMPLE_WIDTH}"
        f"|ch={WAV_CHANNELS}|text={text}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def dynamic_text_path(sounds_dir: str, text: str, voice: str) -> str:
    h = dynamic_text_hash(text, voice)
    return os.path.join(sounds_dir, f"dynamic-{h}.wav")


def write_dynamic_text(
    text: str, voice: str, sounds_dir: str, backend: TTSBackend,
) -> str:
    """Render arbitrary `text` to a cached WAV at
    `<sounds_dir>/dynamic-<hash>.wav`. Mirrors `write_cue` but for
    text not tied to a static CueDef. Returns the absolute path.
    Idempotent: if the file already exists, just returns the path."""
    path = dynamic_text_path(sounds_dir, text, voice)
    if os.path.isfile(path):
        return path
    os.makedirs(sounds_dir, exist_ok=True)
    logger.info(
        "cue: synthesising dynamic text=%r voice=%s hash=%s",
        text, voice, dynamic_text_hash(text, voice),
    )
    result = backend.synthesise(text)
    _write_wav_atomic(path, result.pcm_24k)
    logger.info("cue: wrote %s (%d bytes pcm @ 24kHz)", path, len(result.pcm_24k))
    return path


def prune_stale(sounds_dir: str, cue: CueDef, keep_hash: str) -> int:
    """Remove any `<slug>-*.wav` files in `sounds_dir` whose hash
    doesn't match `keep_hash`. Called after a successful write so
    a hostname/template/voice change cleans up after itself.
    Returns the count of files removed."""
    if not os.path.isdir(sounds_dir):
        return 0
    prefix = f"{cue.slug}-"
    keep_filename = f"{cue.slug}-{keep_hash}.wav"
    removed = 0
    for entry in os.listdir(sounds_dir):
        if (
            entry.startswith(prefix)
            and entry.endswith(".wav")
            and entry != keep_filename
        ):
            try:
                os.unlink(os.path.join(sounds_dir, entry))
                removed += 1
                logger.info("cue: pruned stale %s", entry)
            except OSError as e:
                logger.warning("cue: could not prune %s: %s", entry, e)
    return removed
