# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve researched specifications and explicit edits without copying either."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jasper.output_topology import OutputTopology


def _overlay(base: Mapping[str, Any], edits: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in edits.items():
        if value is None:
            continue
        result[key] = (_overlay(result[key], value)
                       if isinstance(value, Mapping) and isinstance(result.get(key), Mapping)
                       else value)
    return result


def drivers_by_target(source: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """Rows keyed by the output they name; the first row for an output wins (a second one refuses)."""
    drivers = source.get("drivers") if isinstance(source, Mapping) else None
    out: dict[str, Mapping[str, Any]] = {}
    for driver in drivers if isinstance(drivers, list) else []:
        if isinstance(driver, Mapping) and driver.get("target_id"):
            out.setdefault(str(driver["target_id"]), driver)
    return out


def resolve_design_inputs(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
    driver_research: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind researched and operator values by physical target."""
    manual, research = manual_settings or {}, driver_research or {}
    manual_rows, research_rows = drivers_by_target(manual), drivers_by_target(research)
    drivers = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            target_id = channel.target_id(group.id)
            facts = dict(research_rows.get(target_id, {}))
            # Installation belongs to the operator, not an AI specification.
            facts.pop("pad", None)
            facts.pop("installation", None)
            if isinstance(facts.get("cabinet"), Mapping):
                facts["cabinet"] = {key: value for key, value in facts["cabinet"].items()
                                    if key != "enclosure_kind"}
            edits = manual_rows.get(target_id, {})
            if not facts and not edits:
                continue
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
    return {"drivers": drivers, "crossover_candidates": list(candidates.values()),
            "driver_spacing_mm": manual.get("driver_spacing_mm")}


def resolved_draft_inputs(draft: Mapping[str, Any]) -> dict[str, Any]:
    topology = OutputTopology.from_mapping(draft["topology"])
    return resolve_design_inputs(topology, draft.get("manual_settings"), draft.get("driver_research"))
