# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's research announcer: the "tell me later" promise.

Holds finished research jobs until the speaker is free to talk, speaks
the "ready?" prompt, and runs the yes/no confirmation window as one
model turn. Everything it needs from the wake loop arrives through
`TurnHost`; nothing here reads loop state directly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from jasper.log_event import log_event

from ..research import (
    DONE,
    FAILED,
    RESEARCH_EMPTY_RESULT_TEXT,
    ResearchJob,
    ResearchScheduler,
)

logger = logging.getLogger("jasper.voice_daemon")

# Research results are a "tell me later" promise, unlike timer chimes:
# completing during a voice session must defer, not disappear. Keep the
# in-memory hold queue small so a long session plus a burst of completions
# cannot grow without bound.
RESEARCH_PENDING_ANNOUNCE_CAP = 5
RESEARCH_FAILURE_COOLDOWN_SEC = 60.0 * 60.0
RESEARCH_FAILED_CUE_SLUG = "research_failed"
RESEARCH_READY_CONFIRMATION_TEXT = (
    "Your research is ready — want me to read it now?"
)
RESEARCH_CONFIRMATION_REFRACTORY_SEC = 0.35
RESEARCH_CONFIRMATION_OPEN_CANCEL_TIMEOUT_SEC = 20.0


def _research_confirmation_instruction(job: ResearchJob) -> str:
    return (
        "For this turn only, the user is answering yes or no about whether "
        f"to read research result {job.id}. If the answer is yes or an "
        f"affirmative, call read_research_result(job_id='{job.id}', "
        "decision='yes'). If the answer is no or a negative, call "
        f"read_research_result(job_id='{job.id}', decision='no'). Speak "
        "only the tool's returned text field. Do not answer from memory, "
        "summarize, ask a follow-up, or start new research."
    )


@dataclass(frozen=True)
class HostCondition:
    """What the host was doing at one instant — one snapshot per read."""

    in_session: bool
    in_wake: bool
    output_active: bool
    measurement_active: bool
    mic_muted: bool
    spend_allowed: bool
    connection_paused: bool


class ResearchWindow(Enum):
    """The confirmation window's lifecycle.

    OPENING covers the `begin_turn` await; OPEN the live model turn.
    DECIDED and CANCELLED are both terminal and the FIRST one wins: a
    window the household already answered stays DECIDED even if a wake
    lands on it, and one a wake already took stays CANCELLED. Only
    OPENING and OPEN can still be dismissed for silence.
    """

    IDLE = "idle"
    OPENING = "opening"
    OPEN = "open"
    DECIDED = "decided"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WindowSnapshot:
    """The window as the turn that carried it began tearing down."""

    job: ResearchJob | None
    state: ResearchWindow

    @property
    def undecided(self) -> bool:
        return self.job is not None and self.state in (
            ResearchWindow.OPENING, ResearchWindow.OPEN,
        )


class TurnHost(Protocol):
    """The wake loop, as the announcer sees it."""

    def condition(self) -> HostCondition: ...

    def turn_episode_active(self) -> bool: ...

    def hold_wake_refractory(self, sec: float) -> None: ...

    def record_conversation_turn(
        self,
        query: str | None,
        assistant_text: str | None,
        *,
        data_json: dict,
    ) -> None: ...

    async def play_dynamic_text(self, text: str) -> bool: ...

    async def play_cue(self, slug: str) -> bool: ...

    async def begin_turn(
        self, *, pre_roll: bool, text_context: str | None,
    ) -> None: ...

    async def end_turn(self, reason: str) -> None: ...

    async def cleanup_after_failed_begin(self) -> None: ...

    async def play_cancel_timeout_cue(self) -> None: ...


class ResearchAnnouncer:
    def __init__(
        self,
        host: TurnHost,
        *,
        scheduler: ResearchScheduler | None = None,
        pending_cap: int = RESEARCH_PENDING_ANNOUNCE_CAP,
        failure_cooldown_sec: float = RESEARCH_FAILURE_COOLDOWN_SEC,
    ) -> None:
        self._host = host
        self._scheduler = scheduler
        self._provider_id: str | None = None
        self._model: str | None = None
        self._pending: list[ResearchJob] = []
        self._pending_cap = pending_cap
        self._failure_cooldown_sec = failure_cooldown_sec
        self._last_failure_announce_at: float | None = None
        self._lock = asyncio.Lock()
        self._window = ResearchWindow.IDLE
        self._window_job: ResearchJob | None = None
        self._window_opening_done: asyncio.Event | None = None

    def set_scheduler(
        self,
        scheduler: ResearchScheduler | None,
        *,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Wire the research scheduler so announcements can mark jobs
        announced only after the wake loop has attempted the spoken path."""
        self._scheduler = scheduler
        self._provider_id = provider_id
        self._model = model

    @property
    def window_active(self) -> bool:
        return self._window is not ResearchWindow.IDLE

    def status(self) -> dict:
        return {
            "configured": self._scheduler is not None,
            "provider": self._provider_id,
            "model": self._model,
            "pending_announcements": len(self._pending),
            "confirmation_window_active": self.window_active,
        }

    async def announce_ready(self, job: ResearchJob) -> None:
        """Public hook called by `ResearchScheduler` when a job finishes.

        Research is a "tell me later" promise. Unlike timer chimes, a
        result that arrives mid-conversation is held until the wake loop
        returns to WAKE, then drained by the host's turn teardown.

        Held the same way while a room-correction measurement window is
        open (issue #1786): speaking would corrupt the sweep.

        The drain only runs on the household's next COMPLETED voice turn
        (`drain`), not on `MeasurementHold.resume()` — a queued result can
        therefore sit for a while, bounded only by `pending_cap`. Draining
        on resume would fire at the sweep's trailing edge, the
        in-flight-bleed window tracked as issue #1898.
        """
        async with self._lock:
            condition = self._host.condition()
            if condition.measurement_active:
                log_event(
                    logger,
                    "research.announce_suppressed",
                    job_id=job.id,
                    status=job.status,
                    reason="measurement_active",
                )
                self._queue_pending(job)
                return
            if condition.in_session or condition.output_active:
                self._queue_pending(job)
                return
            await self._speak(job)

    def _queue_pending(self, job: ResearchJob) -> None:
        for idx, pending in enumerate(self._pending):
            if pending.id == job.id:
                self._pending[idx] = job
                log_event(
                    logger,
                    "research.announce_pending_coalesced",
                    job_id=job.id,
                    status=job.status,
                )
                return
        self._pending.append(job)
        if len(self._pending) > self._pending_cap:
            dropped = self._pending.pop(0)
            log_event(
                logger,
                "research.announce_pending_dropped",
                job_id=dropped.id,
                status=dropped.status,
                cap=self._pending_cap,
                level=logging.WARNING,
            )
        log_event(
            logger,
            "research.announce_held",
            job_id=job.id,
            status=job.status,
            pending=len(self._pending),
        )

    async def drain(self) -> None:
        # measurement_active is bundled with session here (both mean "don't
        # emit ANY audio right now" — issue #1786), including in the
        # per-iteration re-check below. Without that per-iteration check, a
        # measurement window opening mid-batch loops forever: `_speak`'s own
        # measurement guard re-queues the job, which re-fills `_pending`,
        # which the `while` condition sees as "more work" — a tight
        # busy-spin with no sleep, for as long as the window stays open
        # (potentially minutes for a held crossover-v2 session, unlike the
        # normally-brief `output_active`, which stays out of this check).
        condition = self._host.condition()
        if (
            condition.in_session
            or condition.output_active
            or condition.measurement_active
        ):
            return
        async with self._lock:
            condition = self._host.condition()
            if (
                condition.in_session
                or condition.output_active
                or condition.measurement_active
            ):
                return
            while self._pending and self._host.condition().in_wake:
                batch = self._pending
                self._pending = []
                for idx, job in enumerate(batch):
                    condition = self._host.condition()
                    if condition.in_session or condition.measurement_active:
                        self._pending = batch[idx:] + self._pending
                        return
                    await self._speak(job)

    async def _speak(self, job: ResearchJob) -> None:
        condition = self._host.condition()
        if condition.measurement_active:
            log_event(
                logger,
                "research.announce_suppressed",
                job_id=job.id,
                status=job.status,
                reason="measurement_active",
            )
            self._queue_pending(job)
            return
        if condition.in_session or condition.output_active:
            self._queue_pending(job)
            return
        text: str | None
        if job.status == DONE and job.result:
            text = RESEARCH_READY_CONFIRMATION_TEXT
        elif job.status == DONE:
            log_event(
                logger,
                "research.announce_missing_result",
                job_id=job.id,
                level=logging.WARNING,
            )
            text = RESEARCH_EMPTY_RESULT_TEXT
        elif job.status == FAILED:
            text = None
        else:
            log_event(
                logger,
                "research.announce_skipped",
                job_id=job.id,
                status=job.status,
                reason="unexpected_status",
                level=logging.WARNING,
            )
            return

        if job.status == FAILED:
            remaining = self._failure_cooldown_remaining()
            if remaining > 0:
                log_event(
                    logger,
                    "research.announce_suppressed",
                    job_id=job.id,
                    status=job.status,
                    reason="failure_cooldown",
                    remaining_s=round(remaining, 1),
                    level=logging.WARNING,
                )
                self._mark_announced(job, read=False)
                return

        if job.status == FAILED:
            log_event(
                logger,
                "research.announce",
                job_id=job.id,
                status=job.status,
                mode="cue",
                cue=RESEARCH_FAILED_CUE_SLUG,
            )
            played = await self._host.play_cue(RESEARCH_FAILED_CUE_SLUG)
        else:
            assert text is not None
            # Log shape, not content: a research result can carry personal
            # material (medical/financial queries) and the journal is
            # persistent. Full text stays at DEBUG (cue manager) only.
            log_event(
                logger,
                "research.announce",
                job_id=job.id,
                status=job.status,
                mode="confirmation",
                text_len=len(text),
            )
            played = await self._host.play_dynamic_text(text)
        if not played:
            log_event(
                logger,
                "research.announce_playback_failed",
                job_id=job.id,
                status=job.status,
                level=logging.WARNING,
            )
            return
        if job.status == FAILED:
            self._last_failure_announce_at = asyncio.get_event_loop().time()
        elif job.status == DONE and job.result:
            self._mark_announced(job, read=False)
            self._host.hold_wake_refractory(
                RESEARCH_CONFIRMATION_REFRACTORY_SEC,
            )
            await self.open_confirmation_window(job)
            return
        self._mark_announced(
            job,
            read=job.status == DONE and bool(job.result),
        )

    async def open_confirmation_window(self, job: ResearchJob) -> None:
        reason = self._confirmation_guard_reason()
        if reason is not None:
            log_event(
                logger,
                "research.confirmation_window_skipped",
                job_id=job.id,
                reason=reason,
            )
            # session_active and measurement_active both mean "don't
            # emit ANY audio right now" — queue for the drain path
            # instead (issue #1786). The other reasons (mic_muted,
            # spend_cap_reached, connection_paused) mean "can't listen
            # for a reply" but speaking is still safe, so those fall
            # through to an immediate read.
            if reason in ("session_active", "measurement_active"):
                self._queue_pending(job)
                return
            await self._read_immediately(job)
            return

        self._window = ResearchWindow.OPENING
        self._window_job = job
        opening_done = asyncio.Event()
        self._window_opening_done = opening_done
        reset_window = True
        try:
            await self._host.begin_turn(
                pre_roll=False,
                text_context=_research_confirmation_instruction(job),
            )
            reset_window = False
            if self._window is ResearchWindow.CANCELLED:
                await self._host.end_turn("research_window_wake")
                return
            if self._window is ResearchWindow.OPENING:
                self._window = ResearchWindow.OPEN
            log_event(logger, "research.confirmation_window_opened", job_id=job.id)
        except (
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as e:
            if self._window is ResearchWindow.CANCELLED:
                logger.info(
                    "research confirmation window cancelled while opening "
                    "(id=%s): %s",
                    job.id,
                    e,
                )
                if self._host.turn_episode_active():
                    await self._host.cleanup_after_failed_begin()
                return
            logger.exception(
                "research confirmation window failed; reading immediately "
                "(id=%s): %s",
                job.id,
                e,
            )
            if self._host.turn_episode_active():
                await self._host.cleanup_after_failed_begin()
            await self._read_immediately(job)
        finally:
            if reset_window:
                self._reset_window()
            if self._window_opening_done is opening_done:
                self._window_opening_done = None
            opening_done.set()

    async def cancel_for_wake(self) -> bool:
        """Hand an open confirmation window to a real wake.

        False means the wake must be abandoned: the opener never observed
        the cancellation, so the household is answered with a cue instead.
        """
        if self._window is ResearchWindow.IDLE:
            return True
        self._terminal(ResearchWindow.CANCELLED)
        log_event(
            logger,
            "research.confirmation_window_cancelled",
            reason="wake_detected",
            job_id=self._window_job.id if self._window_job is not None else "",
        )
        opening_done = self._window_opening_done
        if opening_done is not None:
            try:
                await asyncio.wait_for(
                    opening_done.wait(),
                    timeout=RESEARCH_CONFIRMATION_OPEN_CANCEL_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                # NN-6: a dropped wake must never be silent. The
                # confirmation window's own opening race lost the
                # cancellation, so the wake is abandoned here rather
                # than risk colliding with it — but the household
                # still needs to hear something happened.
                log_event(
                    logger,
                    "research.confirmation_window_cancel_timeout",
                    job_id=(
                        self._window_job.id
                        if self._window_job is not None else ""
                    ),
                    level=logging.WARNING,
                )
                await self._host.play_cancel_timeout_cue()
                return False
        elif self._host.condition().in_session:
            await self._host.end_turn("research_window_wake")
        return True

    def window_snapshot(self) -> WindowSnapshot:
        return WindowSnapshot(
            job=self._window_job if self.window_active else None,
            state=self._window,
        )

    def finish_window(self, snapshot: WindowSnapshot) -> None:
        """Close the window the ending turn carried, dismissing a job the
        household never answered."""
        if snapshot.job is None:
            return
        self._reset_window()
        if snapshot.undecided:
            self._mark_announced(snapshot.job, read=False)
            log_event(
                logger,
                "research.confirmation_window_dismissed",
                reason="silence",
                job_id=snapshot.job.id,
            )

    def _terminal(self, state: ResearchWindow) -> None:
        if self._window in (ResearchWindow.OPENING, ResearchWindow.OPEN):
            self._window = state

    def _reset_window(self) -> None:
        self._window = ResearchWindow.IDLE
        self._window_job = None

    def _confirmation_guard_reason(self) -> str | None:
        condition = self._host.condition()
        if condition.in_session:
            return "session_active"
        if condition.mic_muted:
            return "mic_muted"
        if condition.measurement_active:
            return "measurement_active"
        if not condition.spend_allowed:
            return "spend_cap_reached"
        if condition.connection_paused:
            return "connection_paused"
        return None

    async def _read_immediately(self, job: ResearchJob) -> None:
        text = (job.result or "").strip()
        if not text:
            text = RESEARCH_EMPTY_RESULT_TEXT
        played = await self._host.play_dynamic_text(text)
        if not played:
            logger.warning(
                "research immediate readback failed id=%s status=%s",
                job.id,
                job.status,
            )
            return
        self._mark_announced(job, read=bool(job.result))

    def record_delivery(
        self,
        job: ResearchJob,
        assistant_text: str | None,
        decision: str,
    ) -> None:
        if (
            self.window_active
            and self._window_job is not None
            and self._window_job.id == job.id
        ):
            self._terminal(ResearchWindow.DECIDED)
        self._host.record_conversation_turn(
            job.query,
            assistant_text,
            data_json={"kind": "research", "job_id": job.id},
        )
        self._clear_pending(job.id)

    def _clear_pending(self, job_id: str) -> None:
        before = len(self._pending)
        if before == 0:
            return
        self._pending = [
            job for job in self._pending if job.id != job_id
        ]
        cleared = before - len(self._pending)
        if cleared:
            log_event(
                logger,
                "research.announce_pending_cleared",
                job_id=job_id,
                count=cleared,
            )

    def _failure_cooldown_remaining(self) -> float:
        last = self._last_failure_announce_at
        if last is None:
            return 0.0
        elapsed = asyncio.get_event_loop().time() - last
        return max(0.0, self._failure_cooldown_sec - elapsed)

    def _mark_announced(self, job: ResearchJob, *, read: bool) -> None:
        if self._scheduler is not None:
            self._scheduler.mark_announced(job.id)
            if read:
                self._scheduler.mark_read(job.id)
        if read:
            self.record_delivery(job, job.result, "yes")
