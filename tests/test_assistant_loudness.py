# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
    tts_envelope_lufs_for_level,
    update_profile_from_measurement,
    upsample_2x,
)


def _profile_path(tmp_path):
    return tmp_path / "assistant_loudness_profiles.json"


def test_tts_envelope_tracks_user_level():
    assert tts_envelope_lufs_for_level(0) == -54.0
    assert tts_envelope_lufs_for_level(50) == -41.0
    assert tts_envelope_lufs_for_level(100) == -28.0
    assert tts_envelope_lufs_for_level("bad") == -41.0


def test_bs1770_impulse_matches_rust_runtime_golden():
    """Pin Python and Rust to one K-weighting/LUFS contract vector."""
    np = pytest.importorskip("numpy")
    samples = np.zeros(256, dtype=np.float64)
    samples[0] = 1000.0
    energy = assistant_loudness._k_weighted_stereo_energy(samples)
    lufs = assistant_loudness._lufs_from_energy(float(energy.sum()), len(samples))
    assert lufs == pytest.approx(-48.24314664579623, abs=1e-10)


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


def test_active_seed_backend_reuses_cue_provider_dispatch(monkeypatch):
    from jasper.cues import factory

    sentinel = object()
    observed = {}

    def fake_build(cfg, provider, *, max_attempts, retry_backoff_sec):
        observed.update({
            "cfg": cfg,
            "provider": provider,
            "max_attempts": max_attempts,
            "retry_backoff_sec": retry_backoff_sec,
        })
        return sentinel, "voice"

    monkeypatch.setattr(factory, "build_provider_tts_backend", fake_build)
    cfg = SimpleNamespace(voice_provider="gemini")

    assert assistant_loudness._build_active_seed_backend(
        cfg,
        max_attempts=0,
        retry_backoff_sec=-1.0,
    ) is sentinel
    assert observed == {
        "cfg": cfg,
        "provider": "gemini",
        "max_attempts": 0,
        "retry_backoff_sec": -1.0,
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


# 0 Hz is DC; 9 kHz sits just under the 24 kHz source's usable band.
@pytest.mark.parametrize("hz", [0.0, 200.0, 1000.0, 3400.0, 9000.0])
def test_upsample_2x_reconstructs_the_band_limited_source(hz):
    """The invented samples land on the source waveform, not beside it.

    A linear-phase interpolator passes the source through on one output
    phase and invents the samples between them. Both halves are checked
    here, without scipy, because a streambox has no scipy to fall back
    on: zero-order hold, linear interpolation or a dead filter phase all
    reproduce the source samples correctly and only get the in-between
    samples wrong.
    """
    np = pytest.importorskip("numpy")
    n, amplitude = 2400, 8000.0
    sample = np.arange(n)
    source = amplitude * np.cos(2.0 * np.pi * hz * sample / 24_000.0)

    out = upsample_2x(source)

    assert out.shape == (2 * n,)
    # Skip the FIR's edge transient at each end.
    interior = slice(64, n - 64)
    midpoints = amplitude * np.cos(
        2.0 * np.pi * hz * (sample + 0.5) / 24_000.0
    )
    assert np.max(np.abs(out[0::2][interior] - source[interior])) <= 5e-3 * amplitude
    assert np.max(np.abs(out[1::2][interior] - midpoints[interior])) <= 5e-3 * amplitude


def test_upsample_2x_accepts_degenerate_inputs():
    np = pytest.importorskip("numpy")

    assert upsample_2x(np.zeros(0)).size == 0
    assert upsample_2x(np.array([1.0])).size == 2
