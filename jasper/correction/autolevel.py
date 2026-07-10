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
    original_db: float, *, bump_db: float, floor_db: float, ceil_db: float
) -> float:
    """End-of-ramp cap: never above ``original + bump`` or ``ceil_db``.

    ``floor_db`` remains in this legacy API for caller compatibility, but is
    intentionally not applied. Flooring a quiet listener's cap upward can
    exceed the promised bump by tens of decibels.
    """
    del floor_db
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
        al = self.data = AutolevelData()
        # Retain the setter so FAIL/VERIFY endings can restore listening level.
        self._main_volume_setter = set_main_volume_db
        self._lock_event = asyncio.Event()
        self._cancel_event = asyncio.Event()
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
                        pass

        try:
            al.original_main_volume_db = float(await get_main_volume_db())
            if end_db is None:
                end_db = compute_autolevel_cap(
                    al.original_main_volume_db,
                    bump_db=end_db_bump,
                    floor_db=end_db_absolute_min,
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
            al.status = AutolevelStatus.RAMPING
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
                    if self._lock_event is not None and self._lock_event.is_set():
                        al.status = AutolevelStatus.LOCKED
                        al.locked_main_volume_db = current_db
                        logger.info(
                            "autolevel: LOCKED at main_volume=%.1f dB "
                            "(elapsed %.2f s)",
                            current_db,
                            loop.time() - start_time,
                        )
                        await _graceful_stop(current_db)
                        return
                    if (
                        self._cancel_event is not None
                        and self._cancel_event.is_set()
                    ):
                        al.status = AutolevelStatus.CANCELLED
                        logger.info(
                            "autolevel: CANCELLED at main_volume=%.1f dB "
                            "(elapsed %.2f s) — restoring to %.1f dB",
                            current_db,
                            loop.time() - start_time,
                            al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        return
                    if loop.time() - start_time > safety_timeout_s:
                        al.status = AutolevelStatus.CANCELLED
                        al.error = f"safety timeout after {safety_timeout_s}s"
                        logger.warning(
                            "autolevel: SAFETY TIMEOUT at "
                            "main_volume=%.1f dB — restoring to %.1f dB",
                            current_db,
                            al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        return

                current_db = min(end_db, current_db + step_db)
                await set_main_volume_db(current_db)
                al.current_main_volume_db = current_db
                logger.debug("autolevel: step main_volume=%.1f dB", current_db)

            al.status = AutolevelStatus.MAXED_OUT
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
        except Exception as e:  # noqa: BLE001
            al.status = AutolevelStatus.ERROR
            al.error = str(e)
            logger.exception("autolevel failed")
            try:
                if al.original_main_volume_db is not None:
                    await _graceful_stop(al.original_main_volume_db)
                else:
                    cancel_tone()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._lock_event = None
            self._cancel_event = None

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
