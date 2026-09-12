# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared measurement step helpers."""

from __future__ import annotations

import math

# The digital-full-scale hard ceiling: main_volume must never exceed this,
# independent of the dynamic cap. Mirrors camilla.py::_coerce_main_volume_db,
# duplicated here as defense-in-depth. Do not raise.
HARD_CEILING_DBFS = 0.0
SPL_CEILING_EXCEEDED = "spl_ceiling_exceeded"
# Bounds one step's overshoot on a non-linear chain: 75 + 6 stays below the 85 stop.
MAX_STEP_DB = 6.0
CEILING_MARGIN_DB = 3.0


def capped_gap_step_db(
    *, measured_db: float, target_db: float, cap_db: float = math.inf
) -> float:
    """The measured gap, capped upward only; downwards attenuation is uncapped.

    Re-read after every step: the chain need only be locally monotone.
    The caller must still clamp the resulting fader against its own ceiling.
    """
    return min(float(target_db) - float(measured_db), float(cap_db))

# The exception set the ramp treats as recoverable-by-restore. A broad-but-named
# tuple rather than a blind ``except Exception`` (lint contract: no new BLE001
# suppressions): it covers every realistic failure of the injected callables
# while letting CancelledError / SystemExit / MemoryError propagate.
RECOVERABLE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    ArithmeticError,
)
