# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The 1 Hz drift reconciler: Camilla's fader back to the household level.

``VolumeCoordinator`` owns one and keeps the public entry. A pass is a
preflight with no lease held, then the write under the coordinator's mutation
lease and the measurement gate's write lock (ADR-0213, ADR-0368).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from jasper.control.measurement_hold import read_measurement_hold
from jasper.log_event import log_event
from jasper.playback_state.music_sources import Source, VolumeMode, volume_mode
from jasper.volume_curve import main_mute_for_level, percent_to_db
from jasper.volume_floor import RECONCILE_DRIFT_DB

if TYPE_CHECKING:
    from jasper.audio_control.volume_carrier import CamillaCarrier
    from jasper.audio_control.volume_measurement_gate import MeasurementGate

logger = logging.getLogger("jasper.volume_reconcile")


# The hold is read inside the measurement gate's write lock, which
# MEASURE_PAUSE's `note_measurement_active` must take within the voice daemon's
# setup budget (`voice.measurement_hold.MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC`,
# 2.25 s); the control client's 2 s default would spend nearly all of it.
MEASUREMENT_HOLD_READ_TIMEOUT_S = 0.5


def converged(
    expected_db: float,
    expected_mute: bool,
    current_db: float | None,
    current_mute: bool | None,
) -> bool:
    """Camilla carries the expected level: inside the dead band, mute agreeing.

    An unread fader is not converged; an unread mute flag is no mute drift.
    """
    mute_drift = (
        current_mute is not None
        and current_mute != expected_mute
    )
    return (
        current_db is not None
        and abs(expected_db - current_db) <= RECONCILE_DRIFT_DB
        and not mute_drift
    )


class VolumeReconciler:
    """The coordinator's 1 Hz Camilla backstop, with no cached volume intent.

    Its inputs are the coordinator's own doors. The episode state — the
    deferral reason and the two failure counters — lives only here.
    """

    def __init__(
        self,
        *,
        carrier: "CamillaCarrier",
        measurement: "MeasurementGate",
        graph_mutation_in_progress: Callable[[], bool | None],
        voice_session_active: Callable[[], bool],
        active_source: Callable[[], Awaitable[Source]],
        refresh: Callable[[], None],
        effective_level: Callable[[], int],
        mutation: Callable[[], AbstractAsyncContextManager[Any]],
        save_now: Callable[[float], None],
    ) -> None:
        self._carrier = carrier
        self._measurement = measurement
        self._graph_mutation_in_progress = graph_mutation_in_progress
        self._voice_session_active = voice_session_active
        self._active_source = active_source
        self._refresh_from_disk = refresh
        self._effective_level = effective_level
        self._mutation = mutation
        self._save_now = save_now
        # Edge state for the three conditions this reconciler re-evaluates
        # every tick; each is reported once per episode, never at 1 Hz.
        self._reconcile_deferred: str | None = None
        self._write_failures: int = 0
        self._graph_probe_failures: int = 0

    async def maybe_reconcile_camilla(self, source: Source | None = None) -> None:
        """Self-healing convergence: write `percent_to_db(listening_level)`
        back to camilla when `main_volume_db` has drifted from it.

        Pure resilience backstop: the normal write paths keep the two in
        sync, and this catches the divergence some other writer or transient
        left behind. Called from `VolumeObserver._tick` at 1 Hz, which passes
        its own already-resolved ``source`` so the preflight below does not
        re-probe `active_renderers()`; a candidate write still re-resolves
        fresh once it holds the mutation lock (see below).

        Gates (all must pass for a write to land):

        1. No voice session or correction measurement is active — the
           measurement's ramp owns camilla, and a write would clobber it.
        2. Active source is camilla-as-master (idle / AirPlay / USBSINK).
           On push-mode sources camilla is pinned at 0 dB by design and
           listening_level lives on the source's own slider.
        3. `|main_volume_db − expected| > RECONCILE_DRIFT_DB` — a dead band
           around camilla's normal jitter, the same in both directions: a dB
           gap is no evidence of an owner (ADR-0177).
        4. No DSP writer — a graph swap, or the volume-floor audition —
           holds the graph-mutation lock (ADR-0213, ADR-0368). It defers a
           mute correction too: an unmute mid-swap is the loud write the
           graph-swap bracket exists to prevent (`_defer_for_dsp_writer`).
        5. A write that makes the speaker louder also waits while
           jasper-control holds the fader for a measurement, or cannot say:
           the window whose MEASURE_PAUSE never reached this process. A
           write that makes it quieter never waits on it
           (`_defer_for_measurement_hold`).

        A write failure is non-fatal: WARN on the episode's first, then the
        observer keeps ticking (`volume.reconcile_write_failed`).
        """
        # A deferral spans consecutive ticks; a tick that returns before the
        # probes ends it, and a new reason opens a new episode.
        deferred, self._reconcile_deferred = self._reconcile_deferred, None
        if not await self._preflight(source, deferred=deferred):
            return
        # The preflight above avoids taking the cross-daemon lease on every
        # healthy 1 Hz tick; a candidate write then joins the same ordered
        # writer set as user commands and mux handoffs, re-reading every
        # routing/intent/physical fact inside both leases in case a
        # control-daemon command landed while the preflight read was in flight.
        async with self._mutation():
            async with self._measurement.write_lock:
                await self._write_under_lease(deferred=deferred)

    async def _preflight(
        self, source: Source | None, *, deferred: str | None,
    ) -> bool:
        """Whether this tick has a write to attempt, asked with no lease held."""
        self._measurement.lapse_stranded()
        if self._voice_session_active() or self._measurement.active:
            return False
        # Refresh from disk on every tick, push-mode sources too: a remote twist
        # that landed via jasper-control must reach the coordinator's level (the
        # voice daemon reads it for TTS loudness) and the expected dB below.
        self._refresh_from_disk()
        try:
            source = source if source is not None else await self._active_source()
        except Exception:  # noqa: BLE001
            return False
        if volume_mode(source) != VolumeMode.CAMILLA_MASTER:
            return False
        expected_level = self._effective_level()
        expected_db = percent_to_db(expected_level)
        expected_mute = main_mute_for_level(expected_level)
        current_db, current_mute = await self._carrier.read_volume_and_mute()
        # MEASURE_PAUSE can arrive while the Camilla read above is in flight.
        # Re-check at the write boundary so an already-running observer tick
        # cannot cross into the ramp after measurement has taken ownership.
        if self._measurement.active:
            return False
        if current_db is None:
            # Camilla restart blip; next tick retries.
            return False
        if converged(expected_db, expected_mute, current_db, current_mute):
            return False
        return not self._defer_for_dsp_writer(reported=deferred)

    async def _write_under_lease(self, *, deferred: str | None) -> None:
        """Re-read every fact the preflight read, then write; the caller holds
        the mutation lease and the measurement gate's write lock."""
        if self._voice_session_active() or self._measurement.active:
            return
        try:
            source = await self._active_source()
        except Exception:  # noqa: BLE001
            return
        if volume_mode(source) != VolumeMode.CAMILLA_MASTER:
            return
        self._refresh_from_disk()
        expected_level = self._effective_level()
        expected_db = percent_to_db(expected_level)
        expected_mute = main_mute_for_level(expected_level)
        current_db, current_mute = (
            await self._carrier.read_volume_and_mute()
        )
        if (
            self._voice_session_active()
            or self._measurement.active
            or current_db is None
        ):
            return
        drift = expected_db - current_db
        if converged(expected_db, expected_mute, current_db, current_mute):
            return
        louder = not expected_mute and (
            drift > RECONCILE_DRIFT_DB or current_mute is True
        )
        if louder and await self._defer_for_measurement_hold(
            reported=deferred,
        ):
            return
        # Asked last, with no await before the write: a writer admitted
        # while the hold was being read still holds the write off.
        if self._defer_for_dsp_writer(reported=deferred):
            return
        try:
            ok = await self._carrier.write_db_with_mute(
                expected_db,
                context="reconcile",
            )
            failure: str | None = None if ok else "rejected"
        except Exception as e:  # noqa: BLE001
            failure = f"{type(e).__name__}: {e}"
        if failure is not None:
            # A camilla that refuses writes refuses them at 1 Hz, so
            # the retry itself is not news; the episode's open and
            # close are.
            self._write_failures += 1
            if self._write_failures == 1:
                log_event(logger, "volume.reconcile_write_failed",
                          level=logging.WARNING, error=failure)
            return
        self._save_now(expected_db)
        if self._write_failures:
            log_event(logger, "volume.reconcile_write_recovered",
                      consecutive_failures=self._write_failures)
            self._write_failures = 0
        log_event(
            logger,
            "volume.reconciled",
            # `level` collides with log_event's level= param → fields=.
            fields={
                "source": source.value,
                "level": f"{expected_level}%",
                "current_db": f"{current_db:.2f}",
                "expected_db": f"{expected_db:.2f}",
                "drift_db": f"{drift:+.2f}",
                "current_mute": (
                    "unknown"
                    if current_mute is None
                    else str(current_mute).lower()
                ),
                "expected_mute": str(expected_mute).lower(),
            },
        )

    def _defer_for_dsp_writer(self, *, reported: str | None) -> bool:
        """Stand this tick down while a DSP writer owns CamillaDSP's graph.

        The graph-swap bracket and the volume-floor audition take no claim
        this coordinator's `VolumeOwner` can see, and run in whichever
        process holds them — so the answer has to cross processes, and the
        writer lock is the fact that already does (ADR-0213, ADR-0368).
        Synchronous by contract, like the owner's own readers. Edge-reported:
        the audition holds the lock for up to its 10-minute limit.

        Fails open — no probe, an unreadable lock, a raising controller —
        because the loud-direction correction is a safety backstop and must
        not go inert on an infrastructure problem (ADR-0177).
        """
        try:
            held = self._graph_mutation_in_progress()
        except Exception as e:  # noqa: BLE001
            self._graph_probe_failures += 1
            if self._graph_probe_failures == 1:
                log_event(logger, "volume.graph_probe_failed",
                          level=logging.WARNING,
                          error=f"{type(e).__name__}: {e}")
            return False
        if self._graph_probe_failures:
            log_event(logger, "volume.graph_probe_recovered",
                      consecutive_failures=self._graph_probe_failures)
            self._graph_probe_failures = 0
        if held is not True:
            return False
        return self._defer("dsp_writer_lock", reported)

    async def _defer_for_measurement_hold(self, *, reported: str | None) -> bool:
        """Stand a louder write down while jasper-control holds the fader.

        The backstop for a measurement whose MEASURE_PAUSE never landed here —
        a window that went ahead without it, or whose renewal lapsed: the hold
        is the window's copy that outlives both. An unreadable hold counts as
        held, which costs only a late raise; a quieter write never asks, so the
        safety correction stays live (ADR-0177, ADR-0368).
        """
        hold = await asyncio.to_thread(
            read_measurement_hold, timeout=MEASUREMENT_HOLD_READ_TIMEOUT_S,
        )
        if hold is not None and not hold.get("active"):
            return False
        return self._defer(
            "measurement_hold", reported,
            hold="unreadable" if hold is None else "held",
        )

    def _defer(self, reason: str, reported: str | None, **fields: str) -> bool:
        if reason != reported:
            log_event(
                logger, "volume.reconcile_deferred",
                fields={"reason": reason, **fields},
            )
        self._reconcile_deferred = reason
        return True

    @property
    def reconcile_deferred(self) -> bool:
        """Whether the last reconcile tick stood down for a DSP writer or a
        measurement."""
        return self._reconcile_deferred is not None
