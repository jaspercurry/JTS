# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure live-graph proof for one null-walk delay candidate.

The shared null-walk chooses clock-exact candidate coordinates, but its two
hosts own CamillaDSP mutation and read-back.  This module is the narrow safety
boundary between those layers: it freezes the exact predecessor graph and
proves that a live read-back is that graph with only the requested, bound
``Delay.parameters.delay`` value changed.

It performs no I/O and does not schedule a walk. Active-crossover and bass-
management hosts parse CamillaDSP's ``active_raw`` YAML, pass the resulting
mapping here, and retain ownership of apply, capture, restore, and writer-lock
lifecycle.
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
    "positive_gain_refused",
    "delay_filter_invalid",
    "delay_mismatch",
    "graph_mismatch",
]


class DelayGraphProofError(NullWalkError):
    """A typed fail-closed candidate/read-back refusal."""

    def __init__(self, code: DelayGraphFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _refuse(code: DelayGraphFailureCode, message: str) -> NoReturn:
    raise DelayGraphProofError(code, message)


def _finite_number(
    value: Any, *, code: DelayGraphFailureCode, field_name: str
) -> float:
    if isinstance(value, bool):
        _refuse(code, f"{field_name} must be numeric")
    try:
        out = float(value)
    except (TypeError, ValueError):
        _refuse(code, f"{field_name} must be numeric")
    if not math.isfinite(out):
        _refuse(code, f"{field_name} must be finite")
    return out


def _scope(value: Any, *, code: DelayGraphFailureCode) -> DelayWalkScope:
    if isinstance(value, str) and value in DELAY_WALK_SCOPES:
        return cast(DelayWalkScope, value)
    _refuse(code, "delay graph scope is not a supported null-walk scope")


def _topology_id(value: Any, *, code: DelayGraphFailureCode) -> str:
    out = str(value).strip() if isinstance(value, str) else ""
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
    if not isinstance(params, Mapping) or str(params.get("unit") or "") != "ms":
        _refuse(code, f"bound filter {filter_name!r} must use milliseconds")
    delay_ms = _finite_number(
        params.get("delay"),
        code=code,
        field_name=f"{filter_name}.parameters.delay",
    )
    if delay_ms < 0.0 or delay_ms * 1000.0 > MAX_DSP_DELAY_US:
        _refuse(code, f"bound filter {filter_name!r} exceeds the delay safety bound")
    return delay_ms


def _require_graph_safety(
    graph: Mapping[str, Any],
    *,
    invalid_code: DelayGraphFailureCode,
) -> None:
    devices = graph.get("devices")
    limit = devices.get("volume_limit") if isinstance(devices, Mapping) else None
    limit_db = _finite_number(
        limit,
        code="volume_limit_invalid",
        field_name="devices.volume_limit",
    )
    if limit_db > 0.0:
        _refuse(
            "volume_limit_invalid",
            "devices.volume_limit must not exceed the 0 dB JTS ceiling",
        )

    filters = graph.get("filters")
    if not isinstance(filters, Mapping):
        _refuse(invalid_code, "CamillaDSP graph has no filters mapping")
    for name, raw_spec in filters.items():
        if not isinstance(raw_spec, Mapping):
            continue
        params = raw_spec.get("parameters")
        if not isinstance(params, Mapping) or "gain" not in params:
            continue
        gain_db = _finite_number(
            params.get("gain"),
            code="positive_gain_refused",
            field_name=f"filters.{name}.parameters.gain",
        )
        if gain_db > 0.0:
            _refuse(
                "positive_gain_refused",
                "delay audition graph contains a positive filter gain",
            )

    mixers = graph.get("mixers")
    if not isinstance(mixers, Mapping):
        return
    for mixer_name, raw_mixer in mixers.items():
        if not isinstance(raw_mixer, Mapping):
            continue
        mapping = raw_mixer.get("mapping")
        if not isinstance(mapping, list):
            continue
        for destination in mapping:
            if not isinstance(destination, Mapping):
                continue
            sources = destination.get("sources")
            if not isinstance(sources, list):
                continue
            for source in sources:
                if not isinstance(source, Mapping) or "gain" not in source:
                    continue
                gain_db = _finite_number(
                    source.get("gain"),
                    code="positive_gain_refused",
                    field_name=f"mixers.{mixer_name}.mapping.sources.gain",
                )
                if gain_db > 0.0:
                    _refuse(
                        "positive_gain_refused",
                        "delay audition graph contains a positive mixer gain",
                    )


@dataclass(frozen=True, init=False)
class DelayGraphSnapshot:
    """F1 predecessor plus the one delay field a host may mutate.

    ``predecessor`` is the exact :class:`DspPredecessor` the null-walk runner
    will later restore. Its frozen state must carry CamillaDSP's parsed,
    normalized live graph under ``active_raw``; other host-owned restore data
    (for example the durable config path) remains opaque and participates in
    the same predecessor fingerprint.
    """

    scope: DelayWalkScope
    topology_id: str
    crossover_fc_hz: float
    positive_delay_target: str
    negative_delay_target: str
    positive_delay_filter: str
    negative_delay_filter: str
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
        positive_delay_filter: str,
        negative_delay_filter: str,
        predecessor: DspPredecessor,
    ) -> None:
        if not isinstance(spec, NullWalkSpec):
            _refuse("snapshot_invalid", "spec must be NullWalkSpec")
        validated_scope = _scope(scope, code="snapshot_invalid")
        topology = _topology_id(topology_id, code="snapshot_invalid")
        fc = spec.crossover_fc_hz
        positive_target = spec.positive_delay_target
        negative_target = spec.negative_delay_target
        positive_filter = str(positive_delay_filter).strip()
        negative_filter = str(negative_delay_filter).strip()
        if (
            not positive_target
            or not negative_target
            or positive_target == negative_target
        ):
            _refuse("snapshot_invalid", "delay targets must be non-empty and distinct")
        if (
            not positive_filter
            or not negative_filter
            or positive_filter == negative_filter
        ):
            _refuse("snapshot_invalid", "delay filters must be non-empty and distinct")

        if not isinstance(predecessor, DspPredecessor):
            _refuse("snapshot_invalid", "predecessor must be DspPredecessor")
        predecessor_state = predecessor.state
        frozen_graph = _frozen_json_mapping(
            predecessor_state.get("active_raw"),
            code="snapshot_invalid",
            field_name="predecessor active_raw graph",
        )
        _require_graph_safety(frozen_graph, invalid_code="snapshot_invalid")
        _delay_filter_value(frozen_graph, positive_filter, code="delay_filter_invalid")
        _delay_filter_value(frozen_graph, negative_filter, code="delay_filter_invalid")
        binding = DspPredecessor(
            {
                "schema_version": 1,
                "kind": "jts_delay_graph_snapshot_binding",
                "predecessor_fingerprint": predecessor.fingerprint,
                "scope": validated_scope,
                "topology_id": topology,
                "spec": spec.to_dict(),
                "positive_delay_filter": positive_filter,
                "negative_delay_filter": negative_filter,
            }
        )
        object.__setattr__(self, "scope", validated_scope)
        object.__setattr__(self, "topology_id", topology)
        object.__setattr__(self, "crossover_fc_hz", fc)
        object.__setattr__(self, "positive_delay_target", positive_target)
        object.__setattr__(self, "negative_delay_target", negative_target)
        object.__setattr__(self, "positive_delay_filter", positive_filter)
        object.__setattr__(self, "negative_delay_filter", negative_filter)
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
    """Proof that the live graph is one exact, context-bound delay candidate."""

    scope: DelayWalkScope
    topology_id: str
    crossover_fc_hz: float
    snapshot_fingerprint: str
    predecessor_fingerprint: str
    predecessor_graph_fingerprint: str
    candidate_fingerprint: str
    readback_graph_fingerprint: str
    relative_delay_us: float
    delay_target: str | None
    delay_filter: str | None
    delay_us: float
    effective_delay_us: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scope": self.scope,
            "topology_id": self.topology_id,
            "crossover_fc_hz": self.crossover_fc_hz,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "predecessor_fingerprint": self.predecessor_fingerprint,
            "predecessor_graph_fingerprint": self.predecessor_graph_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "readback_graph_fingerprint": self.readback_graph_fingerprint,
            "relative_delay_us": self.relative_delay_us,
            "delay_target": self.delay_target,
            "delay_filter": self.delay_filter,
            "delay_us": self.delay_us,
            "effective_delay_us": self.effective_delay_us,
        }


def _candidate_filter(
    snapshot: DelayGraphSnapshot,
    candidate: DelayCandidate,
) -> str | None:
    relative = _finite_number(
        candidate.relative_delay_us,
        code="candidate_invalid",
        field_name="relative_delay_us",
    )
    delay_us = _finite_number(
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
    if (
        candidate.positive_delay_target != bounded.positive_delay_target
        or candidate.negative_delay_target != bounded.negative_delay_target
        or candidate.delay_target != bounded.delay_target
        or not math.isclose(delay_us, bounded.delay_us, abs_tol=1e-6)
    ):
        _refuse(
            "candidate_invalid", "candidate does not match the bound walk operation"
        )
    if bounded.delay_target == snapshot.positive_delay_target:
        delay_filter = snapshot.positive_delay_filter
    elif bounded.delay_target == snapshot.negative_delay_target:
        delay_filter = snapshot.negative_delay_filter
    else:
        delay_filter = None
    return delay_filter


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
    """Prove one live read-back is the exact requested delay-only graph.

    The expected context arguments are the host's current request binding, not
    values inferred from the graph. They prevent a valid-looking read-back from
    being admitted for a stale topology, crossover region, scope, or entry DSP
    state. Every refusal occurs before a host may capture null evidence.
    """

    if not isinstance(snapshot, DelayGraphSnapshot):
        _refuse("snapshot_invalid", "snapshot must be DelayGraphSnapshot")
    if expected_snapshot_fingerprint != snapshot.fingerprint:
        _refuse(
            "snapshot_fingerprint_mismatch",
            "delay candidate is not bound to the current graph snapshot",
        )
    scope = _scope(expected_scope, code="scope_mismatch")
    if scope != snapshot.scope:
        _refuse("scope_mismatch", "delay candidate belongs to a different scope")
    topology = _topology_id(expected_topology_id, code="topology_mismatch")
    if topology != snapshot.topology_id:
        _refuse("topology_mismatch", "delay candidate belongs to a stale topology")
    fc = _finite_number(
        expected_crossover_fc_hz,
        code="crossover_mismatch",
        field_name="expected_crossover_fc_hz",
    )
    if fc != snapshot.crossover_fc_hz:
        _refuse("crossover_mismatch", "delay candidate belongs to another crossover")
    fc = snapshot.crossover_fc_hz
    if not isinstance(candidate, DelayCandidate):
        _refuse("candidate_invalid", "candidate must be DelayCandidate")
    delay_filter = _candidate_filter(snapshot, candidate)

    readback = _frozen_json_mapping(
        readback_graph,
        code="readback_invalid",
        field_name="live DSP read-back",
    )
    _require_graph_safety(readback, invalid_code="readback_invalid")
    _delay_filter_value(
        readback,
        snapshot.positive_delay_filter,
        code="delay_filter_invalid",
    )
    _delay_filter_value(
        readback,
        snapshot.negative_delay_filter,
        code="delay_filter_invalid",
    )

    expected = snapshot.graph
    expected_delay_ms = float(fmt(candidate.delay_us / 1000.0))
    if delay_filter is not None:
        filters = expected["filters"]
        assert isinstance(filters, dict)
        filter_spec = filters[delay_filter]
        assert isinstance(filter_spec, dict)
        params = filter_spec["parameters"]
        assert isinstance(params, dict)
        params["delay"] = expected_delay_ms

    actual_delay_ms = (
        _delay_filter_value(readback, delay_filter, code="delay_filter_invalid")
        if delay_filter is not None
        else 0.0
    )
    if delay_filter is not None and not math.isclose(
        actual_delay_ms,
        expected_delay_ms,
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        _refuse(
            "delay_mismatch", "live DSP delay does not match the requested candidate"
        )
    if _graph_fingerprint(readback) != _graph_fingerprint(expected):
        _refuse(
            "graph_mismatch", "live DSP graph changed outside the bound delay field"
        )

    candidate_fingerprint = DspPredecessor(
        {
            "schema_version": 1,
            "kind": "jts_delay_candidate_confirmation_request",
            "snapshot_fingerprint": snapshot.fingerprint,
            "predecessor_fingerprint": snapshot.predecessor_fingerprint,
            "scope": scope,
            "topology_id": topology,
            "crossover_fc_hz": fc,
            "candidate": candidate.to_dict(),
        }
    ).fingerprint
    return DelayCandidateConfirmation(
        scope=scope,
        topology_id=topology,
        crossover_fc_hz=fc,
        snapshot_fingerprint=snapshot.fingerprint,
        predecessor_fingerprint=snapshot.predecessor_fingerprint,
        predecessor_graph_fingerprint=snapshot.graph_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        readback_graph_fingerprint=_graph_fingerprint(readback),
        relative_delay_us=candidate.relative_delay_us,
        delay_target=candidate.delay_target,
        delay_filter=delay_filter,
        delay_us=candidate.delay_us,
        effective_delay_us=expected_delay_ms * 1000.0,
    )
