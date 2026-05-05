"""Hearing-safety tests for TtsPlayout's gain handling.

The hard MIN/MAX clamp on TtsPlayout.set_gain_db is the load-bearing
defense against accidentally playing TTS at ear-damaging levels. These
tests pin that contract: even if the volume tracker, env config, or
Camilla websocket all misbehave, no caller can push gain above
MAX_TTS_GAIN_DB.

We don't open a real ALSA stream — set_gain_db is pure float math
that lives outside the stream lifecycle.
"""
from __future__ import annotations

import math

import pytest

from jasper.audio_io import TtsPlayout


def _make() -> TtsPlayout:
    """Construct without entering the async context (no ALSA open)."""
    return TtsPlayout(device="dummy", output_rate=48000, gain_db=-8.0)


def test_constructor_clamps_through_set_gain_db():
    """Whatever the env passes, the constructor routes it through the
    same clamp/validate path as runtime updates."""
    p = TtsPlayout(device="dummy", output_rate=48000, gain_db=-8.0)
    assert p.gain_db == -8.0


def test_max_gain_clamp():
    """Even if a future caller passes 0 dB or higher (which the config
    validator should already block), TtsPlayout must clamp to MAX."""
    p = _make()
    p.set_gain_db(0.0)
    assert p.gain_db == TtsPlayout.MAX_TTS_GAIN_DB
    p.set_gain_db(20.0)
    assert p.gain_db == TtsPlayout.MAX_TTS_GAIN_DB
    p.set_gain_db(1000.0)
    assert p.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


def test_min_gain_clamp():
    """Floor exists so 'mute' / unreachable-Camilla can fall to silence
    without integer-underflow into bizarre territory."""
    p = _make()
    p.set_gain_db(-100.0)
    assert p.gain_db == TtsPlayout.MIN_TTS_GAIN_DB
    p.set_gain_db(-1e6)
    assert p.gain_db == TtsPlayout.MIN_TTS_GAIN_DB


def test_in_range_passes_through():
    p = _make()
    p.set_gain_db(-12.5)
    assert p.gain_db == -12.5
    p.set_gain_db(-30.0)
    assert p.gain_db == -30.0


def test_non_finite_inputs_held():
    """NaN / inf must not corrupt gain — hold the prior value."""
    p = _make()
    p.set_gain_db(-15.0)
    p.set_gain_db(float("nan"))
    assert p.gain_db == -15.0
    p.set_gain_db(float("inf"))
    assert p.gain_db == -15.0
    p.set_gain_db(float("-inf"))
    assert p.gain_db == -15.0


def test_garbage_inputs_held():
    p = _make()
    p.set_gain_db(-12.0)
    p.set_gain_db(None)  # type: ignore[arg-type]
    assert p.gain_db == -12.0
    p.set_gain_db("loud")  # type: ignore[arg-type]
    assert p.gain_db == -12.0
    p.set_gain_db([0.0])  # type: ignore[arg-type]
    assert p.gain_db == -12.0


def test_linear_gain_matches_db():
    """Sanity-check the dB → linear conversion at the boundaries."""
    p = _make()
    p.set_gain_db(0.0)  # clamps to -6
    expected = 10 ** (TtsPlayout.MAX_TTS_GAIN_DB / 20.0)
    assert math.isclose(p._gain_linear, expected, rel_tol=1e-9)
    p.set_gain_db(-20.0)
    assert math.isclose(p._gain_linear, 0.1, rel_tol=1e-9)


def test_max_below_zero_dbfs():
    """Sanity: MAX must be <= 0 dB. If someone bumps the constant
    positive, gain math overflows int16 against Gemini's source peaks."""
    assert TtsPlayout.MAX_TTS_GAIN_DB <= 0.0
    assert TtsPlayout.MIN_TTS_GAIN_DB < TtsPlayout.MAX_TTS_GAIN_DB
