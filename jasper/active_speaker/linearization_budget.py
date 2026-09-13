# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Declared fit limits; realization may only tighten these bounds."""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping


DEFAULT_FIT_BUDGET: Mapping[str, Any] = MappingProxyType({
    "max_filters": 8,
    "boost_floor_hz": None,
    "max_gain_db": 12.0,
    "max_giveback_db": 18.0,
})


def normalise_fit_budget(value: Any) -> dict[str, Any]:
    """Validate a partial declaration without adding absent fields.

    ``boost_floor_hz`` is the minimum boost centre frequency in Hz; filter
    skirts extend below it. ``max_giveback_db=0`` permits no normalization down.
    """
    if not isinstance(value, Mapping) or set(value) - DEFAULT_FIT_BUDGET.keys():
        raise ValueError("fit_budget must be an object containing only fit limits")
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if key == "max_filters":
            if type(raw) is not int or not 1 <= raw <= DEFAULT_FIT_BUDGET[key]:
                raise ValueError("fit_budget.max_filters must be an integer in 1..8")
            out[key] = raw
        elif key == "boost_floor_hz" and raw is None:
            out[key] = None
        else:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                raise ValueError(f"fit_budget.{key} must be finite")
            if (key in {"boost_floor_hz", "max_gain_db"} and raw <= 0) or (
                key != "boost_floor_hz" and not 0 <= raw <= DEFAULT_FIT_BUDGET[key]
            ):
                raise ValueError(f"fit_budget.{key} is out of range")
            out[key] = float(raw)
    return out


def fit_budgets_by_role(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """A shared role must fit inside every physical target's declaration."""
    out: dict[str, dict[str, Any]] = {}
    for target in profile.get("targets", ()):
        if "fit_budget" not in target:
            continue
        budget = normalise_fit_budget(target["fit_budget"])
        role = str(target["role"])
        merged = out.setdefault(role, {})
        for key, value in budget.items():
            previous = merged.get(key, DEFAULT_FIT_BUDGET[key])
            merged[key] = (
                max(previous or 0, value or 0) or None
                if key == "boost_floor_hz" else min(previous, value)
            )
    return out
