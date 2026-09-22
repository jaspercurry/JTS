# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve researched specifications and explicit edits without copying either."""

from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from typing import Any

from jasper.output_topology import OutputTopology
from .driver_protection import declared_protection_highpass_floor_hz


def _overlay(base: Mapping[str, Any], edits: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in edits.items():
        if value is None:
            continue
        result[key] = (_overlay(result[key], value)
                       if isinstance(value, Mapping) and isinstance(result.get(key), Mapping)
                       else value)
    return result


def _target_values(source: Mapping[str, Any], target_id: str, role: str, unique: bool) -> Mapping[str, Any]:
    drivers = source.get("drivers") or []
    return next((driver for driver in drivers if driver.get("target_id") == target_id),
                next((driver for driver in drivers
                      if unique and not driver.get("target_id") and driver.get("role") == role), {}))


def resolve_design_inputs(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
    driver_research: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind by physical target and disclose ambiguous legacy role values."""
    manual, research = manual_settings or {}, driver_research or {}
    drivers, bindings = [], {}
    counts = Counter(channel.role for group in topology.speaker_groups for channel in group.channels)
    for group in topology.speaker_groups:
        for channel in group.channels:
            target_id = channel.target_id(group.id)
            facts = dict(_target_values(research, target_id, channel.role,
                                        channel.output_variant != "rear"))
            # Installation belongs to the operator, not an AI specification.
            facts.pop("pad", None)
            facts.pop("installation", None)
            if isinstance(facts.get("cabinet"), Mapping):
                facts["cabinet"] = {key: value for key, value in facts["cabinet"].items()
                                    if key != "enclosure_kind"}
            edits = _target_values(manual, target_id, channel.role, channel.output_variant != "rear")
            if declared_protection_highpass_floor_hz(edits) is not None and not edits.get("recommended_highpass_hz"):
                facts.pop("recommended_highpass_hz", None)
                facts.pop("recommended_highpass_slope_db_per_octave", None)
            if not facts and not edits:
                continue
            source = edits or facts
            bindings[target_id] = ("explicit" if source.get("target_id") else
                                   "legacy" if counts[channel.role] == 1 else "ambiguous")
            driver = _overlay(facts, edits)
            driver.update(target_id=target_id, role=channel.role)
            drivers.append(driver)
    candidates: dict[tuple[str, ...], dict[str, Any]] = {}
    ranks: dict[tuple[str, ...], tuple[bool, int]] = {}
    for source, priority in ((research, 0), (manual, 10)):
        for candidate in source.get("crossover_candidates") or []:
            pair = tuple(sorted(candidate["between_roles"]))
            rank = (bool(candidate.get("frequency_hz")), priority +
                    {"high": 3, "medium": 2, "low": 1}.get(candidate.get("confidence"), 0))
            if pair not in ranks or rank > ranks[pair]:
                candidates[pair], ranks[pair] = dict(candidate), rank
    return {"drivers": drivers, "bindings": bindings, "crossover_candidates": list(candidates.values()),
            "driver_spacing_mm": manual.get("driver_spacing_mm")}


def resolved_draft_inputs(draft: Mapping[str, Any]) -> dict[str, Any]:
    topology = OutputTopology.from_mapping(draft["topology"])
    return resolve_design_inputs(topology, draft.get("manual_settings"), draft.get("driver_research"))
