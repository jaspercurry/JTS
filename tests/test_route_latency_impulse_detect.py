# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.route_latency.impulse_detect import (
    StreamingDetector,
    refractory_samples_for,
)


def _silence(n: int) -> list[int]:
    return [0] * n


def _impulse(n: int, peak: int) -> list[int]:
    return [peak] * n


def test_detects_single_impulse_at_correct_offset():
    detector = StreamingDetector(threshold=0.2, hysteresis=0.05, refractory_samples=0)
    buffer = _silence(10) + _impulse(1, 20000) + _silence(10)

    detections = detector.feed(buffer)

    assert len(detections) == 1
    assert detections[0].sample_offset == 10


def test_below_threshold_never_fires():
    detector = StreamingDetector(threshold=0.5, hysteresis=0.05, refractory_samples=0)
    buffer = _impulse(50, 10000)  # well below 0.5 * 32768

    detections = detector.feed(buffer)

    assert detections == []


def test_refractory_window_suppresses_immediate_re_fire():
    detector = StreamingDetector(threshold=0.2, hysteresis=0.05, refractory_samples=100)
    # Two impulses back-to-back within the refractory window.
    buffer = _impulse(1, 20000) + _silence(5) + _impulse(1, 20000) + _silence(200)

    detections = detector.feed(buffer)

    assert len(detections) == 1


def test_rearm_after_refractory_and_hysteresis_allows_second_detection():
    detector = StreamingDetector(threshold=0.2, hysteresis=0.05, refractory_samples=10)
    # First impulse, then silence long enough to clear both refractory and
    # hysteresis, then a second impulse.
    buffer = (
        _impulse(1, 20000)
        + _silence(50)
        + _impulse(1, 20000)
    )

    detections = detector.feed(buffer)

    assert len(detections) == 2


def test_slow_decay_through_threshold_band_does_not_double_fire():
    # A signal that decays slowly through the threshold value multiple
    # times without dropping below threshold-hysteresis must not produce
    # multiple detections — this is exactly what hysteresis is for.
    detector = StreamingDetector(threshold=0.2, hysteresis=0.1, refractory_samples=0)
    peak = round(0.2 * 32768)
    # Oscillate right around the threshold without ever dropping below
    # threshold - hysteresis (0.1 * 32768).
    buffer = [peak, peak - 5, peak + 3, peak - 2, peak + 1, peak - 4]

    detections = detector.feed(buffer)

    assert len(detections) == 1


def test_negative_samples_use_absolute_value():
    detector = StreamingDetector(threshold=0.2, hysteresis=0.05, refractory_samples=0)
    buffer = _silence(5) + [-20000] + _silence(5)

    detections = detector.feed(buffer)

    assert len(detections) == 1
    assert detections[0].peak == pytest.approx(20000 / 32768.0)


def test_feed_across_multiple_buffers_preserves_refractory_state():
    # The detector must be genuinely stateful across feed() calls — a
    # refractory window started in one chunk must still suppress a
    # detection in the very next chunk (buffers arrive one UDP packet /
    # ALSA period at a time in real use, never as one giant array).
    detector = StreamingDetector(threshold=0.2, hysteresis=0.05, refractory_samples=20)

    first = detector.feed(_impulse(1, 20000))
    assert len(first) == 1

    # Still within the refractory window in a NEW buffer.
    second = detector.feed(_impulse(5, 20000))
    assert second == []

    # Long silence clears both refractory and hysteresis...
    detector.feed(_silence(50))
    # ...so a third impulse in yet another buffer now fires.
    third = detector.feed(_impulse(1, 20000))
    assert len(third) == 1


def test_multiple_impulses_in_one_buffer_all_detected_when_spaced_out():
    detector = StreamingDetector(threshold=0.2, hysteresis=0.05, refractory_samples=5)
    buffer = (
        _impulse(1, 20000)
        + _silence(20)
        + _impulse(1, 20000)
        + _silence(20)
        + _impulse(1, 20000)
    )

    detections = detector.feed(buffer)

    assert len(detections) == 3
    assert [d.sample_offset for d in detections] == [0, 21, 42]


@pytest.mark.parametrize(
    "threshold,hysteresis,refractory_samples,expectation",
    [
        (0.0, 0.0, 0, "threshold"),
        (1.5, 0.0, 0, "threshold"),
        (0.2, 0.2, 0, "hysteresis"),
        (0.2, 0.3, 0, "hysteresis"),
        (0.2, 0.05, -1, "refractory_samples"),
    ],
)
def test_invalid_construction_raises(threshold, hysteresis, refractory_samples, expectation):
    with pytest.raises(ValueError, match=expectation):
        StreamingDetector(
            threshold=threshold,
            hysteresis=hysteresis,
            refractory_samples=refractory_samples,
        )


def test_refractory_samples_for_converts_ms_to_samples():
    assert refractory_samples_for(250.0, 48_000) == 12_000
    assert refractory_samples_for(250.0, 16_000) == 4_000
    assert refractory_samples_for(80.0, 16_000) == 1_280


def test_refractory_samples_for_rejects_invalid_inputs():
    with pytest.raises(ValueError, match="refractory_ms"):
        refractory_samples_for(-1.0, 48_000)
    with pytest.raises(ValueError, match="sample_rate_hz"):
        refractory_samples_for(250.0, 0)
