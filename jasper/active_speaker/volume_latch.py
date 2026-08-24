# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fail-closed main-fader discipline: set-and-confirm, and hold.

The correction crossover flow owns a durable "restore the listening volume"
latch: it writes intent BEFORE mutating volume, sets the target and confirms it
through an independent readback, and restores exactly once. Two consumers now
need that confirm-readback core — the per-step ``CrossoverLevelLease``
(``jasper.web.correction_crossover_backend``) and the session-scoped
:class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` (Wave 2).

This leaf owns the one implementation so neither grows a copy-paste twin. It is
deliberately pure of state — the primitives plus the two shared constants (the
readback tolerance and the emergency attenuation floor). Each consumer owns its
OWN durable state schema and lifecycle — the schemas differ (the lease carries
source/role; the session plan carries an ``opened_at`` + wall-clock ceiling), so
only the fader primitives are genuinely shared:

* :func:`fader_matches` — the ONE "do these two fader dB values agree?" test.
* :func:`set_and_confirm_volume` — set, then prove through a fresh readback.
* :func:`hold_fader_at` — prove the fader still sits where a
  measurement session declared it, repairing a drift and refusing when the
  repair cannot be proven (issue #2925).

**Why "hold" exists (#2925).** Setting a volume once is not the same as it
STAYING set. A CamillaDSP ``SetConfig`` replace does not preserve runtime
``main_volume``: it re-applies the incoming config's STORED volume and leaves
it there. Measured on jts3 on 2026-08-24 with a byte-identical live config —
the fader was set to −13.596, confirmed, then snapped back to the config's
−9.59596 across the ``set_active_config_raw`` call, with no volume command
issued. Every crossover-v2 MEASURE-phase stimulus is bracketed in exactly that
load/restore pair, so a whole overnight campaign of measuring sweeps played
8.712 dB below the volume its session plan had confirmed — and, because the
excitation-safety ledger admits a program against the DECLARED volume, that
ledger was wrong for every one of them. It was wrong quiet that night; the same
seam reversed is loud. So a declared measurement volume is re-proven per
stimulus, not once per session.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# The independent readback must land within this tolerance of the target for a
# volume mutation to count as confirmed. Ported verbatim from the crossover
# lease so both consumers agree on "confirmed".
READBACK_TOLERANCE_DB = 0.05

# The attenuated fallback a restore path drops to when it cannot confirm the
# exact original volume. A measurement session that cannot prove it restored the
# household's volume must leave the speaker safely quiet, not loud.
EMERGENCY_MEASUREMENT_VOLUME_DB = -60.0

SetMainVolumeDb = Callable[[float], Awaitable[Any]]
GetMainVolumeDb = Callable[[], Awaitable[Any]]

#: Errors a fader read/write is allowed to fail with. Named rather than blind:
#: ``CamillaController`` already closes its own surface, and the callers wrap
#: ``CamillaUnavailable`` into ``RuntimeError`` before it reaches here.
_FADER_IO_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


class MeasurementFaderDrift(RuntimeError):
    """The main fader is not at the volume a measurement session declared.

    Raised by :func:`hold_fader_at` when a drifted fader could not be
    repaired and re-proven. It is a REFUSAL, not a report: the caller must not
    emit the stimulus, because a capture taken at an unknown level is not a
    measurement and the excitation-safety ledger that admitted the program was
    computed against the declared volume, not this one.

    ``observed_db`` is ``None`` when the fader could not be read at all — the
    same refusal, since an unreadable fader is equally unprovable.
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


def fader_matches(
    observed: Any,
    expected_db: float,
    *,
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> bool:
    """The one "do these two fader dB values agree?" test.

    ``True`` only for a real, finite number within ``tolerance_db`` of
    ``expected_db``. A ``None``, a bool, a non-numeric or a non-finite reading
    is a disagreement, never a pass — every caller here is fail-closed, and
    "could not read" must never render as "matches".
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

    Returns ``True`` only when the setter did not report failure AND a fresh
    readback lands within ``tolerance_db`` of ``target_db``. Any setter/readback
    error, a ``False`` setter return, or a non-finite/mismatched readback yields
    ``False`` — the fail-closed contract a restore latch depends on.
    """
    try:
        applied = await set_main_volume_db(float(target_db))
        if applied is False:
            return False
        observed = await get_main_volume_db()
    except _FADER_IO_ERRORS:
        return False
    return fader_matches(observed, target_db, tolerance_db=tolerance_db)


async def hold_fader_at(
    expected_db: float,
    set_main_volume_db: SetMainVolumeDb,
    get_main_volume_db: GetMainVolumeDb,
    *,
    context: str = "",
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> float:
    """Prove the fader is at ``expected_db``; repair one drift, then refuse.

    The per-stimulus half of the measurement-volume discipline (#2925 T1-1),
    and the ONE implementation both the routed MEASURE path and the summed
    VERIFY path use — the defect this closes lived precisely in the gap between
    those two paths, so a per-phase copy would rebuild it. Pure and stateless
    by design: WHICH volume to hold, and whether a session still owns one, is
    :meth:`~jasper.active_speaker.session_volume_plan.SessionVolumePlan.hold_measurement_volume`'s
    question, and that is what a capture path calls.

    Order, all fail-closed:

    1. Read the live fader. An unreadable read is treated as a disagreement,
       so it takes the repair branch rather than passing.
    2. Already within ``tolerance_db``: return it. The happy path costs one
       read and writes nothing, so the hold cannot itself become a source of
       volume churn.
    3. Otherwise DISCLOSE at WARNING — naming what was read, what was expected
       and the gap, or recording an empty reading when there was none. The
       repair below would otherwise be silent, and a silently repaired fader is
       how a whole campaign of stimuli can play at the wrong level without one
       line of evidence. Then set-and-confirm ``expected_db`` and prove it
       through a further INDEPENDENT read.
    4. Still not there: raise :class:`MeasurementFaderDrift`. The caller refuses
       the capture rather than banking it. A read that failed once and answers
       after the set HAS established the level, and is allowed through —
       disclose, never force; the refusal is for a level that cannot be
       established, not for one round-trip that missed.

    **What the refusal line does and does not tell a support read.**
    ``observed_db`` is empty ONLY when the fader could not be read — that is
    the one clean discriminator, and it is a real reading rather than an
    inference because the proving re-read above is unconditional.
    ``set_confirmed`` is the SET-AND-CONFIRM's verdict, not the setter's, so on
    a refusal it is normally ``false`` and does NOT separate "the setter
    refused" from "the setter reported success and the fader did not move";
    those two share a line. It earns its place for the opposite case:
    ``set_confirmed=true`` on a refusal means the repair WAS confirmed at the
    target and the fader moved again before the proving read — something is
    contending for it in real time, which is a different problem from either.

    Returns the proven fader reading.

    ``expected_db`` is not range-checked here. It is a *target* this function
    only ever moves the fader TO, and it is already bounded twice: unavoidably
    by ``jasper.camilla``, which clamps every write to ``MAX_MAIN_VOLUME_DB``
    (0.0 dB), and — for the one caller today — by ``SessionVolumePlan.open``,
    which refuses a non-finite or positive measurement volume before there is
    anything to hold. A third check here would be a third owner of one rule.
    """

    target = float(expected_db)

    async def _read() -> float | None:
        """One live read, normalized to "a usable number, or nothing"."""
        try:
            value = await get_main_volume_db()
        except _FADER_IO_ERRORS:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(float(value)) else None

    observed = await _read()
    if observed is not None and fader_matches(
        observed, target, tolerance_db=tolerance_db
    ):
        return observed

    log_event(
        logger,
        "active_speaker.measurement_fader_drift",
        level=logging.WARNING,
        result="repairing",
        context=context,
        expected_db=f"{target:.6f}",
        observed_db="" if observed is None else f"{observed:.6f}",
        delta_db="" if observed is None else f"{observed - target:.6f}",
        tolerance_db=f"{float(tolerance_db):.6f}",
    )
    repaired = await set_and_confirm_volume(
        target, set_main_volume_db, get_main_volume_db, tolerance_db=tolerance_db,
    )
    # UNCONDITIONAL, even when set-and-confirm reported failure. Two reasons,
    # and the first is the one that matters: the refusal's ``observed_db`` is
    # the only thing distinguishing "the fader read fine and would not move"
    # from "the fader could not be read", and short-circuiting here reported
    # the second for both — stating an observation JTS never made, which is
    # the ``locate_failed`` #2085 class this whole change exists to close.
    # Second, the PROOF is where the fader actually sits, not what the setter
    # returned: if it is at the target anyway, the level is established. Costs
    # one extra read only on the already-failing path.
    proven = await _read()
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
            set_confirmed=repaired,
        )
        raise MeasurementFaderDrift(
            expected_db=target, observed_db=proven, context=context,
        )
    log_event(
        logger,
        "active_speaker.measurement_fader_drift",
        level=logging.WARNING,
        result="repaired",
        context=context,
        expected_db=f"{target:.6f}",
        observed_db=f"{proven:.6f}",
    )
    return proven
