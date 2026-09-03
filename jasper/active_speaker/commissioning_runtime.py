# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact live-DSP state snapshot/restore for commissioning transactions.

Restore is fail-closed: it reinstates the exact entry graph, config path and
listening volume observed at snapshot, under the caller's writer lock.  The
pure ``prepare_summed_excitation`` helper only intersects two adjacent driver
targets into Shared's existing excitation admission values.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

import yaml

from jasper.audio_measurement.evidence_identity import (
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
)
from jasper.output_topology import OutputTopology

from .driver_safety import evaluate_driver_safety_profile
from .excitation_safety_plan import declared_level_ceiling_dbfs
from .measurement import active_driver_targets
from .profile import ADJACENT_PAIRS_BY_WAY
from .test_signal_plan import (
    MAX_DRIVER_TEST_FREQUENCY_HZ,
    MIN_DRIVER_TEST_FREQUENCY_HZ,
    SUMMED_SWEEP_DURATION_S,
)

T = TypeVar("T")
ReadActiveRaw = Callable[[], Awaitable[str | None]]
CanonicalizeRaw = Callable[[str], Awaitable[str | None]]
ApplyActiveRaw = Callable[[str], Awaitable[bool]]
ReadConfigPath = Callable[[], Awaitable[str | None]]
ReadListeningVolume = Callable[[], Awaitable[float | None]]
SetListeningVolume = Callable[[float], Awaitable[bool]]
LoadConfigPath = Callable[[str], Awaitable[bool]]


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
    # CamillaDSP's ReadConfig (normalize_config_raw). The live-graph proof
    # compares a JTS-authored file against CamillaDSP's default-filled
    # readback, so the file has to be canonicalized BY CamillaDSP first. Seam,
    # not a local reimplementation: CamillaDSP owns its own schema.
    canonicalize_raw: CanonicalizeRaw
    _bass_extension_authority_paths: Mapping[str, Path] | None = None

    def __post_init__(self) -> None:
        for name in (
            "read_active_raw",
            "apply_active_raw",
            "read_config_path",
            "read_listening_volume_db",
            "set_listening_volume_db",
            "canonicalize_raw",
        ):
            if not callable(getattr(self, name)):
                raise CommissioningRuntimeError(f"{name} must be callable")
        paths = self._bass_extension_authority_paths
        if paths is not None and set(paths) != {
            "statefile_path",
            "applied_baseline_path",
            "profile_path",
            "intent_path",
            "staged_metadata_path",
        }:
            raise CommissioningRuntimeError(
                "test bass-extension authority paths are incomplete"
            )


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
class RestoreObservation:
    graph: NormalizedActiveRawIdentity
    config_path: str
    listening_volume_db: float


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


def _active_identity(raw: str | None, *, field: str) -> NormalizedActiveRawIdentity:
    return NormalizedActiveRawIdentity(_parse_active_raw(raw, field=field))


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


async def snapshot_exact_dsp_state(
    port: CommissioningRuntimePort,
) -> ExactDspStateIdentity:
    """Freshly observe the exact graph/path/volume state under an owning lock."""

    if not isinstance(port, CommissioningRuntimePort):
        raise CommissioningRuntimeError("port must be CommissioningRuntimePort")
    return (await _snapshot(port)).exact


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
    port: CommissioningRuntimePort,
    predecessor: _Predecessor,
    *,
    load_config_path: LoadConfigPath | None = None,
) -> _RestoreResult:
    issues: list[str] = []

    if load_config_path is not None:
        path_apply = await _capture_awaitable(load_config_path(predecessor.path))
        if path_apply.error is not None:
            issues.append(
                "predecessor config-path apply raised "
                f"{type(path_apply.error).__name__}"
            )
        elif not path_apply.value:
            issues.append("predecessor config-path apply was rejected")

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


async def restore_exact_dsp_state_locked(
    port: CommissioningRuntimePort,
    predecessor: ExactDspStateIdentity,
    *,
    load_config_path: LoadConfigPath | None = None,
) -> RestoreObservation:
    """Restore one predecessor while the caller retains the DSP writer lock."""

    if not isinstance(port, CommissioningRuntimePort):
        raise CommissioningRuntimeError("port must be CommissioningRuntimePort")
    result = await _restore(
        port,
        _predecessor_from_identity(predecessor),
        load_config_path=load_config_path,
    )
    if result.error is not None or result.observation is None:
        raise CommissioningRuntimeError(
            result.error or "exact predecessor restoration was not proved"
        )
    return result.observation


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
        # ``measurement_band_hz[0]`` is the whole low edge here; this used to
        # ALSO take a max over ``hard_excitation_band_hz[0]``, which restated a
        # rule this module does not own and could never bind.
        #
        # The short argument, and it needs nothing from #2603: every confirmed
        # profile satisfies ``_band_subset(measurement, hard)``, because
        # ``driver_safety._target_issues`` raises
        # ``<role>:measurement_band_outside_hard_band`` otherwise and
        # ``evaluate_driver_safety_profile`` turns any derived issue into a
        # NOT-confirmed verdict -- which the ``confirmed_and_current`` gate
        # above already refused. Subset means ``measurement[0] >= hard[0]``, so
        # the ``hard[0]`` term was dominated on every reachable input.
        #
        # #2603 adds a second, independent guarantee for the same inequality:
        # ``apply_driver_low_limit`` stamps ``hard[0]`` at the declared low
        # limit and ``measurement[0]`` at ``max(published response floor, that
        # limit)``.
        #
        # ``excitation_safety_plan.resolve_driver_excitation_ceilings`` keeps its
        # own ``hard[0]`` term and is not wrong to: on its proven-HP
        # high-frequency branch it EXCLUDES ``measurement[0]``, so there
        # ``hard[0]`` is the binding edge. The summed path has no such branch.
        lower_hz = max(
            MIN_DRIVER_TEST_FREQUENCY_HZ,
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
        # ``max_effective_peak_dbfs`` is OPTIONAL, so it is read through the one
        # owner of "what is this target's level ceiling" rather than indexed
        # here (2026-08-23). Indexing it made the ordinary reply shape -- a
        # driver whose maker publishes no level limit -- raise KeyError and
        # surface as "limits are incomplete", which described nothing wrong.
        max_peak = min(
            declared_level_ceiling_dbfs(target)[0] for target in typed_targets
        )
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
