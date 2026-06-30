# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small ownership gate for assistant-facing speaker output."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

AssistantOutputKind = Literal["turn", "proactive", "admin", "feedback"]


@dataclass(frozen=True)
class AssistantOutputEpisode:
    """Token for one audible assistant episode."""

    id: int
    kind: AssistantOutputKind
    epoch: int


class AssistantOutputGate:
    """Allow only one assistant-output episode to own TTS/ducking."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._active: AssistantOutputEpisode | None = None
        self._epoch = 0
        self._next_id = 0

    @property
    def is_active(self) -> bool:
        return self._active is not None

    @property
    def active_kind(self) -> AssistantOutputKind | None:
        active = self._active
        return active.kind if active is not None else None

    @property
    def epoch(self) -> int:
        return self._epoch

    def is_current(self, episode: AssistantOutputEpisode) -> bool:
        active = self._active
        return (
            active is not None
            and active.id == episode.id
            and episode.epoch == self._epoch
        )

    async def begin_turn(self) -> AssistantOutputEpisode:
        while True:
            async with self._lock:
                active = self._active
                if active is None:
                    return self._begin_locked("turn")
                if active.kind == "turn":
                    return active
                self._epoch += 1
                idle = self._idle
            await idle.wait()

    async def end_turn(self, episode: AssistantOutputEpisode | None = None) -> None:
        await self.end(episode, kind="turn")

    async def begin_if_idle(
        self,
        kind: AssistantOutputKind,
    ) -> AssistantOutputEpisode | None:
        async with self._lock:
            if self._active is not None:
                return None
            return self._begin_locked(kind)

    async def end(
        self,
        episode: AssistantOutputEpisode | None,
        *,
        kind: AssistantOutputKind | None = None,
    ) -> None:
        async with self._lock:
            active = self._active
            if active is None:
                return
            matches = (
                active.id == episode.id
                if episode is not None else active.kind == kind
            )
            if not matches:
                return
            self._active = None
            self._idle.set()

    def _begin_locked(self, kind: AssistantOutputKind) -> AssistantOutputEpisode:
        self._epoch += 1
        self._next_id += 1
        episode = AssistantOutputEpisode(
            id=self._next_id,
            kind=kind,
            epoch=self._epoch,
        )
        self._active = episode
        self._idle.clear()
        return episode
