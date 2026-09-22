# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Mapping

import yaml

from jasper.bass_extension.dynamic_graph import (
    apply_dynamic_bass_graph,
    dynamic_bass_owner_groups,
    validated_base_graph,
)

from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, lowest_driver_role
from .document import _reserialize_keeping_header
from .topology import _channels_for_role


def _dynamic_bass_graph(
    base: dict[str, Any], preset: ActiveSpeakerPreset, descriptor: Mapping[str, Any]
) -> dict[str, Any]:
    channels = (
        (preset.local_subwoofer.physical_output_index,)
        if preset.local_subwoofer is not None
        else tuple(_channels_for_role(preset, lowest_driver_role(preset.way_count)))
    )
    groups = dynamic_bass_owner_groups(channels, (
        (output.side, output.driver_role, output.output_variant, output.index)
        for output in preset.channel_map.outputs
    ))
    decorated = apply_dynamic_bass_graph(base, descriptor, channels, groups)
    if validated_base_graph(decorated, descriptor, channels, groups) != base:
        raise ActiveSpeakerConfigError("dynamic bass changed the static speaker tune")
    return decorated


def _with_dynamic_bass(text: str, preset: ActiveSpeakerPreset, descriptor: Mapping[str, Any] | None) -> str:
    if not descriptor:
        return text
    return _reserialize_keeping_header(
        text, _dynamic_bass_graph(yaml.safe_load(text), preset, descriptor),
    )
