# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthesize prompt audio via OpenAI's TTS, cached on disk.

The harness needs to inject *user* audio into the voice loop. We
generate it once per (text, voice) tuple and reuse the WAV forever
— costs stay at $0 after first run.

Single source of truth: one TTS model, one voice. Variance in the
*assistant's* behavior should never be confused with variance in
the prompt. If we later need a multilingual or non-OpenAI source,
swap by changing the constants here — no Protocol, no plug-in
indirection until that's actually required.
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


# OpenAI TTS settings. gpt-4o-mini-tts is ~$0.60 per 1M chars (early
# 2026) and outputs 24kHz mono — we resample to 16kHz for the
# daemon. Voice "alloy" is neutral and clear; pick once and keep.
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"
TTS_OUT_RATE_HZ = 24_000
DAEMON_RATE_HZ = 16_000

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "audio_cache"


def cache_path(text: str, *, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Deterministic path for a (text, voice, model, rate) tuple. SHA-256
    over the inputs that affect bytes-on-disk. Voice/model are baked
    into the hash so a future swap auto-invalidates."""
    key = f"{TTS_MODEL}|{TTS_VOICE}|{DAEMON_RATE_HZ}|{text}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    safe = "".join(c if c.isalnum() else "_" for c in text)[:40]
    return cache_dir / f"{digest}_{safe}.wav"


async def synth(
    text: str, *, cache_dir: Path = DEFAULT_CACHE_DIR, force: bool = False,
) -> Path:
    """Return a path to a 16kHz mono PCM WAV of `text`. Cached. If the
    cache hit exists and `force` is False, returns it without calling
    OpenAI.

    Raises RuntimeError if OPENAI_API_KEY is missing AND no cached
    file exists — the test should `pytest.skip` in that case."""
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
    # NON-streaming on purpose. The streaming variant
    # (`client.audio.speech.with_streaming_response.create(...)` +
    # `async for chunk in resp.iter_bytes()`) is the documented API
    # path, but it ships with a real bug against PCM: chunks come back
    # incomplete and the assembled audio is mostly silence with just
    # the first word or two audible. Confirmed via the OpenAI
    # Community ("TTS streaming does not work", "Gpt-4o-mini-tts
    # Issues: Volume Fluctuations, Silence, Repetition, Distortion",
    # openai-python issue #864) and reproduced here 2026-05-21 against
    # gpt-4o-mini-tts. The non-streaming variant returns the complete
    # response in one shot — no chunk-boundary issues — at the cost of
    # waiting for the full audio before saving (fine for our use case:
    # prompts are short and cached on disk).
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
    """24kHz → 16kHz linear resample (ratio 2/3) on int16 mono.

    Linear is fine here: the daemon's wake / VAD / LLM-side ASR all
    operate on speech-band content and aren't sensitive to the
    high-frequency artefacts a sharper filter would suppress. If
    we ever care, swap to scipy.signal.resample_poly(..., 2, 3)."""
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
