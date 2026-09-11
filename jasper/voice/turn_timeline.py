# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's per-turn latency timeline: when each stage happened.

Owns the in-flight turn's stage stamps, the anchor they are measured from
(wake fire time, or the manual turn's own clock), the wake-event id the
stamps belong to and the last COMPLETE turn's published deltas. Turns a
turn into one `event=turn.timeline` line plus `/state.voice.last_turn_ms`.

`WakeLoop` builds one at construction time, stamps stages from its hot
paths, and reads the public attributes directly for `session_status`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from jasper.log_event import log_event

from ..wake_events import make_event_id
from .wake_telemetry import WakeTelemetry

logger = logging.getLogger("jasper.voice_daemon")

# Published latency stages; absent stages remain absent.
_TURN_TIMELINE_STAGES = (
    "cue_attempt",
    "cue_accepted",
    "first_audio_to_provider",
    "speech_end",
    "end_input",
    "first_response",
    # Hand-off to fan-in: the first assistant PCM write the playout socket
    # accepted. The ring/CamillaDSP/outputd tail past it is not timeable here.
    "first_write",
)


class TurnTimeline:
    """Stage stamps for the in-flight turn, and the last one published."""

    def __init__(
        self,
        wake_telemetry: WakeTelemetry,
        *,
        endpointer: Callable[[], str],
    ) -> None:
        # stage -> time.monotonic(); reset at turn start, rendered as
        # integer-ms deltas from `anchor`. `anchor` 0.0 means no turn is
        # being timed, which is what closes a timeline at teardown.
        self.stages: dict[str, float] = {}
        self.event_id: str | None = None
        self.anchor: float = 0.0
        self.anchor_kind: str = "manual"
        self.last_turn_ms: dict[str, object] = {}
        self._telemetry = wake_telemetry
        self._endpointer = endpointer

    def anchor_at(self, at: float = 0.0) -> None:
        """Wake turns use fire time; manual turns start their own clock."""
        self.stages = {}
        self.event_id = (
            self._telemetry.current_event_id if at else None
        ) or make_event_id()
        self.anchor = at or time.monotonic()
        self.anchor_kind = "wake" if at else "manual"

    def stamp(self, stage: str, *, first: bool = True) -> None:
        """Record one latency stage of the in-flight turn.

        A `time.monotonic()` assignment and nothing else — every caller is
        on a hot path (wake frame, session frame, response playout).
        `first=False` keeps the LAST occurrence, which is what the
        end-of-utterance silence clock wants after a mid-sentence pause.
        """
        if self.anchor == 0.0:
            return
        if first and stage in self.stages:
            return
        self.stages[stage] = time.monotonic()

    def deltas_ms(self) -> dict[str, int]:
        """Integer-ms deltas from this turn's anchor, stages that did not
        happen omitted. Empty when no turn has been anchored."""
        if self.anchor == 0.0:
            return {}
        deltas = {
            f"{stage}_ms": int((at - self.anchor) * 1000)
            for stage in _TURN_TIMELINE_STAGES
            if (at := self.stages.get(stage)) is not None
        }
        deltas["total_ms"] = int((time.monotonic() - self.anchor) * 1000)
        return deltas

    def emit(self, outcome: str) -> None:
        """Publish complete turns to status; close every timeline before teardown."""
        timeline: dict[str, Any] = self.deltas_ms()
        try:
            if timeline:
                log_event(
                    logger,
                    "turn.timeline",
                    event_id=self.event_id,
                    anchor=self.anchor_kind,
                    endpointer=self._endpointer(),
                    outcome=outcome,
                    **timeline,
                )
                if outcome == "complete":
                    self.last_turn_ms = {
                        "event_id": self.event_id,
                        "anchor": self.anchor_kind,
                        "outcome": outcome,
                        **timeline,
                    }
        finally:
            self.anchor = 0.0

    def observer(
        self, stage: str, *, event_stage: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        timeline, anchor = self.stages, self.anchor
        event_id = self.event_id

        async def observe() -> None:
            if timeline is not self.stages or not anchor or anchor != self.anchor:
                return
            self.stamp(stage)
            if event_stage is not None and event_id is not None:
                await self._telemetry.stage(event_stage, event_id=event_id)

        return observe
