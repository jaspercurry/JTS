# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Relative linear response between two complete CamillaDSP graphs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.branch_peak import (
    BranchPeakError,
    complex_channel_transfer,
)


class GraphPredictionError(ValueError):
    """The two graphs cannot support an exact relative-response claim."""


@dataclass(frozen=True)
class RelativeGraphResponse:
    """Target/source complex response, with unusable bins left as NaN."""

    freqs_hz: np.ndarray
    responses_by_role: Mapping[str, np.ndarray]
    usable_by_role: Mapping[str, np.ndarray]
    valid_band_hz_by_role: Mapping[str, tuple[float, float]]
    excluded_bins_by_role: Mapping[str, Mapping[str, int]]
    limiter_passthrough: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid_band_hz_by_role": {
                role: list(band) for role, band in self.valid_band_hz_by_role.items()
            },
            "usable_bins_by_role": {
                role: int(np.count_nonzero(mask))
                for role, mask in self.usable_by_role.items()
            },
            "excluded_bins_by_role": {
                role: dict(counts)
                for role, counts in self.excluded_bins_by_role.items()
            },
            "limiter": {
                "model": "unchanged_pass_through",
                "nonlinear_response_modelled": False,
            },
        }


def _graph_shape(config: Mapping[str, Any]) -> tuple[int, int, int]:
    try:
        devices = config["devices"]
        rate = devices["samplerate"]
        capture = devices["capture"]["channels"]
        playback = devices["playback"]["channels"]
    except (KeyError, TypeError) as exc:
        raise GraphPredictionError("graph devices do not declare channel widths") from exc
    values = (rate, capture, playback)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
        raise GraphPredictionError("graph sample rate and channel widths must be positive integers")
    return int(rate), int(capture), int(playback)


def _channels(step: Mapping[str, Any]) -> tuple[int, ...] | None:
    raw = step.get("channels")
    if isinstance(raw, list):
        return tuple(int(channel) for channel in raw)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return (int(raw),)
    raw = step.get("channel")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return (int(raw),)
    return None


def _limiter_signature(config: Mapping[str, Any]) -> tuple[tuple[Any, ...], ...]:
    filters = config.get("filters")
    pipeline = config.get("pipeline")
    if not isinstance(filters, Mapping) or not isinstance(pipeline, list):
        raise GraphPredictionError("graph filters or pipeline are unreadable")
    found: list[tuple[Any, ...]] = []
    mixers_seen: list[str] = []
    for step in pipeline:
        if not isinstance(step, Mapping):
            continue
        if step.get("type") == "Mixer":
            mixers_seen.append(str(step.get("name") or ""))
            continue
        if step.get("type") != "Filter":
            continue
        for name in step.get("names") or ():
            spec = filters.get(name)
            if not isinstance(spec, Mapping) or spec.get("type") != "Limiter":
                continue
            params = spec.get("parameters")
            if not isinstance(params, Mapping):
                raise GraphPredictionError(f"limiter {name!r} has no parameters")
            clip_limit = params.get("clip_limit")
            if (
                isinstance(clip_limit, bool)
                or not isinstance(clip_limit, (int, float))
                or not math.isfinite(float(clip_limit))
            ):
                raise GraphPredictionError(f"limiter {name!r} has no finite clip limit")
            found.append((
                tuple(mixers_seen),
                str(name),
                _channels(step),
                bool(step.get("bypassed") or False),
                bool(params.get("soft_clip") or False),
                float(clip_limit),
            ))
    return tuple(found)


def _weights(
    by_role: Mapping[str, Mapping[int, complex]], roles: set[str], label: str,
) -> dict[str, Mapping[int, complex]]:
    if set(by_role) != roles:
        raise GraphPredictionError(f"{label} input weights must cover exactly {sorted(roles)}")
    return {role: by_role[role] for role in sorted(roles)}


def relative_branch_response(
    source_graph: Mapping[str, Any],
    target_graph: Mapping[str, Any],
    freqs_hz: Any,
    *,
    role_output_channels: Mapping[str, int],
    source_input_weights_by_role: Mapping[str, Mapping[int, complex]],
    target_input_weights_by_role: Mapping[str, Mapping[int, complex]],
    valid_band_hz_by_role: Mapping[str, tuple[float, float]],
    minimum_source_magnitude: float = 1e-8,
) -> RelativeGraphResponse:
    """Return the target/source transfer for each measured acoustic branch.

    Input weights state how each graph is driven. For example, source branch
    diagnostics use one role channel at unity, while a normal summed graph uses
    two coherent unity channels. The graph's mixer coefficients remain in the
    calculation, so the latter includes both mono-sum legs.
    """
    roles = set(role_output_channels)
    if not roles or set(valid_band_hz_by_role) != roles:
        raise GraphPredictionError("outputs and valid bands must cover the same roles")
    source_weights = _weights(source_input_weights_by_role, roles, "source")
    target_weights = _weights(target_input_weights_by_role, roles, "target")
    source_shape = _graph_shape(source_graph)
    target_shape = _graph_shape(target_graph)
    if source_shape != target_shape:
        raise GraphPredictionError(
            f"graph sample rate/channel topology changed: {source_shape} != {target_shape}"
        )
    if _limiter_signature(source_graph) != _limiter_signature(target_graph):
        raise GraphPredictionError("graph limiter semantics or placement changed")
    if (
        isinstance(minimum_source_magnitude, bool)
        or not isinstance(minimum_source_magnitude, (int, float))
        or not math.isfinite(float(minimum_source_magnitude))
        or minimum_source_magnitude <= 0.0
    ):
        raise GraphPredictionError("minimum source magnitude must be finite and positive")

    freqs = np.asarray(freqs_hz, dtype=np.float64)
    responses: dict[str, np.ndarray] = {}
    usable: dict[str, np.ndarray] = {}
    excluded: dict[str, dict[str, int]] = {}
    bands: dict[str, tuple[float, float]] = {}
    try:
        for role in sorted(roles):
            band = tuple(float(value) for value in valid_band_hz_by_role[role])
            if len(band) != 2 or not all(map(math.isfinite, band)) or band[0] >= band[1]:
                raise GraphPredictionError(f"{role} has an invalid response band")
            source = complex_channel_transfer(
                source_graph,
                freqs,
                input_weights=source_weights[role],
                output_channels={role: role_output_channels[role]},
                allow_limiter_passthrough=True,
            )[role]
            target = complex_channel_transfer(
                target_graph,
                freqs,
                input_weights=target_weights[role],
                output_channels={role: role_output_channels[role]},
                allow_limiter_passthrough=True,
            )[role]
            in_band = (freqs >= band[0]) & (freqs <= band[1])
            finite = np.isfinite(source) & np.isfinite(target)
            near_zero = np.abs(source) < float(minimum_source_magnitude)
            mask = in_band & finite & ~near_zero
            ratio = np.full(freqs.shape, complex(np.nan, np.nan), dtype=np.complex128)
            np.divide(target, source, out=ratio, where=mask)
            responses[role] = ratio
            usable[role] = mask
            bands[role] = band
            excluded[role] = {
                "outside_valid_band": int(np.count_nonzero(~in_band)),
                "non_finite": int(np.count_nonzero(in_band & ~finite)),
                "source_near_zero": int(np.count_nonzero(in_band & finite & near_zero)),
            }
    except BranchPeakError as exc:
        raise GraphPredictionError(str(exc)) from exc
    return RelativeGraphResponse(
        freqs_hz=freqs,
        responses_by_role=responses,
        usable_by_role=usable,
        valid_band_hz_by_role=bands,
        excluded_bins_by_role=excluded,
    )
