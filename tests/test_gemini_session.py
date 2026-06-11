"""Unit tests for GeminiLiveSession's barge-in / interrupted-flag plumbing.

These tests construct a real GeminiLiveSession (no network calls — the
genai.Client constructor is local) and exercise the response-dispatch
pipeline with hand-built fake response objects matching the SDK's shape.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

try:
    from jasper.voice.gemini_session import (
        GOAWAY_DEFER_MIN_TIME_LEFT_SEC,
        GeminiLiveConnection,
        GeminiLiveSession,
        GeminiLiveTurn,
    )
    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

pytestmark = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


@dataclass
class _SC:
    """Stand-in for response.server_content."""
    turn_complete: bool = False
    interrupted: bool = False


@dataclass
class _Resp:
    """Stand-in for the SDK's response objects. _dispatch only uses
    getattr() so any object with the right attributes works."""
    data: bytes | None = None
    tool_call: Any = None
    server_content: _SC | None = None
    usage_metadata: Any = None


@dataclass
class _Usage:
    """Stand-in for response.usage_metadata. Gemini reports these as a
    counter cumulative for the WebSocket's lifetime."""
    prompt_token_count: int = 0
    response_token_count: int = 0


async def _run_turn(conn: "GeminiLiveConnection", cum_in: int, cum_out: int):
    """Open a fresh turn on `conn`, feed it one server message carrying
    the cumulative usage counter + turn_complete, and return the turn.
    Mirrors how acquire_turn snapshots the connection's cumulative as the
    turn's baseline."""
    turn = GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )
    conn._active_turn = turn
    await turn._on_response(_Resp(
        usage_metadata=_Usage(cum_in, cum_out),
        server_content=_SC(turn_complete=True),
    ))
    return turn


@dataclass
class _GoAway:
    """Stand-in for response.go_away. The SDK exposes `time_left` as a
    datetime.timedelta; the receive loop only reads it via getattr."""
    time_left: Any = None


@dataclass
class _GoAwayResp:
    """Response object carrying only a GoAway (no server_content)."""
    go_away: Any = None
    server_content: Any = None
    tool_call: Any = None
    data: bytes | None = None
    session_resumption_update: Any = None
    usage_metadata: Any = None


class _FakeReceiveSession:
    """Drives GeminiLiveConnection._receive_loop with a scripted sequence
    of responses, then raises CancelledError so the loop exits cleanly
    without going through any reconnect/clean-close branch."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def _receive(self):
        if self._responses:
            return self._responses.pop(0)
        raise asyncio.CancelledError


async def _run_receive_loop_with(conn, responses):
    """Bind `conn` to a scripted fake session and run one pass of the
    receive loop over the scripted responses."""
    conn._session = _FakeReceiveSession(responses)
    with pytest.raises(asyncio.CancelledError):
        await conn._receive_loop()


@pytest.mark.asyncio
async def test_goaway_mid_turn_with_ample_time_defers_reconnect():
    """A GoAway arriving while a turn is in flight, with time_left
    comfortably above the deferral threshold, must NOT tear the session
    down: it sets the pending flag and leaves the reconnect event clear
    so the in-flight turn keeps running. The reconnect fires only once
    the turn is released."""
    import datetime
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )
    conn._active_turn = turn

    ample = datetime.timedelta(
        seconds=GOAWAY_DEFER_MIN_TIME_LEFT_SEC + 60.0
    )
    await _run_receive_loop_with(conn, [_GoAwayResp(go_away=_GoAway(ample))])

    # Deferred: pending flag set, reconnect NOT triggered, turn intact.
    assert conn._deferred_reconnect.pending is True
    assert not conn._reconnect_event.is_set()
    assert conn._active_turn is turn

    # Releasing the turn fires the deferred reconnect.
    await conn._on_turn_released(turn)
    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


@pytest.mark.asyncio
async def test_goaway_with_no_active_turn_reconnects_immediately():
    """No turn in flight → reconnect promptly as before, regardless of
    time_left."""
    import datetime
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    assert conn._active_turn is None

    ample = datetime.timedelta(
        seconds=GOAWAY_DEFER_MIN_TIME_LEFT_SEC + 60.0
    )
    await _run_receive_loop_with(conn, [_GoAwayResp(go_away=_GoAway(ample))])

    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


@pytest.mark.asyncio
async def test_goaway_mid_turn_with_little_time_reconnects_immediately():
    """A GoAway mid-turn but with time_left below the threshold can't
    safely defer (the server is about to drop us) — reconnect promptly,
    do not set the pending flag."""
    import datetime
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )
    conn._active_turn = turn

    little = datetime.timedelta(
        seconds=GOAWAY_DEFER_MIN_TIME_LEFT_SEC - 5.0
    )
    await _run_receive_loop_with(conn, [_GoAwayResp(go_away=_GoAway(little))])

    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


@pytest.mark.asyncio
async def test_goaway_mid_turn_with_unparseable_time_reconnects_immediately():
    """If time_left can't be interpreted, fail safe to the existing
    reconnect-immediately behaviour rather than deferring on a value we
    can't reason about."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )
    conn._active_turn = turn

    await _run_receive_loop_with(
        conn, [_GoAwayResp(go_away=_GoAway(object()))]
    )

    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


def test_goaway_defer_threshold_covers_hard_recording_cap():
    """Drift guard for GOAWAY_DEFER_MIN_TIME_LEFT_SEC.

    Deferring a mid-turn GoAway is only safe if the deferred window can
    actually contain a full turn — i.e. the threshold must be >= the
    longest a turn can run, which is the daemon's hard recording cap.
    If a future change raises voice_daemon.HARD_RECORDING_CAP_SEC above
    the threshold, deferral would routinely overrun `time_left`; catch
    that here rather than discovering it on a live 15-min session. (The
    overrun is itself fail-safe — the WS drops and we reconnect — but the
    threshold should still reflect the real bound it claims to cover.)"""
    from jasper.voice_daemon import HARD_RECORDING_CAP_SEC

    assert GOAWAY_DEFER_MIN_TIME_LEFT_SEC >= HARD_RECORDING_CAP_SEC


@pytest.mark.asyncio
async def test_gemini_usage_is_per_turn_delta_not_cumulative():
    """Gemini's usage_metadata is cumulative for the WebSocket's lifetime.
    Each per-turn usage row must hold THIS turn's delta, not the running
    total — otherwise SUM() across rows multi-counts (a 3-turn connection
    would over-report ~2x). Regression for the cumulative-double-count
    bug (per-turn rows storing the lifetime cumulative)."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")

    t1 = await _run_turn(conn, 1000, 500)
    assert t1.usage_tokens() == {"input_tokens": 1000, "output_tokens": 500}

    # Cumulative grows; this turn's delta is the increment only.
    t2 = await _run_turn(conn, 2500, 1300)
    assert t2.usage_tokens() == {"input_tokens": 1500, "output_tokens": 800}

    t3 = await _run_turn(conn, 3000, 1500)
    assert t3.usage_tokens() == {"input_tokens": 500, "output_tokens": 200}

    # The property that makes SUM(cost) across per-turn rows correct:
    # the deltas telescope to the final cumulative, NOT 1000+2500+3000.
    total_in = sum(t.usage_tokens()["input_tokens"] for t in (t1, t2, t3))
    total_out = sum(t.usage_tokens()["output_tokens"] for t in (t1, t2, t3))
    assert (total_in, total_out) == (3000, 1500)


@pytest.mark.asyncio
async def test_gemini_usage_delta_handles_counter_reset_on_reconnect():
    """If the server-side counter resets (a fresh session after a
    reconnect restarts it), the observed value is below the captured
    baseline. The delta must then be the observed post-reset total, not
    a negative number."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    conn._cumulative_usage = {"input_tokens": 5000, "output_tokens": 3000}
    t = await _run_turn(conn, 200, 100)
    assert t.usage_tokens() == {"input_tokens": 200, "output_tokens": 100}


@pytest.mark.asyncio
async def test_gemini_turn_without_usage_metadata_reports_zero():
    """A turn that receives audio but no usage_metadata (silent-failure
    or lost turn) attributes zero tokens to itself rather than a negative
    delta off the connection's cumulative."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    conn._cumulative_usage = {"input_tokens": 1000, "output_tokens": 500}
    turn = GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )
    conn._active_turn = turn
    await turn._on_response(_Resp(
        data=b"audio", server_content=_SC(turn_complete=True),
    ))
    assert turn.usage_tokens() == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.asyncio
async def test_interrupted_drains_queued_audio_and_sets_event():
    """When the model is interrupted, any audio chunks queued ahead of
    the interrupt should be dropped (NOT played to the speaker), and
    the interrupt event should fire so the playback task wakes up to
    flush its output."""
    session = GeminiLiveSession(api_key="fake", model="fake")
    # Pre-populate the audio queue with chunks that arrived BEFORE the
    # interrupt — these should never reach the speaker.
    await session._audio_q.put(b"chunk1")
    await session._audio_q.put(b"chunk2")
    await session._audio_q.put(b"chunk3")
    assert session._audio_q.qsize() == 3

    await session._dispatch(_Resp(server_content=_SC(interrupted=True)))

    assert session._audio_q.empty()
    assert session.interrupted() is True
    assert session._interrupt_event.is_set()


@pytest.mark.asyncio
async def test_wait_for_interrupt_resolves_immediately_after_event_set():
    """The playback task awaits wait_for_interrupt() — must wake up as
    soon as the receive loop sets the event."""
    import asyncio
    session = GeminiLiveSession(api_key="fake", model="fake")
    await session._dispatch(_Resp(server_content=_SC(interrupted=True)))
    # Should resolve quickly since the event is set.
    await asyncio.wait_for(session.wait_for_interrupt(), timeout=0.1)


@pytest.mark.asyncio
async def test_clear_interrupted_resets_flag_and_event():
    session = GeminiLiveSession(api_key="fake", model="fake")
    await session._dispatch(_Resp(server_content=_SC(interrupted=True)))
    assert session.interrupted() is True
    assert session._interrupt_event.is_set()

    session.clear_interrupted()
    assert session.interrupted() is False
    assert not session._interrupt_event.is_set()


@pytest.mark.asyncio
async def test_turn_complete_increments_counter():
    session = GeminiLiveSession(api_key="fake", model="fake")
    assert session.turn_count() == 0
    await session._dispatch(_Resp(server_content=_SC(turn_complete=True)))
    await session._dispatch(_Resp(server_content=_SC(turn_complete=True)))
    assert session.turn_count() == 2


@pytest.mark.asyncio
async def test_audio_data_queued_for_playback():
    session = GeminiLiveSession(api_key="fake", model="fake")
    await session._dispatch(_Resp(data=b"audio_chunk_1"))
    await session._dispatch(_Resp(data=b"audio_chunk_2"))
    assert session._audio_q.qsize() == 2
    assert (await session._audio_q.get()) == b"audio_chunk_1"
    assert (await session._audio_q.get()) == b"audio_chunk_2"


@pytest.mark.asyncio
async def test_interrupt_drops_audio_received_in_same_response():
    """If a response somehow contains both new audio AND an interrupt
    flag, the interrupt drain runs AFTER the audio is queued — net
    effect should still be no audio surviving the dispatch."""
    session = GeminiLiveSession(api_key="fake", model="fake")
    await session._dispatch(_Resp(
        data=b"some_audio",
        server_content=_SC(interrupted=True),
    ))
    # Audio should have been put then immediately drained.
    assert session._audio_q.empty()
    assert session.interrupted() is True


# ---- Dispatch seam reads per-tool timeout ----------------------------------
#
# Pins the fix: the `asyncio.wait_for` cap around each tool coroutine in
# `_handle_tool_call` reads `tool.timeout`, not a hardcoded 12.0. A tool
# with a tiny timeout must time out; a tool with the default budget must
# not — proving the seam honours the per-tool value (the Home Assistant
# 90s case rides on the same mechanism).


@dataclass
class _FC:
    """Stand-in for a Gemini function_call (getattr-accessed)."""
    name: str
    id: str = "fc-1"
    args: dict | None = None


@dataclass
class _ToolCall:
    """Stand-in for response.tool_call."""
    function_calls: list


class _CaptureSession:
    """Minimal _session stub: records the function_responses the
    dispatcher sends so the test can read each call's payload."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_tool_response(self, *, function_responses) -> None:
        self.sent.extend(function_responses)


@pytest.mark.asyncio
async def test_dispatch_honours_short_per_tool_timeout():
    from jasper.tools import ToolRegistry, tool as tool_decorator

    @tool_decorator(timeout=0.05)
    async def slow_tool() -> dict:
        """."""
        await asyncio.sleep(0.5)  # outlives the tool's 0.05s budget
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(slow_tool)

    session = GeminiLiveSession(api_key="fake", model="fake")
    session._registry = reg
    cap = _CaptureSession()
    session._session = cap

    await session._dispatch(_Resp(tool_call=_ToolCall(
        function_calls=[_FC(name="slow_tool", args={})],
    )))

    assert len(cap.sent) == 1
    # FunctionResponse.response carries the per-tool timeout error — proves
    # wait_for fired at 0.05s, not the 12s default (which wouldn't have).
    assert cap.sent[0].response == {"error": "slow_tool timed out"}


@pytest.mark.asyncio
async def test_dispatch_completes_fast_tool_under_default_timeout():
    from jasper.tools import ToolRegistry, tool as tool_decorator

    @tool_decorator()  # default budget
    async def quick_tool() -> dict:
        """."""
        return {"temperature": 62}

    reg = ToolRegistry()
    reg.register(quick_tool)

    session = GeminiLiveSession(api_key="fake", model="fake")
    session._registry = reg
    cap = _CaptureSession()
    session._session = cap

    await session._dispatch(_Resp(tool_call=_ToolCall(
        function_calls=[_FC(name="quick_tool", args={})],
    )))

    assert len(cap.sent) == 1
    assert cap.sent[0].response == {"temperature": 62}


@pytest.mark.asyncio
async def test_acquire_turn_rolls_back_active_turn_when_activity_start_fails():
    """A failed activity_start must not leave the turn slot occupied.

    `acquire_turn` assigns `_active_turn` before sending activity_start;
    if that send raises (WS dropped in the gap), the slot must roll back
    — otherwise every later acquire gets "a turn is already active"
    until a reconnect clears it. Observed wedging the 2026-06-11 eval
    runs; on the daemon it self-heals only because the supervisor's
    reconnect detaches the turn.
    """
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    conn._connected_event.set()

    async def _raise():
        raise RuntimeError("ws closed mid-send")

    conn._send_activity_start = _raise

    with pytest.raises(RuntimeError, match="ws closed mid-send"):
        await conn.acquire_turn()
    assert conn._active_turn is None

    # The next acquire surfaces the real failure again — not the
    # "a turn is already active" wedge.
    with pytest.raises(RuntimeError, match="ws closed mid-send"):
        await conn.acquire_turn()
