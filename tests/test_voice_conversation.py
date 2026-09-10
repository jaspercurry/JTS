# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from jasper.tools import ToolRegistry, dispatch_tool
from jasper.voice.conversation import continuous_watchdog, register_conversation_tools
from jasper.voice_daemon import State
from tests._async_wait import wait_until
from tests._live_turn_fake import FakeLiveTurn, silent_frame
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
    loop._followup.seconds = 5
    return loop


@pytest.mark.parametrize("followup,manual,expected_wait", [(5, False, True), (0, False, False), (5, True, False)])
async def test_answer_holds_conversation_until_followup_window_expires(followup, manual, expected_wait):
    loop = answered_loop()
    loop._followup.seconds = followup
    loop._manual_endpoint_this_turn = manual
    old = loop._turn
    finish = AsyncMock()
    loop._assistant_output.finish_turn_episode = finish
    await loop._finish_response("ended")
    if expected_wait:
        assert loop._state is State.SESSION
        assert old.release_calls == 0
        finish.assert_not_awaited()
        loop._followup.deadline = time.monotonic() - 1
        await wait_until(lambda: loop._state is State.WAKE)
    assert loop._state is State.WAKE
    assert old.release_calls == 1
    assert loop._usage_store.close_calls == 1
    finish.assert_awaited_once()
    await loop._cancel_fire_and_forget_tasks()


async def test_followup_speech_reopens_input_without_closing_output_episode():
    loop = answered_loop()
    old = loop._turn
    new = FakeLiveTurn()
    async def pending_audio():
        await asyncio.Event().wait()
        yield
    new.audio_out_chunks = pending_audio
    new.wait_for_interrupt = asyncio.Event().wait
    new.last_activity_at = time.monotonic
    loop._connection.acquire_turn = AsyncMock(return_value=new)
    loop._content_activity.refresh_now = AsyncMock()
    loop._prepare_assistant_loudness_context = AsyncMock()
    loop._usage_store.open_session = lambda **_: 8
    loop._pre_roll.append(silent_frame())
    finish = AsyncMock()
    loop._assistant_output.finish_turn_episode = finish
    await loop._finish_response("ended")
    await loop._resume_followup()
    await wait_until(lambda: new.send_audio_calls > 0)
    assert old.release_calls == 1
    assert new.send_audio_calls == 1
    finish.assert_not_awaited()
    await loop._end_turn("stopping")
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
        await asyncio.sleep(0.1)
        assert task.done() == (busy == "quiet")
        if task.done():
            assert task.result() == "followup_timeout"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
