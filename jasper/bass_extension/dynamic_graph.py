# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build the bounded native CamillaDSP dynamic-bass pipeline fragment."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from .dynamic import DynamicBassDescriptor, validate_dynamic_bass_descriptor


PREFIX = "bass_ext_dynamic"


@dataclass(frozen=True)
class NativeDynamicBassGraph:
    filters: dict[str, dict[str, Any]]
    mixers: dict[str, dict[str, Any]]
    processors: dict[str, dict[str, Any]]
    pipeline: tuple[dict[str, Any], ...]


def _source(channel: int, *, inverted: bool = False) -> dict[str, Any]:
    return {"channel": channel, "gain": 0.0, "inverted": inverted}


def _mixer(channels_in: int, channels_out: int, sources: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "channels": {"in": channels_in, "out": channels_out},
        "mapping": [
            {"dest": destination, "sources": channel_sources}
            for destination, channel_sources in enumerate(sources)
        ],
    }


def build_native_dynamic_bass_graph(
    *,
    channels: int,
    owner_channels: tuple[int, ...],
    descriptor: DynamicBassDescriptor,
) -> NativeDynamicBassGraph:
    """Return a graph fragment that adds and demand-limits only LF delta.

    CamillaDSP applies Main volume before configured pipeline steps. The
    detector therefore sees the actual level presented to this graph.
    """

    if type(channels) is not int or channels <= 0:
        raise ValueError("channels must be a positive integer")
    if (
        not owner_channels
        or len(set(owner_channels)) != len(owner_channels)
        or any(type(channel) is not int or not 0 <= channel < channels for channel in owner_channels)
    ):
        raise ValueError("owner_channels must be unique channels in the graph")

    owners = tuple(sorted(owner_channels))
    count = len(owners)
    loud_channels = tuple(range(channels, channels + count))
    delta_channels = loud_channels
    detector_channels = tuple(range(channels + count, channels + 2 * count))
    expanded_channels = channels + count
    working_channels = channels + 2 * count

    filters: dict[str, dict[str, Any]] = {
        f"{PREFIX}_loudness": {
            "type": "Loudness",
            "parameters": {
                "fader": "Main",
                "reference_level": descriptor.reference_level_db,
                "high_boost": 0.0,
                "low_boost": descriptor.low_boost_db,
                "attenuate_mid": False,
            },
        },
        f"{PREFIX}_detector_lowpass": {
            "type": "BiquadCombo",
            "parameters": {
                "type": "LinkwitzRileyLowpass",
                "freq": descriptor.detector_lowpass_hz,
                "order": 4,
            },
        },
    }
    delta_filters: list[str] = []
    if descriptor.delta_highpass_hz is not None:
        name = f"{PREFIX}_delta_highpass"
        filters[name] = {
            "type": "BiquadCombo",
            "parameters": {
                "type": "ButterworthHighpass",
                "freq": descriptor.delta_highpass_hz,
                "order": 2,
            },
        }
        delta_filters.append(name)

    expand_sources = [[_source(channel)] for channel in range(channels)]
    expand_sources.extend([[_source(owner)] for owner in owners])
    form_sources = [[_source(channel)] for channel in range(channels)]
    form_sources.extend(
        [_source(loud), _source(owner, inverted=True)]
        for owner, loud in zip(owners, loud_channels, strict=True)
    )
    form_sources.extend([[_source(loud)] for loud in loud_channels])
    reduce_sources = [[_source(channel)] for channel in range(channels)]
    for owner, delta in zip(owners, delta_channels, strict=True):
        reduce_sources[owner].append(_source(delta))

    mixers = {
        f"{PREFIX}_expand": _mixer(channels, expanded_channels, expand_sources),
        f"{PREFIX}_form_delta": _mixer(expanded_channels, working_channels, form_sources),
        f"{PREFIX}_reduce": _mixer(working_channels, channels, reduce_sources),
    }
    processors = {
        f"{PREFIX}_compress_{owner}": {
            "type": "Compressor",
            "parameters": {
                "channels": working_channels,
                "attack": descriptor.compressor_attack_s,
                "release": descriptor.compressor_release_s,
                "threshold": descriptor.compressor_threshold_dbfs,
                "factor": descriptor.compressor_factor,
                "makeup_gain": 0.0,
                "monitor_channels": [detector],
                "process_channels": [delta],
            },
        }
        for owner, delta, detector in zip(
            owners, delta_channels, detector_channels, strict=True
        )
    }

    pipeline: list[dict[str, Any]] = [
        {"type": "Mixer", "name": f"{PREFIX}_expand"},
        {
            "type": "Filter",
            "channels": list(loud_channels),
            "names": [f"{PREFIX}_loudness"],
        },
        {"type": "Mixer", "name": f"{PREFIX}_form_delta"},
    ]
    if delta_filters:
        pipeline.append(
            {"type": "Filter", "channels": list(delta_channels), "names": delta_filters}
        )
    pipeline.append(
        {
            "type": "Filter",
            "channels": list(detector_channels),
            "names": [f"{PREFIX}_detector_lowpass"],
        }
    )
    pipeline.extend(
        {"type": "Processor", "name": name} for name in processors
    )
    pipeline.append({"type": "Mixer", "name": f"{PREFIX}_reduce"})
    return NativeDynamicBassGraph(filters, mixers, processors, tuple(pipeline))


def _coerce_descriptor(value: DynamicBassDescriptor | Mapping[str, Any]) -> DynamicBassDescriptor:
    if isinstance(value, DynamicBassDescriptor):
        return value
    return DynamicBassDescriptor(**validate_dynamic_bass_descriptor(value))


def _owner_limiter_step(payload: Mapping[str, Any], owners: tuple[int, ...]) -> tuple[int, int]:
    filters = payload.get("filters")
    pipeline = payload.get("pipeline")
    if not isinstance(filters, Mapping) or not isinstance(pipeline, list):
        raise ValueError("base graph has no filters or pipeline")
    matches: list[tuple[int, int]] = []
    for step_index, step in enumerate(pipeline):
        if not isinstance(step, Mapping) or step.get("type") != "Filter":
            continue
        if tuple(sorted(step.get("channels", ()))) != owners:
            continue
        names = step.get("names")
        if not isinstance(names, list):
            continue
        limiter_indexes = [
            index
            for index, name in enumerate(names)
            if isinstance(filters.get(name), Mapping)
            and filters[name].get("type") == "Limiter"
        ]
        if len(limiter_indexes) == 1:
            matches.append((step_index, limiter_indexes[0]))
    if len(matches) != 1:
        raise ValueError("bass-owner chain must have exactly one limiter step")
    return matches[0]


def apply_dynamic_bass_graph(
    payload: Mapping[str, Any],
    descriptor: DynamicBassDescriptor | Mapping[str, Any],
    bass_channels: tuple[int, ...],
) -> dict[str, Any]:
    """Decorate one static driver chain immediately before its limiter."""

    result = copy.deepcopy(dict(payload))
    playback = result.get("devices", {}).get("playback", {})
    channels = playback.get("channels") if isinstance(playback, Mapping) else None
    if type(channels) is not int or channels <= 0:
        raise ValueError("base graph playback channel count is invalid")
    owners = tuple(sorted(bass_channels))
    dynamic = build_native_dynamic_bass_graph(
        channels=channels,
        owner_channels=owners,
        descriptor=_coerce_descriptor(descriptor),
    )
    sections = (
        ("filters", dynamic.filters),
        ("mixers", dynamic.mixers),
        ("processors", dynamic.processors),
    )
    for section_name, additions in sections:
        section = result.setdefault(section_name, {})
        if not isinstance(section, dict) or set(section) & set(additions):
            raise ValueError(f"base graph conflicts with dynamic {section_name}")
        section.update(copy.deepcopy(additions))

    step_index, limiter_index = _owner_limiter_step(result, owners)
    owner_step = result["pipeline"][step_index]
    before = {**owner_step, "names": owner_step["names"][:limiter_index]}
    after = {**owner_step, "names": owner_step["names"][limiter_index:]}
    if not before["names"] or not after["names"]:
        raise ValueError("bass-owner limiter must follow its static driver filters")
    result["pipeline"][step_index : step_index + 1] = [
        before,
        *copy.deepcopy(dynamic.pipeline),
        after,
    ]
    return result


def validated_base_graph(
    payload: Mapping[str, Any],
    descriptor: DynamicBassDescriptor | Mapping[str, Any],
    bass_channels: tuple[int, ...],
) -> dict[str, Any]:
    """Validate and remove the native block for the existing static proof."""

    result = copy.deepcopy(dict(payload))
    playback = result.get("devices", {}).get("playback", {})
    channels = playback.get("channels") if isinstance(playback, Mapping) else None
    if type(channels) is not int or channels <= 0:
        raise ValueError("dynamic graph playback channel count is invalid")
    owners = tuple(sorted(bass_channels))
    expected = build_native_dynamic_bass_graph(
        channels=channels,
        owner_channels=owners,
        descriptor=_coerce_descriptor(descriptor),
    )
    for section_name, definitions in (
        ("filters", expected.filters),
        ("mixers", expected.mixers),
        ("processors", expected.processors),
    ):
        section = result.get(section_name)
        prefixed = (
            {name for name in section if str(name).startswith(PREFIX)}
            if isinstance(section, dict)
            else set()
        )
        if not isinstance(section, dict) or prefixed != set(definitions) or any(
            section.get(name) != definition for name, definition in definitions.items()
        ):
            raise ValueError(f"dynamic {section_name} do not match the descriptor")
        for name in definitions:
            del section[name]
        if section_name == "processors" and not section:
            result.pop(section_name)

    pipeline = result.get("pipeline")
    if not isinstance(pipeline, list):
        raise ValueError("dynamic graph pipeline is invalid")
    fragment = list(expected.pipeline)
    starts = [
        index
        for index in range(len(pipeline) - len(fragment) + 1)
        if pipeline[index : index + len(fragment)] == fragment
    ]
    if len(starts) != 1:
        raise ValueError("dynamic pipeline block is missing or duplicated")
    start = starts[0]
    if start == 0 or start + len(fragment) >= len(pipeline):
        raise ValueError("dynamic pipeline block is outside the owner chain")
    before = pipeline[start - 1]
    after = pipeline[start + len(fragment)]
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or before.get("type") != "Filter"
        or after.get("type") != "Filter"
        or tuple(sorted(before.get("channels", ()))) != owners
        or tuple(sorted(after.get("channels", ()))) != owners
        or not isinstance(before.get("names"), list)
        or not isinstance(after.get("names"), list)
    ):
        raise ValueError("dynamic pipeline block does not split the bass-owner chain")
    restored = {**before, "names": [*before["names"], *after["names"]]}
    pipeline[start - 1 : start + len(fragment) + 1] = [restored]
    return result
