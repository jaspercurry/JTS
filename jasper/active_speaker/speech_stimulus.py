# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cached spoken stimulus for active-speaker combined crossover checks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import wave
from array import array
from pathlib import Path
from typing import Any, Callable

from jasper.cues.generator import OPENAI_TTS_MODEL, OpenAITTSGenerator
from jasper.env_load import merged_env_files
from jasper.voice.catalog import default_voice_id

SCHEMA_VERSION = 1
STIMULUS_KIND = "jts_active_speaker_speech_stimulus"
GENERATOR_VERSION = "1"
DEFAULT_TEXT = "Like and subscribe to Jasper tech."
DEFAULT_CACHE_DIR = Path("/var/lib/jasper/active_speaker_stimuli")
DEFAULT_DURATION_S = 12.0
DEFAULT_GAP_S = 0.35
DEFAULT_SAMPLE_RATE_HZ = 48_000
SOURCE_SAMPLE_RATE_HZ = 24_000
SAMPLE_WIDTH_BYTES = 2
MIN_DURATION_S = 2.0
MAX_DURATION_S = 30.0

SynthesiseFn = Callable[[str, dict[str, str]], bytes]


class SpeechStimulusError(RuntimeError):
    """Raised when the spoken combined-test fixture cannot be prepared."""


def _fresh_env(env: dict[str, str] | None = None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    out = dict(os.environ if env is None else env)
    out.update(merged_env_files())
    return out


def _bounded_duration(value: Any) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION_S
    if not math.isfinite(duration):
        duration = DEFAULT_DURATION_S
    return min(max(duration, MIN_DURATION_S), MAX_DURATION_S)


def _cache_dir(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_ACTIVE_SPEAKER_STIMULUS_DIR")
        or DEFAULT_CACHE_DIR
    )


def _voice_and_model(env: dict[str, str]) -> tuple[str, str]:
    voice = (
        env.get("JASPER_OPENAI_TTS_VOICE")
        or env.get("JASPER_OPENAI_VOICE")
        or default_voice_id("openai")
    )
    model = env.get("JASPER_OPENAI_TTS_MODEL") or OPENAI_TTS_MODEL
    return voice, model


def _cache_key(*, text: str, voice: str, model: str, duration_s: float) -> str:
    payload = {
        "version": GENERATOR_VERSION,
        "text": text,
        "voice": voice,
        "model": model,
        "duration_s": round(duration_s, 3),
        "gap_s": DEFAULT_GAP_S,
        "sample_rate_hz": DEFAULT_SAMPLE_RATE_HZ,
        "sample_width_bytes": SAMPLE_WIDTH_BYTES,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _array_to_le_bytes(samples: array) -> bytes:
    out = array("h", samples)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _pcm24k_to_samples48k(pcm_24k: bytes) -> array:
    if len(pcm_24k) < SAMPLE_WIDTH_BYTES:
        raise SpeechStimulusError("OpenAI TTS returned empty PCM")
    trimmed = pcm_24k[: len(pcm_24k) - (len(pcm_24k) % SAMPLE_WIDTH_BYTES)]
    samples = array("h")
    samples.frombytes(trimmed)
    if sys.byteorder != "little":
        samples.byteswap()
    out = array("h")
    for index, sample in enumerate(samples):
        next_sample = samples[index + 1] if index + 1 < len(samples) else sample
        out.append(int(sample))
        out.append(int((int(sample) + int(next_sample)) / 2))
    return out


def _fade_in_out(samples: array, *, sample_rate_hz: int) -> array:
    out = array("h", samples)
    fade_samples = min(len(out) // 2, max(1, int(sample_rate_hz * 0.012)))
    for index in range(fade_samples):
        gain = index / float(fade_samples)
        out[index] = int(out[index] * gain)
        out[-index - 1] = int(out[-index - 1] * gain)
    return out


def _loop_phrase(
    phrase: array,
    *,
    duration_s: float,
    sample_rate_hz: int,
    gap_s: float,
) -> tuple[array, int]:
    target_samples = max(1, int(round(duration_s * sample_rate_hz)))
    gap = array("h", [0]) * max(0, int(round(gap_s * sample_rate_hz)))
    out = array("h")
    reps = 0
    while len(out) < target_samples:
        out.extend(phrase)
        reps += 1
        if len(out) < target_samples and gap:
            out.extend(gap)
    del out[target_samples:]
    return out, reps


def _write_wav_atomic(path: Path, pcm_s16le: bytes, *, sample_rate_hz: int) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with wave.open(str(tmp), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav.setframerate(sample_rate_hz)
        wav.writeframes(pcm_s16le)
    os.replace(tmp, path)


def _synthesise_openai(text: str, env: dict[str, str]) -> bytes:
    api_key = env.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SpeechStimulusError(
            "OPENAI_API_KEY is required to generate the combined test speech"
        )
    voice, model = _voice_and_model(env)
    generator = OpenAITTSGenerator(api_key=api_key, voice=voice, model=model)
    return generator.synthesise(text).pcm_24k


def ensure_combined_speech_stimulus(
    *,
    text: str = DEFAULT_TEXT,
    duration_s: float = DEFAULT_DURATION_S,
    cache_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    synthesise: SynthesiseFn | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Return a cached looped speech WAV and metadata for the combined check."""

    resolved_env = _fresh_env(env)
    voice, model = _voice_and_model(resolved_env)
    duration = _bounded_duration(duration_s)
    cache = _cache_dir(cache_dir)
    key = _cache_key(text=text, voice=voice, model=model, duration_s=duration)
    wav_path = cache / f"combined-speech-{key}.wav"
    meta_path = cache / f"combined-speech-{key}.json"
    if wav_path.exists() and meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict) and metadata.get("kind") == STIMULUS_KIND:
            return wav_path, {**metadata, "path": str(wav_path)}

    synth = synthesise or _synthesise_openai
    pcm_24k = synth(text, resolved_env)
    phrase = _fade_in_out(
        _pcm24k_to_samples48k(pcm_24k),
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
    )
    loop, repetitions = _loop_phrase(
        phrase,
        duration_s=duration,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        gap_s=DEFAULT_GAP_S,
    )
    peak = max((abs(int(sample)) for sample in loop), default=0)
    peak_dbfs = -120.0 if peak <= 0 else 20.0 * math.log10(peak / 32767.0)
    metadata = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": STIMULUS_KIND,
        "generator_version": GENERATOR_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "openai_tts",
        "text": text,
        "voice": voice,
        "model": model,
        "sample_rate_hz": DEFAULT_SAMPLE_RATE_HZ,
        "sample_format": "pcm_s16le",
        "channel_count": 1,
        "duration_s": round(len(loop) / DEFAULT_SAMPLE_RATE_HZ, 3),
        "duration_ms": int(round(len(loop) * 1000 / DEFAULT_SAMPLE_RATE_HZ)),
        "phrase_repetitions": repetitions,
        "gap_s": DEFAULT_GAP_S,
        "peak_dbfs": round(peak_dbfs, 1),
        "wav_basename": wav_path.name,
    }
    cache.mkdir(parents=True, exist_ok=True)
    _write_wav_atomic(
        wav_path,
        _array_to_le_bytes(loop),
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
    )
    tmp_meta = meta_path.with_name(f".{meta_path.name}.{os.getpid()}.tmp")
    tmp_meta.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_meta, meta_path)
    return wav_path, {**metadata, "path": str(wav_path)}
