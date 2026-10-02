# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A banked pose's legend label."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .record_index import whole_degrees


def position_label(row: Mapping[str, Any]) -> str:
    degrees = whole_degrees(row.get("position_deg"))
    elevation = whole_degrees(row.get("vertical_deg"))
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
