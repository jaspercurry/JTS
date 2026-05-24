"""Tap counter state-machine coverage for the HID accessory bridge.

We exercise `_TapCounter` directly with a fake httpx client so the
test runs in milliseconds (real-time `asyncio.sleep` calls aside,
which we trim by giving the action a small window_ms).

The shape of `_TapCounter` matters: the bridge daemon depends on it
to translate every VK-01 click into the right transport action, and
the timing semantics (defer-on-single, immediate-on-triple) are the
whole point of the gesture.
"""
from __future__ import annotations

import asyncio
from typing import List

import httpx
import pytest

from jasper.accessories.bridge import _TapCounter
from jasper.accessories.registry import KeyAction, TapAction


# Window short enough that tests finish quickly but long enough that
# the asyncio scheduler can resolve event ordering deterministically.
WINDOW_MS = 30
WINDOW_SEC = WINDOW_MS / 1000.0


def _make_counter(client: httpx.AsyncClient) -> tuple[_TapCounter, List[str]]:
    """Return a counter + the list its dispatches will append paths to."""
    action = TapAction(
        on_single=KeyAction("POST", "/transport/toggle", {}),
        on_double=KeyAction("POST", "/transport/next", {}),
        on_triple=KeyAction("POST", "/transport/previous", {}),
        window_ms=WINDOW_MS,
    )
    tc = _TapCounter(
        client=client,
        control_url="http://127.0.0.1:1",  # mock transport ignores
        action=action,
        device_name="fake",
        key_name="KEY_FAKE",
    )
    return tc, []


def _client_recording(calls: List[str]) -> httpx.AsyncClient:
    """An AsyncClient that records every request's path into `calls`."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"result": "ok"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_single_tap_commits_toggle_after_window():
    calls: List[str] = []
    async with _client_recording(calls) as client:
        tc, _ = _make_counter(client)
        tc.hit()
        # Before window expires: nothing has fired.
        await asyncio.sleep(WINDOW_SEC / 2)
        assert calls == []
        # After window: toggle.
        await asyncio.sleep(WINDOW_SEC)
        assert calls == ["/transport/toggle"]


@pytest.mark.asyncio
async def test_double_tap_commits_next_after_window():
    calls: List[str] = []
    async with _client_recording(calls) as client:
        tc, _ = _make_counter(client)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC / 4)
        tc.hit()
        # Still no fire — the second tap reset the timer, not committed yet.
        assert calls == []
        await asyncio.sleep(WINDOW_SEC * 2)
        assert calls == ["/transport/next"]


@pytest.mark.asyncio
async def test_triple_tap_commits_previous_immediately():
    """The third tap shouldn't wait another window — we know it's a
    triple at the moment it arrives because there's no quadruple-tap
    semantic. This is the whole point of "fire on count=3"."""
    calls: List[str] = []
    async with _client_recording(calls) as client:
        tc, _ = _make_counter(client)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC / 4)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC / 4)
        tc.hit()
        # One event loop tick for the immediate task to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Previous should fire immediately, not after the window.
        assert calls == ["/transport/previous"]
        # And no spurious additional fire when the window would have expired.
        await asyncio.sleep(WINDOW_SEC * 2)
        assert calls == ["/transport/previous"]


@pytest.mark.asyncio
async def test_slow_double_tap_fires_two_toggles():
    """If the user double-taps but spaces them past the window, each
    tap commits independently as a play/pause toggle. Acceptable —
    the alternative is making the window arbitrarily long."""
    calls: List[str] = []
    async with _client_recording(calls) as client:
        tc, _ = _make_counter(client)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        assert calls == ["/transport/toggle", "/transport/toggle"]


@pytest.mark.asyncio
async def test_sequential_independent_sequences():
    """After a sequence commits, a fresh tap starts a new sequence
    from count=1. The counter must reset fully between gestures."""
    calls: List[str] = []
    async with _client_recording(calls) as client:
        tc, _ = _make_counter(client)
        # Double-tap.
        tc.hit()
        await asyncio.sleep(WINDOW_SEC / 4)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        # Single tap.
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        assert calls == ["/transport/next", "/transport/toggle"]


@pytest.mark.asyncio
async def test_quadruple_fires_previous_then_starts_new_sequence():
    """Four rapid taps: the first three fire previous immediately
    (count=3 is unambiguous), the fourth starts a new sequence at
    count=1 and commits toggle after the window."""
    calls: List[str] = []
    async with _client_recording(calls) as client:
        tc, _ = _make_counter(client)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC / 4)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC / 4)
        tc.hit()
        # Yield so the immediate-fire task runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == ["/transport/previous"]
        # Fourth tap, tightly after.
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        assert calls == ["/transport/previous", "/transport/toggle"]


@pytest.mark.asyncio
async def test_unmapped_tap_count_is_silent():
    """If a TapAction has no on_double, double-tap should silently
    no-op rather than fall through to single or triple."""
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        action = TapAction(
            on_single=KeyAction("POST", "/foo", {}),
            on_double=None,
            on_triple=KeyAction("POST", "/baz", {}),
            window_ms=WINDOW_MS,
        )
        tc = _TapCounter(client, "http://x", action, "fake", "KEY_FAKE")
        tc.hit()
        await asyncio.sleep(WINDOW_SEC / 4)
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        assert calls == []  # no on_double mapping → silent drop


@pytest.mark.asyncio
async def test_http_error_does_not_break_subsequent_taps():
    """If one dispatch fails (HTTP error from jasper-control), the
    counter must keep working for later taps. We've been bitten by
    "one failure poisons the supervisor" in async code before."""
    calls: List[str] = []
    state = {"fail_next": True}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if state["fail_next"]:
            state["fail_next"] = False
            raise httpx.ConnectError("simulated")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        action = TapAction(
            on_single=KeyAction("POST", "/transport/toggle", {}),
            window_ms=WINDOW_MS,
        )
        tc = _TapCounter(client, "http://x", action, "fake", "KEY_FAKE")
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        # First fired and was caught — call recorded, no exception.
        assert calls == ["/transport/toggle"]
        # Second works.
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
        assert calls == ["/transport/toggle", "/transport/toggle"]
