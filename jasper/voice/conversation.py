# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conversation policy shared by endpointed and continuous voice adapters."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from ..log_event import log_event
from ..tools import ToolRegistry, tool

logger = logging.getLogger(__name__)


END_OF_UTTERANCE_SILENCE_SEC = 0.8
# Both turn watchdogs poll on this; every condition they read is
# second-scale.
WATCHDOG_POLL_SEC = 0.25
NO_SPEECH_ABORT_SEC = 5.0
END_CONVERSATION_TOOL = "end_conversation"


# Live may wait longer only after audio has reached playout.
UNANSWERED_SPEECH_SEC = 8.0
ACKNOWLEDGED_BACKEND_SEC = 30.0


def register_conversation_tools(registry: ToolRegistry, request_end: Callable[[], None]) -> None:
    # A dismissal survives cancellation from any source — a new delegation
    # on Live, a barge-in on the other adapters — deliberately.
    @tool(survives_cancellation=True, llm_description=(
        "End this voice conversation and return to wake-word listening. Use for a "
        "standalone cancel, never mind, okay thanks, or goodbye. Do not use for "
        "cancel my timer, stop music, or thanks followed by another request; "
        "use the relevant local tool instead. Ending does not undo completed actions."
    ))
    async def end_conversation() -> dict:
        request_end()
        return {"status": "conversation_ended"}

    registry.register(end_conversation)


async def continuous_watchdog(
    turn, tts, *, followup_seconds, stall_seconds, user_activity, last_accepted_at,
    spend_allowed=lambda: True, write_started_at=lambda: 0.0,
):
    started_at = time.monotonic()
    next_spend_check = started_at
    pending_state, progressed_at = (0, 0.0), started_at
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
        accepted_at = last_accepted_at()
        if accepted_at < speech_started:
            if now - last_speech >= UNANSWERED_SPEECH_SEC:
                return "response_stalled"
            continue
        if turn.backend_pending:
            if now - last_speech >= ACKNOWLEDGED_BACKEND_SEC:
                return "response_stalled"
            continue
        writing_since = write_started_at()
        if writing_since:
            if now - writing_since >= stall_seconds:
                return _resolved(
                    "playout_stalled", now, last_speech, accepted_at,
                    tts.expected_drain_at(), turn.audio_chunks_pending(), turn, None,
                    writing_since,
                )
            continue
        pending = turn.audio_chunks_pending()
        if (pending, accepted_at) != pending_state:
            pending_state, progressed_at = (pending, accepted_at), now
        if pending:
            if now - progressed_at >= stall_seconds:
                return _resolved(
                    "playout_stalled", now, last_speech, accepted_at,
                    tts.expected_drain_at(), pending, turn, None,
                )
            continue
        drain_at = tts.expected_drain_at()
        deadline = followup_seconds + max(last_speech, accepted_at, drain_at)
        if turn.backend_completed_at >= speech_started:
            # Live can speak before or after backend completion, with no
            # final-audio identity. Give the handoff time without treating
            # completion as proof of speech, or renewing on generic activity.
            deadline = max(deadline, min(
                turn.backend_completed_at + UNANSWERED_SPEECH_SEC,
                last_speech + ACKNOWLEDGED_BACKEND_SEC,
            ))
        if now >= deadline:
            return _resolved(
                "followup_timeout", now, last_speech, accepted_at,
                drain_at, pending, turn, deadline,
            )


def _resolved(
    reason, now, last_speech, accepted_at, drain_at, pending, turn, deadline,
    writing_since=0.0,
):
    """Report which branch ended the turn, and the anchors that chose it.

    A turn that ends before the user's answer is spoken looks identical in
    `turn.timeline` to one that ended normally — both are `outcome=complete`,
    because a plain `followup_timeout` IS the normal close. Only the anchors
    distinguish them, so they are recorded where the decision is made. Ages are
    relative to the deciding instant; a negative age means the anchor is in the
    future (audio still scheduled to play). See #5091.
    """
    log_event(
        logger, "voice.turn_deadline", reason=reason,
        last_speech_age_ms=int((now - last_speech) * 1000),
        accepted_age_ms=int((now - accepted_at) * 1000),
        drain_age_ms=int((now - drain_at) * 1000),
        overdue_ms=None if deadline is None else int((now - deadline) * 1000),
        chunks_pending=pending,
        backend_pending=turn.backend_pending,
        backend_completed_age_ms=(
            int((now - turn.backend_completed_at) * 1000) if turn.backend_completed_at else None
        ),
        write_in_flight_ms=int((now - writing_since) * 1000) if writing_since else None,
    )
    return reason
