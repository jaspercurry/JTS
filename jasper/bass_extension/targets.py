# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Target-family margin policy and level-anchor derivation."""
from __future__ import annotations

import math
from dataclasses import dataclass

from jasper.volume_curve import DEFAULT_VOLUME_FLOOR_DB, percent_to_db


@dataclass(frozen=True)
class MarginPolicy:
    name: str
    boost_cap_db: float
    digital_margin_db: float
    subsonic_corner_ratio: float
    subsonic_order: int


MARGINS: dict[str, MarginPolicy] = {
    "conservative": MarginPolicy(
        name="conservative",
        boost_cap_db=6.0,
        digital_margin_db=4.0,
        subsonic_corner_ratio=0.75,
        subsonic_order=4,
    ),
    "normal": MarginPolicy(
        name="normal",
        boost_cap_db=9.0,
        digital_margin_db=3.0,
        subsonic_corner_ratio=0.70,
        subsonic_order=4,
    ),
    "aggressive": MarginPolicy(
        name="aggressive",
        boost_cap_db=12.0,
        digital_margin_db=2.0,
        subsonic_corner_ratio=0.65,
        subsonic_order=2,
    ),
}


def digital_anchor_level(
    boost_headroom_db: float,
    digital_margin_db: float,
    floor_db: float = DEFAULT_VOLUME_FLOOR_DB,
) -> int:
    """Return the highest listening level satisfying the digital margin."""

    values = (boost_headroom_db, digital_margin_db, floor_db)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("anchor inputs must be finite")
    if boost_headroom_db <= 0.0:
        return 100
    ceiling_db = -float(digital_margin_db) - float(boost_headroom_db)
    for level in range(100, -1, -1):
        if percent_to_db(level, floor_db=float(floor_db)) <= ceiling_db:
            return level
    return 0
