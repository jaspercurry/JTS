# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's lease on a room-correction measurement window.

The window itself is owned by ``jasper.measurement_window``; this is the
voice daemon's copy of "a measurement is live", driven by the MEASURE_PAUSE /
MEASURE_RESUME control-socket commands. It closes assistant output admission,
gates the mic, hands the volume-owner lease over, pauses the outputd content
meter, and keeps a crash backstop armed so a coordinator that dies mid-sweep
cannot strand the speaker silent.

``WakeLoop._measurement_active`` stays the hot-path gate every wake/session
path reads; this class is its only writer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING

from ..log_event import log_event

if TYPE_CHECKING:
    from ..voice_daemon import WakeLoop

logger = logging.getLogger("jasper.voice_daemon")

# Bounded wait for assistant audio that was ALREADY in playout when a
# MEASURE_PAUSE landed (issue #1898). #1786 stops new cues, timers, and
# announcements from *starting* once the window is open; this drains the
# tail of one that started a moment earlier, so it cannot bleed into the
# window's first capture.
#
# 2.0 s is a ceiling, not a preference. The coordinator awaits this
# command's reply with a VOICE_MEASURE_PAUSE_TIMEOUT_SEC (3.0 s) read
# timeout. A transport-level timeout leaves an older permissive
# coordinator unable to know that voice armed the pause, so it skips
# MEASURE_RESUME and the daemon's auto-clear must recover. A completed
# drain timeout is different: the daemon keeps ``result=ok`` for rolling
# compatibility and adds ``drained=false``, so current strict callers can
# fail closed while every old/new caller retains cleanup ownership.
# install.sh restarts jasper-voice and jasper-web at different points of a
# deploy, so an OLD coordinator can be talking to a NEW daemon — the bound
# has to fit under the timeout that coordinator already shipped with. The
# drain consumes only what remains of the aggregate setup budget below.
# The 2.0 s ceiling covers fan-in's 1.2 s pace-ahead, one 250 ms IPC
# chunk, the 85 ms drain tail, and Pi scheduler jitter. Pinned against the
# aggregate/client arithmetic by
# tests/test_voice_daemon_measurement_inflight.py.
MEASUREMENT_INFLIGHT_DRAIN_SEC = 2.0

# One daemon-side budget covers transition-lock acquisition, stale-backstop
# join, volume-guard acquisition, the canonical outputd meter PAUSE, and the
# in-flight drain. The coordinator's already-shipped read
# timeout is 3.0 s. Keep 0.5 s outside our budget for UDS response scheduling
# on a loaded 1 GB Pi; reserve the final 0.25 s *inside* our budget for local
# rollback if setup cannot complete. These are compatibility constants, not
# latency goals. Tests pin the exact arithmetic against the coordinator SSOT.
MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC = 2.5
MEASUREMENT_PAUSE_REPLY_MARGIN_SEC = 0.5
MEASUREMENT_PAUSE_ROLLBACK_RESERVE_SEC = 0.25
MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC = (
    MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC
    - MEASUREMENT_PAUSE_ROLLBACK_RESERVE_SEC
)

# How long a measurement window stays gated with no word from the
# coordinator before the daemon clears it itself. This is the crash
# backstop: a coordinator killed mid-sweep never sends MEASURE_RESUME, and
# without this the speaker would stay silent forever. Named because it is a
# contract, not a local timeout — a legitimate window can outlive it (a
# capture setup may wait minutes for a human), so the coordinator renews the
# lease every `measurement_window.MEASUREMENT_LEASE_REFRESH_SEC`. That
# interval must stay under this one with room for a retry, or a healthy
# long window would un-gate mid-sweep and let household music back into the
# capture. Pinned against the refresh interval by
# tests/test_voice_daemon_measurement_inflight.py.
MEASUREMENT_AUTOCLEAR_SEC = 120.0

# Replacing a measurement lease joins the prior crash-recovery task so a stale
# task cannot reopen output after the renewal returns. The task normally exits
# in one event-loop turn after cancellation; 1 s leaves ample Pi scheduling
# headroom while keeping a broken cleanup task from wedging the control socket.
MEASUREMENT_SAFETY_JOIN_TIMEOUT_SEC = 1.0

# Test seam for deterministic lease-expiry interleavings without wall-clock
# sleeps. Production retains asyncio.sleep exactly.
_measurement_safety_sleep = asyncio.sleep
# Same-purpose seam for aggregate-deadline arithmetic. Keeping it local avoids
# patching ``time.monotonic`` process-wide (which would corrupt asyncio clocks).
_measurement_monotonic = time.monotonic


class MeasurementHold:
    """The measurement window's voice-side lease and its crash backstop."""

    def __init__(
        self,
        wake_loop: "WakeLoop",
        *,
        session_active: Callable[[], bool],
    ) -> None:
        self._wake_loop = wake_loop
        # Predicate rather than a State read: State lives in voice_daemon, and
        # importing it here at runtime would close an import cycle.
        self._session_active = session_active
        self._transition_lock = asyncio.Lock()
        self._safety_task: asyncio.Task | None = None
        self._lease_generation = 0

    async def pause_response(self) -> dict[str, object]:
        """Open/renew a pause and include additive drain evidence on the wire."""

        result, drained = await self._pause_detailed()
        response: dict[str, object] = {"result": result}
        if drained is not None:
            response["drained"] = drained
        return response

    async def _pause_detailed(self) -> tuple[str, bool | None]:
        """Open or renew a measurement window and drain in-flight output.

        Refuses with `BUSY` while a voice session is active — yanking it would
        orphan the user's turn. The coordinator
        (jasper.measurement_window) checks STATUS first.

        Ordering is load-bearing (issue #1898): admission closes first, then
        the measurement event is set and the MEASUREMENT_AUTOCLEAR_SEC safety
        timer armed, all before the drain, so that no new episode can start
        mid-drain, mic frames stop before a wake could open a reactive cue
        behind the drain window, and a crash mid-drain still auto-clears. The
        drain defers to audio that already owned the gate and never cancels
        it, so no wake-blocking cue is cut short; audio outliving the timeout
        is refused at the emission seam instead (issue #1913,
        ``WakeLoop._output_admission_refusal``).

        Idempotent: the drain runs only on the opening transition, so the
        coordinator's lease renewals stay latency-free.

        Returns ("ok", True) when the window is open and output drained;
        ("ok", False) when the pause is armed but prior output did not drain
        within the bound (the caller still owns RESUME); ("BUSY", None) when
        refused.

        The scalar result stays ``ok`` whenever the pause is armed: older
        coordinators branch only on that field and would otherwise skip lease
        renewal and RESUME during a rolling deploy.
        """
        started = _measurement_monotonic()
        total_deadline = started + MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC
        setup_deadline = (
            started + MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC
        )
        remaining = setup_deadline - _measurement_monotonic()
        if remaining <= 0.0:
            self._log_pause_timeout("transition_lock")
            raise TimeoutError("MEASURE_PAUSE aggregate deadline expired")
        try:
            async with asyncio.timeout(remaining):
                await self._transition_lock.acquire()
        except TimeoutError:
            self._log_pause_timeout("transition_lock")
            raise TimeoutError(
                "MEASURE_PAUSE transition lock exceeded aggregate deadline"
            ) from None

        try:
            if self._session_active():
                return "BUSY", None
            opening = not self._wake_loop._measurement_active.is_set()
            deferred_cancel = False
            opened = False
            completed = False
            meter_paused = False
            try:
                if opening:
                    # Join an orphaned backstop before admission changes; its
                    # join ceiling is clipped to this request's setup deadline.
                    orphaned = self._safety_task
                    deferred_cancel |= (
                        await self._cancel_safety_locked(
                            orphaned,
                            deadline_monotonic=setup_deadline,
                        )
                    )
                    if deferred_cancel:
                        raise asyncio.CancelledError
                    await self._await_pause_step(
                        self._wake_loop._output_gate.pause_admission(),
                        deadline_monotonic=setup_deadline,
                        phase="admission",
                    )
                    opened = True
                    # Synchronous through safety installation: recovery is
                    # armed before any external await.
                    self._set_active_local(True, trigger="pause")
                    self._wake_loop._content_activity.pause()
                    self._arm_safety_locked()
                else:
                    # Replacement first, so the active measurement never has a
                    # crash-backstop gap; generation + slot make the old task
                    # stale immediately.
                    previous = self._safety_task
                    self._arm_safety_locked()
                    deferred_cancel |= (
                        await self._cancel_safety_locked(
                            previous,
                            deadline_monotonic=setup_deadline,
                        )
                    )
                    if deferred_cancel:
                        raise asyncio.CancelledError

                volume = self._wake_loop._volume_coordinator
                await self._await_pause_step(
                    volume.note_measurement_active(True),
                    deadline_monotonic=setup_deadline,
                    phase="volume_guard",
                )
                await self._await_pause_step(
                    self._wake_loop._tts.pause_content_meter_for_measurement(
                        setup_deadline,
                    ),
                    deadline_monotonic=setup_deadline,
                    phase="content_meter",
                )
                meter_paused = True

                drain = self._wake_loop._drain_inflight_output
                drained = not opening or await drain(
                    timeout_sec=max(
                        0.0,
                        min(
                            MEASUREMENT_INFLIGHT_DRAIN_SEC,
                            setup_deadline - _measurement_monotonic(),
                        ),
                    )
                )
                completed = True
                return "ok", drained
            finally:
                # Completion, not an exception allowlist, owns rollback: any
                # BaseException after admission closes restores local output
                # availability before propagating.
                if opened and not completed:
                    deferred_cancel |= (
                        await self._rollback_open_locked(
                            trigger="pause_error",
                            deadline_monotonic=total_deadline,
                            resume_meter=meter_paused,
                        )
                    )
        finally:
            self._transition_lock.release()

    @staticmethod
    def _log_pause_timeout(phase: str) -> None:
        log_event(
            logger,
            "measurement.pause_timeout",
            phase=phase,
            total_timeout_sec=MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC,
            level=logging.WARNING,
        )

    async def _await_pause_step(
        self,
        operation: Coroutine,
        *,
        deadline_monotonic: float,
        phase: str,
    ) -> None:
        """Await one cancellation-aware setup step inside the shared budget."""

        remaining = deadline_monotonic - _measurement_monotonic()
        if remaining <= 0.0:
            operation.close()
            self._log_pause_timeout(phase)
            raise TimeoutError(
                f"MEASURE_PAUSE {phase} exceeded aggregate deadline"
            )
        try:
            async with asyncio.timeout(remaining):
                await operation
        except TimeoutError:
            self._log_pause_timeout(phase)
            raise TimeoutError(
                f"MEASURE_PAUSE {phase} exceeded aggregate deadline"
            ) from None

    def _arm_safety_locked(self) -> None:
        """Install one generation-bound crash backstop without awaiting."""

        self._lease_generation += 1
        generation = self._lease_generation
        task = asyncio.create_task(
            self._auto_clear(generation),
            name=f"measurement-auto-clear-{generation}",
        )
        # Deliberately not in WakeLoop._bg_tasks: those drive turn completion.
        self._safety_task = task

    async def _auto_clear(self, generation: int) -> None:
        try:
            await _measurement_safety_sleep(MEASUREMENT_AUTOCLEAR_SEC)
        except asyncio.CancelledError:
            return
        async with self._transition_lock:
            current = asyncio.current_task()
            if (
                generation != self._lease_generation
                or self._safety_task is not current
                or not self._wake_loop._measurement_active.is_set()
            ):
                return
            logger.warning(
                "measurement window auto-clearing after %.0f s — "
                "coordinator likely crashed without sending MEASURE_RESUME",
                MEASUREMENT_AUTOCLEAR_SEC,
            )
            try:
                await self._restore_state(trigger="auto_clear")
            finally:
                if self._safety_task is current:
                    self._safety_task = None

    async def _cancel_safety_locked(
        self,
        previous: asyncio.Task | None,
        *,
        deadline_monotonic: float | None = None,
    ) -> bool:
        """Cancel and boundedly join the installed backstop.

        Returns whether the calling transition's own cancellation was deferred
        while it held cleanup ownership; the caller propagates it only once
        measurement state is safe.
        """

        if previous is None:
            return False
        current = asyncio.current_task()
        if previous is current:
            self._safety_task = None
            return False
        previous.cancel()
        deferred_cancel = False
        started = _measurement_monotonic()
        deadline = started + MEASUREMENT_SAFETY_JOIN_TIMEOUT_SEC
        if deadline_monotonic is not None:
            deadline = min(deadline, deadline_monotonic)
        join_bound_sec = max(0.0, deadline - started)
        while not previous.done():
            remaining = deadline - _measurement_monotonic()
            if remaining <= 0:
                log_event(
                    logger,
                    "measurement.safety_join_timeout",
                    generation=self._lease_generation,
                    timeout_sec=join_bound_sec,
                    level=logging.ERROR,
                )
                raise TimeoutError("measurement safety task did not stop")
            try:
                done, _pending = await asyncio.wait(
                    {previous},
                    timeout=remaining,
                )
                if not done:
                    log_event(
                        logger,
                        "measurement.safety_join_timeout",
                        generation=self._lease_generation,
                        timeout_sec=join_bound_sec,
                        level=logging.ERROR,
                    )
                    raise TimeoutError(
                        "measurement safety task did not stop"
                    )
            except asyncio.CancelledError:
                # asyncio.wait never forwards the child's exception, so this
                # cancellation belongs to the calling transition itself.
                if current is None or current.cancelling() == 0:
                    break
                deferred_cancel = True
                current.uncancel()
        if self._safety_task is previous:
            self._safety_task = None
        return deferred_cancel

    async def _rollback_open_locked(
        self,
        *,
        trigger: str,
        deadline_monotonic: float | None = None,
        resume_meter: bool = True,
    ) -> bool:
        """Invalidate a failed opening and restore even if its task wedges."""

        previous = self._safety_task
        self._lease_generation += 1
        self._safety_task = None
        try:
            deferred_cancel = await self._cancel_safety_locked(
                previous,
                deadline_monotonic=deadline_monotonic,
            )
        except TimeoutError:
            # Logged by the join helper. Generation + slot invalidation stops
            # the stale task from restoring a future lease.
            deferred_cancel = False
        try:
            deferred_cancel |= await self._restore_owned(
                trigger=trigger,
                deadline_monotonic=deadline_monotonic,
                resume_meter=resume_meter,
            )
        except BaseException as error:  # noqa: BLE001 - one cleanup boundary
            # The setup exception stays authoritative. Local restore runs
            # before either remote observer, so a broken best-effort cleanup
            # cannot keep household output admission closed.
            log_event(
                logger,
                "measurement.rollback_failed",
                exc_type=type(error).__name__,
                err=str(error),
                level=logging.ERROR,
            )
        return deferred_cancel

    async def _restore_owned(
        self,
        *,
        trigger: str,
        deadline_monotonic: float | None = None,
        resume_meter: bool = True,
    ) -> bool:
        """Finish restore despite repeated cancellation; report it afterward."""

        restore = asyncio.create_task(
            self._restore_state(
                trigger=trigger,
                deadline_monotonic=deadline_monotonic,
                resume_meter=resume_meter,
            ),
            name=f"measurement-restore-{trigger}",
        )
        deferred_cancel = False
        current = asyncio.current_task()
        while not restore.done():
            try:
                await asyncio.wait({restore})
            except asyncio.CancelledError:
                if current is None or current.cancelling() == 0:
                    break
                deferred_cancel = True
                current.uncancel()
        if restore.cancelled():
            raise asyncio.CancelledError
        error = restore.exception()
        if error is not None:
            if deferred_cancel:
                raise asyncio.CancelledError from None
            raise error
        return deferred_cancel

    async def resume(self) -> str:
        """Close a measurement window.

        Idempotent — calling twice, or before any PAUSE, is harmless. Always
        returns "ok".
        """
        async with self._transition_lock:
            previous = self._safety_task
            self._lease_generation += 1
            self._safety_task = None
            try:
                deferred_cancel = await self._cancel_safety_locked(
                    previous
                )
            except TimeoutError:
                # Availability beats waiting forever on a broken backstop;
                # generation invalidation makes that old task harmless.
                deferred_cancel = False
            deferred_cancel |= await self._restore_owned(
                trigger="resume"
            )
        if deferred_cancel:
            raise asyncio.CancelledError
        return "ok"

    async def _restore_state(
        self,
        *,
        trigger: str,
        deadline_monotonic: float | None = None,
        resume_meter: bool = True,
    ) -> None:
        """Restore local output first; deadline-bound observers are best-effort."""

        # Output admission reopens before the mic ungates so no ordering of
        # awaits can leave a wake heard but its chirp refused (non-negotiable 6),
        # and before meter IPC, whose adapter may be recovering from a stuck send.
        await self._wake_loop._output_gate.resume_admission()
        self._set_active_local(False, trigger=trigger)
        self._wake_loop._content_activity.resume()
        volume = self._wake_loop._volume_coordinator

        if deadline_monotonic is not None:
            # The final quarter-second is rollback reserve. Clear the volume
            # guard before best-effort meter recovery so availability does not
            # depend on a poisoned outputd socket.
            await self._restore_step_before_deadline(
                volume.note_measurement_active(False),
                deadline_monotonic=deadline_monotonic,
                event="measurement.volume_resume_failed",
                trigger=trigger,
            )
            if resume_meter:
                await self._restore_step_before_deadline(
                    self._wake_loop._tts.resume_content_meter(),
                    deadline_monotonic=deadline_monotonic,
                    event="measurement.meter_resume_failed",
                    trigger=trigger,
                )
            return

        try:
            await self._wake_loop._tts.resume_content_meter()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
            log_event(
                logger,
                "measurement.meter_resume_failed",
                trigger=trigger,
                exc_type=type(e).__name__,
                err=str(e),
                level=logging.WARNING,
            )
        await volume.note_measurement_active(False)

    @staticmethod
    async def _restore_step_before_deadline(
        operation: Coroutine,
        *,
        deadline_monotonic: float,
        event: str,
        trigger: str,
    ) -> None:
        remaining = deadline_monotonic - _measurement_monotonic()
        if remaining <= 0.0:
            operation.close()
            log_event(
                logger,
                event,
                trigger=trigger,
                reason="aggregate_deadline_expired",
                level=logging.WARNING,
            )
            return
        try:
            async with asyncio.timeout(remaining):
                await operation
        except Exception as error:  # noqa: BLE001 - one cleanup boundary
            log_event(
                logger,
                event,
                trigger=trigger,
                exc_type=type(error).__name__,
                err=str(error),
                level=logging.WARNING,
            )

    def _set_active_local(self, active: bool, *, trigger: str) -> None:
        """Update the hot-path gate synchronously inside transition ownership."""

        gate = self._wake_loop._measurement_active
        changed = gate.is_set() != bool(active)
        if active:
            gate.set()
        else:
            gate.clear()
        if changed:
            log_event(
                logger,
                "measurement.reconcile_guard",
                active=str(bool(active)).lower(),
                trigger=trigger,
            )
