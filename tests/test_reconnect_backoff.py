"""Reconnect backoff schedule — tests don't need the Gemini SDK so
they live here rather than in test_gemini_connection.py (which is
all skipped without google-genai installed)."""
from __future__ import annotations

import pytest


def _import_func():
    """Import lazily so a missing google-genai install doesn't error
    out at collection time. The file under test imports `genai` at
    module level, but only the constants + helper we care about
    don't depend on it being importable."""
    try:
        from jasper.voice.gemini_session import (
            RECONNECT_INITIAL_BACKOFF_SEC,
            RECONNECT_MAX_BACKOFF_SEC,
            _reconnect_backoff_delay,
        )
    except ImportError as e:
        pytest.skip(f"gemini_session import failed (likely no google-genai): {e}")
    return RECONNECT_INITIAL_BACKOFF_SEC, RECONNECT_MAX_BACKOFF_SEC, _reconnect_backoff_delay


def test_first_attempt_is_around_initial():
    init, _, fn = _import_func()
    # Sample a handful of attempts; jitter is ±25%, so attempt 1 must
    # land in [0.75 * init, 1.25 * init].
    for _ in range(20):
        d = fn(1)
        assert 0.75 * init <= d <= 1.25 * init


def test_doubles_per_attempt_until_cap():
    """Without jitter the schedule doubles. With ±25% jitter the
    expected midpoint still doubles up to the cap; verify each
    attempt's delay sits in the right band."""
    init, cap, fn = _import_func()
    # attempt → expected base (without jitter, capped)
    bands = [
        (1, init),       # ~1
        (2, init * 2),   # ~2
        (3, init * 4),   # ~4
        (4, init * 8),   # ~8
        (5, init * 16),  # ~16
        (6, init * 32),  # ~32
        (7, cap),        # capped at 60
        (8, cap),
        (12, cap),
    ]
    for attempt, base in bands:
        for _ in range(20):
            d = fn(attempt)
            assert 0.75 * base <= d <= 1.25 * base, (
                f"attempt={attempt} delay={d:.2f} outside ±25% of {base}"
            )


def test_caps_at_max_backoff():
    """For very high attempt numbers, the delay must remain bounded
    by RECONNECT_MAX_BACKOFF_SEC * 1.25 (jitter ceiling)."""
    _, cap, fn = _import_func()
    for attempt in (100, 1000, 10_000):
        for _ in range(10):
            assert fn(attempt) <= cap * 1.25
