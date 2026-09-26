# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fail-closed main-fader primitives: read, compare, set-and-confirm,
and where a releasing duck lands.

Each consumer owns its own durable state schema and lifecycle, so only the
primitives and their tolerance live here.

CamillaDSP does not reset ``main_volume`` on a config replace — the fader is
process state that survives a reload (read at tag ``v4.1.3``/``05e9cfc``:
``src/config/mod.rs:806`` is ``#[serde(deny_unknown_fields)]`` on ``Devices``
with no fader field, ``ProcessingParameters::new`` is constructed once at
``src/bin.rs:1119``, and ``src/pipeline.rs:270`` re-seeds a rebuilt pipeline
from the LIVE volume). What lands the fader on the declared level across a
graph swap is the duck release reference (ADR-0004).
"""

from __future__ import annotations

import math
from typing import Any, Awaitable, Callable

# The independent readback must land within this tolerance of the target for a
# volume mutation to count as confirmed.
READBACK_TOLERANCE_DB = 0.05

SetMainVolumeDb = Callable[[float], Awaitable[Any]]
GetMainVolumeDb = Callable[[], Awaitable[Any]]

#: Errors a fader read/write is allowed to fail with. Injected setters/getters
#: must REPORT failure rather than raise: bind ``CamillaController``'s methods
#: with ``best_effort=True``. ``CamillaUnavailable`` is absent because naming it
#: would import ``jasper.camilla``, which imports this leaf.
FADER_IO_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


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


def duck_release_target_db(
    *,
    reference_db: float,
    current_db: float | None,
    depth_db: float,
    entry_db: float | None = None,
) -> float:
    """Where a releasing duck lands the fader: ``min(reference, current + depth)``.

    ``reference_db`` is the level that should be in effect, resolved at release
    time and never at duck entry; ``depth_db`` is this holder's own attenuation.
    An unreadable fader (``current_db is None``) lands on
    ``min(reference, entry)`` when the holder knows its entry level, else on
    the reference. See ADR-0004.
    """
    reference = float(reference_db)
    if current_db is None:
        return reference if entry_db is None else min(reference, float(entry_db))
    return min(reference, float(current_db) + abs(float(depth_db)))


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
