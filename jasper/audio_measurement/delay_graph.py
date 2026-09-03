# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure Camilla graph-content proof for a compiled delay binding.

Proves a supplied graph carries exactly one requested Delay filter on exactly
the requested channels. Performs no I/O and establishes no freshness or live
authority: the caller owns the writer lock, the apply, and the read-back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Mapping, NoReturn, TypeAlias

from jasper.camilla_emit import fmt

from .null_walk import (
    MAX_DSP_DELAY_US,
    DelayWalkScope,
    DspPredecessor,
    NullWalkError,
)

DelayGraphFailureCode: TypeAlias = Literal[
    "snapshot_invalid",
    "candidate_invalid",
    "volume_limit_invalid",
    "lane_binding_invalid",
    "delay_filter_invalid",
    "delay_mismatch",
]


class DelayGraphProofError(NullWalkError):
    """A typed fail-closed graph-content refusal."""

    def __init__(self, code: DelayGraphFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _refuse(code: DelayGraphFailureCode, message: str) -> NoReturn:
    raise DelayGraphProofError(code, message)


def _real_number(value: Any, *, code: DelayGraphFailureCode, field_name: str) -> float:
    if type(value) not in {int, float}:
        _refuse(code, f"{field_name} must be a real JSON number")
    out = float(value)
    if not math.isfinite(out):
        _refuse(code, f"{field_name} must be finite")
    return out


def _frozen_json_mapping(
    value: Any,
    *,
    code: DelayGraphFailureCode,
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        _refuse(code, f"{field_name} must be a non-empty mapping")
    try:
        frozen = DspPredecessor({"value": value}).state["value"]
    except NullWalkError as exc:
        raise DelayGraphProofError(
            code, f"{field_name} is not exact JSON data"
        ) from exc
    assert isinstance(frozen, dict)
    return frozen


def _require_volume_limit(
    graph: Mapping[str, Any], *, code: DelayGraphFailureCode
) -> None:
    devices = graph.get("devices")
    limit = devices.get("volume_limit") if isinstance(devices, Mapping) else None
    limit_db = _real_number(
        limit,
        code=code,
        field_name="devices.volume_limit",
    )
    if limit_db > 0.0:
        _refuse(code, "devices.volume_limit must not exceed the 0 dB JTS ceiling")


def quantized_delay_ms(delay_us: float) -> float:
    """The single µs→ms quantizer for a Camilla ``Delay`` filter value.

    CamillaDSP YAML carries delays through ``jasper.camilla_emit.fmt`` (4
    decimal places of ms), so that formatter is the SOLE quantizer: one
    ``fmt`` pass over the raw microsecond value, no intermediate rounding.
    Every producer that folds a requested ``delay_us`` into a graph value
    (the measured-crossover candidate's preset fold / corrections) and every
    proof that recomputes the expected value from the same ``delay_us`` MUST
    share this helper — two recipes quantize differently on ~0.4% of the
    range (e.g. ``round(µs/1000, 6)`` then ``fmt`` vs a single ``fmt``
    disagree at delay_us=11382.15006948647), which turns into a spurious
    fail-closed proof refusal.
    """
    return float(fmt(delay_us / 1000.0))


def _delay_filter_value(
    graph: Mapping[str, Any],
    filter_name: str,
    *,
    code: DelayGraphFailureCode,
) -> float:
    filters = graph.get("filters")
    spec = filters.get(filter_name) if isinstance(filters, Mapping) else None
    if not isinstance(spec, Mapping) or spec.get("type") != "Delay":
        _refuse(code, f"bound filter {filter_name!r} is not a Delay filter")
    params = spec.get("parameters")
    if not isinstance(params, Mapping) or params.get("unit") != "ms":
        _refuse(code, f"bound filter {filter_name!r} must use milliseconds")
    delay_ms = _real_number(
        params.get("delay"),
        code=code,
        field_name=f"{filter_name}.parameters.delay",
    )
    if delay_ms < 0.0 or delay_ms * 1000.0 > MAX_DSP_DELAY_US:
        _refuse(code, f"bound filter {filter_name!r} exceeds the delay safety bound")
    return delay_ms


def _pipeline_filter_placement(
    graph: Mapping[str, Any],
    filter_name: str,
    *,
    code: DelayGraphFailureCode,
) -> tuple[int, tuple[int, ...]]:
    pipeline = graph.get("pipeline")
    if not isinstance(pipeline, list):
        _refuse(code, "CamillaDSP graph has no pipeline list")

    placements: list[tuple[int, tuple[int, ...]]] = []
    for step_index, step in enumerate(pipeline):
        if not isinstance(step, Mapping) or step.get("type") != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list):
            continue
        name_count = sum(name == filter_name for name in names)
        if not name_count:
            continue
        channels = step.get("channels")
        if not isinstance(channels, list) or not channels:
            _refuse(code, f"bound filter {filter_name!r} has no channel set")
        if any(type(channel) is not int or channel < 0 for channel in channels):
            _refuse(code, "bound delay pipeline channel set is invalid")
        if len(set(channels)) != len(channels):
            _refuse(code, "bound delay pipeline channels must be unique")
        placement = (step_index, tuple(sorted(channels)))
        placements.extend(placement for _ in range(name_count))

    if len(placements) != 1:
        _refuse(
            code,
            f"bound filter {filter_name!r} must occur in exactly one pipeline step",
        )
    return placements[0]


@dataclass(frozen=True)
class DelayCandidateConfirmation:
    """Content proof for one context-bound zero-relative candidate graph."""

    scope: DelayWalkScope
    topology_id: str
    crossover_fc_hz: float
    snapshot_fingerprint: str
    predecessor_fingerprint: str
    predecessor_graph_fingerprint: str
    candidate_fingerprint: str
    readback_graph_fingerprint: str
    relative_delay_us: float
    readback_relative_delay_us: float
    delay_target: str | None
    delay_filter: str | None
    delay_us: float
    effective_delay_us: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "scope": self.scope,
            "topology_id": self.topology_id,
            "crossover_fc_hz": self.crossover_fc_hz,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "predecessor_fingerprint": self.predecessor_fingerprint,
            "predecessor_graph_fingerprint": self.predecessor_graph_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "readback_graph_fingerprint": self.readback_graph_fingerprint,
            "relative_delay_us": self.relative_delay_us,
            "readback_relative_delay_us": self.readback_relative_delay_us,
            "delay_target": self.delay_target,
            "delay_filter": self.delay_filter,
            "delay_us": self.delay_us,
            "effective_delay_us": self.effective_delay_us,
        }


def prove_static_delay_binding(
    graph: Mapping[str, Any],
    *,
    delay_filter_name: str,
    channels: tuple[int, ...],
    delay_us: float,
) -> float:
    """Prove a static (non-walk) graph binds exactly one requested Delay filter.

    The one-shot proof a final compiled graph needs — e.g. an active-speaker
    baseline candidate carrying one measured per-driver delay (see
    ``jasper.active_speaker.measured_crossover_candidate``). It proves:

    1. ``delay_filter_name`` is a ``Delay`` filter in milliseconds whose value
       matches ``delay_us`` (converted to ms via the same rounding the emitter
       uses) within the JTS DSP delay bound.
    2. That filter occurs in **exactly one** pipeline ``Filter`` step (reusing
       :func:`_pipeline_filter_placement`'s "occurs exactly once" proof), wired
       to exactly ``channels``.

    Performs no I/O — the caller owns compiling/reading the graph text. Raises
    :class:`DelayGraphProofError` (one of :data:`DelayGraphFailureCode`) on any
    mismatch; never silently accepts a graph it could not prove.
    """
    if type(delay_us) not in {int, float} or not math.isfinite(float(delay_us)):
        _refuse("candidate_invalid", "delay_us must be a finite number")
    delay_us = float(delay_us)
    if delay_us < 0.0 or delay_us > MAX_DSP_DELAY_US:
        _refuse("candidate_invalid", "delay_us is outside the DSP delay bound")
    if not isinstance(delay_filter_name, str) or not delay_filter_name.strip():
        _refuse("candidate_invalid", "delay_filter_name must be a non-empty string")
    if (
        type(channels) is not tuple
        or not channels
        or any(type(channel) is not int or channel < 0 for channel in channels)
        or len(set(channels)) != len(channels)
    ):
        _refuse(
            "candidate_invalid",
            "channels must be a non-empty tuple of unique non-negative integers",
        )

    frozen_graph = _frozen_json_mapping(graph, code="snapshot_invalid", field_name="graph")
    _require_volume_limit(frozen_graph, code="volume_limit_invalid")
    delay_ms = _delay_filter_value(
        frozen_graph, delay_filter_name, code="delay_filter_invalid"
    )
    expected_ms = quantized_delay_ms(delay_us)
    if not math.isclose(delay_ms, expected_ms, rel_tol=0.0, abs_tol=1e-9):
        _refuse(
            "delay_mismatch",
            f"{delay_filter_name!r} does not match the requested delay_us",
        )
    _, bound_channels = _pipeline_filter_placement(
        frozen_graph, delay_filter_name, code="delay_filter_invalid"
    )
    if bound_channels != tuple(sorted(channels)):
        _refuse(
            "lane_binding_invalid",
            f"{delay_filter_name!r} is wired to the wrong channels",
        )
    return delay_ms
