# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A measurement session's fader hold: prove the main fader is at the declared
measurement volume, or refuse."""

from __future__ import annotations

import logging

from jasper.log_event import log_event
from jasper.volume_latch import READBACK_TOLERANCE_DB, GetMainVolumeDb, fader_matches, read_fader_db

logger = logging.getLogger(__name__)

# The attenuated fallback a restore path drops to when it cannot confirm the
# exact original volume. A measurement session that cannot prove it restored the
# household's volume must leave the speaker safely quiet, not loud.
EMERGENCY_MEASUREMENT_VOLUME_DB = -60.0


class MeasurementFaderDrift(RuntimeError):
    """The main fader is not at the volume a measurement session declared.

    A REFUSAL, not a report: the excitation-safety ledger that admitted the
    program was computed against the declared volume. ``observed_db`` is
    ``None`` when the fader could not be read at all — the same refusal.
    """

    def __init__(
        self,
        *,
        expected_db: float,
        observed_db: float | None,
        context: str = "",
    ) -> None:
        seen = "unreadable" if observed_db is None else f"{observed_db:.6f} dB"
        where = f" during {context}" if context else ""
        super().__init__(
            f"the main fader is not at the declared measurement volume{where}: "
            f"expected {float(expected_db):.6f} dB, read {seen}"
        )
        self.expected_db = float(expected_db)
        self.observed_db = None if observed_db is None else float(observed_db)
        self.context = context


async def hold_fader_at(
    expected_db: float,
    get_main_volume_db: GetMainVolumeDb,
    *,
    context: str = "",
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> float:
    """Prove the fader is at ``expected_db`` and return it, or refuse.

    It proves; it never writes — establishing the measurement volume is
    ``SessionVolumePlan.open``'s job, and a repair here would be a second
    writer moving the fader behind the session's back. A disagreeing first read
    is re-read independently before raising :class:`MeasurementFaderDrift`, so
    a raced round-trip is not a refusal. Never gated on a diagnostics flag
    (ADR-0009).
    """

    target = float(expected_db)

    observed = await read_fader_db(get_main_volume_db)
    if observed is not None and fader_matches(
        observed, target, tolerance_db=tolerance_db
    ):
        # The liveness half: a healthy run emits no drift lines, so this INFO
        # line is what distinguishes "the hold ran and found the level" from
        # "the hold never ran". Bounded by captures per session (~16).
        log_event(
            logger,
            "active_speaker.measurement_fader_drift",
            result="held",
            context=context,
            expected_db=f"{target:.6f}",
            observed_db=f"{observed:.6f}",
            delta_db=f"{observed - target:.6f}",
            tolerance_db=f"{float(tolerance_db):.6f}",
        )
        return observed

    log_event(
        logger,
        "active_speaker.measurement_fader_drift",
        level=logging.WARNING,
        result="disagreed",
        context=context,
        expected_db=f"{target:.6f}",
        observed_db="" if observed is None else f"{observed:.6f}",
        delta_db="" if observed is None else f"{observed - target:.6f}",
        tolerance_db=f"{float(tolerance_db):.6f}",
    )
    # Unconditional: the refusal's ``observed_db`` must be a reading JTS
    # actually took, and this is also the second chance a raced round-trip gets.
    proven = await read_fader_db(get_main_volume_db)
    if proven is None or not fader_matches(
        proven, target, tolerance_db=tolerance_db
    ):
        log_event(
            logger,
            "active_speaker.measurement_fader_drift",
            level=logging.ERROR,
            result="refused",
            context=context,
            expected_db=f"{target:.6f}",
            observed_db="" if proven is None else f"{proven:.6f}",
        )
        raise MeasurementFaderDrift(
            expected_db=target, observed_db=proven, context=context,
        )
    log_event(
        logger,
        "active_speaker.measurement_fader_drift",
        level=logging.WARNING,
        result="held",
        context=context,
        expected_db=f"{target:.6f}",
        observed_db=f"{proven:.6f}",
        reread="true",
    )
    return proven
