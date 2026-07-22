# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Auto-level ramp controller for room-correction measurements."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from ..log_event import log_event

logger = logging.getLogger(__name__)


class AutolevelStatus(Enum):
    """Auto-level sub-state, orthogonal to measurement session state."""

    IDLE = "idle"
    RAMPING = "ramping"
    LOCKED = "locked"
    MAXED_OUT = "maxed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class AutolevelData:
    """Tracks one auto-level run. Replaced when a new run starts.

    `original_main_volume_db` is the CamillaDSP `main_volume` value saved at
    the start of the run, so the controller can restore it when the measurement
    ends. `current` is where the ramp is right now; `locked` is populated only
    when the user signalled a successful lock. `cap_db` is the dynamic
    end-of-ramp cap computed from `original + bump`.
    """

    status: AutolevelStatus = AutolevelStatus.IDLE
    current_main_volume_db: float = -50.0
    original_main_volume_db: float | None = None
    locked_main_volume_db: float | None = None
    cap_db: float | None = None
    error: str | None = None
    # Set once the listening level has been restored after a measurement ending,
    # so terminal-state restore stays idempotent.
    restored: bool = False

    def snapshot(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return round(x, 2) if x is not None else None

        return {
            "status": self.status.value,
            "current_main_volume_db": r(self.current_main_volume_db),
            "original_main_volume_db": r(self.original_main_volume_db),
            "locked_main_volume_db": r(self.locked_main_volume_db),
            "cap_db": r(self.cap_db),
            "error": self.error,
        }


def compute_autolevel_cap(
    original_db: float, *, bump_db: float, ceil_db: float
) -> float:
    """End-of-ramp cap: never above ``original + bump`` or ``ceil_db``.

    Flooring a quiet listener's cap upward can exceed the promised bump by
    tens of decibels, so there is no floor parameter — see
    ``MeasurementRamp.dynamic_cap`` in ``jasper/audio_measurement/ramp.py``
    for the same invariant on the newer ramp.
    """
    return min(original_db + bump_db, ceil_db)


class AutolevelController:
    """Owns auto-level ramp state and restoration.

    The measurement session exposes the public API because web handlers already
    call it there, but the event objects, retained volume setter, and ramp loop
    belong together in this controller.
    """

    def __init__(self, *, session_id: str):
        self.session_id = session_id
        self.data = AutolevelData()
        self._lock_event: asyncio.Event | None = None
        self._cancel_event: asyncio.Event | None = None
        self._run_finished: asyncio.Event | None = None
        self._start_reserved = False
        self._reservation_token: object | None = None
        self._reservation_released: asyncio.Event | None = None
        self._main_volume_setter: (
            Callable[[float], Awaitable[Any]] | None
        ) = None

    @property
    def main_volume_setter(
        self,
    ) -> Callable[[float], Awaitable[Any]] | None:
        return self._main_volume_setter

    @main_volume_setter.setter
    def main_volume_setter(
        self,
        setter: Callable[[float], Awaitable[Any]] | None,
    ) -> None:
        self._main_volume_setter = setter

    @property
    def run_in_progress(self) -> bool:
        """Whether a reserved, ramping, or cleanup-phase run still owns audio."""
        return bool(
            self._start_reserved
            or (
                self._run_finished is not None
                and not self._run_finished.is_set()
            )
        )

    @property
    def reservation_token(self) -> object | None:
        """Opaque identity for the adapter slot that owns this run."""
        return self._reservation_token

    @property
    def slot_occupied(self) -> bool:
        """Whether outer orchestration still owns the exact adapter slot."""
        return self._reservation_token is not None

    async def reserve_run(self) -> object | None:
        """Reserve the exact next run before outer orchestration can await.

        The web adapter enters a renderer/voice measurement window before it
        calls :meth:`run`. Reserving here closes the gap where Reset could
        otherwise see an idle controller, roll back the graph, and then be
        followed by a late ramp write.
        """
        if self.slot_occupied or self.run_in_progress:
            return None
        token = object()
        self._reservation_token = token
        self._reservation_released = asyncio.Event()
        self._run_finished = asyncio.Event()
        self._lock_event = asyncio.Event()
        self._cancel_event = asyncio.Event()
        self.data = AutolevelData(status=AutolevelStatus.RAMPING)
        self._start_reserved = True
        return token

    async def release_run_reservation(self, token: object) -> bool:
        """Release only the exact adapter generation that was reserved."""
        if token is not self._reservation_token:
            return False
        if self.run_in_progress and not self._start_reserved:
            return False
        if self._start_reserved:
            self._start_reserved = False
            self._lock_event = None
            self._cancel_event = None
            self.data.status = AutolevelStatus.ERROR
            self.data.error = "autolevel run could not be scheduled"
            if self._run_finished is not None:
                self._run_finished.set()
        released = self._reservation_released
        self._reservation_token = None
        self._reservation_released = None
        if released is not None:
            released.set()
        return True

    async def wait_for_run_reservation_release(
        self,
        token: object,
        *,
        timeout_s: float = 7.0,
    ) -> bool:
        """Wait for one exact adapter generation to finish outer teardown."""
        if token is not self._reservation_token:
            return True
        released = self._reservation_released
        if released is None:
            return True
        await asyncio.wait_for(released.wait(), timeout=timeout_s)
        return True

    async def restore_listening_volume_if_ramped(self) -> None:
        """Restore main_volume when a measurement ends outside apply/reset.

        Autolevel ramps main_volume up to a measurement level and leaves it
        LOCKED for the whole measurement. Failed or verify-ended measurements
        skip the web apply/reset handlers, so this best-effort hook restores
        the user's listening level there. It is idempotent and swallows errors.
        """
        al = self.data
        if al.restored:
            return
        if al.status not in (AutolevelStatus.LOCKED, AutolevelStatus.MAXED_OUT):
            return
        if (
            al.original_main_volume_db is None
            or self._main_volume_setter is None
        ):
            return
        al.restored = True
        try:
            await self._main_volume_setter(al.original_main_volume_db)
            log_event(
                logger,
                "correction_autolevel_volume_restored",
                session=self.session_id,
                to_db=f"{al.original_main_volume_db:.1f}",
                trigger="measurement_ended",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "autolevel volume restore on measurement end failed "
                "(session=%s) — speaker may remain at the measurement level "
                "until /reset",
                self.session_id,
            )

    async def run(
        self,
        *,
        reservation_token: object | None = None,
        get_main_volume_db: Callable[[], Awaitable[float]],
        set_main_volume_db: Callable[[float], Awaitable[Any]],
        play_continuous_tone: Callable[[], Awaitable[Any]],
        cancel_tone: Callable[[], None],
        start_db: float = -40.0,
        end_db: float | None = None,
        end_db_bump: float = 6.0,
        end_db_absolute_max: float = -6.0,
        end_db_absolute_min: float = -20.0,
        step_db: float = 1.0,
        step_interval_s: float = 0.15,
        safety_timeout_s: float = 25.0,
        fade_down_to_db: float = -40.0,
        fade_step_s: float = 0.03,
    ) -> None:
        """Auto-level CamillaDSP main_volume.

        Ramps main_volume from `start_db` up toward `end_db` while a continuous
        tone plays. The browser client watches mic level and signals lock, or
        the user cancels. Order matters for audio safety: set quiet start volume
        before tone playback, and fade down before cancelling the tone.
        """
        auto_release = reservation_token is None
        if reservation_token is None:
            reservation_token = await self.reserve_run()
            if reservation_token is None:
                raise RuntimeError("autolevel run already in progress")
        if (
            reservation_token is self._reservation_token
            and self._start_reserved
        ):
            run_finished = self._run_finished
            lock_event = self._lock_event
            cancel_event = self._cancel_event
            al = self.data
            self._start_reserved = False
        else:
            raise RuntimeError("autolevel run reservation is stale")
        if run_finished is None or lock_event is None or cancel_event is None:
            raise RuntimeError("autolevel run reservation is incomplete")
        # Retain the setter so FAIL/VERIFY endings can restore listening level.
        self._main_volume_setter = set_main_volume_db
        loop = asyncio.get_event_loop()
        tone_task: asyncio.Task | None = None

        async def _graceful_stop(lock_value_db: float | None) -> None:
            """Fade down before killing tone, then set final main_volume."""
            try:
                cur = al.current_main_volume_db
                while cur > fade_down_to_db:
                    cur = max(fade_down_to_db, cur - 2.0)
                    try:
                        await set_main_volume_db(cur)
                    except Exception:  # noqa: BLE001
                        # Best-effort quieting; log and stop fading rather
                        # than silently swallowing the setter failure.
                        logger.warning(
                            "autolevel: fade-down set_main_volume_db(%.1f) "
                            "failed (session=%s) — stopping fade, will still "
                            "attempt tone cancel + final lock-value set",
                            cur, self.session_id, exc_info=True,
                        )
                        break
                    await asyncio.sleep(fade_step_s)
            finally:
                cancel_tone()
                if tone_task is not None:
                    try:
                        await asyncio.wait_for(tone_task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                if lock_value_db is not None:
                    try:
                        await set_main_volume_db(lock_value_db)
                        al.current_main_volume_db = lock_value_db
                    except Exception:  # noqa: BLE001
                        # The final volume set is the one that actually
                        # leaves the speaker at its intended level (lock or
                        # restored listening level). A silent failure here
                        # can strand the speaker at the measurement volume —
                        # per AGENTS.md no-silent-failure, make it observable.
                        logger.warning(
                            "autolevel: final set_main_volume_db(%.1f) failed "
                            "(session=%s) — speaker may remain at the "
                            "measurement level until /reset",
                            lock_value_db, self.session_id, exc_info=True,
                        )

        try:
            al.original_main_volume_db = float(await get_main_volume_db())
            if cancel_event.is_set():
                al.status = AutolevelStatus.CANCELLED
                logger.info(
                    "autolevel: CANCELLED before first volume write "
                    "(session=%s)",
                    self.session_id,
                )
                return
            if end_db is None:
                end_db = compute_autolevel_cap(
                    al.original_main_volume_db,
                    bump_db=end_db_bump,
                    ceil_db=end_db_absolute_max,
                )
                logger.info(
                    "autolevel: dynamic end_db=%.1f dB "
                    "(min(original=%.1f + bump=%.1f, ceiling=%.1f); "
                    "legacy_floor_ignored=%.1f)",
                    end_db,
                    al.original_main_volume_db,
                    end_db_bump,
                    end_db_absolute_max,
                    end_db_absolute_min,
                )
            al.cap_db = float(end_db)
            logger.info(
                "autolevel: START original_main_volume=%.1f dB "
                "(ramp %.1f → %.1f dB, step=%.1f dB/%.0f ms)",
                al.original_main_volume_db,
                start_db,
                end_db,
                step_db,
                step_interval_s * 1000,
            )

            # A very quiet listening level can put the dynamic cap below the
            # usual start_db. Never jump straight past that cap just to reach
            # the nominal start of the legacy ramp.
            current_db = min(float(start_db), float(end_db))
            await set_main_volume_db(current_db)
            al.current_main_volume_db = current_db
            await asyncio.sleep(0.1)

            tone_task = asyncio.create_task(play_continuous_tone())
            start_time = loop.time()

            while current_db < end_db:
                interval_end = loop.time() + step_interval_s
                while loop.time() < interval_end:
                    await asyncio.sleep(0.01)
                    if lock_event.is_set():
                        logger.info(
                            "autolevel: LOCKED at main_volume=%.1f dB "
                            "(elapsed %.2f s)",
                            current_db,
                            loop.time() - start_time,
                        )
                        await _graceful_stop(current_db)
                        al.locked_main_volume_db = current_db
                        al.status = AutolevelStatus.LOCKED
                        return
                    if cancel_event.is_set():
                        logger.info(
                            "autolevel: CANCELLED at main_volume=%.1f dB "
                            "(elapsed %.2f s) — restoring to %.1f dB",
                            current_db,
                            loop.time() - start_time,
                            al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        al.status = AutolevelStatus.CANCELLED
                        return
                    if loop.time() - start_time > safety_timeout_s:
                        al.error = f"safety timeout after {safety_timeout_s}s"
                        logger.warning(
                            "autolevel: SAFETY TIMEOUT at "
                            "main_volume=%.1f dB — restoring to %.1f dB",
                            current_db,
                            al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        al.status = AutolevelStatus.CANCELLED
                        return

                current_db = min(end_db, current_db + step_db)
                await set_main_volume_db(current_db)
                al.current_main_volume_db = current_db
                logger.debug("autolevel: step main_volume=%.1f dB", current_db)

            al.error = (
                "safe cap reached below target; raise the external amplifier "
                "and retry"
            )
            logger.warning(
                "autolevel: MAXED_OUT at main_volume=%.1f dB "
                "(software cap) — no measurement lock; restoring %.1f dB",
                end_db,
                al.original_main_volume_db,
            )
            await _graceful_stop(al.original_main_volume_db)
            al.status = AutolevelStatus.MAXED_OUT
        except Exception as e:  # noqa: BLE001
            al.error = str(e)
            logger.exception("autolevel failed")
            try:
                if al.original_main_volume_db is not None:
                    await _graceful_stop(al.original_main_volume_db)
                else:
                    cancel_tone()
            except Exception:  # noqa: BLE001
                pass
            al.status = AutolevelStatus.ERROR
        finally:
            if self._lock_event is lock_event:
                self._lock_event = None
            if self._cancel_event is cancel_event:
                self._cancel_event = None
            run_finished.set()
            if auto_release:
                await self.release_run_reservation(reservation_token)

    async def lock(self) -> bool:
        """Signal the running autolevel task to lock current main_volume."""
        if self._lock_event is None:
            return False
        self._lock_event.set()
        return True

    async def cancel(self) -> bool:
        """Signal the running autolevel task to abort and restore volume."""
        if self._cancel_event is None:
            return False
        self._cancel_event.set()
        return True

    async def cancel_and_wait(self, *, timeout_s: float = 5.0) -> bool:
        """Cancel an active ramp and wait until its volume restore finishes."""
        finished = self._run_finished
        if finished is None or finished.is_set():
            return False
        fired = await self.cancel()
        if not fired:
            return False
        await asyncio.wait_for(finished.wait(), timeout=timeout_s)
        return True
