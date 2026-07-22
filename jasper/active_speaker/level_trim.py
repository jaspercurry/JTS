# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure attenuation-only level matching for adjacent driver bands."""

from __future__ import annotations

import math
from collections.abc import Sequence


class LevelTrimError(ValueError):
    """Adjacent level evidence cannot produce a complete trim chain."""


def attenuation_from_group_deltas(
    roles: Sequence[str],
    group_deltas_db: Sequence[Sequence[tuple[str, str, float]]],
    *,
    minimum_db: float | None = None,
    reject_below_db: float | None = None,
) -> dict[str, float]:
    """Average group ``upper - lower`` deltas into attenuation-only trims."""

    ordered = tuple(roles)
    if not ordered or len(set(ordered)) != len(ordered) or not group_deltas_db:
        raise LevelTrimError("level trims require roles and at least one group")
    for bound in (minimum_db, reject_below_db):
        if bound is not None and (not math.isfinite(bound) or bound > 0.0):
            raise LevelTrimError("attenuation bound must be finite and non-positive")
    groups: list[dict[str, float]] = []
    for deltas in group_deltas_db:
        raw = {ordered[0]: 0.0}
        for lower, upper, delta in deltas:
            value = float(delta)
            if lower not in raw or upper in raw or not math.isfinite(value):
                raise LevelTrimError("adjacent deltas do not form one role chain")
            raw[upper] = raw[lower] - value
        if set(raw) != set(ordered):
            raise LevelTrimError("adjacent deltas do not cover every role")
        offset = max(raw.values())
        group = {role: round(raw[role] - offset, 1) for role in ordered}
        if reject_below_db is not None and any(
            value < reject_below_db for value in group.values()
        ):
            raise LevelTrimError("required attenuation is below the authority bound")
        if minimum_db is not None:
            group = {role: max(value, minimum_db) for role, value in group.items()}
        groups.append(group)
    averaged = {
        role: sum(group[role] for group in groups) / len(groups) for role in ordered
    }
    offset = max(averaged.values())
    trims = {role: round(averaged[role] - offset, 1) for role in ordered}
    if minimum_db is not None:
        trims = {role: max(value, minimum_db) for role, value in trims.items()}
    return trims
