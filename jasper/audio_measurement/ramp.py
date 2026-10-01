# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared measurement step helpers."""

from __future__ import annotations

import math

SPL_CEILING_EXCEEDED = "spl_ceiling_exceeded"
# Bounds one step's overshoot on a non-linear chain: a step from the 76 dB ramp
# bound stays under the 85 dB stop (ADR-0365).
MAX_STEP_DB = 6.0
CEILING_MARGIN_DB = 3.0
#: A ramp stops this far under its SPL stop, so one blind step past it stays a margin under.
RAMP_MARGIN_DB = MAX_STEP_DB + CEILING_MARGIN_DB


def capped_gap_step_db(
    *, measured_db: float, target_db: float, cap_db: float = math.inf
) -> float:
    """The measured gap, capped upward only; downwards attenuation is uncapped.

    Re-read after every step: the chain need only be locally monotone.
    The caller must still clamp the resulting fader against its own ceiling.
    """
    return min(float(target_db) - float(measured_db), float(cap_db))
