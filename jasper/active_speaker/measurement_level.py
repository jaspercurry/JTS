# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Broadband static gain of compiled measurement graphs."""

from __future__ import annotations

from functools import lru_cache
from typing import Sequence

import numpy as np
import yaml

from jasper.bass_extension.dynamic_graph import PREFIX as DYNAMIC_BASS_PREFIX
from jasper.audio_measurement.program import RoleBand
from jasper.output_topology import OutputTopology, measurement_target_id

from .branch_chain import camilla_filter_response
from .branch_peak import BranchPeakError, _mixer_mapping, _step_channels, _step_transfer
from .measurement import active_driver_targets


@lru_cache(maxsize=32)
def _path_responses(graph_text: str, band_hz: tuple[float, float]) -> np.ndarray:
    graph = yaml.safe_load(graph_text)
    freqs = np.geomspace(*band_hz, 2048)
    width = graph["devices"]["capture"]["channels"]
    responses = np.ones((width, len(freqs)), dtype=np.complex128)
    filters = graph["filters"]
    for index, step in enumerate(graph["pipeline"]):
        # Dynamic bass is unity at rest; this comparison owns static gain only.
        if str(step.get("name", "")).startswith(DYNAMIC_BASS_PREFIX):
            continue
        if step["type"] == "Mixer":
            width, mapping = _mixer_mapping(graph["mixers"][step["name"]], len(responses), step["name"])
            mixed = np.zeros((width, len(freqs)), dtype=np.complex128)
            for dest, sources in mapping:
                for source, gain in sources:
                    mixed[dest] += responses[source] * gain
            responses = mixed
        elif step["type"] == "Filter":
            names = [name for name in step["names"] if not name.startswith(DYNAMIC_BASS_PREFIX)]
            biquads = [name for name in names if filters[name]["type"] in {"Biquad", "BiquadCombo"}]
            response, _ = _step_transfer([name for name in names if name not in biquads], filters, freqs)
            response *= camilla_filter_response([filters[name] for name in biquads], freqs)
            for channel in _step_channels(step, len(responses), index) if names else ():
                responses[channel] *= response
        else:
            raise BranchPeakError(f"unsupported measurement step: {step['type']}")
    return abs(responses)


def scope_gains_db(graph_text_for_scope: str, graph_text_for_candidate: str,
                   roles: Sequence[RoleBand], *, topology: OutputTopology) -> dict[str, float]:
    """Median path-gain ratios in each role's band, keyed by measurement target.

    Coherent unity inputs cover summed and separately routed role programs.
    Limiters are pass-through here; their level caps remain with admission.
    """
    gains = {role.role: 0.0 for role in roles}
    if graph_text_for_scope == graph_text_for_candidate:
        return gains
    targets = active_driver_targets(topology)
    for role in roles:
        band = (role.band.lower_hz, role.band.upper_hz)
        scope, candidate = (_path_responses(text, band)
                            for text in (graph_text_for_scope, graph_text_for_candidate))
        channels: dict[str, list[int]] = {}
        for target in targets:
            name = measurement_target_id(target["role"], target.get("output_variant", "primary"))
            if target["role"] == role.role or name == role.role:
                channels.setdefault(name, []).append(target["output_index"])
        for name, outputs in channels.items():
            live = [ch for ch in outputs if ch < min(len(scope), len(candidate))
                    and np.any(scope[ch]) and np.any(candidate[ch])]
            if not live:
                gains[name] = 0.0
                continue
            numerator, denominator = scope[live].max(axis=0), candidate[live].max(axis=0)
            audible = (numerator > 0) & (denominator > 0)
            gains[name] = float(np.median(20 * np.log10(numerator[audible] / denominator[audible])))
    return gains
