# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest

from jasper.tools import ToolRegistry, dispatch_tool
from jasper.voice.conversation import (
    WATCHDOG_POLL_SEC,
    _resolved,
    continuous_watchdog,
    register_conversation_tools,
)
from jasper.voice.openai_live_session import OpenAILiveConnection, OpenAILiveTurn
from jasper.voice.turn_lifecycle import State
from jasper.voice.turn_playback import PlaybackReport, play_responses
from jasper.voice.session import AudioOutChunk
from tests._async_wait import wait_until
from tests._live_turn_fake import FakeLiveTurn
from tests._log_events import event_fields
from tests._playout import FakeTts
from tests._wake_loop import wake_loop_for_tests
from tests.usage_store_fixtures import FakeUsageStore


def answered_loop(tts=None):
    loop = wake_loop_for_tests(usage_store=FakeUsageStore(), tts=tts)
    loop._turns.state = State.SESSION
    loop._turns.turn = FakeLiveTurn(chunks_received=1)
    loop._turns.session_id = 7
    loop._turns.user_speech_seen = True
    loop._turns.playback_report.accepted_audio = True
    return loop


@pytest.mark.parametrize("accepted_at, drain_at, accepted_age, drain_age", [
    (0.0, 0.0, "null", "null"),
    (19.0, 21.0, "1000", "-1000"),
])
def test_deadline_event_distinguishes_missing_and_future_playout(
    caplog, accepted_at, drain_at, accepted_age, drain_age,
):
    caplog.set_level(logging.INFO)
    assert _resolved(
        "followup_timeout", 20.0, 18.0, accepted_at, drain_at, 0, False, 20.0,
    ) == "followup_timeout"
    fields = event_fields(caplog, "voice.turn_deadline")
    assert fields["accepted_age_ms"] == accepted_age
    assert fields["drain_age_ms"] == drain_age


async def test_endpointed_answer_closes_the_turn_once_playout_drains():
    """No host follow-up window survives: see ADR-0292."""
    loop = answered_loop()
    old = loop._turns.turn
    finish = AsyncMock()
    loop._assistant_output.finish_turn_episode = finish
    ended = AsyncMock()
    loop._peering.session_ended = ended
    await loop._turns.finish_response("ended")
    assert loop._turns.state is State.WAKE
    assert old.release_calls == 1
    assert loop._usage_store.close_calls == 1
    finish.assert_awaited_once()
    ended.assert_awaited_once_with("ended")
    await loop._cancel_fire_and_forget_tasks()


async def test_end_conversation_tool_closes_even_without_more_mic_frames():
    loop = answered_loop()
    registry = ToolRegistry()
    register_conversation_tools(registry, loop._turns.request_conversation_end)
    result = await dispatch_tool(registry, "end_conversation", {})
    await wait_until(lambda: loop._turns.state is State.WAKE)
    assert result == {"status": "conversation_ended"}
    assert loop._usage_store.close_calls == 1
    await loop._cancel_fire_and_forget_tasks()


@pytest.mark.parametrize(
    "busy, followup_seconds",
    [("speaker", 5), ("user", 5), ("tool", 5), ("quiet", 5), ("quiet", 0)],
)
async def test_live_followup_waits_for_playout_speech_and_tools(busy, followup_seconds):
    now = time.monotonic()
    turn = FakeLiveTurn(chunks_received=1)
    turn.last_chunk_at = lambda: now - 8
    turn.audio_chunks_pending = lambda: 0
    turn.backend_pending = busy == "tool"
    turn.last_activity_at = lambda: now if busy == "tool" else now - 8
    tts = FakeTts()
    tts.expected_drain_at = lambda: now + 10 if busy == "speaker" else now - 8
    task = asyncio.create_task(continuous_watchdog(
        turn, tts, followup_seconds=followup_seconds, stall_seconds=120,
        user_activity=lambda: (now - 10, now if busy == "user" else now - 9),
        last_accepted_at=lambda: now - 8,
    ))
    try:
        # Long enough for one full watchdog poll to reach its verdict.
        await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
        assert task.done() == (busy == "quiet")
        if task.done():
            assert task.result() == "followup_timeout"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("acknowledged, backend, elapsed, ended", [
    (False, False, 7, False),
    (False, False, 9, True),
    (False, True, 9, True),
    (True, True, 9, False),
    (True, True, 31, True),
    (True, False, 9, True),
])
async def test_live_deadlines_ignore_provider_chatter(acknowledged, backend, elapsed, ended):
    now = time.monotonic()
    turn = FakeLiveTurn(chunks_received=1)
    turn.backend_pending = backend
    turn.last_chunk_at = lambda: now
    turn.last_activity_at = lambda: time.monotonic()
    task = asyncio.create_task(continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (now - elapsed - 1, now - elapsed),
        last_accepted_at=lambda: now - elapsed + 1 if acknowledged else 0,
    ))
    try:
        await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
        assert task.done() is ended
        if ended:
            assert task.result() == ("response_stalled" if backend or not acknowledged else "followup_timeout")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_partial_transcripts_do_not_end_the_conversation():
    now = time.monotonic()
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), now)
    task = asyncio.create_task(continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (now - 2, now - 1), last_accepted_at=lambda: 0,
    ))
    try:
        for delta in ("stop", " the timer"):
            await turn.on_event({
                "type": "session.input_transcript.delta", "delta": delta,
                "start_ms": 0, "end_ms": 1000,
            })
            await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
            assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("reason", ["followup_timeout", "conversation_ended"])
async def test_the_hang_up_chirp_is_written_ahead_of_the_teardown_behind_it(reason):
    """The teardown runs behind the cue, not in front of it."""
    order: list[str] = []
    chirped = asyncio.Event()

    def note(call: str) -> None:
        order.append(call)
        if call == "write_segment":
            chirped.set()

    async def teardown(_reason):
        order.append("teardown")
        try:
            await asyncio.wait_for(chirped.wait(), timeout=0.25)
        except TimeoutError:
            order.append("still_silent")

    async def release():
        order.append("release")

    async def restore():
        order.append("unduck")

    tts = FakeTts(on_call=note)
    loop = answered_loop(tts=tts)
    loop._peering.session_ended = teardown
    loop._turns.turn.release = release
    loop._assistant_output.ducker.restore = restore
    loop._turns.output_episode = await loop._assistant_output.begin_turn_episode(None)
    await loop._turns.end(reason)
    assert tts.writes == [loop._assistant_output._chirp_off_pcm]
    assert "still_silent" not in order
    chirp = order.index("write_segment")
    assert chirp < order.index("release")
    assert chirp < order.index("unduck")
    await loop._cancel_fire_and_forget_tasks()


async def test_playout_acceptance_updates_after_the_first_answer():
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), time.monotonic())
    turn._enqueue_audio(AudioOutChunk(b"\x00\x40" * 120))
    turn._audio_q.put_nowait(None)
    report = PlaybackReport(accepted_audio=True)
    started = time.monotonic()
    await play_responses(turn, FakeTts(), report=report)
    assert report.last_accepted_at >= started


@pytest.mark.parametrize("reason", ["response_stalled", "playout_stalled"])
async def test_stalled_conversation_plays_one_failure_cue(reason):
    tts = FakeTts()
    loop = answered_loop(tts=tts)
    cue = AsyncMock()
    loop._turns._play_cue = cue
    loop._turns.output_episode = await loop._assistant_output.begin_turn_episode(None)
    await loop._turns.end(reason)
    cue.assert_awaited_once()
    assert not tts.writes
    assert tts.flush_calls == 1
    await loop._cancel_fire_and_forget_tasks()
