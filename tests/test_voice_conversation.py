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
    continuous_watchdog,
    register_conversation_tools,
)
from jasper.voice_daemon import State
from tests._async_wait import wait_until
from tests._live_turn_fake import FakeLiveTurn
from tests._log_events import event_records
from tests._wake_loop import wake_loop_for_tests, FakeTts
from tests.usage_store_fixtures import FakeUsageStore


def answered_loop():
    loop = wake_loop_for_tests()
    loop._state = State.SESSION
    loop._turn = FakeLiveTurn(chunks_received=1)
    loop._session_id = 7
    loop._usage_store = FakeUsageStore()
    loop._user_speech_seen = True
    loop._playback_report.accepted_audio = True
    return loop


async def test_endpointed_answer_closes_the_turn_once_playout_drains(caplog):
    """No host follow-up window survives: see ADR-0292."""
    loop = answered_loop()
    old = loop._turn
    finish = AsyncMock()
    loop._assistant_output.finish_turn_episode = finish
    ended = AsyncMock()
    loop._peering.session_ended = ended
    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        await loop._finish_response("ended")
        assert not event_records(caplog, "conversation.followup")
    assert loop._state is State.WAKE
    assert old.release_calls == 1
    assert loop._usage_store.close_calls == 1
    finish.assert_awaited_once()
    ended.assert_awaited_once_with("ended")
    await loop._cancel_fire_and_forget_tasks()


async def test_end_conversation_tool_closes_even_without_more_mic_frames():
    loop = answered_loop()
    registry = ToolRegistry()
    register_conversation_tools(registry, loop.request_conversation_end)
    result = await dispatch_tool(registry, "end_conversation", {})
    await wait_until(lambda: loop._state is State.WAKE)
    assert result == {"status": "conversation_ended"}
    assert loop._usage_store.close_calls == 1
    await loop._cancel_fire_and_forget_tasks()


@pytest.mark.parametrize("busy", ["speaker", "user", "tool", "quiet"])
async def test_live_followup_waits_for_playout_speech_and_tools(busy):
    now = time.monotonic()
    turn = FakeLiveTurn(chunks_received=1)
    turn.last_chunk_at = lambda: now - 8
    turn.audio_chunks_pending = lambda: 0
    turn.backend_pending = busy == "tool"
    turn.last_activity_at = lambda: now if busy == "tool" else now - 8
    tts = FakeTts()
    tts.expected_drain_at = lambda: now + 10 if busy == "speaker" else now - 8
    task = asyncio.create_task(continuous_watchdog(
        turn, tts, followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (now - 10, now if busy == "user" else now - 9),
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
