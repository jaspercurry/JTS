"""TTS generation + content-addressable caching for audio cues.

Lifecycle:
  - `cue_hash(cue, hostname, voice)` derives the expected filename
    component from everything that should bust the cache (template
    text, hostname substitution, voice, model, audio format).
  - `write_cue(...)` calls the generator, resamples 24kHz → 48kHz,
    writes a WAV at `<sounds_dir>/<slug>-<hash>.wav`, and returns
    the path.

The TTS backend (`GeminiTTSGenerator`) is an injectable interface so
tests can swap in a deterministic fake without hitting the network.
"""
from __future__ import annotations

import hashlib
import logging
import os
import wave
from dataclasses import dataclass
from typing import Protocol

from .registry import CueDef

logger = logging.getLogger(__name__)


# --- Cache key inputs ---
#
# Bump GENERATOR_VERSION if you change generation semantics in a way
# that should invalidate every cached file (e.g., switching the
# resample algorithm). Editing a template string OR changing the
# hostname / voice / model is handled automatically by the hash.
GENERATOR_VERSION = "1"
TTS_MODEL = "gemini-2.5-flash-preview-tts"
TTS_NATIVE_RATE = 24000   # what Gemini's TTS endpoint returns
PLAYBACK_RATE = 48000      # what TtsPlayout / the dongle dmix consume
PLAYBACK_CHANNELS = 1
PLAYBACK_SAMPLE_WIDTH = 2  # 16-bit


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
        f"|rate={PLAYBACK_RATE}|sw={PLAYBACK_SAMPLE_WIDTH}"
        f"|ch={PLAYBACK_CHANNELS}|text={text}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def cue_filename(cue: CueDef, hostname: str, voice: str) -> str:
    return f"{cue.slug}-{cue_hash(cue, hostname, voice)}.wav"


def cue_path(sounds_dir: str, cue: CueDef, hostname: str, voice: str) -> str:
    return os.path.join(sounds_dir, cue_filename(cue, hostname, voice))


# --- Resample + WAV write ---


def _resample_2x_int16(pcm24k_bytes: bytes) -> bytes:
    """Upsample 24kHz int16 PCM → 48kHz by zero-order-hold (each
    sample emitted twice). Audio cues are short voice messages; the
    artifacting is inaudible at speech rates. Pure stdlib so this
    module imports cleanly in environments without numpy (e.g. the
    test runner)."""
    if len(pcm24k_bytes) % 2 != 0:
        raise ValueError("PCM byte length must be even (16-bit samples)")
    out = bytearray(len(pcm24k_bytes) * 2)
    for i in range(0, len(pcm24k_bytes), 2):
        sample = pcm24k_bytes[i:i + 2]
        out[i * 2:i * 2 + 2] = sample
        out[i * 2 + 2:i * 2 + 4] = sample
    return bytes(out)


def _write_wav_atomic(path: str, pcm48k_bytes: bytes) -> None:
    """Write a 16-bit mono PCM 48kHz WAV file atomically (write
    `.tmp` first, then rename). Standard WAV (not raw PCM) so cached
    files are playable with `aplay` / `afplay` for debugging."""
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as f:
        f.setnchannels(PLAYBACK_CHANNELS)
        f.setsampwidth(PLAYBACK_SAMPLE_WIDTH)
        f.setframerate(PLAYBACK_RATE)
        f.writeframes(pcm48k_bytes)
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
    """One-shot TTS via Gemini's audio-modal `generate_content`. Live
    API isn't used here — it's a streaming bidirectional protocol
    overkill for baking a few short messages."""

    def __init__(self, api_key: str, voice: str, model: str = TTS_MODEL):
        if not api_key:
            raise ValueError("GeminiTTSGenerator requires an api_key")
        if not voice:
            raise ValueError("GeminiTTSGenerator requires a voice name")
        self._api_key = api_key
        self._voice = voice
        self._model = model

    def synthesise(self, text: str) -> TTSResult:
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
        parts = response.candidates[0].content.parts
        audio_part = next(
            (p for p in parts if getattr(p, "inline_data", None)), None,
        )
        if audio_part is None:
            raise RuntimeError(
                f"Gemini TTS returned no audio for text={text!r}"
            )
        data = audio_part.inline_data.data
        return TTSResult(pcm_24k=data)


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
    pcm48k = _resample_2x_int16(result.pcm_24k)
    _write_wav_atomic(path, pcm48k)
    logger.info("cue: wrote %s (%d bytes pcm)", path, len(pcm48k))
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
