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
    from jasper.voice.gemini_session import GeminiLiveSession
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
