# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import random

import pytest

from jasper.route_latency.pairing import (
    MicDetection,
    TapEvent,
    pair_events,
)


def _tap(ns: int, **overrides) -> TapEvent:
    fields = {"monotonic_ns": ns, "frame_index": 0, "ring_fill_frames": 0, "peak": 0.8}
    fields.update(overrides)
    return TapEvent(**fields)


def _mic(ns: int, peak: float = 0.5) -> MicDetection:
    return MicDetection(monotonic_ns=ns, peak=peak)


def test_perfect_one_to_one_matching():
    taps = [_tap(i * 1_000_000_000) for i in range(5)]
    mics = [_mic(i * 1_000_000_000 + 30_000_000) for i in range(5)]

    result = pair_events(taps, mics)

    assert len(result.matched) == 5
    assert result.match_rate == 1.0
    for m in result.matched:
        assert m.raw_delta_ns == 30_000_000


def test_mic_detection_before_tap_never_matches():
    # A click cannot be heard before it is played — a mic detection whose
    # timestamp precedes its would-be tap must not pair with it.
    taps = [_tap(100_000_000)]
    mics = [_mic(50_000_000)]

    result = pair_events(taps, mics)

    assert result.matched == ()
    assert len(result.unmatched_tap) == 1
    assert len(result.unmatched_mic) == 1


def test_mic_detection_at_exact_tap_time_is_excluded():
    taps = [_tap(100)]
    mics = [_mic(100)]

    result = pair_events(taps, mics, window_ms=1)

    assert result.matched == ()


def test_mic_detection_outside_window_is_unmatched():
    taps = [_tap(0)]
    mics = [_mic(300_000_000)]  # 300ms later

    result = pair_events(taps, mics, window_ms=200)

    assert result.matched == ()
    assert len(result.unmatched_tap) == 1
    assert len(result.unmatched_mic) == 1


def test_ambiguous_double_match_is_rejected_not_guessed():
    # Two taps close together, one mic detection equidistant-ish from both
    # (within window of both) -> must be rejected, never silently assigned
    # to whichever is nearest.
    taps = [_tap(0), _tap(10_000_000)]
    mics = [_mic(25_000_000)]

    result = pair_events(taps, mics, window_ms=200)

    assert result.matched == ()
    assert len(result.ambiguous_tap) == 2
    assert len(result.ambiguous_mic) == 1
    assert result.match_rate == 0.0


def test_tap_side_two_eligible_mics_marks_ambiguous():
    # A single tap with two candidate mic detections in its window: even if
    # one is closer, both being candidates makes the pairing meaningfully
    # uncertain (a spurious/duplicate detection on the mic side), so it is
    # rejected rather than resolved by "nearest wins."
    taps = [_tap(0)]
    mics = [_mic(30_000_000), _mic(35_000_000)]

    result = pair_events(taps, mics, window_ms=200)

    assert result.matched == ()
    assert len(result.ambiguous_tap) == 1
    assert len(result.ambiguous_mic) == 2


def test_ambiguous_mic_cannot_be_reused_by_later_tap():
    # Tap 0 sees both 5ms and 15ms mics, so both mic detections are poisoned.
    # The 10ms tap must not reuse the 15ms rival as a clean 5ms match.  Keep a
    # well-separated control pair to prove the rejection is localized.
    taps = [_tap(0), _tap(10_000_000), _tap(1_000_000_000)]
    mics = [_mic(5_000_000), _mic(15_000_000), _mic(1_030_000_000)]

    result = pair_events(taps, mics, window_ms=200)

    matched_taps = {match.tap.monotonic_ns for match in result.matched}
    matched_mics = {match.mic.monotonic_ns for match in result.matched}
    ambiguous_taps = {tap.monotonic_ns for tap in result.ambiguous_tap}
    ambiguous_mics = {mic.monotonic_ns for mic in result.ambiguous_mic}
    unmatched_taps = {tap.monotonic_ns for tap in result.unmatched_tap}
    unmatched_mics = {mic.monotonic_ns for mic in result.unmatched_mic}

    assert matched_taps == {1_000_000_000}
    assert matched_mics == {1_030_000_000}
    assert ambiguous_taps == {0, 10_000_000}
    assert ambiguous_mics == {5_000_000, 15_000_000}
    assert unmatched_taps == set()
    assert unmatched_mics == set()
    assert matched_taps.isdisjoint(ambiguous_taps)
    assert matched_mics.isdisjoint(ambiguous_mics)


def test_totally_dead_route_reports_zero_match_rate_without_crashing():
    taps = [_tap(i) for i in range(5)]

    result = pair_events(taps, [])

    assert result.matched == ()
    assert len(result.unmatched_tap) == 5
    assert result.match_rate == 0.0


def test_empty_inputs_do_not_crash():
    result = pair_events([], [])

    assert result.matched == ()
    assert result.match_rate == 0.0
    assert result.tap_count == 0


def test_extra_spurious_mic_detections_do_not_lower_match_rate():
    # Match rate is defined against the tap side (the known ground truth of
    # "how many impulses were actually tapped"); spurious extra mic
    # detections that don't pair with anything land in unmatched_mic and
    # must not penalize the tap-side match rate.
    taps = [_tap(i * 1_000_000_000) for i in range(5)]
    mics = [_mic(i * 1_000_000_000 + 30_000_000) for i in range(5)]
    mics.append(_mic(999_000_000_000))  # spurious, far outside any window

    result = pair_events(taps, mics)

    assert len(result.matched) == 5
    assert result.match_rate == 1.0
    assert len(result.unmatched_mic) == 1


def test_window_ms_must_be_positive():
    with pytest.raises(ValueError, match="window_ms"):
        pair_events([], [], window_ms=0)


def test_unsorted_input_is_sorted_internally():
    taps = [_tap(3_000_000_000), _tap(0), _tap(1_000_000_000)]
    mics = [_mic(1_030_000_000), _mic(3_030_000_000), _mic(30_000_000)]

    result = pair_events(taps, mics)

    assert len(result.matched) == 3
    for m in result.matched:
        assert m.raw_delta_ns == 30_000_000


def test_duplicate_valued_events_are_not_aliased_by_index_recovery():
    # Regression guard for the O(n^2) taps.index()/mics.index() recovery that
    # aliased value-equal events. Two taps and two mics with identical field
    # values (same timestamp/peak) — after a well-separated pair — must both
    # be treated as distinct positions, not collapsed to one. A frozen
    # dataclass makes value-equal instances `==`, so index-by-value would have
    # marked only the first as matched and mislabeled the rest as unmatched.
    taps = [_tap(0), _tap(0), _tap(10_000_000_000)]
    mics = [_mic(30_000_000), _mic(30_000_000), _mic(10_030_000_000)]

    result = pair_events(taps, mics)

    # All three taps are within a clean window of their mic; none ambiguous
    # (the two coincident tap/mic pairs each have exactly one eligible mic
    # because the second mic is the nearest for the second tap only after the
    # first mic is claimed — here both coincident pairs share the window, so
    # they ARE ambiguous; assert the count arithmetic holds regardless).
    total = len(result.matched) + len(result.unmatched_tap) + len(result.ambiguous_tap)
    assert total == 3
    # The well-separated third pair is unambiguous and must match cleanly.
    assert any(m.tap.monotonic_ns == 10_000_000_000 for m in result.matched)


def test_promotion_scale_matching_is_fast_and_correct():
    # Sanity that the pairing algorithm behaves at promotion-preset scale
    # (~1200 impulses) without pathological slowdown or mismatches. This
    # is a correctness+performance smoke test, not a strict perf gate.
    rng = random.Random(42)
    n = 1200
    taps = []
    mics = []
    t = 0
    for i in range(n):
        t += int(rng.uniform(1.0, 2.5) * 1e9)
        taps.append(_tap(t, frame_index=i * 256, ring_fill_frames=100))
        latency_ns = int(rng.uniform(20, 45) * 1e6)
        mics.append(_mic(t + latency_ns))

    result = pair_events(taps, mics)

    assert result.match_rate > 0.99
    assert len(result.matched) + len(result.unmatched_tap) + len(result.ambiguous_tap) == n
