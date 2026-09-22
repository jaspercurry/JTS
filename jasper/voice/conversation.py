# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conversation policy shared by endpointed and continuous voice adapters."""
from __future__ import annotations

import asyncio
import logging
import time

from ..log_event import log_event
from .speech_activity import END_OF_UTTERANCE_SILENCE_SEC

logger = logging.getLogger(__name__)


# Both turn watchdogs poll on this; every condition they read is
# second-scale.
WATCHDOG_POLL_SEC = 0.25
NO_SPEECH_ABORT_SEC = 5.0
# The frontend needs time to voice a completed backend answer.
BACKEND_ANSWER_GRACE_SEC = 8.0
ACKNOWLEDGED_BACKEND_SEC = 30.0
# See ADR-0321.
FIRST_ANSWER_SEC = 5.0


async def continuous_watchdog(
    turn, tts, *, followup_seconds, stall_seconds, speech, playback,
    spend_allowed=lambda: True,
):
    started_at = time.monotonic()
    next_spend_check = started_at
    pending_state, progressed_at = (0, 0.0), started_at
    while True:
        await asyncio.sleep(WATCHDOG_POLL_SEC)
        lost = turn.turn_lost()
        now = time.monotonic()
        if not lost and now >= next_spend_check:
            next_spend_check = now + 1.0
            if not spend_allowed():
                return "spend_cap_reached"
        speech_started, last_speech = speech.started_at, speech.last_at
        if not lost:
            if speech.confirming(now, WATCHDOG_POLL_SEC):
                continue
            if not speech_started:
                if now - started_at >= NO_SPEECH_ABORT_SEC:
                    return "no_speech"
                continue
            if now - last_speech < END_OF_UTTERANCE_SILENCE_SEC:
                continue
        accepted_at = playback.last_accepted_at
        writing_since = playback.write_started_at
        if writing_since:
            if now - writing_since >= stall_seconds:
                return _resolved(
                    "playout_stalled", now, last_speech, accepted_at,
                    tts.expected_drain_at(), turn.audio_chunks_pending(), turn, None,
                    writing_since,
                )
            continue
        if not lost and turn.backend_pending:
            if now - last_speech >= ACKNOWLEDGED_BACKEND_SEC:
                return "response_stalled"
            continue
        pending = turn.audio_chunks_pending()
        drain_at = tts.expected_drain_at()
        if (pending, accepted_at) != pending_state:
            pending_state, progressed_at = (pending, accepted_at), now
        if pending or (lost and now < drain_at):
            if now - progressed_at >= stall_seconds:
                return _resolved(
                    "playout_stalled", now, last_speech, accepted_at,
                    drain_at, pending, turn, None,
                )
            continue
        if lost:
            return "connection_lost"
        if accepted_at < speech_started and turn.backend_completed_at < speech_started:
            deadline = last_speech + FIRST_ANSWER_SEC
            wait = "first_answer"
        else:
            deadline = max(drain_at, followup_seconds + max(last_speech, playback.audible_drain_at))
            wait = "followup"
            if (
                speech_started <= turn.backend_completed_at
                and accepted_at < turn.backend_completed_at
            ):
                # Backend completion alone is not proof that its answer reached playout.
                deadline = max(
                    deadline, turn.backend_completed_at + followup_seconds,
                    min(
                        turn.backend_completed_at + BACKEND_ANSWER_GRACE_SEC,
                        last_speech + ACKNOWLEDGED_BACKEND_SEC,
                    ),
                )
        if now >= deadline:
            return _resolved(
                "followup_timeout", now, last_speech, accepted_at,
                drain_at, pending, turn, deadline, wait=wait,
                audible_drain_at=playback.audible_drain_at,
            )


def _resolved(
    reason, now, last_speech, accepted_at, drain_at, pending, turn, deadline,
    writing_since=0.0, *, wait=None, audible_drain_at=0.0,
):
    """Disambiguate a follow-up close from a playout stall in the turn timeline.

    Zero anchors mean absent; negative ages mean scheduled future playout.
    """
    log_event(
        logger, "voice.turn_deadline", reason=reason, wait=wait,
        activity_age_ms=int((now - turn.last_activity_at()) * 1000),
        last_speech_age_ms=int((now - last_speech) * 1000),
        accepted_age_ms=int((now - accepted_at) * 1000) if accepted_at else None,
        drain_age_ms=int((now - drain_at) * 1000) if drain_at else None,
        audible_drain_age_ms=int((now - audible_drain_at) * 1000) if audible_drain_at else None,
        overdue_ms=None if deadline is None else int((now - deadline) * 1000),
        chunks_pending=pending,
        backend_pending=turn.backend_pending,
        backend_completed_age_ms=(
            int((now - turn.backend_completed_at) * 1000) if turn.backend_completed_at else None
        ),
        write_in_flight_ms=int((now - writing_since) * 1000) if writing_since else None,
    )
    return reason
