# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A `TurnHost` double for `ResearchAnnouncer` tests.

The announcer reads the speaker's state through `condition()` and reaches
output only through this Protocol, so a test states the world as seven
booleans and reads back what the announcer asked the loop to do.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from jasper.research import DONE, RUNNING, ResearchJob
from jasper.voice.research_announcer import HostCondition


def _job(
    *,
    id: str = "job12345",
    status=DONE,
    result: str | None = "Use induction if you want fast response.",
    error: str | None = None,
    created_at: float | None = None,
    announced: bool = False,
    read: bool = False,
) -> ResearchJob:
    now = created_at if created_at is not None else time.time()
    return ResearchJob(
        id=id,
        query="research cooktops",
        status=status,
        result=result,
        error=error,
        created_at=now,
        finished_at=None if status == RUNNING else now,
        announced=announced,
        read=read,
    )


class _MarkingScheduler:
    def __init__(self) -> None:
        self.announced: list[str] = []
        self.read: list[str] = []

    def mark_announced(self, job_id: str) -> None:
        self.announced.append(job_id)

    def mark_read(self, job_id: str) -> None:
        self.read.append(job_id)


class FakeTurnHost:
    def __init__(self, **condition: bool) -> None:
        self.in_session = False
        self.in_wake = True
        self.output_active = False
        self.measurement_active = False
        self.mic_muted = False
        self.spend_allowed = True
        self.connection_paused = False
        for name, value in condition.items():
            assert hasattr(self, name), f"unknown condition field {name!r}"
            setattr(self, name, value)
        self.turn_episode = False
        self.spoken: list[str] = []
        self.cues: list[str] = []
        self.begun: list[str | None] = []
        self.ended: list[str] = []
        self.refractory_holds: list[float] = []
        self.conversation_turns: list[tuple[str | None, str | None, dict]] = []
        self.cleanup_calls = 0
        self.cancel_timeout_cues = 0
        self.play_result = True
        self.cue_result = True
        # Run at the moment the announcer speaks, so a test can open a
        # measurement window or a session mid-announcement.
        self.on_play: Callable[[str], None] | None = None

    def condition(self) -> HostCondition:
        return HostCondition(
            in_session=self.in_session,
            in_wake=self.in_wake,
            output_active=self.output_active,
            measurement_active=self.measurement_active,
            mic_muted=self.mic_muted,
            spend_allowed=self.spend_allowed,
            connection_paused=self.connection_paused,
        )

    def turn_episode_active(self) -> bool:
        return self.turn_episode

    def hold_wake_refractory(self, sec: float) -> None:
        self.refractory_holds.append(sec)

    def record_conversation_turn(
        self,
        query: str | None,
        assistant_text: str | None,
        *,
        data_json: dict,
    ) -> None:
        self.conversation_turns.append((query, assistant_text, data_json))

    async def play_dynamic_text(self, text: str) -> bool:
        self.spoken.append(text)
        if self.on_play is not None:
            self.on_play(text)
        return self.play_result

    async def play_cue(self, slug: str) -> bool:
        self.cues.append(slug)
        return self.cue_result

    async def begin_turn(
        self, *, pre_roll: bool, text_context: str | None,
    ) -> None:
        assert pre_roll is False
        self.begun.append(text_context)

    async def end_turn(self, reason: str) -> None:
        self.ended.append(reason)

    async def cleanup_after_failed_begin(self) -> None:
        self.cleanup_calls += 1

    async def play_cancel_timeout_cue(self) -> None:
        self.cancel_timeout_cues += 1
