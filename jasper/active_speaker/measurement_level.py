# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Broadband static gain of compiled measurement graphs."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping

import numpy as np
import yaml

from jasper.bass_extension.dynamic_graph import PREFIX as DYNAMIC_BASS_PREFIX

from .branch_chain import camilla_filter_response
from .branch_peak import BranchPeakError, _mixer_mapping, _step_channels, _step_transfer


def _broadband_gain_db(graph: Mapping[str, Any], freqs: np.ndarray) -> float:
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
    return float(10 * np.log10(np.max(np.mean(abs(responses) ** 2, axis=1))))


@lru_cache(maxsize=32)
def scope_gain_db(graph_text_for_scope: str, graph_text_for_candidate: str,
                  band_hz: tuple[float, float]) -> float:
    """Difference of loudest-output mean powers, in dB, at equal energy per octave.

    Coherent unity inputs cover summed and separately routed role programs.
    Limiters are pass-through here; their level caps remain with admission.
    """
    if graph_text_for_scope == graph_text_for_candidate:
        return 0.0
    # Log-bin centres give each octave the same weight without endpoint bias.
    edges = np.geomspace(*band_hz, 2049)
    freqs = np.sqrt(edges[:-1] * edges[1:])
    return (_broadband_gain_db(yaml.safe_load(graph_text_for_scope), freqs)
            - _broadband_gain_db(yaml.safe_load(graph_text_for_candidate), freqs))
