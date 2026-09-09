# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared room ceiling and nominal gated-speaker floor (ADR-0256).

A capture's trusted floor can exceed the 500 Hz room ceiling. The clamp does
not close that ungraded interval. Lower band edges and SNR bands have their
own measurement constraints and remain with their consumers.
"""
from __future__ import annotations

# Nominal Hz; each capture's graded floor also respects its trusted 2.5/T.
GATED_SPEC_LOWER_EDGE_HZ: float = 250.0

# The room-correction ceiling, Hz, when no per-room estimate is available: the
# shipped Toole-aligned modal/transition boundary.
ROOM_BOUNDARY_DEFAULT_HZ: float = 350.0

# Clamp bounds for a per-room ceiling estimate. The floor is the gated spec's
# lower edge BY DEFINITION (see the module docstring); the ceiling is the widest
# room-correction band the project admits, and is also the `assertive`
# strategy's band.
ROOM_BOUNDARY_MIN_HZ: float = GATED_SPEC_LOWER_EDGE_HZ
ROOM_BOUNDARY_MAX_HZ: float = 500.0

# The room layer's floor, Hz: below it a seat take says little a cabinet can
# act on, and no room filter is placed.
ROOM_FLOOR_HZ: float = 20.0

# Where a room ceiling came from: the applied candidate's trusted floor, or
# the default above when no floor was readable (ADR-0256 rule 1).
CEILING_SOURCE_APPLIED = "applied_candidate"
CEILING_SOURCE_FALLBACK = "fallback"
CEILING_SOURCES = frozenset({CEILING_SOURCE_APPLIED, CEILING_SOURCE_FALLBACK})

# The window a room median must be read in: the seat cube is the room's own
# measurement and is analyzed ungated (ADR-0260), so a gated or mixed median
# measures the speaker, not the room, and is not evidence a room layer may be
# prescribed against.
ROOM_MEDIAN_WINDOW = "ungated"


def room_ceiling_hz(trusted_floor_hz: float | None) -> float:
    """Where the room layer stops (ADR-0256 rule 1): the applied tune's
    trusted floor inside ``[ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ]``, or
    :data:`ROOM_BOUNDARY_DEFAULT_HZ` when no floor is readable."""
    if trusted_floor_hz is None:
        return ROOM_BOUNDARY_DEFAULT_HZ
    return min(max(float(trusted_floor_hz), ROOM_BOUNDARY_MIN_HZ), ROOM_BOUNDARY_MAX_HZ)
