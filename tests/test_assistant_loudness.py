from __future__ import annotations

import math
import urllib.request
from types import SimpleNamespace

import pytest

import jasper.assistant_loudness as assistant_loudness
import jasper.cues.generator as cue_generator
from jasper.assistant_loudness import (
    AssistantSourceMeter,
    CALIBRATION_TEXT,
    LoudnessMeasurement,
    confidence_for_measurement,
    ensure_seed_profile,
    load_profile,
    measure_pcm_24k_mono,
    profile_for_outputd,
    silence_target_lufs_for_level,
    update_profile_from_measurement,
)


def _profile_path(tmp_path):
    return tmp_path / "assistant_loudness_profiles.json"


def test_silence_target_tracks_user_level():
    assert silence_target_lufs_for_level(0) == -54.0
    assert silence_target_lufs_for_level(50) == -41.0
    assert silence_target_lufs_for_level(100) == -28.0
    assert silence_target_lufs_for_level("bad") == -41.0


def test_profile_round_trip_and_merge(tmp_path):
    path = _profile_path(tmp_path)
    first = LoudnessMeasurement(
        source_lufs=-20.0,
        source_peak_dbfs=-3.0,
        voiced_duration_sec=1.0,
        total_duration_sec=1.0,
    )
    update_profile_from_measurement(
        "gemini",
        "gemini-live",
        "Kore",
        first,
        path=path,
        method="seed_tts",
        confidence=0.60,
        phrase=CALIBRATION_TEXT,
    )
    second = LoudnessMeasurement(
        source_lufs=-14.0,
        source_peak_dbfs=-1.0,
        voiced_duration_sec=2.0,
        total_duration_sec=2.0,
    )
    profile = update_profile_from_measurement(
        "gemini",
        "gemini-live",
        "Kore",
        second,
        path=path,
        method="passive_live",
        confidence=0.90,
    )

    loaded = load_profile("gemini", "gemini-live", "Kore", path=path)
    assert loaded == profile
    assert profile_for_outputd("gemini", "gemini-live", "Kore", path=path) == profile
    assert -20.0 < profile.source_lufs < -14.0
    assert profile.source_peak_dbfs == -1.0
    assert profile.confidence == 0.90


def test_invalid_profile_is_not_used_by_outputd(tmp_path):
    path = _profile_path(tmp_path)
    path.write_text(
        """
        {
          "version": 1,
          "profiles": [{
            "provider": "grok",
            "model": "grok-voice",
            "voice": "Rokk",
            "source_lufs": 3.0,
            "source_peak_dbfs": 1.0,
            "confidence": 1.0
          }]
        }
        """,
        encoding="utf-8",
    )
    assert profile_for_outputd("grok", "grok-voice", "Rokk", path=path) is None


def test_measure_pcm_24k_mono_reports_plausible_loudness():
    np = pytest.importorskip("numpy")
    rate = 24_000
    t = np.arange(rate, dtype=np.float64) / rate
    pcm = (0.25 * 32767.0 * np.sin(2.0 * math.pi * 1000.0 * t)).astype(np.int16)

    measurement = measure_pcm_24k_mono(pcm.tobytes())

    assert -30.0 < measurement.source_lufs < -5.0
    assert -13.0 < measurement.source_peak_dbfs < -11.0
    assert measurement.voiced_duration_sec >= 0.3
    assert measurement.total_duration_sec == 1.0


def test_source_meter_ignores_short_audio():
    np = pytest.importorskip("numpy")
    rate = 24_000
    t = np.arange(int(rate * 0.1), dtype=np.float64) / rate
    pcm = (0.2 * 32767.0 * np.sin(2.0 * math.pi * 1000.0 * t)).astype(np.int16)
    meter = AssistantSourceMeter()

    meter.observe_pcm_24k(pcm.tobytes())

    assert meter.finish() is None


def test_confidence_penalizes_clipped_short_phrases():
    clipped = LoudnessMeasurement(
        source_lufs=-10.0,
        source_peak_dbfs=-0.1,
        voiced_duration_sec=0.45,
        total_duration_sec=0.5,
    )
    clean = LoudnessMeasurement(
        source_lufs=-18.0,
        source_peak_dbfs=-6.0,
        voiced_duration_sec=1.5,
        total_duration_sec=1.6,
    )

    assert confidence_for_measurement(clipped) < confidence_for_measurement(clean)
    assert confidence_for_measurement(clean, seed=True) == 0.65


def _one_second_tone_pcm() -> bytes:
    np = pytest.importorskip("numpy")
    rate = 24_000
    t = np.arange(rate, dtype=np.float64) / rate
    return (
        0.20 * 32767.0 * np.sin(2.0 * math.pi * 1000.0 * t)
    ).astype(np.int16).tobytes()


def test_ensure_seed_profile_passes_bounded_retry_options(tmp_path, monkeypatch):
    captured = {}

    class Backend:
        def synthesise(self, text):
            assert text == CALIBRATION_TEXT
            return SimpleNamespace(pcm_24k=_one_second_tone_pcm())

    def fake_build_backend(cfg, *, max_attempts, retry_backoff_sec):
        captured.update({
            "provider": cfg.voice_provider,
            "max_attempts": max_attempts,
            "retry_backoff_sec": retry_backoff_sec,
        })
        return Backend()

    monkeypatch.setattr(
        assistant_loudness,
        "_build_active_seed_backend",
        fake_build_backend,
    )
    cfg = SimpleNamespace(
        voice_provider="openai",
        openai_model="gpt-realtime-2",
        openai_voice="marin",
    )

    profile = ensure_seed_profile(
        cfg,
        path=_profile_path(tmp_path),
        force=True,
        max_attempts=1,
        retry_backoff_sec=0.0,
    )

    assert profile is not None
    assert captured == {
        "provider": "openai",
        "max_attempts": 1,
        "retry_backoff_sec": 0.0,
    }


def test_gemini_tts_generator_honors_single_attempt(monkeypatch):
    calls = []
    sleeps = []
    generator = cue_generator.GeminiTTSGenerator(
        "AIza-test",
        "Kore",
        max_attempts=1,
        retry_backoff_sec=99.0,
    )
    monkeypatch.setattr(
        generator,
        "_attempt",
        lambda _text: (calls.append(True) or ("empty", None)),
    )
    monkeypatch.setattr(cue_generator.time, "sleep", lambda sec: sleeps.append(sec))

    with pytest.raises(RuntimeError, match="after 1 attempts"):
        generator.synthesise("hello")

    assert calls == [True]
    assert sleeps == []


def test_openai_tts_generator_honors_single_attempt(monkeypatch):
    calls = []
    sleeps = []
    generator = cue_generator.OpenAITTSGenerator(
        "sk-test",
        "marin",
        max_attempts=1,
        retry_backoff_sec=99.0,
    )

    def fail(_text):
        calls.append(True)
        raise cue_generator._RetryableTTSError("empty")

    monkeypatch.setattr(generator, "_attempt", fail)
    monkeypatch.setattr(cue_generator.time, "sleep", lambda sec: sleeps.append(sec))

    with pytest.raises(RuntimeError, match="after 1 attempts"):
        generator.synthesise("hello")

    assert calls == [True]
    assert sleeps == []


def test_grok_tts_generator_honors_single_attempt(monkeypatch):
    calls = []
    sleeps = []

    class EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b""

    def fake_urlopen(_req, *, timeout):
        calls.append(timeout)
        return EmptyResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cue_generator.time, "sleep", lambda sec: sleeps.append(sec))
    generator = cue_generator.GrokTTSGenerator(
        "xai-test",
        "eve",
        endpoint="https://example.invalid/tts",
        max_attempts=1,
        retry_backoff_sec=99.0,
    )

    with pytest.raises(RuntimeError, match="after 1 attempts"):
        generator.synthesise("hello")

    assert calls == [20]
    assert sleeps == []
