# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure attenuation-only per-driver level trims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._common import finite_float, issue
from .driver_pad import effective_sensitivity_db


#: Floor for any single per-driver attenuation, in dB. Exported so anything
#: that resolves, persists or re-validates a trim checks the same number.
MAX_ATTENUATION_DB = -60.0


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
