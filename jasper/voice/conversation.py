# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conversation policy shared by endpointed and continuous voice adapters."""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from ..tools import ToolRegistry, tool


END_OF_UTTERANCE_SILENCE_SEC = 0.8
# Both turn watchdogs poll on this; every condition they read is
# second-scale.
WATCHDOG_POLL_SEC = 0.25
NO_SPEECH_ABORT_SEC = 5.0
END_CONVERSATION_TOOL = "end_conversation"


def register_conversation_tools(registry: ToolRegistry, request_end: Callable[[], None]) -> None:
    @tool(llm_description=(
        "End this voice conversation and return to wake-word listening. Use for a "
        "standalone cancel, never mind, okay thanks, or goodbye. Do not use for "
        "cancel my timer, stop music, or thanks followed by another request; "
        "use the relevant local tool instead. Ending does not undo completed actions."
    ))
    async def end_conversation() -> dict:
        request_end()
        return {"status": "conversation_ended"}

    registry.register(end_conversation)


async def continuous_watchdog(turn, tts, *, followup_seconds, stall_seconds, user_activity, spend_allowed=lambda: True):
    started_at = time.monotonic()
    next_spend_check = started_at
    pending_count, progressed_at = 0, started_at
    while True:
        await asyncio.sleep(WATCHDOG_POLL_SEC)
        if turn.turn_lost():
            return "connection_lost"
        now = time.monotonic()
        if now >= next_spend_check:
            next_spend_check = now + 1.0
            if not spend_allowed():
                return "spend_cap_reached"
        speech_started, last_speech = user_activity()
        if not speech_started:
            if now - started_at >= NO_SPEECH_ABORT_SEC:
                return "no_speech"
            continue
        if now - last_speech < END_OF_UTTERANCE_SILENCE_SEC:
            continue
        if turn.backend_pending:
            if now - turn.last_activity_at() >= stall_seconds:
                return "response_stalled"
            continue
        if turn.last_chunk_at() < speech_started:
            if now - max(last_speech, turn.last_activity_at()) >= stall_seconds:
                return "response_stalled"
            continue
        pending = turn.audio_chunks_pending()
        if pending != pending_count:
            pending_count, progressed_at = pending, now
        if pending:
            if now - progressed_at >= stall_seconds:
                return "playout_stalled"
            continue
        deadline = followup_seconds + max(
            last_speech, turn.last_chunk_at(), turn.last_activity_at(), tts.expected_drain_at(),
        )
        if now >= deadline:
            return "followup_timeout"
