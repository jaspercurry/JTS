# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A banked pose's legend label."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _whole_degrees(value: Any) -> int | None:
    """One banked angle as a whole number, or ``None`` for "not recorded".

    ``bool`` is rejected before ``int`` because it subclasses it, so a
    hand-edited ``true`` cannot be drawn on a legend as 1°.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def position_label(row: Mapping[str, Any]) -> str:
    degrees = _whole_degrees(row.get("position_deg"))
    # Absent on a row banked before the field existed, and 0 on every seat
    # taken at mark height — neither draws a raise on the legend.
    elevation = _whole_degrees(row.get("vertical_deg")) or 0
    raw_role = str(row.get("role") or "")
    role = {"onax": "On axis", "offax": "Off axis"}.get(
        raw_role, raw_role.replace("_", " ").title(),
    )
    parts: list[str] = []
    if degrees is not None:
        parts.append(f"{degrees:+d}°" if degrees else "0°")
    if elevation:
        # The word carries the sign, so the number does not repeat it.
        parts.append(f"{abs(elevation)}° {'up' if elevation > 0 else 'down'}")
    if role:
        parts.append(role)
    return " · ".join(parts) or str(row.get("position_id") or "Measurement")
