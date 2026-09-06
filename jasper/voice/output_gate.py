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
        self._admission_resumed = asyncio.Event()
        self._admission_resumed.set()
        self._admission_paused = False
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
    def admission_paused(self) -> bool:
        return self._admission_paused

    @property
    def epoch(self) -> int:
        return self._epoch

    async def wait_idle(self, timeout: float) -> bool:
        """Wait until no episode owns output. Returns whether it went idle.

        Event-driven off the same idle signal `begin_turn` waits on — no
        polling, and no cost at all when the gate is already idle (the
        loop body never runs, so this never yields to the event loop).

        The re-check loop is deliberate: `asyncio.Event` wakes every
        waiter the instant it is set, so a waiter can be resumed by an
        episode that ended and be scheduled only after the next one has
        begun. Re-reading `_active` after each wake makes the answer
        true at return time rather than at wake time.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._active is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
        return True

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
                if self._admission_paused:
                    resumed = self._admission_resumed
                    idle = None
                else:
                    resumed = None
                    active = self._active
                    if active is None:
                        return self._begin_locked("turn")
                    if active.kind == "turn":
                        return active
                    self._epoch += 1
                    idle = self._idle
            if resumed is not None:
                await resumed.wait()
            elif idle is not None:
                await idle.wait()

    async def end_turn(self, episode: AssistantOutputEpisode | None = None) -> None:
        await self.end(episode, kind="turn")

    async def begin_if_idle(
        self,
        kind: AssistantOutputKind,
    ) -> AssistantOutputEpisode | None:
        async with self._lock:
            return self._begin_if_idle_locked(kind)

    async def hand_over_if_current(
        self,
        episode: AssistantOutputEpisode,
        kind: AssistantOutputKind,
    ) -> AssistantOutputEpisode | None:
        """Hand ``episode``'s ownership to a fresh ``kind`` episode, or refuse.

        Returns ``None`` having changed nothing — so ``episode`` is still
        its caller's to finish — when ``episode`` is no longer the active
        one, or when admission is paused. Refusing before ending is the
        whole difference between a refusal and a silence: ending first
        would strand a caller that still believes it owns output, and would
        open the gate for a queued `begin_turn` waiter rather than for the
        sound this call exists to let through.

        The caller that surrenders an episode so a specific sound can be
        heard needs the succession, not the two halves: `end` then
        `begin_if_idle` only works while nothing yields between them, and a
        `begin_turn` waiter queued on the idle signal would otherwise take
        the gate first and the sound would be skipped (NN-6). Ending and
        beginning inside the same lock hold makes that unrepresentable —
        the woken waiter cannot observe the gap because it must acquire
        this lock to act on it.
        """

        async with self._lock:
            if self._admission_paused or not self.is_current(episode):
                return None
            self._end_locked(episode, kind=None)
            return self._begin_if_idle_locked(kind)

    async def pause_admission(self) -> bool:
        """Atomically refuse every future assistant-output admission.

        Returns whether this call changed the gate. Repeated pauses are
        harmless and retain the same closed admission boundary.
        """

        async with self._lock:
            if self._admission_paused:
                return False
            self._admission_paused = True
            self._admission_resumed.clear()
            return True

    async def drain_paused(self, timeout: float) -> bool:
        """Boundedly drain the episode admitted before ``pause_admission``.

        Refuse misuse rather than turning a non-atomic observe-then-wait back
        into the authority. Once paused, no episode can replace the one this
        method observes, so ``True`` proves the boundary is silent now.
        """

        if not self._admission_paused:
            raise RuntimeError("assistant output admission is not paused")
        return await self.wait_idle(timeout)

    async def resume_admission(self) -> bool:
        """Reopen output admission, idempotently.

        Returns whether this call changed the gate. Waiting turns are released
        together and still serialize through the existing ownership lock.
        """

        async with self._lock:
            if not self._admission_paused:
                return False
            self._admission_paused = False
            self._admission_resumed.set()
            return True

    async def end(
        self,
        episode: AssistantOutputEpisode | None,
        *,
        kind: AssistantOutputKind | None = None,
    ) -> None:
        async with self._lock:
            self._end_locked(episode, kind=kind)

    def _end_locked(
        self,
        episode: AssistantOutputEpisode | None,
        *,
        kind: AssistantOutputKind | None,
    ) -> None:
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

    def _begin_if_idle_locked(
        self,
        kind: AssistantOutputKind,
    ) -> AssistantOutputEpisode | None:
        if self._admission_paused or self._active is not None:
            return None
        return self._begin_locked(kind)

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
