# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free reconnect backoff schedule."""
from __future__ import annotations

def _import_func():
    from jasper.backoff import (
        RECONNECT_INITIAL_BACKOFF_SEC,
        RECONNECT_MAX_BACKOFF_SEC,
        reconnect_backoff_delay,
    )

    return (
        RECONNECT_INITIAL_BACKOFF_SEC,
        RECONNECT_MAX_BACKOFF_SEC,
        reconnect_backoff_delay,
    )


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


def test_terminal_failure_polls_on_the_slow_interval():
    """A failure retrying cannot fix drops to the slow poll.

    The classification, not the attempt number, picks the schedule —
    so a terminal outage never climbs back onto the exponential ramp
    however long it lasts. The interval is pinned outright: an edit
    that leaves it merely above the 60 s cap would still hammer the
    provider all day. See issue #3855."""
    from jasper.backoff import (
        RECONNECT_BACKOFF_JITTER_FRACTION,
        RECONNECT_MAX_BACKOFF_SEC,
        TERMINAL_POLL_INTERVAL_SEC,
        reconnect_delay,
    )

    assert TERMINAL_POLL_INTERVAL_SEC == 900.0
    lo = TERMINAL_POLL_INTERVAL_SEC * (1.0 - RECONNECT_BACKOFF_JITTER_FRACTION)
    hi = TERMINAL_POLL_INTERVAL_SEC * (1.0 + RECONNECT_BACKOFF_JITTER_FRACTION)
    seen = set()
    for attempt in (1, 2, 7, 1000):
        for _ in range(20):
            delay = reconnect_delay(attempt, transient=False)
            assert lo <= delay <= hi
            seen.add(delay)
        assert reconnect_delay(attempt, transient=True) <= (
            RECONNECT_MAX_BACKOFF_SEC * 1.25
        )
    # Jittered, so speakers sharing one key never poll in lockstep.
    assert len(seen) > 1


def test_reconnect_nudge_admits_one_early_retry_per_cap_window():
    """A wake word may shorten a backoff wait, but repeated wakes must
    not retry the provider faster than an ordinary blip already does."""
    from jasper.backoff import RECONNECT_MAX_BACKOFF_SEC, ReconnectNudge

    now = [1000.0]
    nudge = ReconnectNudge(clock=lambda: now[0])

    assert nudge.allow() is True
    assert nudge.allow() is False
    now[0] += RECONNECT_MAX_BACKOFF_SEC - 0.01
    assert nudge.allow() is False
    now[0] += 0.02
    assert nudge.allow() is True
