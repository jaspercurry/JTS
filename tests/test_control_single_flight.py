# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.control.single_flight.SingleFlightTTLCache."""
from __future__ import annotations

import threading

import pytest

from jasper.control.single_flight import SingleFlightTTLCache


def test_single_flight_cache_recomputes_after_ttl_expiry():
    """Within the TTL the cached value is reused; once it expires the
    next caller recomputes. Uses an injected clock so the assertion is
    deterministic, not wall-clock timed."""
    now = {"t": 1000.0}
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    cache = SingleFlightTTLCache(
        ttl_sec=1.0, wait_timeout_sec=60.0, clock=lambda: now["t"],
    )

    assert cache.get_or_compute(compute) == 1
    now["t"] = 1000.9  # still inside the 1 s TTL -> served from cache
    assert cache.get_or_compute(compute) == 1
    assert calls["n"] == 1
    now["t"] = 1001.1  # TTL elapsed -> recompute
    assert cache.get_or_compute(compute) == 2
    assert calls["n"] == 2


def test_single_flight_cache_does_not_cache_failures():
    """A raising compute propagates, is not cached, and clears the
    in-flight flag so the next caller retries cleanly rather than
    inheriting a stuck in-flight state."""
    cache = SingleFlightTTLCache(ttl_sec=60.0, wait_timeout_sec=60.0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return "ok"

    with pytest.raises(RuntimeError, match="boom"):
        cache.get_or_compute(flaky)
    # Failure not cached + in-flight released -> retry succeeds.
    assert cache.get_or_compute(flaky) == "ok"
    assert calls["n"] == 2


def test_single_flight_cache_waiter_gives_up_on_a_wedged_compute():
    """A waiter must not park past the compute's budget.

    It serves the last value instead, because the alternative is holding a
    bounded request worker — and, with enough waiters, the control plane —
    on one compute that overran. Retire with the cache's wait timeout.
    """
    cache = SingleFlightTTLCache(ttl_sec=0.0, wait_timeout_sec=0.05)
    assert cache.get_or_compute(lambda: "first") == "first"

    entered = threading.Event()
    release = threading.Event()

    def wedged():
        entered.set()
        release.wait(timeout=30)
        return "second"

    thread = threading.Thread(
        target=lambda: cache.get_or_compute(wedged), daemon=True,
    )
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert cache.get_or_compute(lambda: "never computed") == "first"
    finally:
        release.set()
        thread.join(timeout=5)
