# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Gemini session lifecycle against an in-memory SDK transport."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import pytest

from tests._provider_fakes import complete_gemini_turn as _complete_turn
from tests._provider_fakes import GeminiConnect as _FakeConnect


from jasper.voice._base import ToolCall, close_code_and_reason
from jasper.voice._supervisor import (
    request_planned_reopen,
)
from tests._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_signalled, wait_until
from tests._gemini_fakes import GoAway as _GoAway
from tests._gemini_fakes import Response as _Resp
from tests._gemini_fakes import ResumptionUpdate as _ResumptionUpdate
from tests._gemini_fakes import ServerContent as _ServerContent
from tests._gemini_fakes import Transcription as _Transcription
from tests._log_events import event_fields, event_records, leaked_lines

try:
    from google.genai import types

    from jasper.voice.gemini_session import (
        GeminiLiveConnection,
        GeminiLiveTurn,
    )
    from jasper.voice.session import ConnectionState
    from jasper.tools import ToolRegistry, tool
    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

pytestmark = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_conn(
    *,
    backoff_schedule=(0.0, 0.0),
    context_reset_sec: float = 9999.0,
    rotate_after_sec: float = 0.0,
) -> tuple[GeminiLiveConnection, _FakeConnect]:
    """Build a connection wired to a _FakeConnect.

    Tests pass `backoff_schedule=(0.0, 0.0)` to make reconnect immediate
    (no real waiting in unit tests). `context_reset_sec` defaults to a
    huge value so the idle reset doesn't fire unless a test explicitly
    overrides it. `rotate_after_sec=0` disables the planned rotation so
    only the tests that exercise it spawn that timer."""
    factory = _FakeConnect()
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        context_reset_sec=context_reset_sec,
        rotate_after_sec=rotate_after_sec,
        backoff_schedule=backoff_schedule,
        connect_factory=factory,
    )
    return conn, factory


def _ws_close_error(code: int, reason: str) -> Exception:
    """A `websockets`-shaped close error: the frame lands on `.rcvd`."""
    class _Rcvd:
        pass
    rcvd = _Rcvd()
    rcvd.code = code
    rcvd.reason = reason
    exc = Exception(f"{code} {reason}")
    exc.rcvd = rcvd
    return exc


def _api_error(code: int, message: str) -> Exception:
    """A genai `APIError`-shaped error: no frame, code on `.code`."""
    exc = Exception(f"{code} None. {message}")
    exc.code = code
    exc.message = message
    return exc


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


@pytest.mark.parametrize("operation,event,level", [
    ("send_audio", "provider.send_failed", logging.WARNING),
    ("end_input", "provider.send_failed", logging.WARNING),
    ("cancel_response", "barge.cancel_failed", logging.WARNING),
])
async def test_turn_failure_events_redact_key(caplog, monkeypatch, operation, event, level):
    caplog.set_level(logging.DEBUG, logger="jasper.voice.gemini_session")
    conn, _ = _make_conn()
    conn._api_key = "plain-secret-value"
    turn = GeminiLiveTurn(conn, started_at=0)

    async def fail(*args, **kwargs):
        raise RuntimeError(f"rejected {conn._api_key}")

    monkeypatch.setattr(conn, "_send_realtime_input", fail)
    if operation == "cancel_response":
        turn._activity_end_sent = True
    args = {"send_audio": (b"pcm",), "end_input": (), "cancel_response": ("user",)}
    await getattr(turn, operation)(*args[operation])
    fields = event_fields(caplog, event)
    assert fields["provider"] == "gemini"
    assert fields["detail"]
    assert not leaked_lines(caplog, conn._api_key)
    (record,) = event_records(caplog, event)
    assert record.levelno == level
    assert record.exc_info is None
    assert turn.turn_lost()


async def test_connection_lifecycle_info_logs_are_concise(caplog):
    """Connect/teardown keep one timing summary without object-id probes."""
    caplog.set_level(logging.INFO, logger="jasper.voice.gemini_session")
    conn, _factory = _make_conn()
    receive_entered = asyncio.Event()
    receive_loop = conn._receive_loop

    async def _tracked_receive_loop():
        receive_entered.set()
        await receive_loop()

    conn._receive_loop = _tracked_receive_loop
    await conn.start(ToolRegistry(), "system")
    await asyncio.wait_for(receive_entered.wait(), timeout=1.0)
    assert conn._receive_task is not None
    assert not conn._receive_task.done()
    await conn.stop()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "jasper.voice.gemini_session"
        and record.levelno == logging.INFO
    ]
    connected = event_fields(caplog, "provider.connected")
    assert connected["provider"] == "gemini"
    assert int(connected["ms"]) >= 0
    assert connected["resumption"] == "false"
    (record,) = event_records(caplog, "provider.connected")
    assert record.name == "jasper.voice.gemini_session"
    assert record.levelno == logging.INFO
    teardown = event_fields(caplog, "provider.teardown")
    assert teardown["provider"] == "gemini"
    assert int(teardown["ms"]) >= 0
    (record,) = event_records(caplog, "provider.teardown")
    assert record.name == "jasper.voice.gemini_session"
    assert record.levelno == logging.INFO
    assert not any("id=" in message or "instrumentation" in message for message in messages)


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
            async for chunk in turn1.audio_out_chunks():
                chunks.append(chunk.pcm)
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


@pytest.mark.parametrize("model", [
    "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-preview-12-2025",
])
async def test_confirmation_context_uses_manual_realtime_input_after_a_completed_turn(model):
    conn, factory = _make_conn()
    conn._model = model
    await conn.start(ToolRegistry(), "system")
    try:
        sess = factory.sessions[0]
        previous = await conn.acquire_turn()
        await previous.end_input()
        sess.feed(types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True)))
        await _wait_until(previous.server_turn_complete)
        await previous.release()
        sess.sent_realtime.clear()

        turn = await conn.acquire_turn()
        context = "Answer yes or no about the pending question."
        await turn.send_text_context(context)
        await turn.send_audio(b"\x00\x00")

        assert len(factory.sessions) == 1
        assert sess.sent_client_content == []
        assert [list(message) for message in sess.sent_realtime] == [["activity_start"], ["text"], ["audio"]]
        assert sess.sent_realtime[1] == {"text": context}
        await turn.end_input()
        await turn.end_input()
        assert [list(message) for message in sess.sent_realtime] == [
            ["activity_start"], ["text"], ["audio"], ["activity_end"],
        ]
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
        # The first connect must still ASK for resumption (handle=None):
        # the server only emits session_resumption_update when setup
        # carried the field, so omitting it meant we never got a handle.
        first_config = factory.configs[0]
        assert first_config.session_resumption is not None
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
        assert factory.configs[1].session_resumption.handle is None
    finally:
        await conn.stop()


async def test_idle_context_reset_drops_resumption_handle_and_reopens():
    """Connection healthy, but idle longer than the configured threshold:
    the next acquire_turn should close + reopen with no resumption
    handle so stale conversational context can't bleed in.

    The handle is dropped at teardown rather than when the reset is
    requested: the old session's receive loop runs until the supervisor
    cancels it, so a `session_resumption_update` that lands in between
    would otherwise resume exactly the context being discarded."""
    # Tiny threshold so the test can hit it.
    conn, factory = _make_conn(context_reset_sec=0.01)
    registry = ToolRegistry()
    await conn.start(registry, "system")
    teardown = conn._teardown_session
    late_updates = 0

    async def _teardown_after_a_late_handle() -> None:
        """The reset's teardown, with the race it must survive run first."""
        nonlocal late_updates
        session = conn._session
        if session is not None and late_updates == 0:
            late_updates += 1
            session.feed(_Resp(
                session_resumption_update=_ResumptionUpdate(
                    new_handle="hndl-late",
                ),
            ))
            await _wait_until(
                lambda: conn._resumption_handle == "hndl-late", timeout=1.0,
            )
        await teardown()

    conn._teardown_session = _teardown_after_a_late_handle
    try:
        # First turn establishes a resumption handle.
        sess1 = factory.sessions[0]
        sess1.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="hndl-stale")))
        await _wait_until(lambda: conn._resumption_handle == "hndl-stale")
        turn1 = await conn.acquire_turn()
        await _complete_turn(turn1, sess1)
        await turn1.release()

        # Wait past the context-reset window.
        await asyncio.sleep(0.05)

        # Next acquire triggers context-reset before opening a turn.
        turn2 = await asyncio.wait_for(conn.acquire_turn(), timeout=5.0)
        # The late update really did land on the old session.
        assert late_updates == 1
        # New session was opened.
        assert len(factory.sessions) == 2
        # New session opened with NO resumption handle (fresh context) —
        # the field is still sent so the server keeps issuing handles.
        assert factory.configs[1].session_resumption.handle is None
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
        await _complete_turn(turn1, sess1)
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
    """websockets handshake errors carry status_code directly."""
    class _WSInvalidStatusCode(Exception):
        status_code = 409

        def __init__(self):
            super().__init__("server rejected WebSocket connection: HTTP 409")

    return _WSInvalidStatusCode()


async def test_reconnect_409_drops_resumption_handle_and_retries_fresh(caplog):
    """A 409 rejection drops the stale handle before the next attempt."""
    conn, factory = _make_conn(backoff_schedule=(0.0, 0.0))
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        sess = factory.sessions[0]
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
        assert conn._resumption_handle is None
        # Successful reconnect's config carries NO handle (reconnected
        # fresh, not with the stale handle).
        assert factory.configs[1].session_resumption.handle is None
        assert event_fields(caplog, "provider.reconnect_conflict")["status"] == "409"
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
    """A 1008 rejection drops the stale handle before the next attempt."""
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
        assert factory.configs[1].session_resumption.handle is None
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
        assert factory.configs[1].session_resumption.handle is None
    finally:
        await conn.stop()


async def test_context_reset_that_cannot_reconnect_raises_for_the_cue():
    """A reset whose reopen never lands raises instead of hanging.

    The wake path answers that raise with a failure cue, so a press
    during a dead connection is never silent (non-negotiable 6)."""
    conn, factory = _make_conn(context_reset_sec=0.01)
    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        turn1 = await conn.acquire_turn()
        await turn1.release()
        await asyncio.sleep(0.05)

        # More failures than the supervisor's bounded test schedule.
        factory.next_exceptions = [_make_websockets_409() for _ in range(20)]

        with pytest.raises(RuntimeError):
            await asyncio.wait_for(conn.acquire_turn(), timeout=20.0)
        assert conn.is_paused()
        assert conn.wake_cue()
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
    (`jasper/voice_daemon.py:idle_watchdog`) reads
    ``turn.last_activity_at()`` and abandons the turn when no audio
    has arrived for ``JASPER_IDLE_TIMEOUT_SEC``. During a tool round
    (model emits a ``tool_call``, client dispatches, calls
    ``send_tool_response``, waits for the audio answer) no audio
    arrives — so without explicit anchor resets the watchdog can fire
    mid-dispatch at small timeout values.

    Mirrors ``test_openai_session.py``'s equivalent contract test.
    Pin: the per-tool reset inside the round advances the anchor. Driven
    by running the round directly rather than feeding a ``tool_call``
    frame: the receive loop resets the anchor on any progress event, so
    going through the wire would move it whatever the round did."""
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

        # The round resets the anchor per-tool and again after
        # send_tool_response.
        await turn._run_tool_calls(
            [ToolCall(id="fc-1", name="get_weather", args={})],
        )
        assert len(sess.sent_tool_responses) == 1

        anchor_after = turn.last_activity_at()
        assert anchor_after > anchor_before, (
            "tool round must advance last_activity_at so the "
            "pre-response idle watchdog doesn't fire while waiting "
            "for the audio answer"
        )

        await turn.release()
    finally:
        await conn.stop()


async def test_tool_round_metadata_captures_tool_name_without_args_or_payload():
    from jasper.tools import tool as tool_decorator
    conn, factory = _make_conn()
    registry = ToolRegistry()

    @tool_decorator()
    def get_weather(location: str = "") -> dict:
        """."""
        return {"location": location or "Brooklyn", "temperature": 62}
    registry.register(get_weather)

    await conn.start(registry, "")
    try:
        sess = factory.sessions[0]
        turn = await conn.acquire_turn()

        sess.feed(_Resp(tool_call=_ToolCall(function_calls=[
            _FC(name="get_weather", id="fc-1", args={"location": "Home"}),
        ])))
        await _wait_until(
            lambda: len(sess.sent_tool_responses) >= 1,
            timeout=2.0,
        )

        capture = turn.capture()
        assert capture is not None
        assert capture.user_text is None
        assert capture.assistant_text is None
        assert capture.data == {
            "kind": "voice_turn",
            "transcripts_available": False,
            "tools": ["get_weather"],
        }
        assert "Home" not in repr(capture)
        assert "temperature" not in repr(capture)

        await turn.release()
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Reconnect nudge (issue #3855). Gemini's wait is its own implementation,
# so the OpenAI pins do not cover it.
# ---------------------------------------------------------------------------


async def test_planned_rotation_rolls_the_session_without_backoff():
    """The rotate watchdog opens a fresh session before the server's
    idle abort can, and the roll skips the reconnect backoff wait."""
    delays: list[float] = []

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)

    factory = _FakeConnect()
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        backoff_schedule=None,
        connect_factory=factory,
        rotate_after_sec=0.05,
        sleep=_sleep,
    )
    await conn.start(ToolRegistry(), "system")
    try:
        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await _wait_until(
            lambda: conn._state is ConnectionState.CONNECTED, timeout=3.0,
        )
        # The rotation's own attempt waited zero seconds — no 1 s
        # reconnect_delay(1) gap in front of the fresh session.
        assert delays and delays[0] == 0.0, delays
        assert conn._planned_rotate is False
    finally:
        await conn.stop()


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_ws_close_error(1006, "abnormal closure"), (1006, "abnormal closure")),
        # genai's APIError: the frame is consumed, the code survives on
        # `.code` — this is the server's idle abort on jts4.
        (_api_error(1008, "The operation was aborted."),
         (1008, "The operation was aborted.")),
        # An HTTP status is not a WebSocket close code.
        (_api_error(503, "unavailable"), (None, None)),
        (RuntimeError("no code anywhere"), (None, None)),
    ],
)
def test_close_code_extraction(exc, expected):
    """Both exception shapes the receive loop can see must yield the
    close code as a value, not just as prose inside the message."""
    assert close_code_and_reason(exc) == expected


@pytest.mark.parametrize("end_input_sent", [True, False])
async def test_first_chunk_event_reports_latency_since_end_input(
    caplog, end_input_sent,
):
    """Twin of the OpenAI adapter's pin: the first-chunk line must be
    anchored on `activity_end`, not on turn open, so it reads as the
    provider's latency and not as the user's utterance plus ~1 s of local
    endpointing."""

    caplog.set_level(logging.INFO, logger="jasper.voice.gemini_session")
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "system")
    try:
        sess = factory.sessions[0]
        turn = await conn.acquire_turn()
        await asyncio.sleep(0.01)
        if end_input_sent:
            await turn.end_input()
        sess.feed(_Resp(data=b"audio_chunk_1"))

        async def consume():
            async for _chunk in turn.audio_out_chunks():
                return
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await turn.release()
        await asyncio.wait_for(task, timeout=1.0)

        fields = event_fields(caplog, "turn.first_chunk")
        assert fields["provider"] == "gemini"
        assert int(fields["since_turn_start_ms"]) >= 10
        if end_input_sent:
            assert 0 <= int(fields["since_end_input_ms"]) <= int(
                fields["since_turn_start_ms"]
            )
        else:
            assert "since_end_input_ms" not in fields
    finally:
        await conn.stop()


@pytest.mark.parametrize("accepted", [False, True])
async def test_sdk_setup_acknowledgement_is_required(accepted):
    conn, factory = _make_conn()
    original_connect = factory.__call__

    def connect(**kwargs):
        cm = original_connect(**kwargs)
        cm._session.setup_complete = types.LiveServerSetupComplete() if accepted else None
        return cm

    conn._connect_factory = connect
    if accepted:
        await conn._open_session()
        assert not conn.is_paused()
    else:
        with pytest.raises(RuntimeError):
            await conn._open_session()
        assert conn.is_paused()
        assert not conn._connected_event.is_set()
        assert conn.last_failure_detail()
        assert factory.sessions[0].closed
    await conn.stop()


@pytest.mark.parametrize("boundary", ["release", "reconnect", "interrupt", "complete", "close", "cancel", "overlap"])
async def test_tool_await_cannot_cross_a_gemini_turn_boundary(boundary):
    conn, factory = _make_conn()
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []
    registry = ToolRegistry()

    @tool()
    async def action() -> dict:
        """Run an action."""
        calls.append(True)
        entered.set()
        try:
            await resume.wait()
        except asyncio.CancelledError:
            await resume.wait()
        return {"ok": True}

    registry.register(action)
    await conn.start(registry, "")
    try:
        old = await conn.acquire_turn()
        await old.end_input()
        old_session = factory.sessions[0]
        old_session.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="old-context")))
        await wait_until(lambda: conn._resumption_handle == "old-context", timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        old_session.feed(types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(function_calls=[
                types.FunctionCall(id=f"call_{i}", name="action", args={}) for i in range(2)
            ]),
            usage_metadata=types.UsageMetadata(prompt_token_count=100, response_token_count=50),
        ))
        await wait_signalled(entered, "tool executor entered")
        if boundary == "reconnect":
            old_session.feed_error(ConnectionError("disconnected"))
            await wait_until(lambda: old.turn_lost(), timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        elif boundary == "overlap":
            old_session.feed(types.LiveServerMessage(tool_call=types.LiveServerToolCall(
                function_calls=[types.FunctionCall(id="overlap", name="action", args={})],
            )))
            await wait_until(lambda: old.turn_lost(), timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        elif boundary == "close":
            await asyncio.wait_for(conn.stop(), DEFAULT_SIGNAL_TIMEOUT_S)
            assert old.turn_lost()
            resume.set()
            await wait_until(lambda: not conn._tool_tasks and registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
            assert calls == [True]
            assert old_session.sent_tool_responses == []
            return
        elif boundary in ("interrupt", "complete"):
            old_session.feed(types.LiveServerMessage(server_content=types.LiveServerContent(
                interrupted=boundary == "interrupt", turn_complete=boundary == "complete",
            )))
            if boundary == "interrupt":
                await asyncio.wait_for(old.wait_for_interrupt(), DEFAULT_SIGNAL_TIMEOUT_S)
            else:
                await wait_until(old.server_turn_complete, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        elif boundary == "cancel":
            await asyncio.wait_for(old.cancel_response("barge_in"), DEFAULT_SIGNAL_TIMEOUT_S)
            await old.cancel_response("again")
        await asyncio.wait_for(old.release(), DEFAULT_SIGNAL_TIMEOUT_S)
        fresh = await conn.acquire_turn()
        assert factory.configs[-1].session_resumption.handle is None
        capture_before = fresh.capture()
        usage_before = fresh.usage()
        resume.set()
        await wait_until(lambda: not conn._tool_tasks and registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert calls == [True]
        assert all(not session.sent_tool_responses for session in factory.sessions)
        assert fresh.capture() == capture_before
        assert fresh.usage() == usage_before
        assert not fresh.server_turn_complete()
    finally:
        resume.set()
        await conn.stop()
        await wait_until(lambda: not conn._tool_tasks and registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)


@pytest.mark.parametrize("send", ["audio", "text", "end_input", "cancel"])
@pytest.mark.parametrize("boundary", ["release", "reconnect"])
async def test_queued_input_cannot_cross_a_gemini_turn_boundary(send, boundary):
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    old = await conn.acquire_turn()
    old_session = factory.sessions[0]
    if send == "cancel":
        await old.end_input()
    sent_before = len(old_session.sent_realtime)
    await conn._send_lock.acquire()
    pending = {
        "audio": lambda: old.send_audio(b"pcm"),
        "text": lambda: old.send_text_context("old instruction"),
        "end_input": old.end_input,
        "cancel": lambda: old.cancel_response("barge_in"),
    }[send]
    sending = asyncio.create_task(pending())
    releasing = None
    try:
        await asyncio.sleep(0)
        if boundary == "release":
            releasing = asyncio.create_task(old.release())
            await _wait_until(lambda: old._released)
        else:
            old_session.feed_error(ConnectionError("disconnected"))
            await _wait_until(lambda: len(factory.sessions) == 2 and conn._connected_event.is_set())
        conn._send_lock.release()
        await sending
        if releasing is not None:
            await releasing
        fresh = await conn.acquire_turn()
        assert old_session.sent_client_content == []
        assert len(old_session.sent_realtime) == sent_before
        assert [set(call) for call in factory.sessions[-1].sent_realtime] == [{"activity_start"}]
        assert not fresh.server_turn_complete()
    finally:
        if conn._send_lock.locked():
            conn._send_lock.release()
        await sending
        if releasing is not None:
            await releasing
        await conn.stop()


async def test_gemini_acquire_cannot_return_a_disconnected_turn():
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    old_session = factory.sessions[0]
    entered, resume = asyncio.Event(), asyncio.Event()
    send = old_session.send_realtime_input

    async def blocked_send(**kwargs):
        entered.set()
        await resume.wait()
        await send(**kwargs)

    old_session.send_realtime_input = blocked_send
    acquiring = asyncio.create_task(conn.acquire_turn())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        old_session.feed_error(ConnectionError("disconnected"))
        await _wait_until(lambda: old_session.closed)
        resume.set()
        with pytest.raises(RuntimeError):
            await acquiring
        fresh = await conn.acquire_turn()
        assert not fresh.turn_lost()
        assert len(factory.sessions) == 2
    finally:
        resume.set()
        await asyncio.gather(acquiring, return_exceptions=True)
        await conn.stop()


async def test_gemini_acquire_that_meets_a_reconnect_waits_it_out():
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    old_session = factory.sessions[0]
    closing, finish_close = asyncio.Event(), asyncio.Event()

    async def parked_close():
        closing.set()
        await finish_close.wait()

    old_session.close = parked_close
    await conn._turn_lock.acquire()
    acquiring = asyncio.create_task(conn.acquire_turn())
    try:
        await asyncio.sleep(0)
        old_session.feed_error(ConnectionError("disconnected"))
        await asyncio.wait_for(closing.wait(), 1)
        conn._turn_lock.release()
        await _wait_until(conn._turn_lock.locked)
        assert not conn._connected_event.is_set()
        finish_close.set()
        turn = await asyncio.wait_for(acquiring, 2)
        assert not turn.turn_lost()
        assert old_session.sent_realtime == []
        assert [set(call) for call in factory.sessions[1].sent_realtime] == [{"activity_start"}]
    finally:
        finish_close.set()
        acquiring.cancel()
        await asyncio.gather(acquiring, return_exceptions=True)
        await conn.stop()


async def test_gemini_input_is_closed_after_end_input():
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        await turn.send_audio(b"first")
        await turn.end_input()
        await turn.send_audio(b"late")
        await turn.send_text_context("late")
        await turn.end_input()
        session = factory.sessions[0]
        assert [set(call) for call in session.sent_realtime] == [
            {"activity_start"}, {"audio"}, {"activity_end"},
        ]
        assert session.sent_client_content == []
    finally:
        await conn.stop()


async def test_closing_gemini_receive_cannot_request_another_reconnect():
    conn, factory = _make_conn()
    entered = asyncio.Event()

    async def receive():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise ConnectionError("socket closed during teardown") from None

    def connect(**kwargs):
        cm = factory(**kwargs)
        if len(factory.sessions) == 1:
            cm._session._receive = receive
        return cm

    conn._connect_factory = connect
    await conn.start(ToolRegistry(), "")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        request_planned_reopen(conn)
        await _wait_until(lambda: conn._connected_event.is_set())
        assert len(factory.sessions) == 2
        assert not conn._reconnect_event.is_set()
    finally:
        await conn.stop()


async def test_late_old_session_content_cannot_complete_or_capture_a_new_turn():
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    entered, resume = asyncio.Event(), asyncio.Event()

    class OldSession:
        async def _receive(self):
            entered.set()
            await resume.wait()
            return types.LiveServerMessage(server_content=types.LiveServerContent(
                model_turn=types.Content(role="model", parts=[
                    types.Part(inline_data=types.Blob(data=b"\x01\x00", mime_type="audio/pcm;rate=24000")),
                ]),
                input_transcription=types.Transcription(text="old user"),
                output_transcription=types.Transcription(text="old assistant"),
                turn_complete=True,
            ))

    conn._session = OldSession()
    receiving = asyncio.create_task(conn._receive_loop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        conn._session = object()
        conn._connected_event.set()
        fresh = GeminiLiveTurn(conn, started_at=0)
        conn._active_turn = fresh
        resume.set()
        await asyncio.wait_for(receiving, 1)
        assert fresh.audio_chunks_pending() == 0
        assert not fresh.server_turn_complete()
        assert fresh.capture().user_text is None
        assert fresh.capture().assistant_text is None
    finally:
        resume.set()
        receiving.cancel()
        await asyncio.gather(receiving, return_exceptions=True)


@pytest.mark.parametrize("tail_at", ["before_release", "idle", "acquired", "new_audio"])
@pytest.mark.parametrize("finished", [True, None])
async def test_input_transcript_owner_outlives_response_turn(tail_at, finished):
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        session = factory.sessions[0]
        old = await conn.acquire_turn()
        await old.send_audio(b"\x01\x00")
        await old.end_input()
        session.feed(types.LiveServerMessage(server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="old ", finished=False if finished else None),
            output_transcription=types.Transcription(text="old answer", finished=True),
            turn_complete=True,
        )))
        await _wait_until(old.server_turn_complete)

        async def tail():
            session.feed(types.LiveServerMessage(server_content=types.LiveServerContent(
                input_transcription=types.Transcription(text="tail", finished=finished),
            )))
            session.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="tail-read")))
            await _wait_until(lambda: conn._resumption_handle == "tail-read")

        if tail_at == "before_release":
            await tail()
        await old.release()
        old_capture = old.capture()
        if tail_at == "idle":
            await tail()
        fresh = await conn.acquire_turn()
        if tail_at == "new_audio":
            await fresh.send_audio(b"\x02\x00")
        if tail_at in {"acquired", "new_audio"}:
            await tail()
        assert fresh.capture().user_text is None
        assert fresh.capture().data["transcripts_available"] is False
        assert old.capture() == old_capture
        assert old.capture().user_text == ("old tail" if tail_at == "before_release" else "old")
        assert old.capture().assistant_text == "old answer"

        await fresh.send_audio(b"\x03\x00")
        session.feed(types.LiveServerMessage(server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="new ", finished=True),
        )))
        session.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle="segment-read")))
        await _wait_until(lambda: conn._resumption_handle == "segment-read")
        await fresh.send_audio(b"\x04\x00")
        await fresh.end_input()
        session.feed(types.LiveServerMessage(server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="speech", finished=True),
            output_transcription=types.Transcription(text="new answer", finished=True),
            turn_complete=True,
        )))
        await _wait_until(fresh.server_turn_complete)
        ambiguous = tail_at == "new_audio" or finished is None
        assert fresh.capture().user_text == (None if ambiguous else "new speech")
        assert fresh.capture().assistant_text == "new answer"
        assert len(factory.sessions) == 1
        await fresh.release()
        request_planned_reopen(conn)
        await _wait_until(lambda: conn._connected_event.is_set() and len(factory.sessions) == 2)
        recovered = await conn.acquire_turn()
        await recovered.send_audio(b"\x05\x00")
        factory.sessions[-1].feed(types.LiveServerMessage(server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="after reset", finished=True),
            turn_complete=True,
        )))
        await _wait_until(recovered.server_turn_complete)
        assert recovered.capture().user_text == "after reset"
    finally:
        await conn.stop()


@pytest.mark.parametrize("usage_on_completion", [False, True])
@pytest.mark.parametrize("incomplete", ["interrupted", "disconnect"])
async def test_gemini_usage_keeps_whole_response_snapshots(usage_on_completion, incomplete):
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        session = factory.sessions[0]
        totals = [0, 0]
        for index, counts in enumerate([(1000, 500), (2500, 1300), (3000, 1500), (200, 100), None]):
            turn = await conn.acquire_turn()
            await turn.send_audio(b"\x01\x00")
            await turn.end_input()
            final_usage = None
            if counts is not None:
                prompt, output = counts
                partial = types.UsageMetadata(prompt_token_count=prompt, response_token_count=output // 2)
                session.feed(types.LiveServerMessage(usage_metadata=partial))
                session.feed(types.LiveServerMessage(usage_metadata=partial))
                marker = f"partial-{index}"
                session.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle=marker)))
                await _wait_until(lambda: conn._resumption_handle == marker)
                assert (turn.usage().input_tokens, turn.usage().output_tokens) == (prompt, output // 2)
                final_usage = types.UsageMetadata(prompt_token_count=prompt, response_token_count=output)
            if not usage_on_completion:
                session.feed(types.LiveServerMessage(usage_metadata=final_usage))
            session.feed(types.LiveServerMessage(
                usage_metadata=final_usage if usage_on_completion else None,
                server_content=types.LiveServerContent(turn_complete=True),
            ))
            await _wait_until(turn.server_turn_complete)
            session.feed(types.LiveServerMessage(usage_metadata=types.UsageMetadata(
                prompt_token_count=99999, response_token_count=99999,
            )))
            marker = f"closed-{index}"
            session.feed(_Resp(session_resumption_update=_ResumptionUpdate(new_handle=marker)))
            await _wait_until(lambda: conn._resumption_handle == marker)
            usage = turn.usage()
            assert usage.breakdown is None
            assert (usage.input_tokens, usage.output_tokens) == (counts or (0, 0))
            totals[0] += usage.input_tokens
            totals[1] += usage.output_tokens
            await turn.release()
        assert totals == [6700, 3400]

        turn = await conn.acquire_turn()
        session.feed(types.LiveServerMessage(
            usage_metadata=types.UsageMetadata(prompt_token_count=600, response_token_count=70),
            server_content=types.LiveServerContent(interrupted=True),
        ))
        await asyncio.wait_for(turn.wait_for_interrupt(), DEFAULT_SIGNAL_TIMEOUT_S)
        if incomplete == "disconnect":
            session.feed_error(ConnectionError("offline test"))
            await _wait_until(turn.turn_lost)
        assert not turn.server_turn_complete()
        assert (turn.usage().input_tokens, turn.usage().output_tokens) == (600, 70)
    finally:
        await conn.stop()


@pytest.mark.parametrize("response, advances", [
    # Progress: the model is transcribing what it heard, or what it is
    # about to say. A slow generation emitting these must not be reaped.
    (_Resp(server_content=_ServerContent(
        input_transcription=_Transcription(text="what is the weather"),
    )), True),
    (_Resp(server_content=_ServerContent(
        output_transcription=_Transcription(text="it is"),
    )), True),
    (_Resp(server_content=_ServerContent(generation_complete=True)), True),
    # Liveness only: the socket is open, the turn is not moving.
    (_Resp(session_resumption_update=_ResumptionUpdate(new_handle="h1")), False),
    (_Resp(), False),
])
async def test_only_progress_messages_advance_the_idle_anchor(response, advances):
    """#4532: the pre-response idle timer must mean "this turn is not
    moving", not "no audio yet" and not "the socket went quiet".

    Transcript text either way shows work happening even though no audio
    has arrived; connection bookkeeping does not, so a session that keeps
    sending handles without ever answering still reaches the watchdog.
    Complements the tool-round pin above, which covers the local
    milestones that produce no server message at all."""
    conn, factory = _make_conn()
    await conn.start(ToolRegistry(), "")
    try:
        sess = factory.sessions[0]
        turn = await conn.acquire_turn()
        stale = asyncio.get_event_loop().time() - 100.0
        turn._last_activity_at = stale

        sess.feed(response)
        await _wait_until(lambda: sess.received >= 1, timeout=2.0)
        await asyncio.sleep(0.05)

        assert (turn.last_activity_at() > stale) is advances
        assert turn.chunks_received() == 0
        assert turn.server_turn_complete() is False
        await turn.release()
    finally:
        await conn.stop()
