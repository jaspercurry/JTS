# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure Camilla graph-content proof for one null-walk delay candidate.

The shared null-walk chooses bounded candidate coordinates. Its active-speaker
and bass-management hosts own the writer lock, apply, fresh ``active_raw``
read-back, capture, evidence/run identity, and exact restore transaction. This
module only proves content: a supplied graph is the bound zero-relative
predecessor with exactly one requested lane delay changed.

It performs no I/O, establishes no freshness or live authority, and does not
schedule or consume a walk. A host can therefore use the same typed proof at
either integration boundary without moving CamillaDSP ownership into the
measurement core.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, NoReturn, TypeAlias, cast

from jasper.camilla_emit import fmt

from .null_walk import (
    DELAY_WALK_SCOPES,
    MAX_DSP_DELAY_US,
    DelayCandidate,
    DelayWalkScope,
    DspPredecessor,
    NullWalkError,
    NullWalkSpec,
)

DelayGraphFailureCode: TypeAlias = Literal[
    "snapshot_invalid",
    "snapshot_fingerprint_mismatch",
    "scope_mismatch",
    "topology_mismatch",
    "crossover_mismatch",
    "candidate_invalid",
    "readback_invalid",
    "volume_limit_invalid",
    "lane_binding_invalid",
    "delay_filter_invalid",
    "delay_mismatch",
    "graph_mismatch",
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


def _scope(value: Any, *, code: DelayGraphFailureCode) -> DelayWalkScope:
    if isinstance(value, str) and value in DELAY_WALK_SCOPES:
        return cast(DelayWalkScope, value)
    _refuse(code, "delay graph scope is not a supported null-walk scope")


def _topology_id(value: Any, *, code: DelayGraphFailureCode) -> str:
    out = value.strip() if isinstance(value, str) else ""
    if not out:
        _refuse(code, "topology_id must be a non-empty string")
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


def _graph_fingerprint(graph: Mapping[str, Any]) -> str:
    return DspPredecessor({"graph": graph}).fingerprint


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


@dataclass(frozen=True)
class DelayLaneBinding:
    """Host-owned target mapped to one graph-proven Camilla channel set.

    ``identity_filter_name`` is a non-delay filter from the target's canonical
    emitter-owned chain.  The owning host derives that name from its emitter
    vocabulary and ``channels`` from its topology; the shared proof only checks
    that the identity and delay filters occupy the same exact pipeline step and
    channel set.
    """

    target: str
    filter_name: str
    identity_filter_name: str
    channels: tuple[int, ...]

    def __post_init__(self) -> None:
        target = self.target.strip().lower() if isinstance(self.target, str) else ""
        filter_name = (
            self.filter_name.strip() if isinstance(self.filter_name, str) else ""
        )
        identity_filter_name = (
            self.identity_filter_name.strip()
            if isinstance(self.identity_filter_name, str)
            else ""
        )
        if not target or not filter_name or not identity_filter_name:
            _refuse(
                "lane_binding_invalid",
                "delay lane target, delay filter, and identity filter must be "
                "non-empty strings",
            )
        if filter_name == identity_filter_name:
            _refuse(
                "lane_binding_invalid",
                "delay lane identity filter must differ from its Delay filter",
            )
        if type(self.channels) is not tuple or not self.channels:
            _refuse(
                "lane_binding_invalid",
                "delay lane channels must be a non-empty tuple",
            )
        if any(type(channel) is not int or channel < 0 for channel in self.channels):
            _refuse(
                "lane_binding_invalid",
                "delay lane channels must be non-negative integers",
            )
        if len(set(self.channels)) != len(self.channels):
            _refuse("lane_binding_invalid", "delay lane channels must be unique")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "filter_name", filter_name)
        object.__setattr__(self, "identity_filter_name", identity_filter_name)
        object.__setattr__(self, "channels", tuple(sorted(self.channels)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "filter_name": self.filter_name,
            "identity_filter_name": self.identity_filter_name,
            "channels": list(self.channels),
        }


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


def _lane_proof(
    graph: Mapping[str, Any],
    binding: DelayLaneBinding,
    *,
    code: DelayGraphFailureCode,
) -> dict[str, Any]:
    _delay_filter_value(graph, binding.filter_name, code="delay_filter_invalid")
    filters = graph.get("filters")
    identity_spec = (
        filters.get(binding.identity_filter_name)
        if isinstance(filters, Mapping)
        else None
    )
    identity_type = (
        identity_spec.get("type") if isinstance(identity_spec, Mapping) else None
    )
    if (
        not isinstance(identity_type, str)
        or not identity_type.strip()
        or identity_type == "Delay"
    ):
        _refuse(
            code,
            f"bound identity filter {binding.identity_filter_name!r} is not a "
            "non-Delay filter",
        )
    delay_placement = _pipeline_filter_placement(
        graph,
        binding.filter_name,
        code=code,
    )
    identity_placement = _pipeline_filter_placement(
        graph,
        binding.identity_filter_name,
        code=code,
    )
    if delay_placement != identity_placement:
        _refuse(
            code,
            f"bound delay filter {binding.filter_name!r} does not share the "
            "authoritative target step and channel set with "
            f"{binding.identity_filter_name!r}",
        )
    step_index, channels = delay_placement
    if channels != binding.channels:
        _refuse(
            code,
            f"bound filter {binding.filter_name!r} is on the wrong channel set",
        )
    return {
        **binding.to_dict(),
        "pipeline_step_index": step_index,
    }


@dataclass(frozen=True, init=False)
class DelayGraphSnapshot:
    """Zero-relative F1 predecessor plus two graph-proven delay lanes.

    The host stages both bound delay slots to numeric zero inside its outer
    exact-restore transaction, reads back that graph, and freezes the same
    :class:`DspPredecessor` the F1 runner will later restore.
    """

    scope: DelayWalkScope
    topology_id: str
    crossover_fc_hz: float
    positive_lane: DelayLaneBinding
    negative_lane: DelayLaneBinding
    _spec: NullWalkSpec = field(repr=False)
    _predecessor: DspPredecessor = field(repr=False)
    fingerprint: str
    predecessor_fingerprint: str
    graph_fingerprint: str

    def __init__(
        self,
        spec: NullWalkSpec,
        *,
        scope: DelayWalkScope,
        topology_id: str,
        positive_lane: DelayLaneBinding,
        negative_lane: DelayLaneBinding,
        predecessor: DspPredecessor,
    ) -> None:
        if not isinstance(spec, NullWalkSpec):
            _refuse("snapshot_invalid", "spec must be NullWalkSpec")
        validated_scope = _scope(scope, code="snapshot_invalid")
        topology = _topology_id(topology_id, code="snapshot_invalid")
        if not isinstance(positive_lane, DelayLaneBinding) or not isinstance(
            negative_lane, DelayLaneBinding
        ):
            _refuse("lane_binding_invalid", "both delay lanes must be typed bindings")
        if positive_lane.target != spec.positive_delay_target:
            _refuse("lane_binding_invalid", "positive delay target binding is unknown")
        if negative_lane.target != spec.negative_delay_target:
            _refuse("lane_binding_invalid", "negative delay target binding is unknown")
        if positive_lane.filter_name == negative_lane.filter_name:
            _refuse("lane_binding_invalid", "delay lanes cannot share one filter")
        if positive_lane.identity_filter_name == negative_lane.identity_filter_name:
            _refuse(
                "lane_binding_invalid",
                "delay lanes cannot share one identity filter",
            )
        if {
            positive_lane.filter_name,
            negative_lane.filter_name,
        } & {
            positive_lane.identity_filter_name,
            negative_lane.identity_filter_name,
        }:
            _refuse(
                "lane_binding_invalid",
                "delay and identity filter bindings must be distinct",
            )
        if set(positive_lane.channels) & set(negative_lane.channels):
            _refuse(
                "lane_binding_invalid", "delay targets cannot share output channels"
            )
        if not isinstance(predecessor, DspPredecessor):
            _refuse("snapshot_invalid", "predecessor must be DspPredecessor")

        # Validate the complete physical fine-grid envelope arithmetically.
        # A resumable schedule may contain more than Shared's exhaustive
        # 25-point budget, but it may never retain endpoints outside Camilla's
        # hard delay bound.
        try:
            spec.fine_grid_coordinate(spec.fine_grid_index_min)
            spec.fine_grid_coordinate(spec.fine_grid_index_max)
        except NullWalkError as exc:
            raise DelayGraphProofError(
                "snapshot_invalid",
                "delay graph spec exceeds the bounded physical grid",
            ) from exc

        frozen_graph = _frozen_json_mapping(
            predecessor.state.get("active_raw"),
            code="snapshot_invalid",
            field_name="predecessor active_raw graph",
        )
        _require_volume_limit(frozen_graph, code="volume_limit_invalid")
        positive_proof = _lane_proof(
            frozen_graph,
            positive_lane,
            code="lane_binding_invalid",
        )
        negative_proof = _lane_proof(
            frozen_graph,
            negative_lane,
            code="lane_binding_invalid",
        )
        if (
            _delay_filter_value(
                frozen_graph,
                positive_lane.filter_name,
                code="delay_filter_invalid",
            )
            != 0.0
            or _delay_filter_value(
                frozen_graph,
                negative_lane.filter_name,
                code="delay_filter_invalid",
            )
            != 0.0
        ):
            _refuse(
                "snapshot_invalid",
                "zero-relative predecessor requires both bound delays at 0.0 ms",
            )

        binding = DspPredecessor(
            {
                "schema_version": 2,
                "kind": "jts_delay_graph_snapshot_binding",
                "predecessor_fingerprint": predecessor.fingerprint,
                "scope": validated_scope,
                "topology_id": topology,
                "spec": spec.to_dict(),
                "lane_proofs": [positive_proof, negative_proof],
            }
        )
        object.__setattr__(self, "scope", validated_scope)
        object.__setattr__(self, "topology_id", topology)
        object.__setattr__(self, "crossover_fc_hz", spec.crossover_fc_hz)
        object.__setattr__(self, "positive_lane", positive_lane)
        object.__setattr__(self, "negative_lane", negative_lane)
        object.__setattr__(self, "_spec", spec)
        object.__setattr__(self, "_predecessor", predecessor)
        object.__setattr__(self, "fingerprint", binding.fingerprint)
        object.__setattr__(self, "predecessor_fingerprint", predecessor.fingerprint)
        object.__setattr__(self, "graph_fingerprint", _graph_fingerprint(frozen_graph))

    @property
    def graph(self) -> dict[str, Any]:
        graph = self._predecessor.state["active_raw"]
        assert isinstance(graph, dict)
        return graph


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


def _candidate_lane(
    snapshot: DelayGraphSnapshot,
    candidate: DelayCandidate,
) -> DelayLaneBinding | None:
    relative = _real_number(
        candidate.relative_delay_us,
        code="candidate_invalid",
        field_name="relative_delay_us",
    )
    delay_us = _real_number(
        candidate.delay_us,
        code="candidate_invalid",
        field_name="delay_us",
    )
    if delay_us < 0.0 or delay_us > MAX_DSP_DELAY_US:
        _refuse("candidate_invalid", "candidate delay is outside the DSP bound")
    if not math.isclose(delay_us, abs(relative), abs_tol=1e-6):
        _refuse(
            "candidate_invalid", "candidate delay does not match its signed coordinate"
        )
    try:
        bounded = snapshot._spec.dsp_candidate(relative)
    except NullWalkError as exc:
        raise DelayGraphProofError(
            "candidate_invalid",
            "candidate is outside the bound null-walk grid",
        ) from exc
    if candidate != bounded:
        _refuse(
            "candidate_invalid", "candidate does not match the bound walk operation"
        )
    if bounded.delay_target == snapshot.positive_lane.target:
        return snapshot.positive_lane
    if bounded.delay_target == snapshot.negative_lane.target:
        return snapshot.negative_lane
    return None


def confirm_delay_candidate(
    snapshot: DelayGraphSnapshot,
    candidate: DelayCandidate,
    readback_graph: Mapping[str, Any],
    *,
    expected_snapshot_fingerprint: str,
    expected_scope: DelayWalkScope,
    expected_topology_id: str,
    expected_crossover_fc_hz: float,
) -> DelayCandidateConfirmation:
    """Prove supplied graph content is the exact requested delay audition.

    Expected context values bind the proof content to the host request. They do
    not prove the read-back is fresh, came from a live CamillaDSP instance, or
    belongs to the current writer transaction; the F2b host must establish
    those authorities before accepting this value as measurement evidence.
    """

    if not isinstance(snapshot, DelayGraphSnapshot):
        _refuse("snapshot_invalid", "snapshot must be DelayGraphSnapshot")
    if expected_snapshot_fingerprint != snapshot.fingerprint:
        _refuse(
            "snapshot_fingerprint_mismatch",
            "delay candidate is not bound to the graph snapshot",
        )
    scope = _scope(expected_scope, code="scope_mismatch")
    if scope != snapshot.scope:
        _refuse("scope_mismatch", "delay candidate belongs to a different scope")
    topology = _topology_id(expected_topology_id, code="topology_mismatch")
    if topology != snapshot.topology_id:
        _refuse("topology_mismatch", "delay candidate belongs to a stale topology")
    fc = _real_number(
        expected_crossover_fc_hz,
        code="crossover_mismatch",
        field_name="expected_crossover_fc_hz",
    )
    if fc != snapshot.crossover_fc_hz:
        _refuse("crossover_mismatch", "delay candidate belongs to another crossover")
    if not isinstance(candidate, DelayCandidate):
        _refuse("candidate_invalid", "candidate must be DelayCandidate")
    lane = _candidate_lane(snapshot, candidate)

    readback = _frozen_json_mapping(
        readback_graph,
        code="readback_invalid",
        field_name="DSP graph read-back",
    )
    _require_volume_limit(readback, code="volume_limit_invalid")
    _lane_proof(readback, snapshot.positive_lane, code="lane_binding_invalid")
    _lane_proof(readback, snapshot.negative_lane, code="lane_binding_invalid")
    positive_ms = _delay_filter_value(
        readback,
        snapshot.positive_lane.filter_name,
        code="delay_filter_invalid",
    )
    negative_ms = _delay_filter_value(
        readback,
        snapshot.negative_lane.filter_name,
        code="delay_filter_invalid",
    )

    effective_delay_ms = float(fmt(candidate.delay_us / 1000.0))
    requested_relative_us = (
        math.copysign(
            effective_delay_ms * 1000.0,
            candidate.relative_delay_us,
        )
        if candidate.relative_delay_us
        else 0.0
    )
    readback_relative_us = (positive_ms - negative_ms) * 1000.0
    if not math.isclose(
        readback_relative_us,
        requested_relative_us,
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        _refuse(
            "delay_mismatch",
            "DSP graph relative delay does not match the requested candidate",
        )

    expected = snapshot.graph
    if lane is not None:
        filters = expected["filters"]
        assert isinstance(filters, dict)
        filter_spec = filters[lane.filter_name]
        assert isinstance(filter_spec, dict)
        params = filter_spec["parameters"]
        assert isinstance(params, dict)
        params["delay"] = effective_delay_ms
    if _graph_fingerprint(readback) != _graph_fingerprint(expected):
        _refuse(
            "graph_mismatch",
            "DSP graph changed outside the exact zero-relative candidate content",
        )

    candidate_fingerprint = DspPredecessor(
        {
            "schema_version": 2,
            "kind": "jts_delay_candidate_confirmation_request",
            "snapshot_fingerprint": snapshot.fingerprint,
            "predecessor_fingerprint": snapshot.predecessor_fingerprint,
            "scope": scope,
            "topology_id": topology,
            "crossover_fc_hz": snapshot.crossover_fc_hz,
            "candidate": candidate.to_dict(),
            "readback_relative_delay_us": readback_relative_us,
        }
    ).fingerprint
    return DelayCandidateConfirmation(
        scope=scope,
        topology_id=topology,
        crossover_fc_hz=snapshot.crossover_fc_hz,
        snapshot_fingerprint=snapshot.fingerprint,
        predecessor_fingerprint=snapshot.predecessor_fingerprint,
        predecessor_graph_fingerprint=snapshot.graph_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        readback_graph_fingerprint=_graph_fingerprint(readback),
        relative_delay_us=candidate.relative_delay_us,
        readback_relative_delay_us=readback_relative_us,
        delay_target=candidate.delay_target,
        delay_filter=lane.filter_name if lane is not None else None,
        delay_us=candidate.delay_us,
        effective_delay_us=effective_delay_ms * 1000.0,
    )
