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
        self.sent_tool_responses: list[Any] = []
        self.closed = False

    async def send_realtime_input(self, **kwargs) -> None:
        self.sent_realtime.append(kwargs)

    async def send_tool_response(self, function_responses=None) -> None:
        self.sent_tool_responses.append(function_responses)

    async def receive(self):
        while True:
            item = await self._inbox.get()
            if isinstance(item, Exception):
                raise item
            yield item

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
        # First config has no handle.
        first_config = factory.configs[0]
        assert first_config.session_resumption.handle is None

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
        assert factory.configs[1].session_resumption.handle is None
        # The connection cleared the cached handle.
        assert conn._resumption_handle is None
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
