# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Source-handoff transactions and downstream push-volume guards.

The caller holds its volume mutation lease across prepare, lane selection,
and finalize or abort. All carrier I/O uses the coordinator's existing doors.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from . import volume_diagnostics
from .log_event import log_event
from .music_sources import Source, VolumeMode, volume_mode
from .volume_curve import percent_to_db

logger = logging.getLogger(__name__)

# Below human-noticeable drift, above Camilla's normal <0.1 dB jitter.
RECONCILE_DRIFT_DB = 1.0


def main_mute_for_level(level: int) -> bool:
    return int(level) <= 0


@dataclass(frozen=True)
class SourceHandoff:
    """Preparation result for a mux-owned source transition.

    ``level`` is the effective level captured during preparation, not the
    separately remembered post-unmute level.
    """
    prev_source: Source
    current_source: Source
    reason: str
    level: int
    prev_mode: VolumeMode
    current_mode: VolumeMode
    guard_db: float | None = None
    camilla_before_db: float | None = None
    push_ok: bool | None = None
    camilla_guarded: bool = False
    settled_ms: int = 0
    result: str = "ok"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.result in {"ok", "degraded_safe", "noop"}


class GuardWriter(Protocol):
    def __call__(
        self, db: float, *, context: str, persist: bool,
    ) -> Awaitable[bool]: ...


class VolumeHandoff:
    """Own handoff timing and guard transitions, with no cached volume intent."""

    def __init__(
        self,
        *,
        effective_level: Callable[[], int],
        read_carrier: Callable[[], Awaitable[tuple[float | None, bool | None]]],
        persisted_carrier: Callable[[], float | None],
        write_guard: GuardWriter,
        push_source: Callable[[Source, int], Awaitable[bool]],
        camilla_locked: Callable[[], Awaitable[bool | None]],
        write_level: Callable[[int], Awaitable[bool]],
        handoff_settle_sec: float,
        push_settle_sec: float,
    ) -> None:
        self._effective_level = effective_level
        self._read_camilla_volume_and_mute = read_carrier
        self._persisted_main_volume_db = persisted_carrier
        self._set_camilla_db = write_guard
        self._set_push_source_for_handoff = push_source
        self._camilla_locked = camilla_locked
        self._set_camilla = write_level
        # Camilla's main-volume ramp is 400 ms; settle before opening the lane.
        self._handoff_settle_sec = max(0.0, float(handoff_settle_sec))
        # Spotify/AVRCP can acknowledge before their attenuator is audible.
        self._push_settle_sec = max(0.0, float(push_settle_sec))

    async def guard_camilla_after_push_failure(
        self,
        source: Source,
        level: int,
        *,
        context: str,
        reason: str,
        warning_prefix: str,
        guarded_warning_suffix: str,
    ) -> bool:
        """Fall back to Camilla after a source-volume push fails.

        Callers own their operator-facing warning wording; this helper owns
        the safety sequence and diagnostics so dispatch and source-transition
        paths cannot drift. The bounded success suffix may reference
        ``guard_db`` and ``level``; every failure path shares the same suffix.
        """
        guard_db = percent_to_db(level)
        previous_db = self._persisted_main_volume_db()
        guarded = await self._set_camilla_db(
            guard_db,
            context=context,
            persist=True,
        )
        if guarded:
            volume_diagnostics.record_push_guard(
                source,
                level=level,
                guard_db=guard_db,
                reason=reason,
                context=context,
                previous_db=previous_db,
            )
            logger.warning(
                "%s%s",
                warning_prefix,
                guarded_warning_suffix.format(
                    guard_db=guard_db,
                    level=level,
                ),
            )
        else:
            logger.warning(
                "%s and camilla guard could not be confirmed for %.1f dB",
                warning_prefix,
                guard_db,
            )
        return bool(guarded)

    async def prepare_source_handoff(
        self, prev_source: Source, current_source: Source, *, reason: str,
    ) -> SourceHandoff:
        """Prepare downstream volume before mux exposes a new fan-in lane.

        This is the synchronous safety gate used by jasper-mux. It
        enforces the invariant that a new source is not made audible
        until its volume carrier is safe for the canonical state's
        effective level.
        """
        level = self._effective_level()
        prev_mode = volume_mode(prev_source)
        current_mode = volume_mode(current_source)
        guard_db = percent_to_db(level)
        camilla_before, camilla_before_mute = (
            await self._read_camilla_volume_and_mute()
        )

        def _handoff(
            *,
            push_ok: bool | None = None,
            camilla_guarded: bool = False,
            settled_ms: int = 0,
            result: str = "ok",
            detail: str = "",
        ) -> SourceHandoff:
            return SourceHandoff(
                prev_source=prev_source,
                current_source=current_source,
                reason=reason,
                level=level,
                prev_mode=prev_mode,
                current_mode=current_mode,
                guard_db=guard_db,
                camilla_before_db=camilla_before,
                push_ok=push_ok,
                camilla_guarded=camilla_guarded,
                settled_ms=settled_ms,
                result=result,
                detail=detail,
            )

        if prev_source == current_source:
            return _handoff(result="noop")

        if current_mode == VolumeMode.CAMILLA_MASTER:
            settled_ms = 0
            expected_mute = main_mute_for_level(level)
            mute_drift = (
                camilla_before_mute is not None
                and camilla_before_mute != expected_mute
            )
            needs_guard = (
                camilla_before is None
                or camilla_before > guard_db + RECONCILE_DRIFT_DB
                or mute_drift
            )
            if needs_guard:
                ok = await self._set_camilla_db(
                    guard_db,
                    context="source_handoff_guard",
                    persist=True,
                )
                if not ok:
                    return _handoff(
                        result="failed",
                        detail="camilla_guard_failed",
                    )
                level, guard_db, settled_ms, ok = (
                    await self._settle_handoff_guard(
                        level, guard_db,
                        context="source_handoff_guard_catchdown",
                    )
                )
                if not ok:
                    return _handoff(
                        camilla_guarded=True,
                        settled_ms=settled_ms,
                        result="failed",
                        detail="camilla_guard_catchdown_failed",
                    )
            else:
                latest_level = self._effective_level()
                latest_guard_db = percent_to_db(latest_level)
                if latest_guard_db < guard_db - RECONCILE_DRIFT_DB:
                    ok = await self._set_camilla_db(
                        latest_guard_db,
                        context="source_handoff_guard_catchdown",
                        persist=True,
                    )
                    if not ok:
                        level = latest_level
                        guard_db = latest_guard_db
                        return _handoff(
                            result="failed",
                            detail="camilla_guard_catchdown_failed",
                        )
                    level, guard_db, settled_ms, ok = (
                        await self._settle_handoff_guard(
                            latest_level,
                            latest_guard_db,
                            context="source_handoff_guard_catchdown",
                        )
                    )
                    if not ok:
                        return _handoff(
                            camilla_guarded=True,
                            settled_ms=settled_ms,
                            result="failed",
                            detail="camilla_guard_catchdown_failed",
                        )
            return _handoff(
                camilla_guarded=True,
                settled_ms=settled_ms,
            )

        push_ok = await self._set_push_source_for_handoff(current_source, level)
        if push_ok:
            latest_level = self._effective_level()
            if latest_level != level:
                level = latest_level
                guard_db = percent_to_db(level)
                push_ok = await self._set_push_source_for_handoff(
                    current_source, level,
                )
        if push_ok:
            return _handoff(push_ok=True)

        ok = await self._set_camilla_db(
            guard_db,
            context="source_handoff_push_degraded_guard",
            persist=True,
        )
        if not ok:
            return _handoff(
                push_ok=False,
                result="failed",
                detail="push_failed_and_camilla_guard_failed",
            )
        level, guard_db, settled_ms, settle_ok = (
            await self._settle_handoff_guard(
                level, guard_db,
                context="source_handoff_push_degraded_catchdown",
            )
        )
        if not settle_ok:
            return _handoff(
                push_ok=False,
                camilla_guarded=True,
                settled_ms=settled_ms,
                result="failed",
                detail="push_failed_camilla_guard_catchdown_failed",
            )
        volume_diagnostics.record_push_guard(
            current_source,
            level=level,
            guard_db=guard_db,
            reason=volume_diagnostics.GUARD_SOURCE_HANDOFF_PUSH_FAILED,
            context="source_handoff_push_degraded",
            previous_db=camilla_before,
        )
        return _handoff(
            push_ok=False,
            camilla_guarded=True,
            settled_ms=settled_ms,
            result="degraded_safe",
            detail="push_volume_failed_camilla_guarded",
        )

    async def finalize_source_handoff(self, handoff: SourceHandoff) -> bool:
        """Finish a mux source transition after fan-in has selected a lane."""
        if not handoff.ok:
            return False
        if handoff.current_mode == VolumeMode.PUSH:
            if handoff.push_ok:
                if self._push_settle_sec > 0:
                    await asyncio.sleep(self._push_settle_sec)
                latest_level = self._effective_level()
                final_level = latest_level
                if latest_level != handoff.level:
                    if latest_level < handoff.level:
                        guard_db = percent_to_db(latest_level)
                        guard_ok = await self._set_camilla_db(
                            guard_db,
                            context="source_handoff_push_finalize_catchdown",
                            persist=True,
                        )
                        if not guard_ok:
                            return False
                    push_ok = await self._set_push_source_for_handoff(
                        handoff.current_source, latest_level,
                    )
                    if not push_ok:
                        guard_db = percent_to_db(latest_level)
                        previous_db = self._persisted_main_volume_db()
                        guarded = await self._set_camilla_db(
                            guard_db,
                            context="source_handoff_push_finalize_degraded",
                            persist=True,
                        )
                        if guarded:
                            reason = volume_diagnostics.GUARD_SOURCE_HANDOFF_PUSH_FAILED
                            volume_diagnostics.record_push_guard(
                                handoff.current_source,
                                level=latest_level,
                                guard_db=guard_db,
                                reason=reason,
                                context="source_handoff_push_finalize_degraded",
                                previous_db=previous_db,
                            )
                        return guarded
                    if self._push_settle_sec > 0:
                        await asyncio.sleep(self._push_settle_sec)
                return await self.confirm_push_mode_carrier(
                    handoff.current_source,
                    final_level,
                    context="source_handoff_push_finalize",
                )
            # Keep the guard in place when the push surface failed.
            return True
        if handoff.current_mode == VolumeMode.CAMILLA_MASTER:
            # If the guard had to be quieter than the canonical level,
            # converge back to the intended level after the selected
            # lane is open. Camilla's own ramp makes this smooth.
            return await self._set_camilla(self._effective_level())
        return True

    async def _settle_handoff_guard(
        self, level: int, guard_db: float, *, context: str,
    ) -> tuple[int, float, int, bool]:
        """Wait for Camilla's volume ramp and catch a lowering user edit.

        The mux must not expose a camilla-master lane while Camilla is
        still ramping down. If the user lowers the canonical level
        during that settle window, lower Camilla again and settle once
        more before allowing the handoff. If the user keeps dragging
        continuously, fail safe rather than opening the lane at a stale
        louder level.
        """
        settled_ms = 0
        adjustments = 0
        while True:
            if self._handoff_settle_sec > 0:
                await asyncio.sleep(self._handoff_settle_sec)
                settled_ms += round(self._handoff_settle_sec * 1000)
            latest_level = self._effective_level()
            latest_guard_db = percent_to_db(latest_level)
            if latest_guard_db >= guard_db - RECONCILE_DRIFT_DB:
                return latest_level, guard_db, settled_ms, True
            if adjustments >= 3:
                logger.warning(
                    "source handoff guard could not catch lowering "
                    "listening_level after %d adjustments", adjustments,
                )
                return latest_level, guard_db, settled_ms, False
            ok = await self._set_camilla_db(
                latest_guard_db, context=context, persist=True,
            )
            if not ok:
                return latest_level, latest_guard_db, settled_ms, False
            guard_db = latest_guard_db
            adjustments += 1

    def _log_push_guard_clear_failed(
        self,
        source: Source,
        level: int,
        *,
        previous_db: float | None,
        previous_mute: bool | None,
        context: str,
        reason: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "source": source.value,
            "level": level,
            "previous_db": (
                "unknown" if previous_db is None else f"{previous_db:.1f}"
            ),
            "previous_mute": (
                "unknown" if previous_mute is None else str(previous_mute).lower()
            ),
            "context": context,
        }
        if reason is not None:
            fields["reason"] = reason
        log_event(
            logger,
            "volume.push_guard_clear_failed",
            level=logging.WARNING,
            # `level` field collides with log_event's level= param → fields=.
            fields=fields,
        )

    async def _clear_confirmed_push_guard(
        self, source: Source, level: int, *, context: str,
    ) -> bool:
        """Clear a degraded Camilla guard after push-volume confirmation.

        A push-mode source proves it can carry `listening_level` in two
        ways: an outbound source write succeeds, or the observer sees the
        active source already sitting at the canonical level. In either
        case, keeping a stale downstream Camilla guard or final mute would
        create the "source says 100%, speaker is quiet" failure mode.
        """
        if volume_mode(source) != VolumeMode.PUSH:
            return False
        previous_db = self._persisted_main_volume_db()
        current_db, current_mute = await self._read_camilla_volume_and_mute()
        persisted_guard_active = (
            previous_db is not None
            and previous_db < -RECONCILE_DRIFT_DB
        )
        live_guard_active = (
            current_db is not None
            and current_db < -RECONCILE_DRIFT_DB
        )
        volume_guard_active = persisted_guard_active or live_guard_active
        mute_guard_active = current_mute is True
        if not volume_guard_active and not mute_guard_active:
            return False
        effective_previous_db = (
            previous_db if persisted_guard_active else current_db
        )
        if await self._camilla_locked() is True:
            volume_diagnostics.record_push_guard_clear(
                source,
                level=level,
                previous_db=effective_previous_db,
                reason=volume_diagnostics.GUARD_CLEAR_DEFERRED_DUCK_ACTIVE,
                context=context,
                ok=False,
            )
            self._log_push_guard_clear_failed(
                source,
                level,
                previous_db=effective_previous_db,
                previous_mute=current_mute,
                context=context,
                reason="duck_active",
            )
            return False
        cleared = await self._set_camilla_db(
            0.0,
            context=context,
            persist=True,
        )
        if cleared:
            volume_diagnostics.record_push_guard_clear(
                source,
                level=level,
                previous_db=effective_previous_db,
                context=context,
                ok=True,
            )
            log_event(
                logger,
                "volume.push_guard_cleared",
                # `level` collides with log_event's level= param → fields=.
                fields={
                    "source": source.value,
                    "level": level,
                    "previous_db": (
                        "unknown"
                        if effective_previous_db is None
                        else f"{effective_previous_db:.1f}"
                    ),
                    "previous_mute": (
                        "unknown"
                        if current_mute is None
                        else str(current_mute).lower()
                    ),
                    "context": context,
                },
            )
        else:
            volume_diagnostics.record_push_guard_clear(
                source,
                level=level,
                previous_db=effective_previous_db,
                context=context,
                ok=False,
            )
            self._log_push_guard_clear_failed(
                source,
                level,
                previous_db=effective_previous_db,
                previous_mute=current_mute,
                context=context,
            )
        return bool(cleared)

    async def confirm_push_mode_carrier(
        self,
        source: Source,
        level: int,
        *,
        context: str,
        include_live_guard: bool = False,
    ) -> bool:
        ok, _mutated = await self.confirm_push_mode_carrier_with_mutation(
            source,
            level,
            context=context,
            include_live_guard=include_live_guard,
        )
        return ok

    async def confirm_push_mode_carrier_with_mutation(
        self,
        source: Source,
        level: int,
        *,
        context: str,
        include_live_guard: bool = False,
    ) -> tuple[bool, bool]:
        """Keep Camilla's final carrier consistent for push-mode sources.

        For 1-100%, Spotify/Bluetooth carry volume and Camilla returns
        to an unmuted 0 dB pin. At 0%, the source slider is still pushed
        to zero, but Camilla also asserts `main_mute` so content/music
        silence does not depend on the renderer's idea of "zero".
        """
        if volume_mode(source) != VolumeMode.PUSH:
            return False, False
        if main_mute_for_level(level):
            ok = await self._set_camilla_db(
                percent_to_db(0),
                context=f"{context}_zero_mute",
                persist=True,
            )
            return ok, ok

        current_db, current_mute = await self._read_camilla_volume_and_mute()
        previous_db = self._persisted_main_volume_db()
        needs_clear = (
            current_mute is True
            or (
                include_live_guard
                and current_db is not None
                and current_db < -RECONCILE_DRIFT_DB
            )
            or (
                previous_db is not None
                and previous_db < -RECONCILE_DRIFT_DB
            )
        )
        if not needs_clear:
            return True, False
        cleared = await self._clear_confirmed_push_guard(
            source, level, context=context,
        )
        return cleared, cleared

    async def abort_source_handoff(self, handoff: SourceHandoff) -> bool:
        """Best-effort rollback when fan-in selection fails after prepare.

        Prepare may have changed Camilla to guard the target source.
        If the low-level fan-in gate does not move, restore the carrier
        expected by the source that is still audible.
        """
        if not handoff.ok:
            return True
        effective_level = self._effective_level()
        if handoff.prev_mode == VolumeMode.PUSH:
            return await self.confirm_push_mode_carrier(
                handoff.prev_source,
                effective_level,
                context="source_handoff_abort_restore_push",
            )
        if handoff.prev_mode == VolumeMode.CAMILLA_MASTER:
            return await self._set_camilla(effective_level)
        return True
