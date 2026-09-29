# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from typing import Any, Mapping


def _correction_value(
    corrections: Mapping[str, Mapping[str, float | bool]],
    role: str,
    field: str,
    default: float,
) -> float:
    value: Any = corrections.get(role, {}).get(field)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _correction_bool(
    corrections: Mapping[str, Mapping[str, float | bool]],
    role: str,
    field: str,
) -> bool:
    return bool(corrections.get(role, {}).get(field))


# Never silently mute the program through headroom absorption (ADR-0219).
MAX_PROGRAM_HEADROOM_DB = 40.0
