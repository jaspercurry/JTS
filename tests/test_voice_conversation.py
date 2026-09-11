# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from jasper.tools import ToolRegistry, dispatch_tool
from jasper.voice.conversation import (
    THINK_ALLOWANCE_SEC,
    WATCHDOG_POLL_SEC,
    continuous_watchdog,
    register_conversation_tools,
)
from jasper.voice.turn_lifecycle import State
from tests._async_wait import wait_until
from tests._live_turn_fake import FakeLiveTurn
from tests._playout import FakeTts
from tests._wake_loop import wake_loop_for_tests
from tests.usage_store_fixtures import FakeUsageStore


def answered_loop():
    loop = wake_loop_for_tests(usage_store=FakeUsageStore())
    loop._turns.state = State.SESSION
    loop._turns.turn = FakeLiveTurn(chunks_received=1)
    loop._turns.session_id = 7
    loop._turns.user_speech_seen = True
    loop._turns.playback_report.accepted_audio = True
    return loop


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
        request_end=lambda: None,
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


@pytest.mark.parametrize(
    "run_text, dismissed",
    [
        ("Okay, thanks.", True),
        ("okay thank you", True),
        ("That's all", True),
        ("Nevermind", True),
        ("okay thanks for the weather report", False),
        ("stop the timer", False),
        ("", False),
    ],
)
async def test_a_standalone_dismissal_ends_the_turn_without_the_model(run_text, dismissed):
    """A whole-utterance dismissal needs no delegation and no tool call."""
    now = time.monotonic()
    turn = FakeLiveTurn(chunks_received=1)
    turn.user_run_text = run_text
    turn.last_chunk_at = lambda: now
    turn.last_activity_at = lambda: now
    ends = []
    task = asyncio.create_task(continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (now - 10, now - 9),
        request_end=lambda: ends.append(1),
    ))
    try:
        await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
        assert task.done() is dismissed
        assert len(ends) == int(dismissed)
        if dismissed:
            assert task.result() == "dismissed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_dismissal_ends_the_conversation_the_way_the_tool_does():
    loop = answered_loop()
    ended = AsyncMock()
    loop._peering.session_ended = ended
    now = time.monotonic()
    turn = loop._turns.turn
    turn.user_run_text = "Okay, thank you."
    turn.last_chunk_at = lambda: now
    turn.last_activity_at = lambda: now
    reason = await continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (now - 10, now - 9),
        request_end=loop._turns.request_conversation_end,
    )
    assert reason == "dismissed"
    await wait_until(lambda: loop._turns.state is State.WAKE)
    ended.assert_awaited_once_with("conversation_ended")
    assert loop._usage_store.close_calls == 1
    await loop._cancel_fire_and_forget_tasks()


@pytest.mark.parametrize("slack, ended", [(2.0, False), (-0.5, True)])
async def test_a_user_run_that_draws_no_audio_is_bounded_by_the_followup_window(
    slack, ended,
):
    """Not by `stall_seconds`, which no longer funds a long delegation."""
    followup_seconds = 1.0
    now = time.monotonic()
    silent_since = now - (followup_seconds + THINK_ALLOWANCE_SEC) + slack
    turn = FakeLiveTurn()
    turn.last_chunk_at = lambda: 0.0
    turn.last_activity_at = lambda: silent_since
    task = asyncio.create_task(continuous_watchdog(
        turn, FakeTts(), followup_seconds=followup_seconds, stall_seconds=120,
        user_activity=lambda: (now - 30, silent_since),
        request_end=lambda: None,
    ))
    try:
        await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
        assert task.done() is ended
        if ended:
            assert task.result() == "response_stalled"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
