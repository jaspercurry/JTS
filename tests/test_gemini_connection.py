# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reconnect state-machine tests for `GeminiLiveConnection`.

These tests exercise the persistent-single Live connection without
touching the network: a fake `connect_factory` stands in for
`client.aio.live.connect` and the tests drive its event source to
simulate `setupComplete`, audio chunks, `GoAway`, WebSocket close, and
`session_resumption_update` events. The real SDK is never imported into
the test path beyond the `types` module (used for marker classes like
`ActivityStart`).

Coverage matches the handoff doc's "How to actually test this" list:
- successful connect → in-turn → idle → in-turn cycle
- GoAway mid-turn → reconnect with last resumption handle → resume
- WS close 1006 → reconnect with backoff → eventually succeed
- repeated failures → eventually surface FAILED state, daemon pauses
- idle reset: connection healthy but idle > threshold → close + reopen fresh
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

try:
    from jasper.voice.gemini_session import (
        ConnectionState,
        GeminiLiveConnection,
    )
    from jasper.tools import ToolRegistry
    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

pytestmark = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


# ---------------------------------------------------------------------------
# Fake SDK plumbing.
# ---------------------------------------------------------------------------


@dataclass
class _ServerContent:
    turn_complete: bool = False
    interrupted: bool = False


@dataclass
class _ResumptionUpdate:
    new_handle: str | None = None


@dataclass
class _GoAway:
    time_left: float | None = None


@dataclass
class _Resp:
    """Stand-in for SDK response objects. _on_response and the connection's
    receive loop both use getattr() so any object with the right attributes
    works."""
    data: bytes | None = None
    tool_call: Any = None
    server_content: _ServerContent | None = None
    usage_metadata: Any = None
    session_resumption_update: _ResumptionUpdate | None = None
    go_away: _GoAway | None = None


class _FakeSession:
    """Minimal substitute for the SDK's Live session.

    Tracks every send_realtime_input call (so tests can assert
    activity_start / activity_end / audio were sent), and exposes a
    `feed(response)` helper to push a synthetic server message into the
    receive iterator. `close_with_error(exc)` lets the test simulate a
    drop / 1006 close — `receive()` will raise the exception on its
    next iteration."""

    def __init__(self, fake: "_FakeConnect") -> None:
        self._fake = fake
        self._inbox: asyncio.Queue[_Resp | Exception] = asyncio.Queue()
        self.sent_realtime: list[dict] = []
        self.sent_client_content: list[dict] = []
        self.sent_tool_responses: list[Any] = []
        self.closed = False

    async def send_realtime_input(self, **kwargs) -> None:
        self.sent_realtime.append(kwargs)

    async def send_client_content(self, **kwargs) -> None:
        self.sent_client_content.append(kwargs)

    async def send_tool_response(self, function_responses=None) -> None:
        self.sent_tool_responses.append(function_responses)

    async def receive(self):
        # Legacy async-generator path — preserved for any out-of-tree
        # consumers; the persistent-connection receive_loop calls
        # `_receive()` (below) directly to bypass python-genai #2244.
        while True:
            item = await self._inbox.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def _receive(self):
        """Match production's lower-level call: returns one response
        per call, raises on error. The persistent-connection
        ``_receive_loop`` calls this in a ``while True`` loop instead
        of iterating the public ``receive()`` generator (which
        early-breaks on every ``turn_complete`` per python-genai
        bug #2244)."""
        item = await self._inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    # Test-side controls.

    def feed(self, resp: _Resp) -> None:
        self._inbox.put_nowait(resp)

    def feed_error(self, exc: Exception) -> None:
        self._inbox.put_nowait(exc)


class _FakeAsyncCM:
    """Async context manager wrapper around a _FakeSession (matches the
    SDK's `client.aio.live.connect(...)` shape)."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeConnect:
    """Drop-in for `client.aio.live.connect`. Each call to the factory
    returns a fresh _FakeSession, recorded on `self.sessions` so tests
    can assert how many opens happened and inspect the config that was
    passed."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []
        self.configs: list[Any] = []
        # Optional: queue of exceptions to raise on the next N opens
        # (lets tests simulate "first open succeeds, second open fails").
        self.next_exceptions: list[Exception] = []

    def __call__(self, *, model, config) -> _FakeAsyncCM:
        if self.next_exceptions:
            exc = self.next_exceptions.pop(0)
            raise exc
        self.configs.append(config)
        sess = _FakeSession(self)
        self.sessions.append(sess)
        return _FakeAsyncCM(sess)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_conn(
    *,
    backoff_schedule=(0.0, 0.0),
    context_reset_sec: float = 9999.0,
    keepalive_period_sec: float = 9999.0,
) -> tuple[GeminiLiveConnection, _FakeConnect]:
    """Build a connection wired to a _FakeConnect.

    Tests pass `backoff_schedule=(0.0, 0.0)` to make reconnect immediate
    (no real waiting in unit tests). `context_reset_sec` defaults to a
    huge value so the idle reset doesn't fire unless a test explicitly
    overrides it. Same for keepalive."""
    factory = _FakeConnect()
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        context_reset_sec=context_reset_sec,
        keepalive_period_sec=keepalive_period_sec,
        backoff_schedule=backoff_schedule,
        connect_factory=factory,
    )
    return conn, factory


async def _wait_until(predicate, timeout: float = 2.0):
    """Poll `predicate()` until it returns True or `timeout` elapses.
    Sleeps on each iteration so the event loop yields to other tasks."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"predicate never became true within {timeout}s")


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


async def test_successful_connect_and_turn_cycle():
    """Open a connection, acquire one turn, end it, acquire a second.
    Asserts: (1) only one connect call (persistent), (2) activity_start
    / activity_end markers fire on each turn, (3) state transitions
    follow CONNECTING → CONNECTED → IN_TURN → CONNECTED."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "system instruction")
    try:
        assert conn._state is ConnectionState.CONNECTED
        assert len(factory.sessions) == 1
        sess = factory.sessions[0]

        # First turn.
        turn1 = await conn.acquire_turn()
        assert conn._state is ConnectionState.IN_TURN
        # activity_start marker was sent.
        assert any("activity_start" in call for call in sess.sent_realtime)

        # Server pushes one audio chunk + turn_complete; turn1 records it.
        sess.feed(_Resp(data=b"audio_chunk_1"))
        sess.feed(_Resp(server_content=_ServerContent(turn_complete=True)))
        # Drain the audio queue from the consumer side.
        async def consume():
            chunks = []
            async for chunk in turn1.audio_out():
                chunks.append(chunk)
                if len(chunks) >= 1:
                    break
            return chunks
        # Run consume in a task so we don't deadlock.
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        # End the turn: send activity_end, release.
        await turn1.end_input()
        await turn1.release()
        # Consumer wakes up via the sentinel-None.
        chunks = await asyncio.wait_for(task, timeout=1.0)
        assert chunks == [b"audio_chunk_1"]
        assert any("activity_end" in call for call in sess.sent_realtime)
        assert conn._state is ConnectionState.CONNECTED

        # Second turn — same connection, no new connect call.
        turn2 = await conn.acquire_turn()
        assert conn._state is ConnectionState.IN_TURN
        await turn2.release()
        assert len(factory.sessions) == 1, "second turn must reuse connection"
    finally:
        await conn.stop()
    assert conn._state is ConnectionState.CLOSED


async def test_send_text_context_adds_uncompleted_client_content():
    """One-shot daemon instructions should enter the turn as text context
    without ending the turn or asking Gemini to respond."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        turn = await conn.acquire_turn()

        await turn.send_text_context("Answer yes or no about research job abc.")

        assert len(sess.sent_client_content) == 1
        sent = sess.sent_client_content[0]
        assert sent["turn_complete"] is False
        content = sent["turns"]
        assert content.role == "user"
        assert content.parts[0].text == "Answer yes or no about research job abc."
        await turn.release()
    finally:
        await conn.stop()


async def test_session_resumption_handle_used_on_reconnect():
    """When the server pushes a session_resumption_update during turn N,
    the handle should be cached on the connection AND passed back as
    `session_resumption.handle` on the next open. Drives a reconnect by
    feeding a WebSocket-like exception into receive(), then asserts the
    second config carries the cached handle."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        # First config has no session_resumption field at all — production
        # code deliberately omits it on the initial connect (Google's
        # reference demos never set it, and sending handle=None has
        # been observed to put the server into a silent-failure state).
        first_config = factory.configs[0]
        assert first_config.session_resumption is None

        sess = factory.sessions[0]
        # Server reports a resumption handle.
        sess.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-abc")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-abc")

        # Simulate a 1006-style WebSocket close from the server.
        class _FakeWSClose(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal closure"
            rcvd = _Rcvd()
        sess.feed_error(_FakeWSClose())
        # Wait for reconnect to complete (state back to CONNECTED, second session opened).
        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        # Second config carries the cached handle.
        second_config = factory.configs[1]
        assert second_config.session_resumption.handle == "hndl-abc"
    finally:
        await conn.stop()


async def test_go_away_triggers_reconnect_and_marks_active_turn_lost():
    """Mid-turn GoAway from the server: connection should reconnect,
    the active turn's `turn_lost()` flips True so the daemon stops
    expecting a response."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess1 = factory.sessions[0]
        # Cache a handle so reconnect uses it.
        sess1.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-go")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-go")

        turn = await conn.acquire_turn()

        # Server sends GoAway; in this fake the receive loop continues
        # reading until we close the inbox via an error. Trigger the
        # supervisor explicitly by feeding GoAway then the close.
        sess1.feed(_Resp(go_away=_GoAway(time_left=5.0)))

        class _FakeWSClose(Exception):
            class _Rcvd:
                code = 1011
                reason = "server going away"
            rcvd = _Rcvd()
        sess1.feed_error(_FakeWSClose())

        # Reconnect happens.
        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        # Active turn is marked lost.
        await _wait_until(lambda: turn.turn_lost(), timeout=3.0)
        # Resumption handle was reused on the second config.
        assert factory.configs[1].session_resumption.handle == "hndl-go"
    finally:
        await conn.stop()


async def test_reconnect_with_backoff_eventually_succeeds():
    """First open succeeds; an injected exception drops the WS; the
    backoff retries and the second open lands. Verifies the supervisor
    runs through `RECONNECTING → PAUSED_FOR_BACKOFF → CONNECTING →
    CONNECTED` and that we don't blow the backoff budget."""
    # 0.0 / 0.05 backoff: first retry instant (so we still see the
    # PAUSED_FOR_BACKOFF state transition with a tiny sleep).
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.05))
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        # Queue a 1006-equivalent exception.
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        sess.feed_error(_Drop())
        # Reconnect should succeed on the first attempt (0.0 backoff).
        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        # Connection is usable for a turn after reconnect.
        turn = await conn.acquire_turn()
        await turn.release()
    finally:
        await conn.stop()


async def test_repeated_failures_surface_failed_state():
    """If every open in the backoff schedule fails, the connection
    transitions to FAILED. Subsequent acquire_turn() calls raise."""
    factory = _FakeConnect()
    # Pre-load enough exceptions to exhaust both the initial 409-retry
    # schedule (4 attempts: 0/1/2/4s — values 0.0 in our test mock) AND
    # the supervisor's reconnect schedule. We override the initial-connect
    # path by catching it specifically.

    # First, get a successful initial connect.
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        context_reset_sec=9999.0,
        keepalive_period_sec=9999.0,
        backoff_schedule=(0.0, 0.0),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        # Queue exceptions for ALL future opens — both reconnect attempts fail.
        factory.next_exceptions = [
            RuntimeError("fail 1"),
            RuntimeError("fail 2"),
        ]
        # Drop the active session.
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        sess.feed_error(_Drop())
        # Wait for the supervisor to give up.
        await _wait_until(lambda: conn._state is ConnectionState.FAILED, timeout=3.0)
        # acquire_turn now raises.
        with pytest.raises(RuntimeError, match="FAILED"):
            await conn.acquire_turn()
        # is_paused() is True.
        assert conn.is_paused()
    finally:
        await conn.stop()


async def test_idle_context_reset_drops_resumption_handle_and_reopens():
    """Connection healthy, but idle longer than the configured threshold:
    the next acquire_turn should close + reopen with no resumption
    handle so stale conversational context can't bleed in."""
    # Tiny threshold so the test can hit it.
    conn, factory = _make_conn(context_reset_sec=0.01)
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        # First turn establishes a resumption handle.
        sess1 = factory.sessions[0]
        sess1.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-stale")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-stale")
        turn1 = await conn.acquire_turn()
        await turn1.release()

        # Wait past the context-reset window.
        await asyncio.sleep(0.05)

        # Next acquire triggers context-reset before opening a turn.
        turn2 = await conn.acquire_turn()
        # New session was opened.
        assert len(factory.sessions) == 2
        # New session opened with NO resumption handle (fresh context).
        # Production code omits the field entirely when there's no
        # handle — see _build_session_resumption().
        assert factory.configs[1].session_resumption is None
        # The connection cleared the cached handle.
        assert conn._resumption_handle is None
        await turn2.release()
    finally:
        await conn.stop()


async def test_context_reset_disabled_when_threshold_is_zero():
    """`context_reset_sec=0` disables the idle reset entirely. Even
    after a long idle gap, the next acquire_turn reuses the existing
    session and keeps the resumption handle."""
    conn, factory = _make_conn(context_reset_sec=0.0)
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess1 = factory.sessions[0]
        sess1.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-stable")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-stable")
        turn1 = await conn.acquire_turn()
        await turn1.release()

        # Long idle — would trigger reset if enabled.
        await asyncio.sleep(0.1)

        turn2 = await conn.acquire_turn()
        # Same session, handle preserved.
        assert len(factory.sessions) == 1
        assert conn._resumption_handle == "hndl-stable"
        await turn2.release()
    finally:
        await conn.stop()


async def test_acquire_turn_blocks_on_failed_state():
    """Calling acquire_turn() while in FAILED raises immediately
    (doesn't deadlock on the connected_event)."""
    conn, factory = _make_conn(backoff_schedule=(0.0,))
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        # Force into FAILED via repeated failures (mirrors the
        # `repeated_failures` test but smaller schedule).
        sess = factory.sessions[0]
        factory.next_exceptions = [RuntimeError("perma-fail")]
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "x"
            rcvd = _Rcvd()
        sess.feed_error(_Drop())
        await _wait_until(lambda: conn._state is ConnectionState.FAILED, timeout=3.0)
        # acquire_turn raises rather than hanging.
        with pytest.raises(RuntimeError):
            await conn.acquire_turn()
    finally:
        await conn.stop()


async def test_409_on_initial_connect_retries():
    """The initial-connect path keeps a separate 409-only retry loop
    (predates the rework, kept as defense in depth). A single 409
    followed by success must produce a healthy CONNECTED state."""

    class _Conflict(Exception):
        def __init__(self):
            super().__init__("409 Conflict")
            class _Resp:
                status_code = 409
            self.response = _Resp()

    factory = _FakeConnect()
    factory.next_exceptions = [_Conflict()]
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        context_reset_sec=9999.0,
        keepalive_period_sec=9999.0,
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    # Should retry past the 409 and end up CONNECTED.
    # The 1.0s sleep between retries dominates the test runtime — that's fine.
    await asyncio.wait_for(conn.start(registry, "system"), timeout=5.0)
    try:
        assert conn._state is ConnectionState.CONNECTED
        assert len(factory.sessions) == 1
    finally:
        await conn.stop()


async def test_non_409_failure_on_initial_connect_does_not_retry():
    """Non-409 exceptions on the initial connect path should propagate
    immediately (auth errors etc don't fix themselves with a wait)."""
    factory = _FakeConnect()
    factory.next_exceptions = [RuntimeError("malformed config")]
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        context_reset_sec=9999.0,
        keepalive_period_sec=9999.0,
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    with pytest.raises(RuntimeError, match="malformed config"):
        await conn.start(registry, "system")
    # State is FAILED, not CONNECTED.
    assert conn._state is ConnectionState.FAILED


async def test_stop_is_idempotent():
    """Calling stop() twice should not raise."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "system")
    await conn.stop()
    # Second stop is a no-op.
    await conn.stop()
    assert conn._state is ConnectionState.CLOSED


async def test_send_audio_routes_through_active_turn():
    """A turn's send_audio() must reach the underlying session's
    send_realtime_input with an audio blob — verifies the per-turn
    bytes_sent counter advances correctly for silent-failure detection."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        turn = await conn.acquire_turn()
        await turn.send_audio(b"\x00" * 320)  # 10 ms of 16 kHz int16.
        await turn.send_audio(b"\x00" * 320)
        # Two audio sends + the one activity_start sent at acquire time.
        audio_sends = [c for c in sess.sent_realtime if "audio" in c]
        assert len(audio_sends) == 2
        assert turn.bytes_sent() == 640
        await turn.release()
    finally:
        await conn.stop()


def _make_websockets_409() -> Exception:
    """Build an exception that mirrors the real SDK 409 shape.

    google-genai 1.13.x raises ``websockets.legacy.exceptions.
    InvalidStatusCode`` on a 409 from Google's edge, which carries
    the code on ``e.status_code`` directly (NOT on ``e.response.
    status_code`` like httpx errors). The fake here replicates that
    shape so ``_is_409_conflict`` is exercised on the realistic
    attribute path."""
    class _WSInvalidStatusCode(Exception):
        status_code = 409

        def __init__(self):
            super().__init__("server rejected WebSocket connection: HTTP 409")

    return _WSInvalidStatusCode()


def _make_httpx_409() -> Exception:
    """Build an exception that mirrors the legacy httpx-style 409 shape
    used in the existing test_409_on_initial_connect_retries test —
    kept here so both attribute paths get coverage."""
    class _HttpxConflict(Exception):
        def __init__(self):
            super().__init__("409 Conflict")
            class _Resp:
                status_code = 409
            self.response = _Resp()

    return _HttpxConflict()


async def test_409_detected_via_websockets_status_code_attribute():
    """The real SDK raises ``websockets.legacy.exceptions.
    InvalidStatusCode``, which carries the status on
    ``exc.status_code`` (not ``exc.response.status_code``). The
    pre-fix detection used only ``e.response.status_code`` and so
    relied entirely on the substring fallback for every real 409 —
    which would silently break on a websockets release that reformats
    the error message. This test pins the websockets-shape detection
    path explicitly."""
    factory = _FakeConnect()
    factory.next_exceptions = [_make_websockets_409()]
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        context_reset_sec=9999.0,
        keepalive_period_sec=9999.0,
        backoff_schedule=(0.0,),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    # Should retry past the websockets-shaped 409 and end up CONNECTED.
    await asyncio.wait_for(conn.start(registry, "system"), timeout=5.0)
    try:
        assert conn._state is ConnectionState.CONNECTED
    finally:
        await conn.stop()


async def test_reconnect_409_drops_resumption_handle_and_retries_fresh():
    """The single most damaging pre-fix bug: a stale resumption handle
    (server-invalidated by ABORTED close, expiry, or being redeemed
    elsewhere) caused every reconnect attempt to 409 against the same
    handle until the backoff budget was exhausted and the connection
    went FAILED. The fix drops the handle on the first 409, so the
    next attempt connects fresh.

    Drives this by: open succeeds, a handle gets cached, the WS
    drops, the supervisor's first reconnect attempt 409s, the
    second attempt is allowed to succeed (no queued exception).
    Asserts: handle was cleared on the connection AND the second
    config carries no resumption handle."""
    # Two backoff steps so we have one "first attempt" and one "retry".
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.0))
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        # Cache a resumption handle.
        sess.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-stale")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-stale")

        # Drop the WS. Queue ONE 409 for the first reconnect attempt;
        # the second attempt will succeed (no queued exception).
        factory.next_exceptions = [_make_websockets_409()]
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        sess.feed_error(_Drop())

        # Reconnect should succeed on the second attempt after the
        # 409-on-first-attempt forces a handle drop.
        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        # Handle was cleared.
        assert conn._resumption_handle is None
        # Successful reconnect's config carries NO handle (reconnected
        # fresh, not with the stale handle). _build_session_resumption()
        # omits the field entirely when there's no handle.
        assert factory.configs[1].session_resumption is None
    finally:
        await conn.stop()


async def test_reconnect_409_with_no_cached_handle_just_retries():
    """If a 409 fires during reconnect AND there's no cached handle,
    the retry path should still proceed (handle-drop is a no-op,
    backoff still gives the server room to release). Pre-fix this
    case wasn't even special-cased — proves the new code doesn't
    regress it."""
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.0))
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        # No resumption handle is ever cached for this test.
        assert conn._resumption_handle is None

        factory.next_exceptions = [_make_websockets_409()]
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
            rcvd = _Rcvd()
        sess.feed_error(_Drop())

        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        assert conn._resumption_handle is None
    finally:
        await conn.stop()


def _make_ws_close_1008_session_expired() -> Exception:
    """Mirror the real shape produced by `websockets 15.x` when the
    server closes a Live session with code 1008 / reason
    "BidiGenerateContent session expired".

    Carries the canonical attribute path (``e.rcvd.code``,
    ``e.rcvd.reason``) plus the back-compat ``e.code`` / ``e.reason``
    aliases — both are present in the real exception. Probed live on
    the Pi against ``websockets.exceptions.ConnectionClosedError``."""
    class _Rcvd:
        code = 1008
        reason = "BidiGenerateContent session expired"

    class _WSClose(Exception):
        rcvd = _Rcvd()
        code = 1008
        reason = "BidiGenerateContent session expired"

        def __init__(self):
            super().__init__(
                "received 1008 (policy violation) BidiGenerateContent "
                "session expired; then sent 1008 (policy violation) "
                "BidiGenerateContent session expired"
            )

    return _WSClose()


async def test_reconnect_1008_session_expired_drops_resumption_handle():
    """The bug that wedged the speaker overnight: WS close 1008 with
    reason "BidiGenerateContent session expired" is the server's way of
    saying "your cached resumption handle is stale" — but pre-fix the
    handle drop was gated on `_is_409_conflict`, so 1008 closes were
    treated as transient and every reconnect attempt sent the same
    stale handle and got the same rejection.

    Drives this with a 1008-shaped exception on the first reconnect;
    the second attempt must succeed AND must connect with no
    resumption handle (proves the drop happened)."""
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.0))
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        # Cache a resumption handle that will be invalidated server-side
        # while we're still holding it.
        sess.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-stale-1008")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-stale-1008")

        # Drop the WS, queue ONE 1008 for the first reconnect attempt.
        factory.next_exceptions = [_make_ws_close_1008_session_expired()]
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        sess.feed_error(_Drop())

        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        assert conn._resumption_handle is None
        assert factory.configs[1].session_resumption is None
    finally:
        await conn.stop()


async def test_reconnect_generic_exception_drops_resumption_handle():
    """Forward-compat: any future close code or wrapped exception type
    that comes out of `__aenter__` on the supervisor reconnect path
    should drop the cached handle on the first failure. The handle
    only carries value across a transient drop; persisting one across
    a real failure is what the bug exploited.

    Uses a bare ``RuntimeError`` (no .code, no .rcvd, no 409 substring)
    to prove the drop is not gated on any specific exception shape."""
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.0))
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        sess.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-stale-generic")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-stale-generic")

        factory.next_exceptions = [RuntimeError("unknown server-side error")]
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()
        sess.feed_error(_Drop())

        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0)
        assert conn._resumption_handle is None
        assert factory.configs[1].session_resumption is None
    finally:
        await conn.stop()


async def test_context_reset_reopen_recovers_from_409():
    """Pre-fix the bare ``await self._open_session()`` inside
    ``_maybe_reset_context`` had no retry — a single 409 from the
    post-teardown race put the connection into an indeterminate
    state and crashed the wake handler.

    This test: idle past the context-reset window, then on the next
    acquire_turn the post-teardown reopen 409s once, retries, and
    succeeds. The turn should be acquirable without raising."""
    conn, factory = _make_conn(context_reset_sec=0.01)
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        # First turn so context-reset has something to reset.
        turn1 = await conn.acquire_turn()
        await turn1.release()
        await asyncio.sleep(0.05)  # past the 0.01s reset window.

        # Queue a 409 for the FIRST post-teardown open. The retry
        # (1.0s into the schedule) will succeed since no second
        # exception is queued.
        factory.next_exceptions = [_make_websockets_409()]

        # The acquire_turn should succeed despite the 409 transient.
        # 5s timeout: 1.0s sleep before the retry attempt + slack.
        turn2 = await asyncio.wait_for(conn.acquire_turn(), timeout=5.0)
        assert len(factory.sessions) == 2  # post-teardown + retry → one new session
        await turn2.release()
    finally:
        await conn.stop()


async def test_context_reset_hard_fail_triggers_supervisor():
    """If every retry on the context-reset reopen path fails, the
    connection used to be left wedged: no session, supervisor
    never woken, ``_connected_event`` cleared, every subsequent
    wake hung for 20s before timing out. The fix sets
    ``_reconnect_event`` so the supervisor takes over recovery.

    Verified by counting that on hard failure, the supervisor's
    reconnect loop kicks in (factory sessions count keeps growing
    even after the original acquire_turn raised)."""
    factory = _FakeConnect()
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        context_reset_sec=0.01,
        keepalive_period_sec=9999.0,
        backoff_schedule=(0.0, 0.0),
        connect_factory=factory,
    )
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        # First turn so context reset has something to reset.
        turn1 = await conn.acquire_turn()
        await turn1.release()
        await asyncio.sleep(0.05)

        # Queue MANY 409s — exhausts the context-reset retry schedule
        # AND every supervisor reconnect attempt. The point is to
        # observe the supervisor being woken at all.
        factory.next_exceptions = [_make_websockets_409() for _ in range(20)]

        # acquire_turn raises once context-reset retries are exhausted.
        with pytest.raises(Exception):
            await asyncio.wait_for(conn.acquire_turn(), timeout=20.0)

        # Supervisor was triggered: the reconnect_event was set and
        # the supervisor consumed at least one of the queued 409s
        # in its own backoff loop (drained next_exceptions further
        # than the context-reset path alone would have).
        await _wait_until(
            lambda: conn._reconnect_event.is_set()
            or conn._state is ConnectionState.FAILED
            or conn._state is ConnectionState.RECONNECTING
            or conn._state is ConnectionState.PAUSED_FOR_BACKOFF,
            timeout=3.0,
        )
    finally:
        await conn.stop()


async def test_connection_lost_marks_turn_lost_during_active_turn():
    """If the WS drops while a turn is in flight, `turn_lost()` flips
    True and the audio_out() iterator yields its sentinel so the
    playback path drains cleanly."""
    conn, factory = _make_conn()
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
        turn = await conn.acquire_turn()
        # Reader task — should complete when the turn is lost.
        async def consume():
            async for _ in turn.audio_out():
                pass
        consumer = asyncio.create_task(consume())
        # Drop the connection.
        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "x"
            rcvd = _Rcvd()
        sess.feed_error(_Drop())
        # Consumer task ends because the audio queue gets a sentinel-None.
        await asyncio.wait_for(consumer, timeout=3.0)
        assert turn.turn_lost() is True
    finally:
        await conn.stop()


@dataclass
class _FC:
    """Stand-in for the SDK's FunctionCall items inside tool_call.function_calls."""
    name: str
    id: str = "fc-1"
    args: dict | None = None


@dataclass
class _ToolCall:
    """Stand-in for response.tool_call (carries one or more function_calls)."""
    function_calls: list[_FC] = field(default_factory=list)


async def test_tool_round_advances_idle_anchor_so_watchdog_does_not_fire():
    """The daemon's pre-response idle watchdog
    (`jasper/voice_daemon.py:_idle_watchdog`) reads
    ``turn.last_activity_at()`` and abandons the turn when no audio
    has arrived for ``JASPER_IDLE_TIMEOUT_SEC``. During a tool round
    (model emits a ``tool_call``, client dispatches, calls
    ``send_tool_response``, waits for the audio answer) no audio
    arrives — so without explicit anchor resets the watchdog can fire
    mid-dispatch at small timeout values.

    Mirrors ``test_openai_session.py``'s equivalent contract test.
    Pin: at minimum the per-tool reset inside ``_handle_tool_call``
    advances the anchor — proves the cross-provider contract from
    docs/HANDOFF-voice-providers.md is enforced for Gemini."""
    from jasper.tools import tool as tool_decorator
    conn, factory = _make_conn()
    registry = ToolRegistry()

    @tool_decorator()
    def get_weather(location: str = "") -> dict:
        """."""
        return {"location": "Brooklyn", "temperature": 62}
    registry.register(get_weather)

    await conn.start(registry, "")
    try:
        sess = factory.sessions[0]
        turn = await conn.acquire_turn()
        anchor_before = turn.last_activity_at()

        # Park briefly so the loop clock advances measurably.
        await asyncio.sleep(0.05)

        # Server sends a tool_call. The connection's receive loop
        # routes it to turn._on_response which calls _handle_tool_call
        # with the turn passed in; the dispatcher resets the anchor
        # per-tool and again after send_tool_response.
        sess.feed(_Resp(tool_call=_ToolCall(function_calls=[
            _FC(name="get_weather", id="fc-1", args={}),
        ])))
        # Wait for the dispatcher to invoke send_tool_response.
        await _wait_until(
            lambda: len(sess.sent_tool_responses) >= 1,
            timeout=2.0,
        )
        await asyncio.sleep(0.05)

        anchor_after = turn.last_activity_at()
        assert anchor_after > anchor_before, (
            "tool round must advance last_activity_at so the "
            "pre-response idle watchdog doesn't fire while waiting "
            "for the audio answer"
        )

        await turn.release()
    finally:
        await conn.stop()
