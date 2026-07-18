# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fail-closed set-and-confirm volume primitive.

The correction crossover flow owns a durable "restore the listening volume"
latch: it writes intent BEFORE mutating volume, sets the target and confirms it
through an independent readback, and restores exactly once. Two consumers now
need that confirm-readback core — the per-step ``CrossoverLevelLease``
(``jasper.web.correction_crossover_backend``) and the session-scoped
:class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` (Wave 2).

This leaf owns the one implementation so neither grows a copy-paste twin. It is
deliberately tiny and pure of state: the confirm primitive plus the two shared
constants (the readback tolerance and the emergency attenuation floor). Each
consumer owns its OWN durable state schema and lifecycle — the schemas differ
(the lease carries source/role; the session plan carries an ``opened_at`` +
wall-clock ceiling), so only the confirm primitive is genuinely shared.
"""

from __future__ import annotations

import math
from typing import Any, Awaitable, Callable

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
    except (OSError, RuntimeError, TimeoutError, ValueError):
        return False
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(float(observed))
    ):
        return False
    return abs(float(observed) - float(target_db)) <= tolerance_db
