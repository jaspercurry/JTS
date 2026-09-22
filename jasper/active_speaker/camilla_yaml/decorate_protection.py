# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Sequence

import yaml

from jasper.camilla_emit import emit_linkwitz_riley

from ..camilla_names import (
    baseline_protection_name,
    driver_baseline_limiter_name,
    driver_delay_name,
)
from ..graph_safety import protection_requirement_present, view_from_yaml_dict
from ..profile import (
    SUPPORTED_LR_ORDERS,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    required_driver_roles,
)

if TYPE_CHECKING:
    from ..branch_chain import CrossoverSection
from .devices import _finite_float, _positive_int
from .topology import _channels_for_role


def _add_baseline_protection(
    preset: ActiveSpeakerPreset,
    filter_yaml: str,
    pipeline_yaml: str,
    sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None,
) -> tuple[str, str]:
    if sections_by_role is None:
        return filter_yaml, pipeline_yaml
    if set(sections_by_role) != set(required_driver_roles(preset.way_count)):
        raise ActiveSpeakerConfigError("baseline protection must cover every driver role")
    graph = yaml.safe_load(f"filters:\n{filter_yaml}\npipeline:\n{pipeline_yaml}")
    for role, sections in sections_by_role.items():
        channels = set(_channels_for_role(preset, role))
        added = 0
        for section in sections:
            cutoff = _finite_float(section.fc_hz, "protection cutoff")
            order = _positive_int(section.order, "protection order")
            if cutoff <= 0 or order not in SUPPORTED_LR_ORDERS:
                raise ActiveSpeakerConfigError("unsupported baseline protection section")
            requirement = {
                "kind": "highpass" if section.highpass else "lowpass",
                "cutoff_hz": cutoff,
                "minimum_slope_db_per_octave": order * 6.0,
                "family_or_equivalent": "equivalent_or_steeper",
            }
            view = view_from_yaml_dict(graph)
            if all(protection_requirement_present(
                view, output_index=channel, allowed_channels=channels,
                requirement=requirement,
            ) for channel in channels):
                continue
            name = baseline_protection_name(role, added, section.highpass)
            definition = "\n".join(emit_linkwitz_riley(
                name, highpass=section.highpass, freq_hz=cutoff, order=order,
            ))
            graph["filters"].update(yaml.safe_load(definition))
            filter_yaml += "\n" + definition
            for step in graph["pipeline"]:
                if step["type"] == "Filter" and set(step["channels"]) == channels:
                    names = step["names"]
                    if driver_baseline_limiter_name(role) in names:
                        original = f"names: [{', '.join(names)}]"
                        names.insert(names.index(driver_delay_name(role)), name)
                        pipeline_yaml = pipeline_yaml.replace(original, f"names: [{', '.join(names)}]")
            added += 1
    return filter_yaml, pipeline_yaml
