# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""TTS generation + content-addressable caching for audio cues.

Lifecycle:
  - `cue_hash(cue, hostname, voice, model)` derives the expected
    filename component from everything that should bust the cache
    (template text, hostname substitution, voice, model, audio
    format). `model` is the backend's ACTUAL synthesis model
    (`backend_model(backend)`), not a constant — so flipping
    `JASPER_GEMINI_TTS_MODEL` (or a provider TTS default) re-bakes.
  - `write_cue(...)` calls the generator, writes a 24 kHz WAV at
    `<sounds_dir>/<slug>-<hash>.wav`, and returns the path.

A TTS backend (`GeminiTTSGenerator` / `OpenAITTSGenerator` /
`GrokTTSGenerator`) is an injectable interface so tests can swap in
a deterministic fake without hitting the network. The factory at
`jasper.cues.factory.build_cue_tts_backend` picks one to match the
active `JASPER_VOICE_PROVIDER` so cue audio comes from the same
provider that drives the live conversation — no Gemini round-trips
when the user is on OpenAI Realtime, and vice versa. It's called from
`jasper.voice.daemon_main._build_cues_manager`, which builds the whole
`AudioCueManager` and is called from `run()` at daemon startup.

`ChimeTTSGenerator` is the fourth backend: provider-free, used by
`jasper.cues.factory.build_env_cue_manager` when no provider backend
can be built at all (no `JASPER_VOICE_PROVIDER` chosen yet) so a
genuinely fresh box still has audible park cues (AGENTS.md
non-negotiable 6, issue #4814).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import wave
from dataclasses import dataclass
from typing import Callable, Collection, Protocol

from ..voice.earcons import LISTENING_CHIRP_RECIPE, render_recipe
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

# Filename prefix of `speak_text`'s cache — arbitrary text, not a cue.
_DYNAMIC_PREFIX = "dynamic"


def render_template(cue: CueDef, hostname: str) -> str:
    """Substitute the {hostname} placeholder in the cue's template."""
    return cue.template.format(hostname=hostname)


def cue_hash(
    cue: CueDef, hostname: str, voice: str, model: str = GEMINI_TTS_MODEL,
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


def cue_filename(
    cue: CueDef, hostname: str, voice: str, model: str = GEMINI_TTS_MODEL,
) -> str:
    return f"{cue.slug}-{cue_hash(cue, hostname, voice, model)}.wav"


def cue_path(
    sounds_dir: str, cue: CueDef, hostname: str, voice: str,
    model: str = GEMINI_TTS_MODEL,
) -> str:
    return os.path.join(sounds_dir, cue_filename(cue, hostname, voice, model))


def backend_model(backend: object | None) -> str:
    """The cache-key model identifier for a TTS backend — its actual
    synthesis model where exposed (every shipped generator, including
    `ChimeTTSGenerator`, has a `.model` property), else the legacy
    `GEMINI_TTS_MODEL` default.

    The fallback keeps two cases stable: a playback-only manager
    (backend=None — regen disabled, plays whatever WAVs exist) and
    minimal test fakes, both of which hash exactly as they did before
    the model was threaded through. The single derivation point keeps
    the manager's read-side paths and `write_cue`'s write-side path
    agreeing on the same hash."""
    return getattr(backend, "model", None) or GEMINI_TTS_MODEL


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


class _RetryableTTSError(Exception):
    """Marker class for "the call returned but with no audio" — the
    retry loop catches this and tries again. Other exception types
    (HTTP 4xx, network unreachable) propagate up immediately."""


class _ProviderTTS:
    """Shared base for the provider TTS backends: validates api_key +
    voice, stores the model, clamps attempts/backoff, and owns the
    retry loop. Subclasses implement `_attempt(text) -> TTSResult`,
    raising `_RetryableTTSError` for a transient empty/invalid
    response (retried) — any other exception propagates immediately.
    """

    def __init__(
        self,
        api_key: str,
        voice: str,
        model: str,
        *,
        max_attempts: int = TTS_MAX_ATTEMPTS,
        retry_backoff_sec: float = TTS_RETRY_BACKOFF_SEC,
    ) -> None:
        if not api_key:
            raise ValueError(f"{type(self).__name__} requires an api_key")
        if not voice:
            raise ValueError(f"{type(self).__name__} requires a voice name")
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._max_attempts = max(1, int(max_attempts))
        self._retry_backoff_sec = max(0.0, float(retry_backoff_sec))

    @property
    def model(self) -> str:
        return self._model

    @property
    def _label(self) -> str:
        """Human-readable provider name for retry/error messages,
        e.g. GeminiTTSGenerator -> "Gemini"."""
        return type(self).__name__.removesuffix("TTSGenerator")

    def _attempt(self, text: str) -> TTSResult:
        raise NotImplementedError

    def synthesise(self, text: str) -> TTSResult:
        last_err: _RetryableTTSError | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._attempt(text)
            except _RetryableTTSError as e:
                last_err = e
                if attempt + 1 >= self._max_attempts:
                    break
                logger.warning(
                    "%s TTS empty response on attempt %d/%d (%s); "
                    "retrying", self._label, attempt + 1, self._max_attempts, e,
                )
                time.sleep(self._retry_backoff_sec * (attempt + 1))
        raise RuntimeError(
            f"{self._label} TTS returned no audio after {self._max_attempts} "
            f"attempts (last={last_err!r}, text={text!r})"
        )


class GeminiTTSGenerator(_ProviderTTS):
    """One-shot TTS via Gemini's audio-modal `generate_content`. The
    Live API isn't used here — it's a streaming bidirectional
    protocol, overkill for baking a few short messages.

    Default model is `gemini-3.1-flash-tts-preview`; pass `model=` to
    override.
    """

    def __init__(
        self,
        api_key: str,
        voice: str,
        model: str = GEMINI_TTS_MODEL,
        *,
        max_attempts: int = TTS_MAX_ATTEMPTS,
        retry_backoff_sec: float = TTS_RETRY_BACKOFF_SEC,
    ) -> None:
        super().__init__(
            api_key, voice, model,
            max_attempts=max_attempts, retry_backoff_sec=retry_backoff_sec,
        )

    def _attempt(self, text: str) -> TTSResult:
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
            raise _RetryableTTSError("no_candidates")
        candidate = candidates[0]
        finish_reason = getattr(candidate, "finish_reason", None)
        content = getattr(candidate, "content", None)
        if content is None:
            raise _RetryableTTSError(f"finish={finish_reason}_content=None")
        parts = getattr(content, "parts", None) or []
        audio_part = next(
            (p for p in parts if getattr(p, "inline_data", None)), None,
        )
        if audio_part is None:
            raise _RetryableTTSError(f"finish={finish_reason}_no_inline_audio")
        data = audio_part.inline_data.data
        if not data:
            raise _RetryableTTSError(f"finish={finish_reason}_empty_data")
        return TTSResult(pcm_24k=data)


class OpenAITTSGenerator(_ProviderTTS):
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
    ) -> None:
        super().__init__(
            api_key, voice, model,
            max_attempts=max_attempts, retry_backoff_sec=retry_backoff_sec,
        )
        self._base_url = base_url

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


class GrokTTSGenerator(_ProviderTTS):
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
    ) -> None:
        super().__init__(
            api_key, voice, model,
            max_attempts=max_attempts, retry_backoff_sec=retry_backoff_sec,
        )
        self._endpoint = endpoint
        self._language = language

    def _attempt(self, text: str) -> TTSResult:
        import urllib.error
        import urllib.request

        body = json.dumps({
            "text": text,
            "voice_id": self._voice,
            "language": self._language,
            "output_format": {"codec": "pcm", "sample_rate": WAV_RATE},
        }).encode()
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
            # 4xx is unrecoverable — bad voice / bad auth / bad
            # text — don't burn retries on it.
            if 400 <= e.code < 500:
                raise RuntimeError(
                    f"Grok TTS HTTP {e.code} (text={text!r}): "
                    f"{e.read()[:200]!r}"
                ) from e
            raise _RetryableTTSError(str(e)) from e
        except urllib.error.URLError as e:
            raise _RetryableTTSError(str(e)) from e
        if not data:
            raise _RetryableTTSError("grok_empty_pcm")
        return TTSResult(pcm_24k=data)


# --- Provider-free fallback backend ---

# Cache-key model token for chime-baked cues, distinct from every real
# provider's default TTS model constant above. cue_hash() folds this in, so a
# chime-baked WAV and a provider-baked WAV for the same cue never share
# a filename: configuring a provider later computes a different hash,
# misses the cache, and write_cue re-bakes real speech over the chime.
CHIME_MODEL = "chime-v1"
CHIME_VOICE_LABEL = "chime"


class ChimeTTSGenerator:
    """Provider-free fallback: a short deterministic chime, not speech.

    Used only when no provider backend can be built at all —
    `jasper.cues.factory.build_env_cue_manager` reaches for this when
    `JASPER_VOICE_PROVIDER` is unset (or its key is missing), which is
    otherwise the one way a genuinely fresh box has no audio at all for
    its park cues (AGENTS.md non-negotiable 6, issue #4814). Once a
    provider is configured, its own model name wins the cache key
    (`backend_model`) and `write_cue` re-bakes real speech over the
    chime on the next regenerate.

    The chime itself is `jasper.voice.earcons`'s already-designed
    "listening chirp" (an ascending-fifth chime with a shimmer tail),
    not a reimplementation: one sine+envelope synthesizer for this
    concern, not a third one.
    """

    @property
    def model(self) -> str:
        return CHIME_MODEL

    def synthesise(self, text: str) -> TTSResult:
        del text  # a chime carries no words
        return TTSResult(pcm_24k=render_recipe(LISTENING_CHIRP_RECIPE))


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
    exists (will just rewrite the same content). The hash is keyed
    on the backend's actual model so a model change lands in a new
    filename."""
    text = render_template(cue, hostname)
    model = backend_model(backend)
    path = cue_path(sounds_dir, cue, hostname, voice, model)
    os.makedirs(sounds_dir, exist_ok=True)
    logger.info(
        "cue: synthesising %s (text=%r, voice=%s, model=%s, hash=%s)",
        cue.slug, text, voice, model, cue_hash(cue, hostname, voice, model),
    )
    result = backend.synthesise(text)
    _write_wav_atomic(path, result.pcm_24k)
    logger.info("cue: wrote %s (%d bytes pcm @ 24kHz)", path, len(result.pcm_24k))
    return path


def dynamic_text_hash(text: str, voice: str, model: str = GEMINI_TTS_MODEL) -> str:
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


def dynamic_text_path(
    sounds_dir: str, text: str, voice: str, model: str = GEMINI_TTS_MODEL,
) -> str:
    h = dynamic_text_hash(text, voice, model)
    return os.path.join(sounds_dir, f"{_DYNAMIC_PREFIX}-{h}.wav")


def write_dynamic_text(
    text: str, voice: str, sounds_dir: str, backend: TTSBackend,
) -> str:
    """Render arbitrary `text` to a cached WAV at
    `<sounds_dir>/dynamic-<hash>.wav`. Mirrors `write_cue` but for
    text not tied to a static CueDef. Returns the absolute path.
    Idempotent: if the file already exists, just returns the path."""
    model = backend_model(backend)
    path = dynamic_text_path(sounds_dir, text, voice, model)
    if os.path.isfile(path):
        return path
    os.makedirs(sounds_dir, exist_ok=True)
    # Log shape, not content: dynamic text (timer labels) can
    # be personal and the journal is persistent. Full text only at DEBUG.
    logger.info(
        "cue: synthesising dynamic text (%d chars) voice=%s model=%s hash=%s",
        len(text), voice, model, dynamic_text_hash(text, voice, model),
    )
    logger.debug("cue: synthesising dynamic text=%r", text)
    result = backend.synthesise(text)
    _write_wav_atomic(path, result.pcm_24k)
    logger.info("cue: wrote %s (%d bytes pcm @ 24kHz)", path, len(result.pcm_24k))
    return path


def _prune_where(sounds_dir: str, doomed: Callable[[str], bool]) -> int:
    """Unlink every filename in `sounds_dir` the predicate condemns.
    Returns the count removed."""
    if not os.path.isdir(sounds_dir):
        return 0
    removed = 0
    for entry in os.listdir(sounds_dir):
        if not doomed(entry):
            continue
        try:
            os.unlink(os.path.join(sounds_dir, entry))
            removed += 1
            logger.info("cue: pruned %s", entry)
        except OSError as e:
            logger.warning("cue: could not prune %s: %s", entry, e)
    return removed


def prune_retired(sounds_dir: str, known_slugs: Collection[str]) -> int:
    """Remove `<slug>-<hash>.wav` files whose slug left the registry.
    `dynamic-*` is `speak_text`'s cache, not a cue. Returns the count."""
    def doomed(entry: str) -> bool:
        if not entry.endswith(".wav"):
            return False
        slug, sep, _ = entry[:-4].rpartition("-")
        return bool(sep) and slug not in known_slugs and slug != _DYNAMIC_PREFIX

    return _prune_where(sounds_dir, doomed)


def prune_stale(sounds_dir: str, cue: CueDef, keep_hash: str) -> int:
    """Remove any `<slug>-*.wav` files in `sounds_dir` whose hash
    doesn't match `keep_hash`. Called after a successful write so
    a hostname/template/voice change cleans up after itself.
    Returns the count of files removed."""
    keep = f"{cue.slug}-{keep_hash}.wav"
    return _prune_where(
        sounds_dir,
        lambda e: (
            e.startswith(f"{cue.slug}-") and e.endswith(".wav") and e != keep
        ),
    )
