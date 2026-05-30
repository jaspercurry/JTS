"""Unit tests for GeminiLiveSession's barge-in / interrupted-flag plumbing.

These tests construct a real GeminiLiveSession (no network calls — the
genai.Client constructor is local) and exercise the response-dispatch
pipeline with hand-built fake response objects matching the SDK's shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

try:
    from jasper.voice.gemini_session import (
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
