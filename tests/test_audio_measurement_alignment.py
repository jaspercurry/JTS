# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the alignment-confidence gate.

An integrity hash proves a capture is intact; it cannot catch an
intact-but-misaligned capture (stimulus buried in noise / absent). These tests
prove the cross-correlation gate locates a clean stimulus confidently and fails
loud on a weak/ambiguous one — but only when the caller requires alignment.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement import alignment


def _stimulus(rng, n=4096):
    # A broadband stimulus stands in for a swept sine (strongly auto-correlated).
    return rng.standard_normal(n).astype(np.float64)


def test_clean_capture_aligns_confidently_at_the_right_lag():
    rng = np.random.default_rng(1)
    stim = _stimulus(rng)
    lag = 1500
    captured = np.zeros(lag + stim.size + 2000, dtype=np.float64)
    captured[lag : lag + stim.size] = stim
    captured += 0.01 * rng.standard_normal(captured.size)  # mild noise

    result = alignment.assert_alignment_confident(captured, stim, require=True)
    assert abs(result.lag_samples - lag) <= 2
    assert result.confidence > alignment.DEFAULT_CONFIDENCE_THRESHOLD
    assert result.peak > 0.5  # strong similarity at the dominant lag


def test_noise_only_capture_fails_loud_when_required():
    rng = np.random.default_rng(2)
    stim = _stimulus(rng)
    captured = rng.standard_normal(stim.size + 3000).astype(np.float64)  # no stimulus

    with pytest.raises(alignment.AlignmentError) as ei:
        alignment.assert_alignment_confident(captured, stim, require=True)
    assert ei.value.confidence < alignment.DEFAULT_CONFIDENCE_THRESHOLD


def test_require_false_never_raises():
    rng = np.random.default_rng(3)
    stim = _stimulus(rng)
    captured = rng.standard_normal(stim.size + 3000).astype(np.float64)
    # Same noisy capture, but a level-style kind that does not gate on alignment.
    result = alignment.assert_alignment_confident(captured, stim, require=False)
    assert isinstance(result, alignment.AlignmentResult)  # returned, not raised


def test_empty_signals_are_zero_confidence():
    res = alignment.cross_correlation_alignment(np.array([]), np.array([1.0, 2.0]))
    assert res.confidence == 0.0
    assert res.peak == 0.0


def test_capture_shorter_than_stimulus_is_zero_confidence():
    # A capture that cannot contain the stimulus must not produce a degenerate
    # 1-point "confidence".
    res = alignment.cross_correlation_alignment(np.ones(100), np.ones(1000))
    assert res.confidence == 0.0
    assert res.peak == 0.0


def test_nan_capture_does_not_poison_peak_and_is_gated():
    rng = np.random.default_rng(5)
    stim = _stimulus(rng)
    captured = np.full(stim.size + 1000, np.nan, dtype=np.float64)
    res = alignment.cross_correlation_alignment(captured, stim)
    assert np.isfinite(res.peak)  # N2: NaN input must not yield a NaN peak
    with pytest.raises(alignment.AlignmentError):
        alignment.assert_alignment_confident(captured, stim, require=True)


def test_a_ten_second_sweep_correlates_in_well_under_a_second():
    # B1 regression: an O(N·M) correlator takes minutes on this pair; the
    # budget is ~50x the FFT path's measured cost, so only an algorithm
    # change can breach it.
    import time

    rng = np.random.default_rng(3)
    stim = _stimulus(rng, n=48000)
    captured = np.zeros(10 * 48000, dtype=np.float64)
    captured[20000 : 20000 + stim.size] = stim

    start = time.monotonic()
    corr = alignment.correlation(captured, stim)
    elapsed = time.monotonic() - start

    assert corr.size > 0
    assert elapsed < 10.0


def test_capture_is_truncated_before_float64_normalization(monkeypatch):
    normalized_sizes: list[int] = []
    real_normalize = alignment._normalize

    def recording_normalize(value):
        normalized_sizes.append(np.asarray(value).size)
        return real_normalize(value)

    monkeypatch.setattr(alignment, "_normalize", recording_normalize)
    alignment.cross_correlation_alignment(
        np.ones(10_000, dtype=np.int16),
        np.ones(100, dtype=np.int16),
        sample_rate=1_000,
        max_capture_s=1.0,
    )

    assert normalized_sizes == [1_000, 100]


def test_threshold_env_knob(monkeypatch):
    # The gate is a deploy-time knob so on-device tuning needs no code change.
    monkeypatch.setenv("JASPER_CAPTURE_ALIGNMENT_THRESHOLD", "0.65")
    assert alignment._env_threshold() == 0.65
    monkeypatch.setenv("JASPER_CAPTURE_ALIGNMENT_THRESHOLD", "1.5")  # out of range
    assert alignment._env_threshold() == 0.40
    monkeypatch.setenv("JASPER_CAPTURE_ALIGNMENT_THRESHOLD", "nonsense")
    assert alignment._env_threshold() == 0.40
    monkeypatch.delenv("JASPER_CAPTURE_ALIGNMENT_THRESHOLD", raising=False)
    assert alignment._env_threshold() == 0.40


def test_threshold_is_honored():
    rng = np.random.default_rng(4)
    stim = _stimulus(rng)
    lag = 800
    captured = np.zeros(lag + stim.size + 1000, dtype=np.float64)
    captured[lag : lag + stim.size] = stim
    captured += 0.2 * rng.standard_normal(captured.size)
    # An absurdly high threshold rejects even this decent capture.
    with pytest.raises(alignment.AlignmentError):
        alignment.assert_alignment_confident(
            captured, stim, require=True, threshold=0.999
        )
    # The same capture passes a sane threshold.
    alignment.assert_alignment_confident(captured, stim, require=True, threshold=0.3)
