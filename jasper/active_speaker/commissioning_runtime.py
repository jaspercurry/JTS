# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded live-DSP transaction for admitted summed commissioning captures.

The evidence host owns runs, attempts, artifacts, playback, capture, and
lifecycle progress.  This adapter owns the smaller hardware-facing boundary:
one writer-lock transaction which applies a server-derived summed graph, keeps
it live through the supplied admitted capture callback, and restores the exact
entry graph and listening volume before releasing the lock.

No scheduler or second safety-profile model lives here.  The optional pure
``prepare_summed_excitation`` helper only intersects two current adjacent
driver targets into Shared's existing excitation admission values.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeAlias, TypeVar, cast

import yaml

from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
from jasper.audio_measurement.delay_graph import (
    DelayCandidateConfirmation,
    DelayGraphSnapshot,
    DelayLaneBinding,
    confirm_delay_candidate,
)
from jasper.audio_measurement.evidence_identity import (
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
)
from jasper.audio_measurement.excitation_artifacts import (
    GenerationAdmissionArtifact,
    PlaybackAdmissionArtifact,
)
from jasper.audio_measurement.null_walk import (
    MAX_DSP_DELAY_US,
    DelayCandidate,
    DelayWalkScope,
    DspPredecessor,
    NullWalkError,
    NullWalkSpec,
)
from jasper.dsp_apply import DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S, dsp_writer_lock
from jasper.output_topology import OutputTopology

from .baseline_profile import topology_config_fingerprint
from .camilla_yaml import STARTUP_MUTE_GAIN_DB
from .driver_safety import evaluate_driver_safety_profile
from .graph_evidence import (
    driver_baseline_gain_name,
    driver_delay_name,
    output_commission_mute_name,
)
from .measurement import active_driver_targets
from .profile import ADJACENT_PAIRS_BY_WAY
from .runtime_contract import classify_camilla_graph
from .test_signal_plan import (
    MAX_DRIVER_TEST_FREQUENCY_HZ,
    MIN_DRIVER_TEST_FREQUENCY_HZ,
    SUMMED_SWEEP_DURATION_S,
)

DEFAULT_SUMMED_RUNTIME_LOCK_TIMEOUT_S = DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S

T = TypeVar("T")
SummedGraphKind: TypeAlias = Literal["normal", "reverse", "delay"]

ReadActiveRaw = Callable[[], Awaitable[str | None]]
ApplyActiveRaw = Callable[[str], Awaitable[bool]]
ReadConfigPath = Callable[[], Awaitable[str | None]]
ReadListeningVolume = Callable[[], Awaitable[float | None]]
SetListeningVolume = Callable[[float], Awaitable[bool]]
RecordMutationIntent = Callable[[ExactDspStateIdentity], Awaitable[None]]


class CommissioningRuntimeError(ValueError):
    """A runtime request or one live observation is malformed."""


@dataclass(frozen=True)
class CommissioningRuntimePort:
    """Injected CamillaController-like side-effect seams."""

    read_active_raw: ReadActiveRaw
    apply_active_raw: ApplyActiveRaw
    read_config_path: ReadConfigPath
    read_listening_volume_db: ReadListeningVolume
    set_listening_volume_db: SetListeningVolume

    def __post_init__(self) -> None:
        for name in (
            "read_active_raw",
            "apply_active_raw",
            "read_config_path",
            "read_listening_volume_db",
            "set_listening_volume_db",
        ):
            if not callable(getattr(self, name)):
                raise CommissioningRuntimeError(f"{name} must be callable")


def _role(value: Any, *, field: str) -> str:
    role = value.strip().lower() if isinstance(value, str) else ""
    if not role:
        raise CommissioningRuntimeError(f"{field} must be a non-empty role")
    return role


def _channels(value: Any, *, field: str) -> tuple[int, ...]:
    if type(value) is not tuple or not value:
        raise CommissioningRuntimeError(f"{field} must be a non-empty tuple")
    if any(type(channel) is not int or channel < 0 for channel in value):
        raise CommissioningRuntimeError(
            f"{field} must contain non-negative integers"
        )
    if len(set(value)) != len(value):
        raise CommissioningRuntimeError(f"{field} must not contain duplicates")
    return tuple(sorted(value))


def _volume(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommissioningRuntimeError(f"{field} must be finite and non-positive")
    volume = float(value)
    if not math.isfinite(volume) or volume > 0.0:
        raise CommissioningRuntimeError(f"{field} must be finite and non-positive")
    return 0.0 if volume == 0.0 else volume


def _parse_active_raw(value: str | None, *, field: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise CommissioningRuntimeError(f"{field} must be non-empty YAML")
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise CommissioningRuntimeError(f"{field} must be parseable YAML") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise CommissioningRuntimeError(f"{field} must be a non-empty object")
    try:
        return NormalizedActiveRawIdentity(parsed).normalized_active_raw
    except ValueError as exc:
        raise CommissioningRuntimeError(
            f"{field} must contain exact JSON-domain values"
        ) from exc


@dataclass(frozen=True)
class SummedGraphRequest:
    """One exact adjacent-region graph audition requested by the evidence host.

    ``normal_active_raw`` is emitted/composed by the Active host.  This adapter
    never invents a second graph emitter: it adds only canonical per-output
    isolation mutes, plus one target-scoped zero-gain inversion lane for reverse
    or two target-scoped bounded relative-delay lanes for delay.
    """

    kind: SummedGraphKind
    normal_active_raw: str
    lower_role: str
    upper_role: str
    lower_channels: tuple[int, ...]
    upper_channels: tuple[int, ...]
    listening_volume_db: float
    topology_id: str
    topology_fingerprint: str
    delay_spec: NullWalkSpec | None = None
    delay_candidate: DelayCandidate | None = None
    delay_scope: DelayWalkScope | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"normal", "reverse", "delay"}:
            raise CommissioningRuntimeError("kind must be normal, reverse, or delay")
        lower_role = _role(self.lower_role, field="lower_role")
        upper_role = _role(self.upper_role, field="upper_role")
        if lower_role == upper_role:
            raise CommissioningRuntimeError("summed region roles must differ")
        lower_channels = _channels(self.lower_channels, field="lower_channels")
        upper_channels = _channels(self.upper_channels, field="upper_channels")
        if set(lower_channels) & set(upper_channels):
            raise CommissioningRuntimeError("summed region channels must be disjoint")
        _parse_active_raw(self.normal_active_raw, field="normal_active_raw")
        volume = _volume(self.listening_volume_db, field="listening_volume_db")
        topology_id = (
            self.topology_id.strip() if isinstance(self.topology_id, str) else ""
        )
        if not topology_id:
            raise CommissioningRuntimeError("topology_id is required")
        topology_fingerprint = self.topology_fingerprint
        if (
            not isinstance(topology_fingerprint, str)
            or len(topology_fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in topology_fingerprint)
        ):
            raise CommissioningRuntimeError(
                "topology_fingerprint must be a lowercase SHA-256"
            )
        if self.kind == "delay":
            if not isinstance(self.delay_spec, NullWalkSpec):
                raise CommissioningRuntimeError("delay_spec is required for delay")
            if not isinstance(self.delay_candidate, DelayCandidate):
                raise CommissioningRuntimeError(
                    "delay_candidate is required for delay"
                )
            if self.delay_scope != "active_crossover":
                raise CommissioningRuntimeError(
                    "summed delay scope must be active_crossover"
                )
            if {
                self.delay_spec.positive_delay_target,
                self.delay_spec.negative_delay_target,
            } != {lower_role, upper_role}:
                raise CommissioningRuntimeError(
                    "delay targets must be the exact adjacent roles"
                )
            expected_candidate = self.delay_spec.dsp_candidate(
                self.delay_candidate.relative_delay_us
            )
            if self.delay_candidate != expected_candidate:
                raise CommissioningRuntimeError(
                    "delay_candidate must be the exact bound spec candidate"
                )
        elif any(
            value is not None
            for value in (
                self.delay_spec,
                self.delay_candidate,
                self.delay_scope,
            )
        ):
            raise CommissioningRuntimeError(
                "delay-only fields must be omitted for stationary graphs"
            )
        object.__setattr__(self, "lower_role", lower_role)
        object.__setattr__(self, "upper_role", upper_role)
        object.__setattr__(self, "lower_channels", lower_channels)
        object.__setattr__(self, "upper_channels", upper_channels)
        object.__setattr__(self, "listening_volume_db", volume)
        object.__setattr__(self, "topology_id", topology_id)
        object.__setattr__(self, "topology_fingerprint", topology_fingerprint)


@dataclass(frozen=True)
class CommissioningFreshReadback:
    """One immutable, fresh observation of the still-live candidate."""

    graph: NormalizedActiveRawIdentity
    active_raw: str
    config_path: str
    listening_volume_db: float
    delay_confirmation: DelayCandidateConfirmation | None


FreshCommissioningReadback: TypeAlias = Callable[
    [], Awaitable[CommissioningFreshReadback]
]


@dataclass(frozen=True)
class CommissioningLiveContext:
    """Candidate plus a read-only fresh-observation seam under the writer lock."""

    graph: NormalizedActiveRawIdentity
    active_raw: str
    config_path: str
    listening_volume_db: float
    delay_confirmation: DelayCandidateConfirmation | None
    fresh_readback: FreshCommissioningReadback

    def __post_init__(self) -> None:
        if not callable(self.fresh_readback):
            raise CommissioningRuntimeError("fresh_readback must be callable")


@dataclass(frozen=True)
class AdmittedCaptureCallbackResult(Generic[T]):
    """Feature-owned admitted playback/capture outcome returned under the lock."""

    generation: GenerationAdmissionArtifact
    playback: PlaybackAdmissionArtifact
    stimulus: GeneratedExcitationWav
    protection_evidence: ProtectionEvidence
    payload: T

    def __post_init__(self) -> None:
        if not isinstance(self.generation, GenerationAdmissionArtifact):
            raise CommissioningRuntimeError(
                "generation must be a GenerationAdmissionArtifact"
            )
        if not isinstance(self.playback, PlaybackAdmissionArtifact):
            raise CommissioningRuntimeError(
                "playback must be a PlaybackAdmissionArtifact"
            )
        if not isinstance(self.stimulus, GeneratedExcitationWav):
            raise CommissioningRuntimeError("stimulus must be a GeneratedExcitationWav")
        if not isinstance(self.protection_evidence, ProtectionEvidence):
            raise CommissioningRuntimeError(
                "protection_evidence must be ProtectionEvidence"
            )
        if self.playback.generation != self.generation:
            raise CommissioningRuntimeError(
                "playback must retain the exact generation artifact"
            )
        if not self.playback.admission.allowed:
            raise CommissioningRuntimeError("playback admission must be allowed")
        if self.playback.admission.protection_evidence != self.protection_evidence:
            raise CommissioningRuntimeError(
                "playback admission must retain the supplied protection evidence"
            )
        if not self.protection_evidence.current:
            raise CommissioningRuntimeError("protection evidence must be current")
        if (
            self.stimulus.generation_artifact_fingerprint
            != self.generation.artifact.fingerprint
        ):
            raise CommissioningRuntimeError(
                "stimulus must retain the exact generation artifact"
            )
        if (
            self.stimulus.excitation_plan_fingerprint
            != self.generation.admission.request.excitation_plan_fingerprint
        ):
            raise CommissioningRuntimeError(
                "stimulus must retain the exact excitation plan"
            )

    @property
    def admission_id(self) -> str:
        return self.generation.admission_id


CaptureCallback: TypeAlias = Callable[
    [CommissioningLiveContext], Awaitable[AdmittedCaptureCallbackResult[T]]
]


@dataclass(frozen=True)
class RestoreObservation:
    graph: NormalizedActiveRawIdentity
    config_path: str
    listening_volume_db: float


@dataclass(frozen=True)
class SummedRecoveryResult:
    observation: RestoreObservation
    cancelled: bool


RecordMutationRestored = Callable[[RestoreObservation], Awaitable[None]]


@dataclass(frozen=True)
class CommissioningMutationJournal:
    """Host-owned durability callbacks around the runtime's live mutation."""

    record_intent: RecordMutationIntent
    record_restored: RecordMutationRestored

    def __post_init__(self) -> None:
        if not callable(self.record_intent) or not callable(self.record_restored):
            raise CommissioningRuntimeError(
                "mutation journal callbacks must be callable"
            )


@dataclass(frozen=True)
class SummedCaptureRuntimeResult(Generic[T]):
    """One admitted result returned only after exact predecessor restoration."""

    predecessor: ExactDspStateIdentity
    candidate_graph: NormalizedActiveRawIdentity
    delay_confirmation: DelayCandidateConfirmation | None
    capture: AdmittedCaptureCallbackResult[T]
    restore: RestoreObservation

    @property
    def graph_fingerprint(self) -> str:
        return self.candidate_graph.active_raw_fingerprint

    @property
    def admission_id(self) -> str:
        return self.capture.admission_id


@dataclass(frozen=True)
class RuntimeSideEffectState:
    graph_may_have_mutated: bool
    audio_may_have_emitted: bool
    restore_attempted: bool
    restore_succeeded: bool | None


class CommissioningRuntimeFailure(RuntimeError):
    """A live transaction failed with an honest side-effect classification."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        side_effects: RuntimeSideEffectState,
        cancelled: bool = False,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.side_effects = side_effects
        self.cancelled = cancelled


class CommissioningRuntimeCancelled(asyncio.CancelledError):
    """Cancellation reported only after the protected transaction terminates."""

    def __init__(
        self,
        *,
        side_effects: RuntimeSideEffectState,
        completed_result: SummedCaptureRuntimeResult[Any] | None = None,
    ) -> None:
        super().__init__("summed commissioning transaction was cancelled")
        self.side_effects = side_effects
        self.completed_result = completed_result


class _OperationFailure(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _Predecessor:
    raw: str
    graph: NormalizedActiveRawIdentity
    path: str
    volume_db: float
    exact: ExactDspStateIdentity


@dataclass(frozen=True)
class _SummedTopologyBinding:
    group_id: str
    lower_all_channels: tuple[int, ...]
    upper_all_channels: tuple[int, ...]
    all_role_channels: tuple[tuple[str, tuple[int, ...]], ...]
    output_channels: tuple[int, ...]


@dataclass(frozen=True)
class _ScopedDelayLane:
    delay_name: str
    identity_name: str
    offset_name: str


@dataclass
class _SideEffectTracker:
    graph_apply_attempted: bool = False


def _active_identity(raw: str | None, *, field: str) -> NormalizedActiveRawIdentity:
    return NormalizedActiveRawIdentity(_parse_active_raw(raw, field=field))


def _topology_binding(
    request: SummedGraphRequest,
    topology: OutputTopology,
) -> _SummedTopologyBinding:
    def role_channels(groups: list[Any], role: str) -> tuple[int, ...]:
        values = [
            channel.physical_output_index
            for group in groups
            for channel in group.channels
            if channel.role == role
        ]
        if not values or any(type(value) is not int or value < 0 for value in values):
            raise CommissioningRuntimeError(
                "summed topology roles must resolve to physical output channels"
            )
        return tuple(sorted(cast(list[int], values)))

    active_groups = [
        group
        for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"}
    ]
    matches: list[Any] = []
    for group in active_groups:
        way_count = 2 if group.mode == "active_2_way" else 3
        if (request.lower_role, request.upper_role) not in ADJACENT_PAIRS_BY_WAY[
            way_count
        ]:
            continue
        lower = role_channels([group], request.lower_role)
        upper = role_channels([group], request.upper_role)
        if lower == request.lower_channels and upper == request.upper_channels:
            matches.append(group)
    if len(matches) != 1:
        raise CommissioningRuntimeError(
            "summed roles and channels must bind one exact current adjacent region"
        )
    group = matches[0]
    target_channels = request.lower_channels + request.upper_channels
    if len(target_channels) != 2:
        raise CommissioningRuntimeError(
            "summed region must bind exactly two adjacent physical channels"
        )
    lower_all = role_channels(active_groups, request.lower_role)
    upper_all = role_channels(active_groups, request.upper_role)
    all_roles = sorted(
        {channel.role for item in active_groups for channel in item.channels}
    )
    all_role_channels = tuple(
        (role, role_channels(active_groups, role)) for role in all_roles
    )
    assigned_outputs = [
        channel.physical_output_index
        for item in topology.speaker_groups
        for channel in item.channels
    ]
    if (
        not assigned_outputs
        or any(type(value) is not int or value < 0 for value in assigned_outputs)
        or len(set(assigned_outputs)) != len(assigned_outputs)
    ):
        raise CommissioningRuntimeError(
            "summed topology must have unique physical output assignments"
        )
    assigned = cast(list[int], assigned_outputs)
    output_channels = tuple(range(max(assigned) + 1))
    return _SummedTopologyBinding(
        group.id,
        lower_all,
        upper_all,
        all_role_channels,
        output_channels,
    )


async def _snapshot(port: CommissioningRuntimePort) -> _Predecessor:
    raw = await port.read_active_raw()
    graph = _active_identity(raw, field="predecessor active_raw")
    path = await port.read_config_path()
    if not isinstance(path, str) or not path.strip() or path != path.strip():
        raise _OperationFailure(
            "snapshot_invalid", "predecessor config path is unavailable"
        )
    volume = _volume(
        await port.read_listening_volume_db(),
        field="predecessor listening volume",
    )
    assert isinstance(raw, str)
    exact = ExactDspStateIdentity(
        {
            "active_raw": raw,
            "normalized_active_raw": graph.normalized_active_raw,
            "config_path": path,
            "listening_volume_db": volume,
        }
    )
    return _Predecessor(raw, graph, path, volume, exact)


def _filter_params(
    graph: Mapping[str, Any],
    name: str,
    *,
    filter_type: str,
) -> dict[str, Any]:
    filters = graph.get("filters")
    definition = filters.get(name) if isinstance(filters, Mapping) else None
    params = definition.get("parameters") if isinstance(definition, Mapping) else None
    if (
        not isinstance(definition, Mapping)
        or definition.get("type") != filter_type
        or not isinstance(params, Mapping)
    ):
        raise CommissioningRuntimeError(
            f"server-derived graph has no {filter_type} filter {name!r}"
        )
    return cast(dict[str, Any], params)


def _filter_channels(graph: Mapping[str, Any], name: str) -> tuple[int, ...]:
    pipeline = graph.get("pipeline")
    placements: list[tuple[int, ...]] = []
    if isinstance(pipeline, list):
        for step in pipeline:
            if not isinstance(step, Mapping) or step.get("type") != "Filter":
                continue
            names = step.get("names")
            channels = step.get("channels")
            if not isinstance(names, list) or not isinstance(channels, list):
                continue
            placements.extend(
                tuple(sorted(channels)) for item in names if item == name
            )
    if len(placements) != 1:
        raise CommissioningRuntimeError(
            f"filter {name!r} must occur in exactly one pipeline step"
        )
    return placements[0]


def _normal_graph(
    request: SummedGraphRequest,
    binding: _SummedTopologyBinding,
) -> dict[str, Any]:
    graph = _parse_active_raw(request.normal_active_raw, field="normal_active_raw")
    devices = graph.get("devices")
    volume_limit = devices.get("volume_limit") if isinstance(devices, Mapping) else None
    if (
        not isinstance(devices, dict)
        or isinstance(volume_limit, bool)
        or not isinstance(volume_limit, (int, float))
        or not math.isfinite(float(volume_limit))
        or float(volume_limit) > 0.0
    ):
        raise CommissioningRuntimeError(
            "server-derived graph must retain a non-positive volume ceiling"
        )
    devices["volume_limit"] = min(float(volume_limit), request.listening_volume_db)
    filters = graph.get("filters")
    if isinstance(filters, Mapping) and any(
        isinstance(name, str)
        and (
            name.startswith("as_commission_")
            or (
                name.startswith("as_out")
                and name.endswith("_commission_mute")
            )
        )
        for name in filters
    ):
        raise CommissioningRuntimeError(
            "server-derived normal graph must not predeclare runtime lanes or mutes"
        )
    maximum_delay_ms = MAX_DSP_DELAY_US / 1000.0
    for role, all_channels in (
        (request.lower_role, binding.lower_all_channels),
        (request.upper_role, binding.upper_all_channels),
    ):
        gain_name = driver_baseline_gain_name(role)
        gain = _filter_params(graph, gain_name, filter_type="Gain")
        if type(gain.get("inverted")) is not bool:
            raise CommissioningRuntimeError(
                f"baseline gain {gain_name!r} has no exact inversion flag"
            )
        if _filter_channels(graph, gain_name) != all_channels:
            raise CommissioningRuntimeError(
                f"baseline gain {gain_name!r} is not on all current role channels"
            )
    delay_by_role: dict[str, float] = {}
    for role, all_channels in binding.all_role_channels:
        delay_name = driver_delay_name(role)
        delay = _filter_params(graph, delay_name, filter_type="Delay")
        delay_value = delay.get("delay")
        if (
            isinstance(delay_value, bool)
            or not isinstance(delay_value, (int, float))
            or not math.isfinite(float(delay_value))
            or delay.get("unit") != "ms"
            or float(delay_value) < 0.0
            or float(delay_value) > maximum_delay_ms
        ):
            raise CommissioningRuntimeError(
                f"driver delay {delay_name!r} must be within 0-{maximum_delay_ms:g} ms"
            )
        if _filter_channels(graph, delay_name) != all_channels:
            raise CommissioningRuntimeError(
                f"driver delay {delay_name!r} is not on all current role channels"
            )
        delay_by_role[role] = float(delay_value)
    if request.kind == "delay":
        assert request.delay_spec is not None
        try:
            maximum_candidate_us = max(
                abs(
                    request.delay_spec.fine_grid_coordinate(
                        request.delay_spec.fine_grid_index_min
                    )
                ),
                abs(
                    request.delay_spec.fine_grid_coordinate(
                        request.delay_spec.fine_grid_index_max
                    )
                ),
            )
        except NullWalkError as exc:
            raise CommissioningRuntimeError(
                "delay walk grid is outside the shared DSP bound"
            ) from exc
        baseline_common_ms = max(
            delay_by_role[request.lower_role],
            delay_by_role[request.upper_role],
        )
        if baseline_common_ms * 1000.0 + maximum_candidate_us > MAX_DSP_DELAY_US:
            raise CommissioningRuntimeError(
                "delay walk has no safe headroom above the emitter baseline"
            )
    return graph


def _stationary_candidate(
    request: SummedGraphRequest,
    normal: dict[str, Any],
    binding: _SummedTopologyBinding,
) -> dict[str, Any]:
    candidate = copy.deepcopy(normal)
    if request.kind == "reverse":
        _append_scoped_lane(
            request,
            binding,
            candidate,
            role=request.upper_role,
            channels=request.upper_channels,
            inverted=True,
        )
    _append_output_isolation(request, binding, candidate)
    return candidate


def _append_output_isolation(
    request: SummedGraphRequest,
    binding: _SummedTopologyBinding,
    graph: dict[str, Any],
) -> None:
    """Append the one canonical final mute tail derived from current topology."""

    filters = graph.get("filters")
    pipeline = graph.get("pipeline")
    if not isinstance(filters, dict) or not isinstance(pipeline, list):
        raise CommissioningRuntimeError(
            "server-derived graph has no mutable filters and pipeline"
        )
    audible = set(request.lower_channels) | set(request.upper_channels)
    if len(audible) != 2 or not audible <= set(binding.output_channels):
        raise CommissioningRuntimeError(
            "summed target must be two exact current physical outputs"
        )
    for channel in binding.output_channels:
        name = output_commission_mute_name(channel)
        if name in filters:
            raise CommissioningRuntimeError(
                "server-derived graph collides with commissioning output mutes"
            )
        is_audible = channel in audible
        filters[name] = {
            "type": "Gain",
            "parameters": {
                "gain": 0.0 if is_audible else STARTUP_MUTE_GAIN_DB,
                "inverted": False,
                "mute": not is_audible,
            },
        }
        pipeline.append(
            {"type": "Filter", "channels": [channel], "names": [name]}
        )


def _scoped_lane_names(
    request: SummedGraphRequest,
    binding: _SummedTopologyBinding,
    role: str,
) -> _ScopedDelayLane:
    token = json_fingerprint(
        {
            "topology_id": request.topology_id,
            "group_id": binding.group_id,
            "role": role,
        }
    )[:16]
    return _ScopedDelayLane(
        delay_name=f"as_commission_{token}_delay",
        identity_name=f"as_commission_{token}_identity",
        offset_name=f"as_commission_{token}_offset",
    )


def _append_scoped_lane(
    request: SummedGraphRequest,
    binding: _SummedTopologyBinding,
    graph: dict[str, Any],
    *,
    role: str,
    channels: tuple[int, ...],
    inverted: bool,
    offset_delay_ms: float = 0.0,
) -> _ScopedDelayLane:
    lane = _scoped_lane_names(request, binding, role)
    filters = graph.get("filters")
    pipeline = graph.get("pipeline")
    if not isinstance(filters, dict) or not isinstance(pipeline, list):
        raise CommissioningRuntimeError(
            "server-derived graph has no mutable filters and pipeline"
        )
    if any(
        name in filters
        for name in (lane.delay_name, lane.identity_name, lane.offset_name)
    ):
        raise CommissioningRuntimeError(
            "server-derived graph collides with commissioning lane names"
        )
    filters[lane.delay_name] = {
        "type": "Delay",
        "parameters": {"delay": 0.0, "unit": "ms"},
    }
    filters[lane.offset_name] = {
        "type": "Delay",
        "parameters": {"delay": offset_delay_ms, "unit": "ms"},
    }
    filters[lane.identity_name] = {
        "type": "Gain",
        "parameters": {"gain": 0.0, "inverted": inverted, "mute": False},
    }
    pipeline.append(
        {
            "type": "Filter",
            "channels": list(channels),
            "names": [lane.offset_name, lane.delay_name, lane.identity_name],
        }
    )
    return lane


def _zero_relative_graph(
    request: SummedGraphRequest,
    normal: dict[str, Any],
    binding: _SummedTopologyBinding,
) -> tuple[dict[str, Any], Mapping[str, _ScopedDelayLane]]:
    graph = copy.deepcopy(normal)
    lower_baseline_ms = float(
        _filter_params(
            graph,
            driver_delay_name(request.lower_role),
            filter_type="Delay",
        )["delay"]
    )
    upper_baseline_ms = float(
        _filter_params(
            graph,
            driver_delay_name(request.upper_role),
            filter_type="Delay",
        )["delay"]
    )
    common_baseline_ms = max(lower_baseline_ms, upper_baseline_ms)
    lanes = {
        request.lower_role: _append_scoped_lane(
            request,
            binding,
            graph,
            role=request.lower_role,
            channels=request.lower_channels,
            inverted=False,
            offset_delay_ms=common_baseline_ms - lower_baseline_ms,
        ),
        request.upper_role: _append_scoped_lane(
            request,
            binding,
            graph,
            role=request.upper_role,
            channels=request.upper_channels,
            inverted=True,
            offset_delay_ms=common_baseline_ms - upper_baseline_ms,
        ),
    }
    _append_output_isolation(request, binding, graph)
    return graph, lanes


def _source_header(raw: str) -> str:
    markers = [
        line
        for line in raw.splitlines()
        if line.startswith("# Source: ") and line == line.strip()
    ]
    if len(markers) != 1:
        raise CommissioningRuntimeError(
            "server-derived graph must retain one exact source marker"
        )
    return markers[0]


def _dump_graph(graph: Mapping[str, Any], *, source_header: str) -> str:
    return f"{source_header}\n{yaml.safe_dump(dict(graph), sort_keys=False)}"


async def _apply_graph(
    port: CommissioningRuntimePort,
    graph: Mapping[str, Any],
    topology: OutputTopology,
    tracker: _SideEffectTracker,
    *,
    source_header: str,
    expected_path: str,
    expected_volume_db: float,
    set_volume: bool,
) -> tuple[str, NormalizedActiveRawIdentity, float]:
    if set_volume and not await port.set_listening_volume_db(expected_volume_db):
        raise _OperationFailure(
            "volume_apply_failed", "CamillaDSP rejected the locked listening volume"
        )
    pre_apply_volume = _volume(
        await port.read_listening_volume_db(),
        field="pre-apply listening volume",
    )
    if not math.isclose(
        pre_apply_volume,
        expected_volume_db,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise _OperationFailure(
            "volume_readback_mismatch",
            "fresh listening-volume readback was not safe before graph apply",
        )
    tracker.graph_apply_attempted = True
    if not await port.apply_active_raw(
        _dump_graph(graph, source_header=source_header)
    ):
        raise _OperationFailure(
            "graph_apply_failed", "CamillaDSP rejected the summed candidate graph"
        )
    raw = await port.read_active_raw()
    observed = _active_identity(raw, field="candidate active_raw readback")
    expected = NormalizedActiveRawIdentity(graph)
    if observed.active_raw_fingerprint != expected.active_raw_fingerprint:
        raise _OperationFailure(
            "graph_readback_mismatch", "fresh active_raw did not equal the candidate"
        )
    safety = classify_camilla_graph(topology=topology, text=raw)
    if not safety.allowed:
        issue_codes = ",".join(
            str(issue.get("code") or "unknown") for issue in safety.issues
        )
        raise _OperationFailure(
            "graph_readback_unsafe",
            f"fresh candidate graph failed the current topology contract: {issue_codes}",
        )
    path = await port.read_config_path()
    if path != expected_path:
        raise _OperationFailure(
            "config_path_drift", "summed graph apply changed the persisted config path"
        )
    volume = _volume(
        await port.read_listening_volume_db(), field="candidate listening volume"
    )
    if not math.isclose(volume, expected_volume_db, rel_tol=0.0, abs_tol=1e-6):
        raise _OperationFailure(
            "volume_readback_mismatch",
            "fresh listening-volume readback did not equal the locked value",
        )
    assert isinstance(raw, str)
    return raw, observed, volume


async def _confirm_candidate_still_live(
    port: CommissioningRuntimePort,
    candidate: NormalizedActiveRawIdentity,
    *,
    expected_path: str,
    expected_volume_db: float,
    delay_confirmation: DelayCandidateConfirmation | None,
) -> CommissioningFreshReadback:
    raw = await port.read_active_raw()
    observed = _active_identity(
        raw,
        field="post-capture active_raw readback",
    )
    if observed.active_raw_fingerprint != candidate.active_raw_fingerprint:
        raise _OperationFailure(
            "post_capture_graph_drift",
            "active_raw changed while the admitted capture callback was running",
        )
    path = await port.read_config_path()
    if path != expected_path:
        raise _OperationFailure(
            "post_capture_config_path_drift",
            "config path changed while the admitted capture callback was running",
        )
    volume = _volume(
        await port.read_listening_volume_db(),
        field="post-capture listening volume",
    )
    if not math.isclose(volume, expected_volume_db, rel_tol=0.0, abs_tol=1e-6):
        raise _OperationFailure(
            "post_capture_volume_drift",
            "listening volume changed while the admitted capture callback was running",
        )
    assert isinstance(raw, str)
    assert isinstance(path, str)
    return CommissioningFreshReadback(
        graph=observed,
        active_raw=raw,
        config_path=path,
        listening_volume_db=volume,
        delay_confirmation=delay_confirmation,
    )


def _delay_snapshot(
    request: SummedGraphRequest,
    zero_graph: Mapping[str, Any],
    lanes: Mapping[str, _ScopedDelayLane],
) -> DelayGraphSnapshot:
    assert request.delay_spec is not None
    assert request.delay_scope is not None
    assert request.topology_id is not None
    bindings = {
        request.lower_role: DelayLaneBinding(
            target=request.lower_role,
            filter_name=lanes[request.lower_role].delay_name,
            identity_filter_name=lanes[request.lower_role].identity_name,
            channels=request.lower_channels,
        ),
        request.upper_role: DelayLaneBinding(
            target=request.upper_role,
            filter_name=lanes[request.upper_role].delay_name,
            identity_filter_name=lanes[request.upper_role].identity_name,
            channels=request.upper_channels,
        ),
    }
    return DelayGraphSnapshot(
        request.delay_spec,
        scope=request.delay_scope,
        topology_id=request.topology_id,
        positive_lane=bindings[request.delay_spec.positive_delay_target],
        negative_lane=bindings[request.delay_spec.negative_delay_target],
        predecessor=DspPredecessor({"active_raw": zero_graph}),
    )


def _delay_candidate_graph(
    snapshot: DelayGraphSnapshot, candidate: DelayCandidate
) -> dict[str, Any]:
    graph = snapshot.graph
    binding = None
    if candidate.delay_target == snapshot.positive_lane.target:
        binding = snapshot.positive_lane
    elif candidate.delay_target == snapshot.negative_lane.target:
        binding = snapshot.negative_lane
    if binding is not None:
        params = _filter_params(graph, binding.filter_name, filter_type="Delay")
        params["delay"] = float(f"{candidate.delay_us / 1000.0:.4f}")
    return graph


@dataclass(frozen=True)
class _RestoreResult:
    observation: RestoreObservation | None
    error: str | None


@dataclass(frozen=True)
class _AwaitOutcome(Generic[T]):
    value: T | None
    error: BaseException | None


async def _capture_awaitable(awaitable: Awaitable[T]) -> _AwaitOutcome[T]:
    """Capture one arbitrary adapter exit at the explicit transaction edge."""

    try:
        return _AwaitOutcome(await awaitable, None)
    except BaseException as exc:  # noqa: BLE001 - includes async cancellation
        return _AwaitOutcome(None, exc)


async def _restore(
    port: CommissioningRuntimePort, predecessor: _Predecessor
) -> _RestoreResult:
    issues: list[str] = []

    graph_apply = await _capture_awaitable(port.apply_active_raw(predecessor.raw))
    if graph_apply.error is not None:
        issues.append(
            f"predecessor graph apply raised {type(graph_apply.error).__name__}"
        )
    elif not graph_apply.value:
        issues.append("predecessor graph apply was rejected")

    raw: str | None = None
    path: str | None = None
    volume: float | None = None
    graph: NormalizedActiveRawIdentity | None = None

    async def _read_graph() -> tuple[str | None, NormalizedActiveRawIdentity]:
        observed_raw = await port.read_active_raw()
        return observed_raw, _active_identity(
            observed_raw,
            field="restored active_raw readback",
        )

    graph_read = await _capture_awaitable(_read_graph())
    if graph_read.error is not None:
        issues.append(
            f"restored graph readback raised {type(graph_read.error).__name__}"
        )
    else:
        assert graph_read.value is not None
        raw, graph = graph_read.value
        if graph.active_raw_fingerprint != predecessor.graph.active_raw_fingerprint:
            issues.append("restored graph readback mismatch")

    path_read = await _capture_awaitable(port.read_config_path())
    if path_read.error is not None:
        issues.append(
            f"restored config path readback raised {type(path_read.error).__name__}"
        )
    else:
        path = path_read.value
        if path != predecessor.path:
            issues.append("restored config path readback mismatch")

    graph_and_path_restored = bool(
        graph is not None
        and graph.active_raw_fingerprint == predecessor.graph.active_raw_fingerprint
        and path == predecessor.path
    )
    if not graph_and_path_restored:
        return _RestoreResult(
            None,
            "; ".join(issues) or "predecessor graph/path restoration was not proved",
        )

    volume_apply = await _capture_awaitable(
        port.set_listening_volume_db(predecessor.volume_db)
    )
    if volume_apply.error is not None:
        issues.append(
            f"predecessor volume apply raised {type(volume_apply.error).__name__}"
        )
    elif not volume_apply.value:
        issues.append("predecessor volume apply was rejected")

    async def _read_volume() -> float:
        return _volume(
            await port.read_listening_volume_db(),
            field="restored listening volume",
        )

    volume_read = await _capture_awaitable(_read_volume())
    if volume_read.error is not None:
        issues.append(
            f"restored volume readback raised {type(volume_read.error).__name__}"
        )
    else:
        volume = volume_read.value
        assert volume is not None
        if not math.isclose(
            volume, predecessor.volume_db, rel_tol=0.0, abs_tol=1e-6
        ):
            issues.append("restored listening-volume readback mismatch")
    if issues or graph is None or path is None or volume is None:
        return _RestoreResult(None, "; ".join(issues) or "restore readback failed")
    return _RestoreResult(RestoreObservation(graph, path, volume), None)


def _predecessor_from_identity(identity: ExactDspStateIdentity) -> _Predecessor:
    if not isinstance(identity, ExactDspStateIdentity):
        raise CommissioningRuntimeError(
            "predecessor must be ExactDspStateIdentity"
        )
    state = identity.state
    raw = state.get("active_raw")
    path = state.get("config_path")
    graph = _active_identity(
        raw if isinstance(raw, str) else None,
        field="recovery predecessor active_raw",
    )
    if state.get("normalized_active_raw") != graph.normalized_active_raw:
        raise CommissioningRuntimeError(
            "recovery predecessor normalized graph does not equal exact raw"
        )
    if not isinstance(path, str) or not path.strip() or path != path.strip():
        raise CommissioningRuntimeError(
            "recovery predecessor config path is unavailable"
        )
    volume = _volume(
        state.get("listening_volume_db"),
        field="recovery predecessor listening volume",
    )
    assert isinstance(raw, str)
    return _Predecessor(raw, graph, path, volume, identity)


async def recover_summed_predecessor(
    port: CommissioningRuntimePort,
    predecessor: ExactDspStateIdentity,
    *,
    config_dir: str | Path,
    lock_timeout_s: float = DEFAULT_SUMMED_RUNTIME_LOCK_TIMEOUT_S,
) -> SummedRecoveryResult:
    """Exactly restore a durable predecessor before any new operation issues."""

    if not isinstance(port, CommissioningRuntimePort):
        raise CommissioningRuntimeError("port must be CommissioningRuntimePort")
    if (
        isinstance(lock_timeout_s, bool)
        or not isinstance(lock_timeout_s, (int, float))
        or not math.isfinite(float(lock_timeout_s))
        or float(lock_timeout_s) <= 0.0
    ):
        raise CommissioningRuntimeError("lock_timeout_s must be finite and positive")
    exact = _predecessor_from_identity(predecessor)
    entered = False
    try:
        async with dsp_writer_lock(
            config_dir,
            source="active_speaker_summed_recovery",
            timeout_s=float(lock_timeout_s),
        ):
            entered = True
            task = asyncio.create_task(_restore(port, exact))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
            side_effects = RuntimeSideEffectState(True, False, True, result.error is None)
            if result.error is not None or result.observation is None:
                raise CommissioningRuntimeFailure(
                    "restore_failed",
                    result.error or "exact recovery readback failed",
                    side_effects=side_effects,
                    cancelled=cancelled,
                )
            return SummedRecoveryResult(result.observation, cancelled)
    except asyncio.CancelledError as exc:
        if entered or isinstance(exc, CommissioningRuntimeCancelled):
            raise
        raise CommissioningRuntimeCancelled(
            side_effects=RuntimeSideEffectState(False, False, False, None)
        ) from exc


async def _run_locked(
    port: CommissioningRuntimePort,
    request: SummedGraphRequest,
    topology: OutputTopology,
    binding: _SummedTopologyBinding,
    mutation_journal: CommissioningMutationJournal,
    capture_callback: CaptureCallback[T],
) -> SummedCaptureRuntimeResult[T]:
    mutation_attempted = False
    callback_started = False
    predecessor: _Predecessor | None = None
    capture: AdmittedCaptureCallbackResult[T] | None = None
    candidate_identity: NormalizedActiveRawIdentity | None = None
    delay_confirmation: DelayCandidateConfirmation | None = None
    intent_recorded = False
    tracker = _SideEffectTracker()

    async def _execute_live_transaction() -> None:
        nonlocal callback_started
        nonlocal candidate_identity
        nonlocal capture
        nonlocal delay_confirmation
        nonlocal intent_recorded
        nonlocal mutation_attempted
        nonlocal predecessor

        predecessor = await _snapshot(port)
        try:
            normal = _normal_graph(request, binding)
        except CommissioningRuntimeError as exc:
            raise _OperationFailure("normal_graph_invalid", str(exc)) from exc
        safety = classify_camilla_graph(
            topology=topology,
            text=request.normal_active_raw,
        )
        if not safety.allowed:
            issue_codes = ",".join(
                str(issue.get("code") or "unknown") for issue in safety.issues
            )
            raise _OperationFailure(
                "unsafe_normal_graph",
                f"server-derived normal graph failed the current topology contract: {issue_codes}",
            )
        try:
            source_header = _source_header(request.normal_active_raw)
        except CommissioningRuntimeError as exc:
            raise _OperationFailure("normal_graph_invalid", str(exc)) from exc
        intent = await _capture_awaitable(
            mutation_journal.record_intent(predecessor.exact)
        )
        if intent.error is not None:
            if not isinstance(intent.error, (Exception, asyncio.CancelledError)):
                raise intent.error
            raise _OperationFailure(
                "mutation_intent_failed",
                "could not durably record the exact predecessor: "
                f"{type(intent.error).__name__}",
            ) from intent.error
        intent_recorded = True
        # From this point an exact restore is required even if cancellation
        # arrives before the first candidate apply begins.
        mutation_attempted = True
        if request.kind == "delay":
            zero_graph, lanes = _zero_relative_graph(request, normal, binding)
            zero_raw, zero_readback, zero_volume = await _apply_graph(
                port,
                zero_graph,
                topology,
                tracker,
                source_header=source_header,
                expected_path=predecessor.path,
                expected_volume_db=request.listening_volume_db,
                set_volume=True,
            )
            snapshot = _delay_snapshot(
                request,
                zero_readback.normalized_active_raw,
                lanes,
            )
            assert request.delay_spec is not None
            assert request.delay_candidate is not None
            candidate_graph = _delay_candidate_graph(
                snapshot, request.delay_candidate
            )
            if request.delay_candidate.delay_target is None:
                candidate_raw = zero_raw
                candidate_identity = zero_readback
                candidate_volume = zero_volume
            else:
                candidate_raw, candidate_identity, candidate_volume = await _apply_graph(
                    port,
                    candidate_graph,
                    topology,
                    tracker,
                    source_header=source_header,
                    expected_path=predecessor.path,
                    expected_volume_db=request.listening_volume_db,
                    set_volume=False,
                )
            delay_confirmation = confirm_delay_candidate(
                snapshot,
                request.delay_candidate,
                candidate_identity.normalized_active_raw,
                expected_snapshot_fingerprint=snapshot.fingerprint,
                expected_scope="active_crossover",
                expected_topology_id=cast(str, request.topology_id),
                expected_crossover_fc_hz=request.delay_spec.crossover_fc_hz,
            )
        else:
            candidate_graph = _stationary_candidate(request, normal, binding)
            candidate_raw, candidate_identity, candidate_volume = await _apply_graph(
                port,
                candidate_graph,
                topology,
                tracker,
                source_header=source_header,
                expected_path=predecessor.path,
                expected_volume_db=request.listening_volume_db,
                set_volume=True,
            )
        readback_open = True

        async def fresh_readback() -> CommissioningFreshReadback:
            if not readback_open:
                raise CommissioningRuntimeError(
                    "fresh_readback is available only during the live callback"
                )
            assert candidate_identity is not None
            observation = await _confirm_candidate_still_live(
                port,
                candidate_identity,
                expected_path=predecessor.path,
                expected_volume_db=request.listening_volume_db,
                delay_confirmation=delay_confirmation,
            )
            if not readback_open:
                raise CommissioningRuntimeError(
                    "fresh_readback callback ended before observation completed"
                )
            return observation

        callback_started = True
        try:
            capture = await capture_callback(
                CommissioningLiveContext(
                    graph=candidate_identity,
                    active_raw=candidate_raw,
                    config_path=predecessor.path,
                    listening_volume_db=candidate_volume,
                    delay_confirmation=delay_confirmation,
                    fresh_readback=fresh_readback,
                )
            )
        finally:
            readback_open = False
        if not isinstance(capture, AdmittedCaptureCallbackResult):
            raise _OperationFailure(
                "capture_result_invalid",
                "capture callback did not return admitted capture evidence",
            )
        assert candidate_identity is not None
        await _confirm_candidate_still_live(
            port,
            candidate_identity,
            expected_path=predecessor.path,
            expected_volume_db=request.listening_volume_db,
            delay_confirmation=delay_confirmation,
        )

    transaction = await _capture_awaitable(_execute_live_transaction())
    primary = transaction.error

    restore = (
        await _restore(port, predecessor)
        if mutation_attempted and predecessor is not None
        else _RestoreResult(None, None)
    )
    side_effects = RuntimeSideEffectState(
        graph_may_have_mutated=tracker.graph_apply_attempted,
        audio_may_have_emitted=tracker.graph_apply_attempted or callback_started,
        restore_attempted=mutation_attempted,
        restore_succeeded=(restore.error is None if mutation_attempted else None),
    )
    restoration_record_error: BaseException | None = None
    if intent_recorded and restore.error is None and restore.observation is not None:
        restoration_record = await _capture_awaitable(
            mutation_journal.record_restored(restore.observation)
        )
        restoration_record_error = restoration_record.error
    if restore.error is not None:
        raise CommissioningRuntimeFailure(
            "restore_failed",
            restore.error,
            side_effects=side_effects,
            cancelled=isinstance(primary, asyncio.CancelledError),
        ) from primary
    if restoration_record_error is not None:
        raise CommissioningRuntimeFailure(
            "restore_record_failed",
            "exact live restoration could not be durably recorded",
            side_effects=side_effects,
            cancelled=isinstance(primary, asyncio.CancelledError),
        ) from restoration_record_error
    if primary is not None:
        if not isinstance(primary, (Exception, asyncio.CancelledError)):
            raise primary
        if isinstance(primary, asyncio.CancelledError):
            raise CommissioningRuntimeCancelled(side_effects=side_effects) from primary
        code = primary.code if isinstance(primary, _OperationFailure) else "capture_failed"
        detail = (
            primary.detail
            if isinstance(primary, _OperationFailure)
            else f"summed capture failed: {type(primary).__name__}: {primary}"
        )
        raise CommissioningRuntimeFailure(
            code, detail, side_effects=side_effects
        ) from primary
    assert predecessor is not None
    assert candidate_identity is not None
    assert capture is not None
    assert restore.observation is not None
    return SummedCaptureRuntimeResult(
        predecessor=predecessor.exact,
        candidate_graph=candidate_identity,
        delay_confirmation=delay_confirmation,
        capture=capture,
        restore=restore.observation,
    )


async def _await_cancellation_resilient(
    task: asyncio.Task[SummedCaptureRuntimeResult[T]],
) -> SummedCaptureRuntimeResult[T]:
    cancelled = False
    cancellation_forwarded = False
    waiter = asyncio.create_task(asyncio.wait({task}))
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
            if not cancellation_forwarded and not task.done():
                task.cancel()
                cancellation_forwarded = True
    waiter.result()
    try:
        result = task.result()
    except CommissioningRuntimeFailure as exc:
        if cancelled:
            exc.cancelled = True
        raise
    except CommissioningRuntimeCancelled:
        raise
    except asyncio.CancelledError as exc:
        raise CommissioningRuntimeCancelled(
            side_effects=RuntimeSideEffectState(False, False, False, None)
        ) from exc
    if cancelled:
        raise CommissioningRuntimeCancelled(
            side_effects=RuntimeSideEffectState(
                graph_may_have_mutated=True,
                audio_may_have_emitted=True,
                restore_attempted=True,
                restore_succeeded=True,
            ),
            completed_result=result,
        )
    return result


async def run_summed_capture(
    port: CommissioningRuntimePort,
    request: SummedGraphRequest,
    capture_callback: CaptureCallback[T],
    *,
    topology: OutputTopology,
    mutation_journal: CommissioningMutationJournal,
    config_dir: str | Path,
    lock_timeout_s: float = DEFAULT_SUMMED_RUNTIME_LOCK_TIMEOUT_S,
) -> SummedCaptureRuntimeResult[T]:
    """Apply, capture, and exactly restore one summed-region graph.

    The Shared writer-lock default bounds lock admission.  Once admitted, the
    graph/capture/restore transaction forwards cancellation to the callback,
    then drains exact restoration before reporting; cleanup failure outranks
    cancellation.
    """

    if not isinstance(port, CommissioningRuntimePort):
        raise CommissioningRuntimeError("port must be CommissioningRuntimePort")
    if not isinstance(request, SummedGraphRequest):
        raise CommissioningRuntimeError("request must be SummedGraphRequest")
    if not isinstance(topology, OutputTopology):
        raise CommissioningRuntimeError("topology must be OutputTopology")
    if not isinstance(mutation_journal, CommissioningMutationJournal):
        raise CommissioningRuntimeError(
            "mutation_journal must be CommissioningMutationJournal"
        )
    if (
        request.topology_id != topology.topology_id
        or request.topology_fingerprint != topology_config_fingerprint(topology)
    ):
        raise CommissioningRuntimeError(
            "request topology identity is not the exact current topology"
        )
    if not callable(capture_callback):
        raise CommissioningRuntimeError("capture_callback must be callable")
    binding = _topology_binding(request, topology)
    if (
        isinstance(lock_timeout_s, bool)
        or not isinstance(lock_timeout_s, (int, float))
        or not math.isfinite(float(lock_timeout_s))
        or float(lock_timeout_s) <= 0.0
    ):
        raise CommissioningRuntimeError("lock_timeout_s must be finite and positive")
    entered = False
    try:
        async with dsp_writer_lock(
            config_dir,
            source="active_speaker_summed_commissioning",
            timeout_s=float(lock_timeout_s),
        ):
            entered = True
            return await _await_cancellation_resilient(
                asyncio.create_task(
                    _run_locked(
                        port,
                        request,
                        topology,
                        binding,
                        mutation_journal,
                        capture_callback,
                    )
                )
            )
    except asyncio.CancelledError as exc:
        if entered or isinstance(exc, CommissioningRuntimeCancelled):
            raise
        raise CommissioningRuntimeCancelled(
            side_effects=RuntimeSideEffectState(False, False, False, None)
        ) from exc


@dataclass(frozen=True)
class PreparedSummedExcitation:
    """Thin two-target reduction into Shared's existing admission values."""

    target_fingerprints: tuple[str, str]
    request: ExcitationRequest
    limits: ExcitationLimits
    minimum_cooldown_s: float

def prepare_summed_excitation(
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    *,
    target_fingerprints: tuple[str, str],
    evidence_target_fingerprint: str,
    band: FrequencyBand,
    effective_peak_dbfs: float,
    duration_s: float,
    excitation_plan_fingerprint: str,
) -> PreparedSummedExcitation:
    """Intersect two current adjacent driver policies for one-repeat playback."""

    if not isinstance(topology, OutputTopology):
        raise CommissioningRuntimeError("topology must be OutputTopology")
    evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not evaluation.confirmed_and_current or evaluation.profile_fingerprint is None:
        raise CommissioningRuntimeError("driver safety profile is not current")
    if (
        type(target_fingerprints) is not tuple
        or len(target_fingerprints) != 2
        or len(set(target_fingerprints)) != 2
    ):
        raise CommissioningRuntimeError(
            "target_fingerprints must name two distinct adjacent drivers"
        )
    if (
        not isinstance(evidence_target_fingerprint, str)
        or len(evidence_target_fingerprint) != 64
        or any(ch not in "0123456789abcdef" for ch in evidence_target_fingerprint)
    ):
        raise CommissioningRuntimeError(
            "evidence_target_fingerprint must be a lowercase SHA-256"
        )
    current_by_fingerprint = {
        target["target_fingerprint"]: target for target in active_driver_targets(topology)
    }
    current = [current_by_fingerprint.get(fingerprint) for fingerprint in target_fingerprints]
    if any(target is None for target in current):
        raise CommissioningRuntimeError("summed targets are not current")
    current_targets = cast(list[dict[str, Any]], current)
    if current_targets[0]["speaker_group_id"] != current_targets[1]["speaker_group_id"]:
        raise CommissioningRuntimeError("summed targets must share one speaker group")
    group_id = current_targets[0]["speaker_group_id"]
    group = next((item for item in topology.speaker_groups if item.id == group_id), None)
    if group is None:
        raise CommissioningRuntimeError("summed speaker group is not current")
    way_count = 2 if group.mode == "active_2_way" else 3
    roles = tuple(target["role"] for target in current_targets)
    if roles not in ADJACENT_PAIRS_BY_WAY[way_count]:
        raise CommissioningRuntimeError("summed driver targets must be adjacent")

    profile_targets = safety_profile.get("targets")
    if not isinstance(profile_targets, list):
        raise CommissioningRuntimeError("driver safety profile targets are missing")
    profile_by_fingerprint = {
        item.get("target_fingerprint"): item
        for item in profile_targets
        if isinstance(item, Mapping)
    }
    targets = [profile_by_fingerprint.get(fingerprint) for fingerprint in target_fingerprints]
    if any(not isinstance(target, Mapping) for target in targets):
        raise CommissioningRuntimeError("driver safety profile targets are stale")
    typed_targets = cast(list[Mapping[str, Any]], targets)
    try:
        lower_hz = max(
            MIN_DRIVER_TEST_FREQUENCY_HZ,
            *(float(target["hard_excitation_band_hz"][0]) for target in typed_targets),
        )
        lower_hz = max(
            lower_hz,
            *(float(target["measurement_band_hz"][0]) for target in typed_targets),
        )
        upper_hz = min(
            MAX_DRIVER_TEST_FREQUENCY_HZ,
            *(float(target["hard_excitation_band_hz"][1]) for target in typed_targets),
        )
        upper_hz = min(
            upper_hz,
            *(float(target["measurement_band_hz"][1]) for target in typed_targets),
        )
        limits = [cast(Mapping[str, Any], target["level_duration_limits"]) for target in typed_targets]
        max_peak = min(float(item["max_effective_peak_dbfs"]) for item in limits)
        max_duration = min(
            SUMMED_SWEEP_DURATION_S,
            *(float(item["max_sweep_duration_s"]) for item in limits),
        )
        cooldown = max(float(item["minimum_cooldown_s"]) for item in limits)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CommissioningRuntimeError(
            "driver safety profile target limits are incomplete"
        ) from exc
    if lower_hz > upper_hz:
        raise CommissioningRuntimeError("adjacent driver measurement bands do not overlap")
    permitted_band = FrequencyBand(lower_hz, upper_hz)
    if not isinstance(band, FrequencyBand):
        raise CommissioningRuntimeError("band must be FrequencyBand")
    requirement_fingerprint = json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_summed_protection_requirement",
            "target_fingerprints": list(target_fingerprints),
            "required_filters": [
                target.get("required_protection_filters") for target in typed_targets
            ],
        }
    )
    authority = ExcitationLimits(
        permitted_band=permitted_band,
        maximum_effective_peak_dbfs=max_peak,
        maximum_duration_s=max_duration,
        maximum_repeat_count=1,
        target_fingerprint=evidence_target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        protection_requirement_fingerprint=requirement_fingerprint,
        excitation_plan_fingerprint=excitation_plan_fingerprint,
    )
    request = ExcitationRequest(
        band=band,
        effective_peak_dbfs=effective_peak_dbfs,
        duration_s=duration_s,
        repeat_count=1,
        target_fingerprint=evidence_target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        authority_fingerprint=authority.fingerprint,
        excitation_plan_fingerprint=excitation_plan_fingerprint,
    )
    return PreparedSummedExcitation(
        target_fingerprints=target_fingerprints,
        request=request,
        limits=authority,
        minimum_cooldown_s=cooldown,
    )
