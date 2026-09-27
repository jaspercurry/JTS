# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A correction measurement's hold on this process's fader.

``VolumeCoordinator`` owns one gate. MEASURE_PAUSE raises it through
``note_measurement_active``; the coordinator's level doors and its 1 Hz
reconciler ask it. Nothing here writes the fader.
"""
from __future__ import annotations

import asyncio
import logging

from .log_event import log_event
from .voice import measurement_hold as voice_measurement
from .volume_owner import VolumeClaimRefused

logger = logging.getLogger(__name__)


class MeasurementGate:
    """The measurement flag, when it was raised, and the reconcile write lock."""

    def __init__(self) -> None:
        # Correction-measurement gate for the voice daemon's own 1 Hz
        # reconciler AND for the voice tools' level doors (set/adjust/unmute),
        # which reach the coordinator in-process and so never pass
        # jasper-control's measurement hold. It does not turn this
        # process-local flag into a cross-daemon Camilla lock, and never blocks
        # an emergency user MUTE. It prevents any writer from replacing a ramp
        # value with the persisted listening_level mid-measurement.
        self.active: bool = False
        # When the flag above was raised, so a missed lower can lapse rather
        # than refuse for the life of the process. See :meth:`holds_fader`.
        self._active_at: float = 0.0
        self._lapse_logged: bool = False
        # Serializes the final reconciler write with MEASURE_PAUSE acquisition.
        # Pause does not acknowledge until an already-started write has landed;
        # after the flag flips, no new reconcile write may enter this lock.
        self.write_lock = asyncio.Lock()

    def holds_fader(self) -> bool:
        """True while a live measurement owns the fader; False once it lapses.

        A pure predicate: it must not clear ``active``, or a volume write
        arriving after the lapse would also un-pause the 1 Hz reconciler —
        which reads the raw flag — in the middle of a window that is merely
        renewing late. The reconciler clears it on its own tick instead
        (:meth:`lapse_stranded`).

        The lapse itself exists because ``note_measurement_active(False)`` is
        best-effort on the voice daemon's rollback path: past its aggregate
        deadline the resume coroutine is closed unawaited and the safety task
        is already cancelled, so the flag can stay raised with nothing left to
        lower it. Treating it as lapsed after MEASUREMENT_AUTOCLEAR_SEC — the
        backstop that would have cleared it — bounds a stranded flag's effect
        on these doors to one window instead of the life of the process.
        """
        if not self.active:
            return False
        held_for = voice_measurement.measurement_monotonic() - self._active_at
        if held_for < voice_measurement.MEASUREMENT_AUTOCLEAR_SEC:
            return True
        if not self._lapse_logged:
            self._lapse_logged = True
            log_event(
                logger,
                "volume.measurement_flag_expired",
                held_for_s=f"{held_for:.1f}",
                autoclear_s=f"{voice_measurement.MEASUREMENT_AUTOCLEAR_SEC:.1f}",
                level=logging.WARNING,
            )
        return False

    def lapse_stranded(self) -> None:
        """Clear a stranded flag on the reconciler's OWN tick.

        :meth:`holds_fader` may not clear it — a volume write is the wrong
        clock (see there). This 1 Hz tick is the right one: it runs on the
        same schedule the flag pauses, so applying the same
        MEASUREMENT_AUTOCLEAR_SEC bound here bounds a stranded flag's effect on
        drift reconciliation to one window instead of the life of the process.
        No await between the read and the write, so a window renewing
        concurrently cannot have its fresh flag cleared.
        """
        if self.active and not self.holds_fader():
            self.active = False

    def refuse_level_write(self) -> None:
        """Refuse a level write while a measurement holds the fader.

        These doors are reached IN-PROCESS — `jasper.tools.audio` calls them on
        the coordinator whenever the box is not a bonded follower, so the
        request never crosses HTTP and jasper-control's measurement hold never
        sees it. While the hold is live the measurement OWNS the fader: it
        drives camilla's main_volume directly and never writes the persistence
        file, so no persisted level here can be compared against where the
        fader actually sits, and a write in EITHER direction can land a
        stimulus above the driver's declared cap. Raising is what makes the
        refusal visible: `tools` turns the exception into the tool's
        `{"error": ...}` payload, so the model says the speaker is busy. MUTE
        stays open as the emergency door; unmute does not, because restoring
        the household level is a level write.
        """
        if self.holds_fader():
            raise VolumeClaimRefused(
                "a measurement is in progress and holds the volume"
            )

    async def note_active(self, active: bool) -> None:
        """Raise or lower the flag once no reconcile write is in flight."""
        async with self.write_lock:
            self.active = bool(active)
            if self.active:
                self._active_at = voice_measurement.measurement_monotonic()
                self._lapse_logged = False
