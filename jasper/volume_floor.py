# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""User-volume floor: constants and normalization, kept a dependency-free leaf.

Both ``jasper.volume_curve`` (the percent<->dB curve) and
``jasper.sound.settings`` (the wizard-persisted floor value) need these.
Splitting them out here lets ``volume_curve`` import ``sound.settings`` at
module scope instead of through a ``# lazy: cycle`` function-local import.
"""
from __future__ import annotations

from typing import Any

DEFAULT_VOLUME_FLOOR_DB = -50.0
VOLUME_CEILING_DB = 0.0

# UI/setting clamp. A floor above -10 dB makes 1% potentially loud; a floor
# below -60 dB is effectively silence for this product and wastes slider travel.
VOLUME_FLOOR_MIN_DB = -60.0
VOLUME_FLOOR_MAX_DB = -10.0


def normalize_volume_floor_db(value: Any) -> float:
    try:
        floor = float(value)
    except (TypeError, ValueError):
        return DEFAULT_VOLUME_FLOOR_DB
    if floor != floor or floor in (float("inf"), float("-inf")):
        return DEFAULT_VOLUME_FLOOR_DB
    floor = min(VOLUME_FLOOR_MAX_DB, max(VOLUME_FLOOR_MIN_DB, floor))
    return round(floor, 3)
