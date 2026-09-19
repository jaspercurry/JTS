# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Broadband static gain of compiled measurement graphs."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import yaml

from jasper.audio_measurement.program import RoleBand
from jasper.output_topology import OutputTopology, measurement_target_id

from .branch_peak import complex_channel_transfer
from .measurement import active_driver_targets


def scope_gains_db(graph_text_for_scope: str, graph_text_for_candidate: str,
                   roles: Sequence[RoleBand], *, topology: OutputTopology) -> dict[str, float]:
    """Median path-gain ratios in each role's band, keyed by measurement target.

    The anchor's mono program passes through ALSA plug onto both capture channels, requiring coherent unity inputs.
    Limiters are pass-through here; their level caps remain with admission.
    """
    gains = {role.role: 0.0 for role in roles}
    if graph_text_for_scope == graph_text_for_candidate:
        return gains
    targets = active_driver_targets(topology)
    graphs = [yaml.safe_load(text) for text in (graph_text_for_scope, graph_text_for_candidate)]
    outputs = {target["output_index"]: target["output_index"] for target in targets}
    for role in roles:
        band = (role.band.lower_hz, role.band.upper_hz)
        scope, candidate = ({channel: abs(response) for channel, response in complex_channel_transfer(
            graph, np.geomspace(*band, 2048),
            input_weights=dict.fromkeys(range(graph["devices"]["capture"]["channels"]), 1.0),
            output_channels=outputs, allow_limiter_passthrough=True, dynamic_bass_at_rest=True,
        ).items()} for graph in graphs)
        channels: dict[str, list[int]] = {}
        for target in targets:
            name = measurement_target_id(target["role"], target.get("output_variant", "primary"))
            if target["role"] == role.role or name == role.role:
                channels.setdefault(name, []).append(target["output_index"])
        for name, role_outputs in channels.items():
            live = [ch for ch in role_outputs if np.any(scope[ch]) and np.any(candidate[ch])]
            if not live:
                gains[name] = 0.0
                continue
            numerator, denominator = (np.max([response[ch] for ch in live], axis=0) for response in (scope, candidate))
            audible = (numerator > 0) & (denominator > 0)
            gains[name] = float(np.median(20 * np.log10(numerator[audible] / denominator[audible])))
    return gains
