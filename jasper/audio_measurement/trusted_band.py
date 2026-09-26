# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The band one reading trusts, stated from the take's pose, the declared size
of the drivers that played and the declared room (ADR-0366). A reader clips to
it and says so; no reader refuses a take for its band (ADR-0101)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .gating import SEARCH_T_MAX_MS, f_trusted_floor_hz
from .measurement_geometry import DeclaredGeometry
from .piston import at_driver_near_field, beaming_onset_hz, far_field_ceiling_hz

GATE_FLOOR = "gate_floor"
NEAR_FIELD_LIMIT = "near_field_limit"
FAR_FIELD_CEILING = "far_field_ceiling"
ROOM_UNDECLARED = "room_undeclared"
DRIVER_SIZE_UNDECLARED = "driver_size_undeclared"
#: Keele's bound: on its axis a cone reads as a piston up to ka = 1.
NEAR_FIELD_KA = 1.0


@dataclass(frozen=True)
class TrustedBand:
    """Each edge in Hz with its source, ``None`` for no edge; ``undeclared``
    names each input an edge needed and nobody declared."""

    low_hz: float | None = None
    low_source: str | None = None
    high_hz: float | None = None
    high_source: str | None = None
    undeclared: tuple[str, ...] = ()


def trusted_band(
    *, distance_m: float | None, driver: str, gated: bool,
    diameters_mm: Sequence[float | None], room: DeclaredGeometry | None,
) -> TrustedBand:
    """``distance_m`` is the pose's distance from its reference, ``None`` for a
    pose that states none from the speaker (a seat); ``driver`` is the one
    driver the pose names, if any; ``diameters_mm`` holds each played driver's
    declared diameter, ``None`` where none is declared."""
    undeclared: list[str] = []
    low = high = None
    high_source = None
    if gated:
        if room is None:
            undeclared.append(ROOM_UNDECLARED)
        low = f_trusted_floor_hz(room.first_bounce_s(distance_m) if room is not None else SEARCH_T_MAX_MS / 1000.0)
    if distance_m is not None:
        sizes = [size for size in diameters_mm if size is not None]
        if not sizes or len(sizes) < len(diameters_mm):
            undeclared.append(DRIVER_SIZE_UNDECLARED)
        elif at_driver_near_field(driver, distance_m):
            high, high_source = beaming_onset_hz(max(sizes), ka=NEAR_FIELD_KA), NEAR_FIELD_LIMIT
        else:
            high, high_source = far_field_ceiling_hz(max(sizes) / 1000.0, distance_m), FAR_FIELD_CEILING
    return TrustedBand(low, None if low is None else GATE_FLOOR, high, high_source, tuple(undeclared))
