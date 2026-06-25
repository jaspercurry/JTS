# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import wave
from pathlib import Path

from jasper.active_speaker.speech_stimulus import ensure_combined_speech_stimulus


def _pcm24k_fixture() -> bytes:
    # A short non-silent 24 kHz mono int16 ramp. The production synthesizer
    # returns this same raw PCM shape before the fixture upsamples and loops it.
    frames = []
    for index in range(240):
        sample = int((index % 80) * 200)
        frames.append(sample.to_bytes(2, "little", signed=True))
    return b"".join(frames)


def test_combined_speech_stimulus_writes_looped_48k_wav(tmp_path: Path) -> None:
    calls: list[str] = []

    def synthesise(text: str, env: dict[str, str]) -> bytes:
        calls.append(text)
        assert env["OPENAI_API_KEY"] == "sk-test"
        return _pcm24k_fixture()

    path, meta = ensure_combined_speech_stimulus(
        cache_dir=tmp_path,
        duration_s=2.0,
        env={
            "OPENAI_API_KEY": "sk-test",
            "JASPER_OPENAI_TTS_MODEL": "gpt-4o-mini-tts",
            "JASPER_OPENAI_TTS_VOICE": "marin",
        },
        synthesise=synthesise,
    )

    assert calls == ["Like and subscribe to Jasper tech."]
    assert path.exists()
    assert meta["kind"] == "jts_active_speaker_speech_stimulus"
    assert meta["text"] == "Like and subscribe to Jasper tech."
    assert meta["model"] == "gpt-4o-mini-tts"
    assert meta["voice"] == "marin"
    assert meta["sample_rate_hz"] == 48_000
    assert meta["duration_ms"] == 2000
    assert meta["phrase_repetitions"] > 1

    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 48_000
        assert wav.getnframes() == 96_000


def test_combined_speech_stimulus_reuses_cached_wav(tmp_path: Path) -> None:
    calls = 0

    def synthesise(text: str, env: dict[str, str]) -> bytes:
        nonlocal calls
        calls += 1
        return _pcm24k_fixture()

    kwargs = {
        "cache_dir": tmp_path,
        "duration_s": 2.0,
        "env": {
            "OPENAI_API_KEY": "sk-test",
            "JASPER_OPENAI_TTS_MODEL": "gpt-4o-mini-tts",
            "JASPER_OPENAI_TTS_VOICE": "marin",
        },
        "synthesise": synthesise,
    }
    first_path, first_meta = ensure_combined_speech_stimulus(**kwargs)
    second_path, second_meta = ensure_combined_speech_stimulus(**kwargs)

    assert calls == 1
    assert second_path == first_path
    assert second_meta["wav_basename"] == first_meta["wav_basename"]
