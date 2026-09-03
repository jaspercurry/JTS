# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fail-closed main-fader discipline: set-and-confirm, and hold.

The one implementation of the fader primitives both the per-step
``CrossoverLevelLease`` (``jasper.web.correction_crossover_backend``) and the
session-scoped ``session_volume_plan.SessionVolumePlan`` use; each consumer
owns its own durable state schema and lifecycle, so only the primitives and
their two constants live here.

CamillaDSP does not reset ``main_volume`` on a config replace — the fader is
process state that survives a reload (read at tag ``v4.1.3``/``05e9cfc``:
``src/config/mod.rs:806`` is ``#[serde(deny_unknown_fields)]`` on ``Devices``
with no fader field, ``ProcessingParameters::new`` is constructed once at
``src/bin.rs:1119``, and ``src/pipeline.rs:270`` re-seeds a rebuilt pipeline
from the LIVE volume). What lands the fader on the declared level across a
graph swap is the duck release reference (ADR-0004).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# The independent readback must land within this tolerance of the target for a
# volume mutation to count as confirmed.
READBACK_TOLERANCE_DB = 0.05

# The attenuated fallback a restore path drops to when it cannot confirm the
# exact original volume. A measurement session that cannot prove it restored the
# household's volume must leave the speaker safely quiet, not loud.
EMERGENCY_MEASUREMENT_VOLUME_DB = -60.0

SetMainVolumeDb = Callable[[float], Awaitable[Any]]
GetMainVolumeDb = Callable[[], Awaitable[Any]]

#: Errors a fader read/write is allowed to fail with. Injected setters/getters
#: must REPORT failure rather than raise: bind ``CamillaController``'s methods
#: with ``best_effort=True``. ``CamillaUnavailable`` is absent because naming it
#: would import ``jasper.camilla``, which imports this leaf.
FADER_IO_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


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


async def read_fader_db(get_main_volume_db: GetMainVolumeDb) -> float | None:
    """One live read, normalized to "a usable number, or nothing".

    A bool, a non-numeric, a non-finite reading and a :data:`FADER_IO_ERRORS`
    raise all normalize to ``None``: an unreadable fader must never render as
    a value.
    """
    try:
        value = await get_main_volume_db()
    except FADER_IO_ERRORS:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def fader_matches(
    observed: Any,
    expected_db: float,
    *,
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> bool:
    """The one "do these two fader dB values agree?" test.

    ``True`` only for a real, finite number within ``tolerance_db`` of
    ``expected_db``: "could not read" must never render as "matches".
    """
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(float(observed))
    ):
        return False
    return abs(float(observed) - float(expected_db)) <= tolerance_db


async def set_and_confirm_volume(
    target_db: float,
    set_main_volume_db: SetMainVolumeDb,
    get_main_volume_db: GetMainVolumeDb,
    *,
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> bool:
    """Set the main volume and confirm it through an independent readback.

    ``True`` only when the setter did not report failure AND a fresh readback
    lands within ``tolerance_db`` of ``target_db``.
    """
    try:
        applied = await set_main_volume_db(float(target_db))
        if applied is False:
            return False
        observed = await get_main_volume_db()
    except FADER_IO_ERRORS:
        return False
    return fader_matches(observed, target_db, tolerance_db=tolerance_db)


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
