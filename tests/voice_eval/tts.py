# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthesize prompt audio via OpenAI TTS and cache it on disk.

Reuse each complete prompt so prompt variation does not confound comparisons
of the assistant. The cache key includes text, voice, model and output rate.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import wave
from pathlib import Path

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment]


# OpenAI returns 24 kHz mono PCM; the harness sends 16 kHz mono.
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"
TTS_OUT_RATE_HZ = 24_000
DAEMON_RATE_HZ = 16_000

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "audio_cache"


def cache_path(text: str, *, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Hash text, voice, model and output rate so changed inputs use a new cache file."""
    key = f"{TTS_MODEL}|{TTS_VOICE}|{DAEMON_RATE_HZ}|{text}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    safe = "".join(c if c.isalnum() else "_" for c in text)[:40]
    return cache_dir / f"{digest}_{safe}.wav"


async def synth(
    text: str, *, cache_dir: Path = DEFAULT_CACHE_DIR, force: bool = False,
) -> Path:
    """Return a 16 kHz mono PCM WAV, reusing the cache unless force=True.

    A cache miss or forced synthesis needs the OpenAI package and API key.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(text, cache_dir=cache_dir)
    if path.exists() and not force:
        return path

    if AsyncOpenAI is None:
        raise RuntimeError(
            "openai package not installed — pip install openai>=1.0",
        )
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not set; cannot synthesize prompt audio "
            f"(no cached file at {path})",
        )

    client = AsyncOpenAI(api_key=key)
    # Wait for the whole prompt because playback consumes a complete cached WAV.
    response = await client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        response_format="pcm",
    )
    pcm_24k = await response.aread()

    pcm_16k = _resample_24k_to_16k(pcm_24k)
    _write_wav_atomic(path, pcm_16k, sample_rate=DAEMON_RATE_HZ)
    return path


def _resample_24k_to_16k(pcm: bytes) -> bytes:
    """Resample int16 mono from 24 kHz to 16 kHz with linear interpolation."""
    import array
    src = array.array("h")
    src.frombytes(pcm)
    if not src:
        return b""
    # ratio: 16/24 = 2/3 — produce 2 output samples per 3 input.
    out = array.array("h")
    n = len(src)
    # Output length: floor(n * 2 / 3)
    out_len = (n * 2) // 3
    for i in range(out_len):
        src_pos = (i * 3) / 2
        i0 = int(src_pos)
        if i0 >= n - 1:
            out.append(src[n - 1])
            continue
        frac = src_pos - i0
        v = int(src[i0] * (1 - frac) + src[i0 + 1] * frac)
        # Clamp to int16
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out.append(v)
    return out.tobytes()


def _write_wav(path: Path, pcm: bytes, *, sample_rate: int) -> None:
    """Write `pcm` (16-bit mono LE) to `path` as a WAV file."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def _write_wav_atomic(path: Path, pcm: bytes, *, sample_rate: int) -> None:
    """Write a complete WAV to a tempfile, then atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        _write_wav(tmp_path, pcm, sample_rate=sample_rate)
        os.replace(tmp_path, path)
    except Exception:  # noqa: BLE001
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def synth_sync(text: str, **kw) -> Path:
    """Sync convenience wrapper. Useful when generating cache entries
    from a `python -c` one-liner outside of pytest."""
    return asyncio.run(synth(text, **kw))
