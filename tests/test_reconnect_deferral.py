"""Unit tests for `DeferredReconnect` — the shared mid-turn reconnect-deferral
primitive in `jasper.voice._supervisor`.

It is provider-agnostic (no genai/openai import), so its tests live in this
un-gated module rather than under the genai-skipped provider test files."""
from __future__ import annotations

from jasper.voice._supervisor import DeferredReconnect


def test_deferred_reconnect_request_clear_pending():
    """request() marks pending; clear() drops it. Starts not-pending."""
    d = DeferredReconnect()
    assert d.pending is False
    d.request()
    assert d.pending is True
    d.clear()
    assert d.pending is False


def test_deferred_reconnect_fire_if_pending_fires_once_and_clears():
    """When pending, fire_if_pending() calls the fire callback exactly
    once, clears the flag, and returns True. A second call with no new
    request is a no-op returning False — the mechanism that keeps a
    later turn release from firing a spurious second reconnect."""
    d = DeferredReconnect()
    calls: list[int] = []
    d.request()
    assert d.fire_if_pending(lambda: calls.append(1)) is True
    assert calls == [1]
    assert d.pending is False
    # No pending request remains → no second fire.
    assert d.fire_if_pending(lambda: calls.append(1)) is False
    assert calls == [1]


def test_deferred_reconnect_fire_if_pending_noop_when_not_pending():
    """With nothing deferred, fire_if_pending() must not call fire and
    must return False (the common path on every turn release)."""
    d = DeferredReconnect()
    called = False

    def _fire() -> None:
        nonlocal called
        called = True

    assert d.fire_if_pending(_fire) is False
    assert called is False
