# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure attenuation-only level matching for adjacent driver bands."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from ._common import finite_float, issue
from .driver_pad import effective_sensitivity_db


#: Floor for any single per-driver attenuation, in dB: the bound this module's
#: chain solver clamps to and rejects below. Exported so anything that solves,
#: persists or re-validates a trim checks the same number the solver used.
MAX_ATTENUATION_DB = -60.0


class LevelTrimError(ValueError):
    """Adjacent level evidence cannot produce a complete trim chain."""


def declared_driver_gains(
    roles: Sequence[str], drivers: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, str], dict[str, float], list[dict[str, str]]]:
    """Resolve explicit offsets and pad-adjusted sensitivity trims."""
    gains: dict[str, float] = {}
    provenance: dict[str, str] = {}
    sensitivities: dict[str, float] = {}
    issues: list[dict[str, str]] = []
    for role in roles:
        driver = drivers.get(role)
        if not isinstance(driver, Mapping):
            continue
        sensitivity = effective_sensitivity_db(finite_float(driver.get("sensitivity_db_2v83_1m")), driver.get("pad"))
        if sensitivity is not None:
            sensitivities[role] = sensitivity
        gain = finite_float(driver.get("gain_offset_db"))
        if gain is None:
            continue
        if gain > 0:
            issues.append(issue("warning", "positive_driver_gain_ignored", f"positive gain for {role} was ignored; baseline gains only attenuate"))
            gain = 0.0
        if gain < MAX_ATTENUATION_DB:
            issues.append(issue("warning", "driver_gain_clamped", f"gain for {role} was clamped to -60 dB"))
            gain = MAX_ATTENUATION_DB
        source = str(driver.get("gain_offset_db_provenance") or "").strip()
        # Missing provenance preserves an operator's deliberate attenuation.
        provenance[role] = source if source in {"research_estimate", "sensitivity_estimate"} else "operator_pinned"
        gains[role] = gain
    datasheet: dict[str, float] = {}
    if len(sensitivities) >= 2:
        reference_db = min(sensitivities.values())
        for role, sensitivity in sensitivities.items():
            trim = reference_db - sensitivity
            if provenance.get(role) != "operator_pinned":
                gains.pop(role, None)
                provenance[role] = "sensitivity_estimate"
                if trim < -0.05:
                    datasheet[role] = max(round(trim, 1), MAX_ATTENUATION_DB)
    return {**dict.fromkeys(roles, 0.0), **datasheet, **gains}, provenance, datasheet, issues


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
