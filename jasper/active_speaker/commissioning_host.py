# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Server-owned orchestration for authoritative summed-region evidence.

The host is deliberately limited to composition: deterministic ordering,
durable attempt reuse, bounded null-walk progress, crash recovery, and
lifecycle commits.  Runtime mutation, capture production, artifact I/O, and
evidence validation remain in their owning modules.  Browser fields never
select a region, polarity, delay, placement, or capture ordinal.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeAlias

import yaml

from jasper.audio_measurement.evidence_identity import (
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.null_walk import (
    BoundedNullWalkSchedule,
    MIN_CAPTURE_COUNT,
    NullWalkError,
    NullWalkSpec,
    select_scheduled_delay,
)
from jasper.correction.playback import DEFAULT_ALSA_DEVICE
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from .baseline_profile import topology_config_fingerprint
from .baseline_profile import recompose_applied_baseline_yaml
from .capture_geometry import (
    comparison_set_valid,
    driver_level_lock,
    quietest_locked_main_volume,
)
from .crossover_contract import preset_matches_applied_profile
from .commissioning_evidence import (
    DELAY_WALK_ALGORITHM_ID,
    DELAY_WALK_ALGORITHM_VERSION,
    STATIONARY_CAPTURE_COUNT,
    AdmittedRegionCapture,
    CompleteCommissioningEvidence,
    DelayPointEvidence,
    DelayWalkEvidence,
    EvidenceKind,
    RegionCommissioningEvidence,
    RegionEvidencePlan,
    RegionEvidenceTarget,
    RegionGeometryAttestation,
    StationaryRegionEvidence,
    active_region_context_fingerprint,
    delay_point_context_base_fingerprint,
    delay_point_target_fingerprint,
    evidence_attempt_target_id,
    region_evidence_preset_fingerprint,
)
from .commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
    attempt_capture_relative_path,
    complete_relative_path,
    delay_point_relative_path,
    delay_walk_relative_path,
    region_relative_path,
    stationary_relative_path,
)
from .commissioning_capture_producer import (
    CurrentCaptureAuthority,
    RawCaptureTransport,
    SummedCaptureProducer,
    SummedCaptureProducerError,
)
from .commissioning_lifecycle import CommissioningTransition
from .commissioning_runtime import (
    CommissioningMutationJournal,
    CommissioningRuntimeCancelled,
    CommissioningRuntimeFailure,
    CommissioningRuntimePort,
    RestoreObservation,
    RuntimeSideEffectState,
    SummedGraphRequest,
    recover_summed_predecessor,
    run_summed_capture,
)
from .commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningLiveMutation,
    CommissioningRunConflict,
    CommissioningRunHandle,
    CommissioningRunError,
    CommissioningRunStore,
)
from .driver_safety import evaluate_driver_safety_profile
from .measurement import active_driver_targets
from .profile import ActiveSpeakerPreset
from .test_signal_plan import CROSSOVER_CAPTURE_PLAY_DEADLINE_S

logger = logging.getLogger(__name__)

CommissioningGraphKind: TypeAlias = Literal["normal", "reverse", "delay"]


class CommissioningHostError(RuntimeError):
    """One server-owned commissioning operation cannot safely progress."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _sha256(value: Any, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CommissioningHostError(
            "host_input_invalid", f"{field_name} must be a lowercase SHA-256"
        )
    return value


def _attempt_payload(attempt: CommissioningAttemptHandle) -> dict[str, Any]:
    run = attempt.run
    return {
        "run": {
            "session_id": run.session_id,
            "session_fingerprint": run.session_fingerprint,
            "run_id": run.run_id,
            "owner_id": run.owner_id,
            "owner_generation": run.owner_generation,
        },
        "attempt_id": attempt.attempt_id,
        "attempt_number": attempt.attempt_number,
        "target_id": attempt.target_id,
        "target_fingerprint": attempt.target_fingerprint,
    }


@dataclass(frozen=True, slots=True)
class RegionCommissioningInputs:
    """Server-owned placement and geometry inputs for one exact plan target."""

    target_fingerprint: str
    placement_fingerprint: str
    geometry: RegionGeometryAttestation
    null_walk_spec: NullWalkSpec

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_fingerprint",
            _sha256(self.target_fingerprint, field_name="target_fingerprint"),
        )
        object.__setattr__(
            self,
            "placement_fingerprint",
            _sha256(self.placement_fingerprint, field_name="placement_fingerprint"),
        )
        if not isinstance(self.geometry, RegionGeometryAttestation):
            raise CommissioningHostError(
                "host_input_invalid", "geometry must be RegionGeometryAttestation"
            )
        if not isinstance(self.null_walk_spec, NullWalkSpec):
            raise CommissioningHostError(
                "host_input_invalid", "null_walk_spec must be NullWalkSpec"
            )


@dataclass(frozen=True, slots=True)
class CommissioningHostAuthoritySnapshot:
    """Fresh product-owned authorities required to emit one runtime request."""

    topology: OutputTopology
    preset: ActiveSpeakerPreset
    safety_profile: Mapping[str, Any]
    comparison_set: Mapping[str, Any]
    applied_profile: Mapping[str, Any]
    calibration_id: str
    calibration: CalibrationCurve

    def __post_init__(self) -> None:
        if not isinstance(self.topology, OutputTopology):
            raise CommissioningHostError(
                "host_input_invalid", "authority topology must be OutputTopology"
            )
        if not isinstance(self.preset, ActiveSpeakerPreset):
            raise CommissioningHostError(
                "host_input_invalid", "authority preset must be ActiveSpeakerPreset"
            )
        for field_name in ("safety_profile", "comparison_set", "applied_profile"):
            if not isinstance(getattr(self, field_name), Mapping):
                raise CommissioningHostError(
                    "host_input_invalid", f"authority {field_name} must be a mapping"
                )
        if (
            not isinstance(self.calibration_id, str)
            or not self.calibration_id
            or self.calibration_id != self.calibration_id.strip()
        ):
            raise CommissioningHostError(
                "host_input_invalid", "authority calibration_id must be trimmed"
            )
        if not isinstance(self.calibration, CalibrationCurve):
            raise CommissioningHostError(
                "host_input_invalid", "authority calibration must be typed"
            )


CurrentAuthorityLoader: TypeAlias = Callable[[], CommissioningHostAuthoritySnapshot]


@dataclass(frozen=True, slots=True)
class RegionCaptureOperation:
    """The only summed capture operation a production adapter may execute."""

    plan_fingerprint: str
    target: RegionEvidenceTarget
    attempt: CommissioningAttemptHandle
    evidence_kind: EvidenceKind
    placement_fingerprint: str
    driver_target_fingerprints: tuple[str, str]
    lower_channels: tuple[int, ...]
    upper_channels: tuple[int, ...]
    capture_ordinal: int
    required_capture_count: int
    relative_delay_us: float | None = None
    null_walk_spec: NullWalkSpec | None = None
    issuance_id: str | None = None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_fingerprint",
            _sha256(self.plan_fingerprint, field_name="plan_fingerprint"),
        )
        if not isinstance(self.target, RegionEvidenceTarget):
            raise CommissioningHostError(
                "operation_invalid", "operation target must be RegionEvidenceTarget"
            )
        if not isinstance(self.attempt, CommissioningAttemptHandle):
            raise CommissioningHostError(
                "operation_invalid", "operation attempt must be durable"
            )
        if self.evidence_kind not in {"normal", "reverse", "delay_null"}:
            raise CommissioningHostError(
                "operation_invalid", "operation evidence kind is unsupported"
            )
        object.__setattr__(
            self,
            "placement_fingerprint",
            _sha256(self.placement_fingerprint, field_name="placement_fingerprint"),
        )
        if (
            type(self.driver_target_fingerprints) is not tuple
            or len(self.driver_target_fingerprints) != 2
            or len(set(self.driver_target_fingerprints)) != 2
        ):
            raise CommissioningHostError(
                "operation_invalid",
                "operation requires two distinct driver target fingerprints",
            )
        for item in self.driver_target_fingerprints:
            _sha256(item, field_name="driver_target_fingerprints[]")
        for name in ("lower_channels", "upper_channels"):
            channels = getattr(self, name)
            if (
                type(channels) is not tuple
                or not channels
                or any(type(item) is not int or item < 0 for item in channels)
            ):
                raise CommissioningHostError(
                    "operation_invalid", f"{name} must contain physical channels"
                )
        if set(self.lower_channels) & set(self.upper_channels):
            raise CommissioningHostError(
                "operation_invalid", "adjacent region channels must be disjoint"
            )
        if (
            type(self.capture_ordinal) is not int
            or type(self.required_capture_count) is not int
            or not 0 <= self.capture_ordinal < self.required_capture_count
        ):
            raise CommissioningHostError(
                "operation_invalid", "operation capture ordinal is outside its set"
            )
        if self.evidence_kind == "delay_null":
            if not isinstance(self.null_walk_spec, NullWalkSpec):
                raise CommissioningHostError(
                    "operation_invalid", "delay operation requires an exact walk spec"
                )
            if isinstance(self.relative_delay_us, bool) or not isinstance(
                self.relative_delay_us, (int, float)
            ):
                raise CommissioningHostError(
                    "operation_invalid", "delay operation requires a coordinate"
                )
            coordinate = float(self.relative_delay_us)
            if not math.isfinite(coordinate):
                raise CommissioningHostError(
                    "operation_invalid", "delay coordinate must be finite"
                )
            self.null_walk_spec.dsp_candidate(coordinate)
            object.__setattr__(self, "relative_delay_us", coordinate)
            expected_target = delay_point_target_fingerprint(
                self.target, self.null_walk_spec, coordinate
            )
        else:
            if self.relative_delay_us is not None or self.null_walk_spec is not None:
                raise CommissioningHostError(
                    "operation_invalid",
                    "stationary operation cannot carry delay-only fields",
                )
            expected_target = self.target.target_fingerprint_for(self.evidence_kind)
        if self.issuance_id is not None and (
            len(self.issuance_id) != 32
            or any(
                character not in "0123456789abcdef"
                for character in self.issuance_id
            )
        ):
            raise CommissioningHostError(
                "operation_invalid",
                "operation issuance must be a lowercase UUID hex id",
            )
        if (
            self.attempt.target_fingerprint != expected_target
            or self.attempt.target_id
            != evidence_attempt_target_id(self.evidence_kind, expected_target)
        ):
            raise CommissioningHostError(
                "operation_invalid", "operation does not equal its durable attempt"
            )
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    @property
    def graph_kind(self) -> CommissioningGraphKind:
        return "delay" if self.evidence_kind == "delay_null" else self.evidence_kind

    @property
    def target_fingerprint(self) -> str:
        return self.attempt.target_fingerprint

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_region_capture_operation",
            "plan_fingerprint": self.plan_fingerprint,
            "target_fingerprint": self.target.fingerprint,
            "attempt": _attempt_payload(self.attempt),
            "evidence_kind": self.evidence_kind,
            "placement_fingerprint": self.placement_fingerprint,
            "driver_target_fingerprints": list(self.driver_target_fingerprints),
            "lower_channels": list(self.lower_channels),
            "upper_channels": list(self.upper_channels),
            "capture_ordinal": self.capture_ordinal,
            "required_capture_count": self.required_capture_count,
            "relative_delay_us": self.relative_delay_us,
            "null_walk_spec_fingerprint": (
                self.null_walk_spec.fingerprint
                if self.null_walk_spec is not None
                else None
            ),
        }


def _program_key(plan: RegionEvidencePlan) -> tuple[Any, ...]:
    """Stable program identity across owner-generation restart claims."""

    authority = plan.authority
    run = authority.run
    return (
        run.session_id,
        run.session_fingerprint,
        run.run_id,
        authority.topology_id,
        authority.topology_fingerprint,
        authority.protected_safety_profile_fingerprint,
        authority.comparison_set_fingerprint,
        authority.threshold_profile_fingerprint,
        authority.context_fingerprint,
        authority.expected_geometry_id,
        plan.preset_id,
        plan.preset_fingerprint,
        tuple(
            (
                target.speaker_group_id,
                target.region_id,
                target.region_fingerprint,
                target.lower_role,
                target.upper_role,
                target.electrical_fc_hz,
                target.electrical_family,
                target.electrical_order,
            )
            for target in plan.targets
        ),
    )


class CommissioningEvidenceHost:
    """Deterministic production host for one exact run owner generation."""

    def __init__(
        self,
        *,
        plan: RegionEvidencePlan,
        topology: OutputTopology,
        run_store: CommissioningRunStore,
        evidence_store: CommissioningEvidenceStore,
        region_inputs: Sequence[RegionCommissioningInputs],
        load_current_authority: CurrentAuthorityLoader | None = None,
        raw_capture_transport: RawCaptureTransport | None = None,
    ) -> None:
        if not isinstance(plan, RegionEvidencePlan):
            raise CommissioningHostError("host_input_invalid", "plan is invalid")
        if not isinstance(topology, OutputTopology):
            raise CommissioningHostError(
                "host_input_invalid", "topology must be OutputTopology"
            )
        if not isinstance(run_store, CommissioningRunStore):
            raise CommissioningHostError(
                "host_input_invalid", "run_store must be CommissioningRunStore"
            )
        if not isinstance(evidence_store, CommissioningEvidenceStore):
            raise CommissioningHostError(
                "host_input_invalid",
                "evidence_store must be CommissioningEvidenceStore",
            )
        if plan.authority.commissioning_session_id != evidence_store.session_id:
            raise CommissioningHostError(
                "host_input_invalid", "plan and evidence store sessions differ"
            )
        if load_current_authority is not None and not callable(load_current_authority):
            raise CommissioningHostError(
                "host_input_invalid", "load_current_authority must be callable"
            )
        if raw_capture_transport is not None and not callable(raw_capture_transport):
            raise CommissioningHostError(
                "host_input_invalid", "raw_capture_transport must be callable"
            )
        if (
            topology.topology_id != plan.authority.topology_id
            or topology_config_fingerprint(topology)
            != plan.authority.topology_fingerprint
            or topology.evaluation().get("status") != "verified"
        ):
            raise CommissioningHostError(
                "host_input_invalid", "topology does not equal the verified plan"
            )
        physical_targets = {
            (target["speaker_group_id"], target["role"]): (
                target["target_fingerprint"],
                (target["output_index"],),
            )
            for target in active_driver_targets(topology)
            if type(target.get("output_index")) is int
        }
        channels_by_target: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {}
        drivers_by_target: dict[str, tuple[str, str]] = {}
        for target in plan.targets:
            lower = physical_targets.get(
                (target.speaker_group_id, target.lower_role)
            )
            upper = physical_targets.get(
                (target.speaker_group_id, target.upper_role)
            )
            if lower is None or upper is None or set(lower[1]) & set(upper[1]):
                raise CommissioningHostError(
                    "host_input_invalid",
                    "plan target does not resolve to distinct topology channels",
                )
            drivers_by_target[target.fingerprint] = (lower[0], upper[0])
            channels_by_target[target.fingerprint] = (lower[1], upper[1])
        supplied = {item.target_fingerprint: item for item in region_inputs}
        if len(supplied) != len(region_inputs) or set(supplied) != {
            target.fingerprint for target in plan.targets
        }:
            raise CommissioningHostError(
                "host_input_invalid", "region inputs must exactly cover the plan"
            )
        geometry_artifacts = [
            item.geometry.attestation_artifact for item in supplied.values()
        ]
        if (
            len({item.fingerprint for item in geometry_artifacts})
            != len(geometry_artifacts)
            or len({item.relative_path for item in geometry_artifacts})
            != len(geometry_artifacts)
        ):
            raise CommissioningHostError(
                "host_input_invalid",
                "every region requires a distinct geometry attestation artifact",
            )
        for target in plan.targets:
            inputs = supplied[target.fingerprint]
            if (
                inputs.geometry.speaker_group_id != target.speaker_group_id
                or inputs.geometry.region_id != target.region_id
                or inputs.geometry.region_target_fingerprint != target.fingerprint
                or inputs.null_walk_spec.crossover_fc_hz
                != target.electrical_fc_hz
                or inputs.null_walk_spec.positive_delay_target != target.upper_role
                or inputs.null_walk_spec.negative_delay_target != target.lower_role
                or not math.isclose(
                    inputs.null_walk_spec.geometry_seed_us,
                    inputs.geometry.signed_geometry_seed_us,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                raise CommissioningHostError(
                    "host_input_invalid",
                    "region geometry/spec inputs do not equal their exact target",
                )
            evidence_store.reopen_artifact(inputs.geometry.attestation_artifact)

        self.plan = plan
        self.topology = topology
        self.run_store = run_store
        self.evidence_store = evidence_store
        self._inputs = supplied
        self._drivers_by_target = drivers_by_target
        self._channels_by_target = channels_by_target
        self._load_current_authority = load_current_authority
        self._raw_capture_transport = raw_capture_transport
        self._lock = threading.RLock()
        self._prepared = False
        self._complete: CompleteCommissioningEvidence | None = None
        self._stationary: dict[str, StationaryRegionEvidence] = {}
        self._points: dict[str, DelayPointEvidence] = {}
        self._walks: dict[str, DelayWalkEvidence] = {}
        self._regions: dict[str, RegionCommissioningEvidence] = {}

    @property
    def run(self) -> CommissioningRunHandle:
        return self.plan.authority.run

    def _missing(self, error: CommissioningEvidenceStoreError) -> bool:
        return error.code == CommissioningEvidenceStoreErrorCode.MISSING

    def _require_current(self) -> None:
        if not self.run_store.callback_is_current(self.run):
            raise CommissioningHostError(
                "run_generation_stale", "commissioning run owner is stale"
            )

    def _current_authority_snapshot(
        self,
    ) -> tuple[CommissioningHostAuthoritySnapshot, str]:
        loader = self._load_current_authority
        if loader is None:
            raise CommissioningHostError(
                "fresh_authority_unavailable",
                "summed commissioning has no fresh product-authority loader",
            )
        try:
            snapshot = loader()
        except (OSError, TypeError, ValueError) as exc:
            raise CommissioningHostError(
                "fresh_authority_unavailable",
                f"fresh product authority could not be loaded: {type(exc).__name__}",
            ) from exc
        if not isinstance(snapshot, CommissioningHostAuthoritySnapshot):
            raise CommissioningHostError(
                "fresh_authority_unavailable",
                "fresh product authority loader returned the wrong type",
            )
        topology = snapshot.topology
        if (
            topology.topology_id != self.plan.authority.topology_id
            or topology_config_fingerprint(topology)
            != self.plan.authority.topology_fingerprint
            or topology.evaluation().get("status") != "verified"
        ):
            raise CommissioningHostError(
                "fresh_authority_stale",
                "fresh topology no longer equals the commissioned plan",
            )
        try:
            preset_fingerprint = region_evidence_preset_fingerprint(snapshot.preset)
        except ValueError as exc:
            raise CommissioningHostError(
                "fresh_authority_stale", "fresh typed preset is invalid"
            ) from exc
        if (
            snapshot.preset.preset_id != self.plan.preset_id
            or preset_fingerprint != self.plan.preset_fingerprint
            or not preset_matches_applied_profile(
                snapshot.preset,
                snapshot.applied_profile,
            )
        ):
            raise CommissioningHostError(
                "fresh_authority_stale",
                "fresh typed preset no longer equals the commissioned plan",
            )
        safety = evaluate_driver_safety_profile(snapshot.safety_profile, topology)
        if (
            not safety.confirmed_and_current
            or safety.profile_fingerprint
            != self.plan.authority.protected_safety_profile_fingerprint
        ):
            raise CommissioningHostError(
                "fresh_authority_stale",
                "fresh driver safety profile no longer equals the commissioned plan",
            )
        comparison_set = snapshot.comparison_set
        if (
            not comparison_set_valid(comparison_set)
            or comparison_set.get("topology_id") != topology.topology_id
            or comparison_set.get("fingerprint")
            != self.plan.authority.comparison_set_fingerprint
            or comparison_set.get("calibration_id") != snapshot.calibration_id
        ):
            raise CommissioningHostError(
                "fresh_authority_stale",
                "fresh comparison set no longer equals the commissioned plan",
            )
        try:
            normal_active_raw, issues = recompose_applied_baseline_yaml(
                topology,
                applied_profile=snapshot.applied_profile,
            )
        except (TypeError, ValueError) as exc:
            raise CommissioningHostError(
                "fresh_authority_stale", "applied profile snapshot is invalid"
            ) from exc
        if normal_active_raw is None or issues:
            reason = str(issues[0].get("code") or "unknown") if issues else "unknown"
            raise CommissioningHostError(
                "fresh_authority_stale",
                f"applied profile cannot be freshly re-emitted: {reason}",
            )
        try:
            context_fingerprint = active_region_context_fingerprint(
                baseline_active_raw_fingerprint=NormalizedActiveRawIdentity(
                    yaml.safe_load(normal_active_raw)
                ).active_raw_fingerprint,
                calibration_id=snapshot.calibration_id,
                calibration=snapshot.calibration,
            )
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise CommissioningHostError(
                "fresh_authority_stale",
                "fresh baseline or calibration context is invalid",
            ) from exc
        if context_fingerprint != self.plan.authority.context_fingerprint:
            raise CommissioningHostError(
                "fresh_authority_stale",
                "fresh baseline or calibration changed since planning",
            )
        return snapshot, normal_active_raw

    def _runtime_request(
        self, operation: RegionCaptureOperation
    ) -> tuple[SummedGraphRequest, CommissioningHostAuthoritySnapshot]:
        snapshot, normal_active_raw = self._current_authority_snapshot()
        roles = frozenset((operation.target.lower_role, operation.target.upper_role))
        locked_volume_by_role: dict[str, float] = {}
        for role in roles:
            lock = driver_level_lock(
                snapshot.comparison_set,
                operation.target.speaker_group_id,
                role,
            )
            if lock is None:
                raise CommissioningHostError(
                    "safe_listening_volume_unavailable",
                    "adjacent drivers do not have fresh durable level locks",
                )
            locked_volume_by_role[role] = float(lock["locked_main_volume_db"])
        quietest = quietest_locked_main_volume(locked_volume_by_role, roles)
        if quietest is None or quietest[1] >= 0.0:
            raise CommissioningHostError(
                "safe_listening_volume_unavailable",
                "adjacent-driver level locks do not prove an attenuated volume",
            )
        delay_spec = operation.null_walk_spec
        delay_candidate = (
            delay_spec.dsp_candidate(operation.relative_delay_us)
            if delay_spec is not None and operation.relative_delay_us is not None
            else None
        )
        return (
            SummedGraphRequest(
                kind=operation.graph_kind,
                normal_active_raw=normal_active_raw,
                lower_role=operation.target.lower_role,
                upper_role=operation.target.upper_role,
                lower_channels=operation.lower_channels,
                upper_channels=operation.upper_channels,
                listening_volume_db=quietest[1],
                topology_id=self.plan.authority.topology_id,
                topology_fingerprint=self.plan.authority.topology_fingerprint,
                delay_spec=delay_spec,
                delay_candidate=delay_candidate,
                delay_scope=(
                    "active_crossover" if delay_spec is not None else None
                ),
            ),
            snapshot,
        )

    def _pending_live_mutation(self) -> CommissioningLiveMutation | None:
        return self.run_store.pending_live_mutation(self.run)

    def _current_live_mutation(self) -> CommissioningLiveMutation | None:
        return self.run_store.current_live_mutation(self.run)

    def _issued_mutation(
        self, operation: RegionCaptureOperation
    ) -> CommissioningLiveMutation:
        if operation.issuance_id is None:
            raise CommissioningHostError(
                "operation_unissued", "capture operation has no execution issuance"
            )
        mutation = self._current_live_mutation()
        if (
            mutation is None
            or mutation.status != "issued"
            or mutation.issuance_id != operation.issuance_id
            or mutation.operation_fingerprint != operation.fingerprint
        ):
            raise CommissioningHostError(
                "operation_stale",
                "capture operation does not own the current execution issuance",
            )
        return mutation

    def _restored_mutation(
        self, operation: RegionCaptureOperation
    ) -> CommissioningLiveMutation:
        if operation.issuance_id is None:
            raise CommissioningHostError(
                "operation_unissued", "capture operation has no execution issuance"
            )
        mutation = self._current_live_mutation()
        if (
            mutation is None
            or mutation.status != "restored"
            or mutation.issuance_id != operation.issuance_id
            or mutation.operation_fingerprint != operation.fingerprint
        ):
            raise CommissioningHostError(
                "operation_not_restored",
                "capture operation lacks exact live-restoration evidence",
            )
        return mutation

    @staticmethod
    def _restore_payload(
        mutation: CommissioningLiveMutation,
        predecessor: ExactDspStateIdentity,
        observation: RestoreObservation,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_summed_measurement_restore",
            "issuance_id": mutation.issuance_id,
            "operation_fingerprint": mutation.operation_fingerprint,
            "predecessor_fingerprint": predecessor.fingerprint,
            "restored_graph_fingerprint": (
                observation.graph.active_raw_fingerprint
            ),
            "restored_config_path": observation.config_path,
            "restored_listening_volume_db": observation.listening_volume_db,
        }

    def _publish_restore_marker(
        self,
        mutation: CommissioningLiveMutation,
        predecessor: ExactDspStateIdentity,
        observation: RestoreObservation,
    ) -> Any:
        payload = self._restore_payload(mutation, predecessor, observation)
        artifact = self.evidence_store.publish_json_artifact(
            (
                f"runtime-rollback/{self.run.run_id}/"
                f"{mutation.started_owner_generation}/"
                f"{mutation.issuance_id}/restored.json"
            ),
            payload,
        )
        if self.evidence_store.reopen_json_artifact(artifact) != payload:
            raise CommissioningHostError(
                "restore_marker_mismatch", "restore marker changed on reopen"
            )
        return artifact

    def _block_live_state_unknown(
        self,
        mutation: CommissioningLiveMutation,
        *,
        reason: str,
    ) -> None:
        state = self.run_store.lifecycle_state(self.run)
        if state == "blocked_live_state_unknown":
            return
        if state not in {"unconfigured", "protected"}:
            raise CommissioningHostError(
                "lifecycle_not_collecting",
                f"uncertain measurement mutation is incompatible with {state}",
            )
        payload = {
            "schema_version": 1,
            "kind": "jts_active_summed_measurement_uncertain_mutation",
            "issuance_id": mutation.issuance_id,
            "operation_fingerprint": mutation.operation_fingerprint,
            "mutation_fingerprint": mutation.fingerprint,
            "rollback_artifact_fingerprint": (
                mutation.rollback_artifact_fingerprint
            ),
            "reason": reason,
        }
        artifact = self.evidence_store.publish_json_artifact(
            (
                f"runtime-rollback/{self.run.run_id}/"
                f"{mutation.started_owner_generation}/"
                f"{mutation.issuance_id}/uncertain.json"
            ),
            payload,
        )
        if self.evidence_store.reopen_json_artifact(artifact) != payload:
            raise CommissioningHostError(
                "uncertain_marker_mismatch",
                "uncertain mutation marker changed on reopen",
            )
        committed = self.run_store.transition(
            self.run,
            CommissioningTransition(
                from_state=state,
                to_state="blocked_live_state_unknown",
                evidence_kind="uncertain_mutation_evidence",
                evidence_fingerprint=artifact.fingerprint,
                failure_code="measurement_restore_failed",
            ),
        )
        if not committed:
            raise CommissioningHostError(
                "run_generation_stale", "uncertain mutation lost run ownership"
            )

    def _runtime_mutation_journal(
        self,
        operation: RegionCaptureOperation,
    ) -> tuple[CommissioningMutationJournal, Callable[[], bool]]:
        issued = self._issued_mutation(operation)
        mutation: CommissioningLiveMutation | None = None
        predecessor: ExactDspStateIdentity | None = None
        restored_by_caller = False

        async def record_intent(exact: ExactDspStateIdentity) -> None:
            nonlocal mutation, predecessor
            artifact = self.evidence_store.publish_json_artifact(
                (
                    f"runtime-rollback/{self.run.run_id}/"
                    f"{self.run.owner_generation}/{issued.issuance_id}/"
                    "predecessor.json"
                ),
                exact.to_dict(),
            )
            try:
                reopened = ExactDspStateIdentity.from_mapping(
                    self.evidence_store.reopen_json_artifact(artifact)
                )
            except ValueError as exc:
                raise CommissioningHostError(
                    "rollback_anchor_invalid",
                    "rollback anchor is not exact DSP state evidence",
                ) from exc
            if reopened != exact:
                raise CommissioningHostError(
                    "rollback_anchor_mismatch", "rollback anchor changed on reopen"
                )
            mutation = self.run_store.record_live_mutation_intent(
                self.run,
                issued,
                rollback_artifact_path=artifact.relative_path,
                rollback_artifact_fingerprint=artifact.fingerprint,
            )
            predecessor = reopened

        async def record_restored(observation: RestoreObservation) -> None:
            nonlocal mutation, restored_by_caller
            if mutation is None or predecessor is None:
                raise CommissioningHostError(
                    "rollback_anchor_missing",
                    "restore marker has no exact pending rollback anchor",
                )
            artifact = self._publish_restore_marker(
                mutation, predecessor, observation
            )
            mutation = self.run_store.record_live_mutation_restored(
                self.run,
                mutation,
                restoration_evidence_fingerprint=artifact.fingerprint,
            )
            restored_by_caller = True

        return (
            CommissioningMutationJournal(record_intent, record_restored),
            lambda: restored_by_caller,
        )

    def _release_unstarted_execution(
        self, operation: RegionCaptureOperation
    ) -> None:
        mutation = self._current_live_mutation()
        if (
            mutation is not None
            and mutation.status == "issued"
            and mutation.issuance_id == operation.issuance_id
            and mutation.operation_fingerprint == operation.fingerprint
        ):
            self.run_store.release_live_mutation(self.run, mutation)

    def _abort_restored_execution(
        self,
        operation: RegionCaptureOperation,
        *,
        failure_code: str,
        restored_by_caller: bool,
    ) -> None:
        if not restored_by_caller:
            return
        mutation = self._current_live_mutation()
        if (
            mutation is None
            or mutation.status != "restored"
            or mutation.issuance_id != operation.issuance_id
            or mutation.operation_fingerprint != operation.fingerprint
        ):
            return
        self._abort_restored_mutation(mutation, failure_code=failure_code)

    def _abort_restored_mutation(
        self,
        mutation: CommissioningLiveMutation,
        *,
        failure_code: str,
    ) -> None:
        payload = {
            "schema_version": 1,
            "kind": "jts_active_summed_measurement_execution_abort",
            "issuance_id": mutation.issuance_id,
            "operation_fingerprint": mutation.operation_fingerprint,
            "restoration_evidence_fingerprint": (
                mutation.restoration_evidence_fingerprint
            ),
            "failure_code": failure_code,
        }
        artifact = self.evidence_store.publish_json_artifact(
            (
                f"runtime-rollback/{self.run.run_id}/"
                f"{mutation.started_owner_generation}/"
                f"{mutation.issuance_id}/aborted.json"
            ),
            payload,
        )
        if self.evidence_store.reopen_json_artifact(artifact) != payload:
            raise CommissioningHostError(
                "abort_marker_mismatch", "execution abort marker changed on reopen"
            )
        self.run_store.record_live_mutation_aborted(
            self.run,
            mutation,
            failure_evidence_fingerprint=artifact.fingerprint,
        )

    def _capture_commit_marker_path(
        self, mutation: CommissioningLiveMutation
    ) -> str:
        return (
            f"runtime-rollback/{self.run.run_id}/"
            f"{mutation.started_owner_generation}/"
            f"{mutation.issuance_id}/capture-commit.json"
        )

    def _recover_restored_capture_commit(
        self, mutation: CommissioningLiveMutation
    ) -> None:
        """Finish only a typed capture publish proven before a process crash."""

        marker_artifact = self._identity_or_none(
            self._capture_commit_marker_path(mutation)
        )
        if marker_artifact is None:
            self._abort_restored_mutation(
                mutation,
                failure_code="restart_before_capture_commit",
            )
            return
        marker = self.evidence_store.reopen_json_artifact(marker_artifact)
        expected_fields = {
            "schema_version",
            "kind",
            "issuance_id",
            "operation_fingerprint",
            "capture_fingerprint",
            "capture_relative_path",
        }
        capture_path = marker.get("capture_relative_path")
        if (
            set(marker) != expected_fields
            or marker.get("schema_version") != 1
            or marker.get("kind")
            != "jts_active_summed_measurement_capture_commit"
            or marker.get("issuance_id") != mutation.issuance_id
            or marker.get("operation_fingerprint")
            != mutation.operation_fingerprint
            or not isinstance(capture_path, str)
        ):
            raise CommissioningHostError(
                "restored_commit_marker_invalid",
                "restored capture commit marker is malformed or stale",
            )
        capture_artifact = self._identity_or_none(capture_path)
        if capture_artifact is None:
            self._abort_restored_mutation(
                mutation,
                failure_code="restart_before_capture_publish",
            )
            return
        capture = self.evidence_store.reopen_admitted_region_capture(
            capture_artifact
        )
        if capture.fingerprint != marker.get("capture_fingerprint"):
            raise CommissioningHostError(
                "restored_capture_mismatch",
                "restored capture does not equal its pre-commit marker",
            )
        self.run_store.record_live_mutation_committed(
            self.run,
            mutation,
            commit_evidence_fingerprint=capture_artifact.fingerprint,
        )

    def _recover_restored_exclusively(self) -> None:
        """Recover restored state only while no live executor owns the run."""

        try:
            with self.run_store.claim_live_execution(self.run):
                mutation = self._current_live_mutation()
                if mutation is not None and mutation.status == "restored":
                    self._recover_restored_capture_commit(mutation)
        except CommissioningRunConflict as exc:
            raise CommissioningHostError(
                "live_mutation_execution_in_progress",
                "another summed measurement caller still owns execution",
            ) from exc

    async def _recover_pending_live_mutation(
        self,
        port: CommissioningRuntimePort,
        *,
        config_dir: str,
    ) -> None:
        mutation = self._pending_live_mutation()
        if mutation is None:
            return
        if mutation.purpose != "summed_measurement":
            raise CommissioningHostError(
                "live_mutation_owner_mismatch",
                "pending live mutation belongs to another commissioning phase",
            )
        assert mutation.rollback_artifact_path is not None
        assert mutation.rollback_artifact_fingerprint is not None
        try:
            artifact = self.evidence_store.identify_artifact(
                mutation.rollback_artifact_path
            )
            if artifact.fingerprint != mutation.rollback_artifact_fingerprint:
                raise CommissioningHostError(
                    "rollback_anchor_mismatch",
                    "pending rollback pointer does not equal its exact artifact",
                )
            predecessor = ExactDspStateIdentity.from_mapping(
                self.evidence_store.reopen_json_artifact(artifact)
            )
            recovery = await recover_summed_predecessor(
                port,
                predecessor,
                config_dir=config_dir,
            )
            terminal = self._publish_restore_marker(
                mutation, predecessor, recovery.observation
            )
            self.run_store.record_live_mutation_restored(
                self.run,
                mutation,
                restoration_evidence_fingerprint=terminal.fingerprint,
            )
        except CommissioningRuntimeFailure as exc:
            self._block_live_state_unknown(mutation, reason=exc.code)
            raise CommissioningHostError(
                "live_mutation_recovery_failed",
                "pending summed measurement predecessor could not be restored",
            ) from exc
        except (
            CommissioningEvidenceStoreError,
            CommissioningHostError,
            CommissioningRunError,
            ValueError,
        ) as exc:
            self._block_live_state_unknown(
                mutation, reason="rollback_anchor_invalid"
            )
            raise CommissioningHostError(
                "live_mutation_recovery_failed",
                "pending summed measurement rollback anchor is invalid",
            ) from exc
        if self.run_store.lifecycle_state(self.run) == "blocked_live_state_unknown":
            committed = self.run_store.transition(
                self.run,
                CommissioningTransition(
                    from_state="blocked_live_state_unknown",
                    to_state="rolled_back",
                    evidence_kind="exact_restore_evidence",
                    evidence_fingerprint=terminal.fingerprint,
                ),
            )
            if not committed:
                raise CommissioningHostError(
                    "run_generation_stale", "exact recovery lost run ownership"
                )
        if recovery.cancelled:
            raise CommissioningRuntimeCancelled(
                side_effects=RuntimeSideEffectState(True, False, True, True)
            )

    def _identity_or_none(self, relative_path: str) -> Any | None:
        try:
            return self.evidence_store.identify_artifact(relative_path)
        except CommissioningEvidenceStoreError as exc:
            if self._missing(exc):
                return None
            raise

    def _recover_complete(self) -> CompleteCommissioningEvidence | None:
        try:
            complete = self.evidence_store.reopen_complete_commissioning_evidence(
                run_id=self.run.run_id
            )
        except CommissioningEvidenceStoreError as exc:
            if self._missing(exc):
                return None
            raise
        if _program_key(complete.plan) != _program_key(self.plan):
            raise CommissioningHostError(
                "complete_evidence_stale",
                "durable complete evidence does not equal the current program",
            )
        return complete

    def _require_measured_transition(self, artifact_fingerprint: str) -> None:
        transition = self.run_store.lifecycle_transition(self.run)
        if (
            transition is None
            or transition.to_state != "measured"
            or transition.evidence_kind != "admitted_measurement_set"
            or transition.evidence_fingerprint != artifact_fingerprint
        ):
            raise CommissioningHostError(
                "complete_evidence_stale",
                "measured lifecycle does not name the exact complete evidence",
            )

    def prepare(self) -> CompleteCommissioningEvidence | None:
        """Reopen authority and recover complete evidence without inventing proof."""

        with self._lock:
            self._require_current()
            live_mutation = self._current_live_mutation()
            if (
                live_mutation is not None
                and live_mutation.status == "issued"
                and live_mutation.started_owner_generation < self.run.owner_generation
            ):
                self.run_store.release_live_mutation(self.run, live_mutation)
                live_mutation = self._current_live_mutation()
            if live_mutation is not None and live_mutation.status == "mutation_pending":
                raise CommissioningHostError(
                    "live_mutation_recovery_required",
                    "pending summed measurement must be recovered before preparation",
                )
            if live_mutation is not None and live_mutation.status == "restored":
                self._recover_restored_exclusively()
            recovered = self._recover_complete()
            state = self.run_store.lifecycle_state(self.run)
            if recovered is not None:
                self._current_authority_snapshot()
                artifact = self.evidence_store.identify_artifact(
                    complete_relative_path(self.run.run_id)
                )
                if state == "protected":
                    committed = self.run_store.transition(
                        self.run,
                        CommissioningTransition(
                            from_state="protected",
                            to_state="measured",
                            evidence_kind="admitted_measurement_set",
                            evidence_fingerprint=artifact.fingerprint,
                        ),
                    )
                    if not committed:
                        raise CommissioningHostError(
                            "run_generation_stale",
                            "complete evidence lost current run ownership",
                        )
                elif state != "measured":
                    raise CommissioningHostError(
                        "lifecycle_not_collecting",
                        f"complete evidence is incompatible with lifecycle {state}",
                    )
                self._require_measured_transition(artifact.fingerprint)
                self._complete = recovered
                self._prepared = True
                return recovered

            artifact = self.evidence_store.publish_region_evidence_plan(self.plan)
            reopened = self.evidence_store.reopen_region_evidence_plan(
                run=self.run, artifact=artifact
            )
            if reopened != self.plan:
                raise CommissioningHostError(
                    "plan_readback_mismatch", "persisted plan changed on reopen"
                )
            if state not in {"unconfigured", "protected"}:
                raise CommissioningHostError(
                    "lifecycle_not_collecting",
                    f"commissioning evidence cannot progress from {state}",
                )
            self._prepared = True
            return None

    def _attempt(
        self, evidence_kind: EvidenceKind, target_fingerprint: str
    ) -> CommissioningAttemptHandle:
        return self.run_store.reserve_attempt(
            self.run,
            target_id=evidence_attempt_target_id(
                evidence_kind, target_fingerprint
            ),
            target_fingerprint=target_fingerprint,
            reuse_existing=True,
        )

    def _reopen_stationary(
        self, attempt: CommissioningAttemptHandle
    ) -> StationaryRegionEvidence | None:
        cached = self._stationary.get(attempt.attempt_id)
        if cached is not None:
            return cached
        artifact = self._identity_or_none(stationary_relative_path(attempt.attempt_id))
        if artifact is None:
            return None
        result = self.evidence_store.reopen_stationary_region_evidence(artifact)
        if result.attempt != attempt:
            raise CommissioningHostError(
                "evidence_attempt_mismatch", "stationary evidence attempt changed"
            )
        self._stationary[attempt.attempt_id] = result
        return result

    def _stationary_or_operation(
        self,
        target: RegionEvidenceTarget,
        evidence_kind: Literal["normal", "reverse"],
    ) -> StationaryRegionEvidence | RegionCaptureOperation:
        target_fingerprint = target.target_fingerprint_for(evidence_kind)
        attempt = self._attempt(evidence_kind, target_fingerprint)
        existing = self._reopen_stationary(attempt)
        if existing is not None:
            return existing
        captures = self.evidence_store.reopen_attempt_captures(attempt.attempt_id)
        if len(captures) > STATIONARY_CAPTURE_COUNT:
            raise CommissioningHostError(
                "capture_set_overflow", "stationary capture set exceeded its bound"
            )
        inputs = self._inputs[target.fingerprint]
        lower_channels, upper_channels = self._channels_by_target[target.fingerprint]
        if len(captures) < STATIONARY_CAPTURE_COUNT:
            return RegionCaptureOperation(
                plan_fingerprint=self.plan.fingerprint,
                target=target,
                attempt=attempt,
                evidence_kind=evidence_kind,
                placement_fingerprint=inputs.placement_fingerprint,
                driver_target_fingerprints=self._drivers_by_target[target.fingerprint],
                lower_channels=lower_channels,
                upper_channels=upper_channels,
                capture_ordinal=len(captures),
                required_capture_count=STATIONARY_CAPTURE_COUNT,
            )
        ordered = tuple(sorted(captures, key=lambda item: item.canonical_key))
        evidence = StationaryRegionEvidence(
            authority=self.plan.authority,
            plan_fingerprint=self.plan.fingerprint,
            attempt=attempt,
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            evidence_kind=evidence_kind,
            target_fingerprint=target_fingerprint,
            context_base_fingerprint=target.context_base_fingerprint_for(
                evidence_kind
            ),
            placement_fingerprint=inputs.placement_fingerprint,
            graph_fingerprint=ordered[0].graph_fingerprint,
            captures=ordered,
        )
        self.evidence_store.publish_stationary_region_evidence(evidence)
        self._stationary[attempt.attempt_id] = evidence
        return evidence

    def _reopen_delay_point(
        self, attempt: CommissioningAttemptHandle
    ) -> DelayPointEvidence | None:
        cached = self._points.get(attempt.attempt_id)
        if cached is not None:
            return cached
        artifact = self._identity_or_none(delay_point_relative_path(attempt.attempt_id))
        if artifact is None:
            return None
        result = self.evidence_store.reopen_delay_point_evidence(artifact)
        if result.attempt != attempt:
            raise CommissioningHostError(
                "evidence_attempt_mismatch", "delay evidence attempt changed"
            )
        self._points[attempt.attempt_id] = result
        return result

    def _delay_point_or_operation(
        self,
        target: RegionEvidenceTarget,
        spec: NullWalkSpec,
        relative_delay_us: float,
    ) -> DelayPointEvidence | RegionCaptureOperation:
        target_fingerprint = delay_point_target_fingerprint(
            target, spec, relative_delay_us
        )
        attempt = self._attempt("delay_null", target_fingerprint)
        existing = self._reopen_delay_point(attempt)
        if existing is not None:
            return existing
        captures = self.evidence_store.reopen_attempt_captures(attempt.attempt_id)
        if len(captures) > MIN_CAPTURE_COUNT:
            raise CommissioningHostError(
                "capture_set_overflow", "delay capture set exceeded its bound"
            )
        inputs = self._inputs[target.fingerprint]
        lower_channels, upper_channels = self._channels_by_target[target.fingerprint]
        if len(captures) < MIN_CAPTURE_COUNT:
            return RegionCaptureOperation(
                plan_fingerprint=self.plan.fingerprint,
                target=target,
                attempt=attempt,
                evidence_kind="delay_null",
                placement_fingerprint=inputs.placement_fingerprint,
                driver_target_fingerprints=self._drivers_by_target[target.fingerprint],
                lower_channels=lower_channels,
                upper_channels=upper_channels,
                capture_ordinal=len(captures),
                required_capture_count=MIN_CAPTURE_COUNT,
                relative_delay_us=relative_delay_us,
                null_walk_spec=spec,
            )
        ordered = tuple(sorted(captures, key=lambda item: item.canonical_key))
        graph_fingerprint = ordered[0].graph_fingerprint
        evidence = DelayPointEvidence(
            authority=self.plan.authority,
            plan_fingerprint=self.plan.fingerprint,
            attempt=attempt,
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            relative_delay_us=relative_delay_us,
            target_fingerprint=target_fingerprint,
            context_base_fingerprint=delay_point_context_base_fingerprint(
                target, spec, relative_delay_us, graph_fingerprint
            ),
            placement_fingerprint=inputs.placement_fingerprint,
            graph_fingerprint=graph_fingerprint,
            captures=ordered,
        )
        self.evidence_store.publish_delay_point_evidence(evidence)
        self._points[attempt.attempt_id] = evidence
        return evidence

    def _analysis_rows(
        self, point: DelayPointEvidence
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            self.evidence_store.reopen_json_artifact(
                capture.capture.analysis_input_artifact
            )
            for capture in point.captures
        )

    def _reopen_walk(
        self, target: RegionEvidenceTarget
    ) -> DelayWalkEvidence | None:
        cached = self._walks.get(target.fingerprint)
        if cached is not None:
            return cached
        path = delay_walk_relative_path(
            self.run, target.speaker_group_id, target.region_id
        )
        artifact = self._identity_or_none(path)
        if artifact is None:
            return None
        result = self.evidence_store.reopen_delay_walk_evidence(artifact)
        if result.plan_fingerprint != self.plan.fingerprint:
            raise CommissioningHostError(
                "evidence_plan_mismatch", "delay walk belongs to another plan"
            )
        self._walks[target.fingerprint] = result
        return result

    def _delay_walk_or_operation(
        self, target: RegionEvidenceTarget
    ) -> DelayWalkEvidence | RegionCaptureOperation:
        existing = self._reopen_walk(target)
        if existing is not None:
            return existing
        inputs = self._inputs[target.fingerprint]
        spec = inputs.null_walk_spec
        coarse: list[DelayPointEvidence] = []
        for coordinate in spec.coarse_candidate_delays_us():
            point = self._delay_point_or_operation(target, spec, coordinate)
            if isinstance(point, RegionCaptureOperation):
                return point
            coarse.append(point)
        coarse_rows = {
            point.relative_delay_us: self._analysis_rows(point) for point in coarse
        }
        try:
            schedule = BoundedNullWalkSchedule.from_coarse_evidence(
                spec, coarse_rows
            )
        except NullWalkError as exc:
            self._block_measurement_failure(
                target,
                stage="coarse_schedule",
                failure_type=type(exc).__name__,
            )
            raise CommissioningHostError(
                "delay_schedule_refused",
                "coarse delay evidence could not produce a bounded schedule",
            ) from exc
        schedule_artifact = self.evidence_store.publish_bounded_null_walk_schedule(
            schedule,
            spec=spec,
            run=self.run,
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
        )
        reopened_schedule = self.evidence_store.reopen_bounded_null_walk_schedule(
            spec=spec,
            run=self.run,
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            artifact=schedule_artifact,
        )
        if reopened_schedule != schedule:
            raise CommissioningHostError(
                "schedule_readback_mismatch", "bounded schedule changed on reopen"
            )
        points_by_coordinate = {point.relative_delay_us: point for point in coarse}
        for coordinate in schedule.refinement_delays_us:
            point = self._delay_point_or_operation(target, spec, coordinate)
            if isinstance(point, RegionCaptureOperation):
                return point
            points_by_coordinate[coordinate] = point
        points = tuple(points_by_coordinate[item] for item in schedule.scheduled_delays_us)
        rows = {
            point.relative_delay_us: self._analysis_rows(point) for point in points
        }
        try:
            selection = select_scheduled_delay(spec, schedule, rows)
        except NullWalkError as exc:
            self._block_measurement_failure(
                target,
                stage="final_selection",
                failure_type=type(exc).__name__,
            )
            raise CommissioningHostError(
                "delay_selection_refused",
                "bounded delay evidence could not produce a final selection",
            ) from exc
        repeatability = self.evidence_store.publish_json_artifact(
            (
                f"runs/{self.run.run_id}/generations/"
                f"{self.run.owner_generation}/regions/{target.fingerprint}/"
                "delay-repeatability.json"
            ),
            selection,
        )
        if selection.get("status") != "selected":
            committed = self.run_store.transition(
                self.run,
                CommissioningTransition(
                    from_state="protected",
                    to_state="blocked",
                    evidence_kind="failure_evidence",
                    evidence_fingerprint=repeatability.fingerprint,
                    failure_code="measurement_failed",
                ),
            )
            if not committed:
                raise CommissioningHostError(
                    "run_generation_stale", "delay failure lost run ownership"
                )
            raise CommissioningHostError(
                "delay_selection_refused",
                f"bounded delay evidence was refused: {selection.get('reason')}",
            )
        walk = DelayWalkEvidence(
            authority=self.plan.authority,
            plan_fingerprint=self.plan.fingerprint,
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            algorithm_id=DELAY_WALK_ALGORITHM_ID,
            algorithm_version=DELAY_WALK_ALGORITHM_VERSION,
            geometry_attestation=inputs.geometry,
            spec=spec,
            schedule=schedule,
            placement_fingerprint=inputs.placement_fingerprint,
            points=points,
            repeatability_artifact=repeatability,
        )
        self.evidence_store.publish_delay_walk_evidence(walk)
        self._walks[target.fingerprint] = walk
        return walk

    def _block_measurement_failure(
        self,
        target: RegionEvidenceTarget,
        *,
        stage: str,
        failure_type: str,
    ) -> None:
        artifact = self.evidence_store.publish_json_artifact(
            (
                f"runs/{self.run.run_id}/generations/"
                f"{self.run.owner_generation}/regions/{target.fingerprint}/"
                f"{stage}-failure.json"
            ),
            {
                "schema_version": 1,
                "kind": "jts_active_region_measurement_failure",
                "plan_fingerprint": self.plan.fingerprint,
                "target_fingerprint": target.fingerprint,
                "stage": stage,
                "failure_type": failure_type,
            },
        )
        self.evidence_store.reopen_json_artifact(artifact)
        committed = self.run_store.transition(
            self.run,
            CommissioningTransition(
                from_state="protected",
                to_state="blocked",
                evidence_kind="failure_evidence",
                evidence_fingerprint=artifact.fingerprint,
                failure_code="measurement_failed",
            ),
        )
        if not committed:
            raise CommissioningHostError(
                "run_generation_stale", "measurement failure lost run ownership"
            )

    def _reopen_region(
        self, target: RegionEvidenceTarget
    ) -> RegionCommissioningEvidence | None:
        cached = self._regions.get(target.fingerprint)
        if cached is not None:
            return cached
        path = region_relative_path(
            self.run, target.speaker_group_id, target.region_id
        )
        artifact = self._identity_or_none(path)
        if artifact is None:
            return None
        result = self.evidence_store.reopen_region_commissioning_evidence(artifact)
        if result.plan != self.plan or result.target != target:
            raise CommissioningHostError(
                "evidence_plan_mismatch", "region evidence belongs to another plan"
            )
        self._regions[target.fingerprint] = result
        return result

    def _region_or_operation(
        self, target: RegionEvidenceTarget
    ) -> RegionCommissioningEvidence | RegionCaptureOperation:
        existing = self._reopen_region(target)
        if existing is not None:
            return existing
        normal = self._stationary_or_operation(target, "normal")
        if isinstance(normal, RegionCaptureOperation):
            return normal
        reverse = self._stationary_or_operation(target, "reverse")
        if isinstance(reverse, RegionCaptureOperation):
            return reverse
        if normal.graph_fingerprint == reverse.graph_fingerprint:
            raise CommissioningHostError(
                "graph_identity_replayed",
                "normal and reverse evidence require distinct live graphs",
            )
        walk = self._delay_walk_or_operation(target)
        if isinstance(walk, RegionCaptureOperation):
            return walk
        delay_graphs = [point.graph_fingerprint for point in walk.points]
        if (
            len(set(delay_graphs)) != len(delay_graphs)
            or normal.graph_fingerprint in delay_graphs
            or reverse.graph_fingerprint in delay_graphs
        ):
            raise CommissioningHostError(
                "graph_identity_replayed",
                "normal, reverse, and delay evidence require distinct live graphs",
            )
        region = RegionCommissioningEvidence(
            plan=self.plan,
            target=target,
            normal=normal,
            reverse=reverse,
            delay_walk=walk,
        )
        self.evidence_store.publish_region_commissioning_evidence(region)
        self._regions[target.fingerprint] = region
        return region

    def _advance(
        self,
    ) -> RegionCaptureOperation | CompleteCommissioningEvidence:
        regions: list[RegionCommissioningEvidence] = []
        for target in self.plan.targets:
            region = self._region_or_operation(target)
            if isinstance(region, RegionCaptureOperation):
                return region
            regions.append(region)
        complete = CompleteCommissioningEvidence(
            plan=self.plan, regions=tuple(regions)
        )
        artifact = self.evidence_store.publish_complete_commissioning_evidence(
            complete
        )
        reopened = self.evidence_store.reopen_complete_commissioning_evidence(
            run_id=self.run.run_id, artifact=artifact
        )
        if reopened != complete:
            raise CommissioningHostError(
                "complete_readback_mismatch", "complete evidence changed on reopen"
            )
        committed = self.run_store.transition(
            self.run,
            CommissioningTransition(
                from_state="protected",
                to_state="measured",
                evidence_kind="admitted_measurement_set",
                evidence_fingerprint=artifact.fingerprint,
            ),
        )
        if not committed:
            raise CommissioningHostError(
                "run_generation_stale", "complete evidence lost run ownership"
            )
        self._complete = reopened
        log_event(
            logger,
            "correction.active_commissioning_measurement_complete",
            session=self.run.session_id,
            run_id=self.run.run_id,
            owner_generation=self.run.owner_generation,
            plan_fingerprint=self.plan.fingerprint,
            evidence_fingerprint=reopened.fingerprint,
        )
        return reopened

    def next_operation(self) -> RegionCaptureOperation | None:
        """Return one exact next operation, or ``None`` after durable complete."""

        with self._lock:
            current_mutation = self._current_live_mutation()
            if (
                current_mutation is not None
                and current_mutation.status == "mutation_pending"
            ):
                raise CommissioningHostError(
                    "live_mutation_recovery_required",
                    "pending summed measurement must be recovered before issuing work",
                )
            if current_mutation is not None and current_mutation.status == "restored":
                self._recover_restored_exclusively()
            if not self._prepared:
                self.prepare()
            if self._complete is not None:
                return None
            self._require_current()
            state = self.run_store.lifecycle_state(self.run)
            if state not in {"unconfigured", "protected"}:
                raise CommissioningHostError(
                    "lifecycle_not_collecting",
                    f"commissioning evidence cannot progress from {state}",
                )
            advanced = self._advance()
            if not isinstance(advanced, RegionCaptureOperation):
                return None
            try:
                issuance = self.run_store.issue_live_mutation(
                    self.run,
                    purpose="summed_measurement",
                    operation_fingerprint=advanced.fingerprint,
                )
            except CommissioningRunError as exc:
                raise CommissioningHostError(
                    "execution_already_owned",
                    "summed measurement execution is already owned",
                ) from exc
            return replace(advanced, issuance_id=issuance.issuance_id)

    @staticmethod
    def _artifact_roles(capture: AdmittedRegionCapture) -> tuple[Any, ...]:
        return (
            capture.capture.raw_artifact,
            capture.capture.analysis_input_artifact,
            capture.capture.quality_artifact,
            capture.playback_artifact,
            capture.stimulus.artifact,
            capture.generation_artifact,
        )

    def _validate_capture(
        self,
        operation: RegionCaptureOperation,
        capture: AdmittedRegionCapture,
        prior: Sequence[AdmittedRegionCapture],
    ) -> None:
        target = operation.target
        expected_context_base = (
            target.context_base_fingerprint_for(operation.evidence_kind)
            if operation.evidence_kind != "delay_null"
            else delay_point_context_base_fingerprint(
                target,
                operation.null_walk_spec,  # type: ignore[arg-type]
                operation.relative_delay_us,  # type: ignore[arg-type]
                capture.graph_fingerprint,
            )
        )
        if (
            capture.authority != self.plan.authority
            or capture.plan_fingerprint != self.plan.fingerprint
            or capture.attempt != operation.attempt
            or capture.speaker_group_id != target.speaker_group_id
            or capture.region_id != target.region_id
            or capture.evidence_kind != operation.evidence_kind
            or capture.target_fingerprint != operation.target_fingerprint
            or capture.context_base_fingerprint != expected_context_base
            or capture.placement_fingerprint != operation.placement_fingerprint
        ):
            raise CommissioningHostError(
                "capture_operation_mismatch",
                "admitted capture does not equal the server-issued operation",
            )
        if prior and any(
            item.graph_fingerprint != capture.graph_fingerprint
            or item.context_base_fingerprint != capture.context_base_fingerprint
            or item.placement_fingerprint != capture.placement_fingerprint
            for item in prior
        ):
            raise CommissioningHostError(
                "capture_context_drift",
                "capture set changed graph, context, or placement",
            )
        prior_capture_ids = {item.capture.capture_id for item in prior}
        prior_admission_ids = {item.admission_id for item in prior}
        prior_raw_hashes = {item.capture.raw_artifact.sha256 for item in prior}
        prior_artifacts = {
            (artifact.relative_path, artifact.fingerprint)
            for item in prior
            for artifact in self._artifact_roles(item)
        }
        current_artifacts = {
            (artifact.relative_path, artifact.fingerprint)
            for artifact in self._artifact_roles(capture)
        }
        if (
            capture.capture.capture_id in prior_capture_ids
            or capture.admission_id in prior_admission_ids
            or capture.capture.raw_artifact.sha256 in prior_raw_hashes
            or prior_artifacts & current_artifacts
        ):
            raise CommissioningHostError(
                "capture_identity_replayed", "capture retry reused one-shot evidence"
            )

    def commit_capture(
        self,
        operation: RegionCaptureOperation,
        capture: AdmittedRegionCapture,
    ) -> AdmittedRegionCapture:
        """Persist one exact admitted result, then advance durable aggregates."""

        if not isinstance(operation, RegionCaptureOperation) or not isinstance(
            capture, AdmittedRegionCapture
        ):
            raise CommissioningHostError(
                "capture_invalid", "commit requires typed operation and capture"
            )
        with self._lock:
            if not self._prepared:
                self.prepare()
            self._require_current()
            state = self.run_store.lifecycle_state(self.run)
            if state not in {"unconfigured", "protected"}:
                raise CommissioningHostError(
                    "lifecycle_not_collecting",
                    f"commissioning evidence cannot progress from {state}",
                )
            # Re-prove the whole-program authority after runtime restoration and
            # immediately before any capture or aggregate can become durable.
            self._current_authority_snapshot()
            if not self.run_store.callback_is_current(operation.attempt):
                raise CommissioningHostError(
                    "run_generation_stale", "capture attempt is no longer current"
                )
            restored = self._restored_mutation(operation)
            expected = self._advance()
            if (
                not isinstance(expected, RegionCaptureOperation)
                or expected.fingerprint != operation.fingerprint
            ):
                raise CommissioningHostError(
                    "operation_stale", "capture is not the exact current operation"
                )
            prior = self.evidence_store.reopen_attempt_captures(
                operation.attempt.attempt_id
            )
            if len(prior) != operation.capture_ordinal:
                raise CommissioningHostError(
                    "operation_stale", "capture ordinal has already advanced"
                )
            self._validate_capture(operation, capture, prior)
            capture_path = attempt_capture_relative_path(
                operation.attempt.attempt_id,
                operation.capture_ordinal,
            )
            commit_marker = {
                "schema_version": 1,
                "kind": "jts_active_summed_measurement_capture_commit",
                "issuance_id": restored.issuance_id,
                "operation_fingerprint": operation.fingerprint,
                "capture_fingerprint": capture.fingerprint,
                "capture_relative_path": capture_path,
            }
            marker_artifact = self.evidence_store.publish_json_artifact(
                self._capture_commit_marker_path(restored),
                commit_marker,
            )
            if (
                self.evidence_store.reopen_json_artifact(marker_artifact)
                != commit_marker
            ):
                raise CommissioningHostError(
                    "capture_commit_marker_mismatch",
                    "capture commit marker changed on reopen",
                )
            artifact = self.evidence_store.publish_admitted_region_capture(
                capture, ordinal=operation.capture_ordinal
            )
            reopened = self.evidence_store.reopen_admitted_region_capture(artifact)
            if reopened != capture:
                raise CommissioningHostError(
                    "capture_readback_mismatch", "capture changed on typed reopen"
                )
            self.run_store.record_live_mutation_committed(
                self.run,
                restored,
                commit_evidence_fingerprint=artifact.fingerprint,
            )
            if not self.run_store.callback_is_current(operation.attempt):
                raise CommissioningHostError(
                    "run_generation_stale",
                    "capture completed after its run generation was replaced",
                )
            if state == "unconfigured":
                committed = self.run_store.transition(
                    self.run,
                    CommissioningTransition(
                        from_state="unconfigured",
                        to_state="protected",
                        evidence_kind="protection_evidence",
                        evidence_fingerprint=capture.generation_artifact.fingerprint,
                    ),
                )
                if not committed:
                    raise CommissioningHostError(
                        "run_generation_stale",
                        "fresh protection evidence lost current run ownership",
                    )
            log_event(
                logger,
                "correction.active_commissioning_capture_committed",
                session=self.run.session_id,
                run_id=self.run.run_id,
                owner_generation=self.run.owner_generation,
                attempt_id=operation.attempt.attempt_id,
                group=operation.target.speaker_group_id,
                region=operation.target.region_id,
                evidence_kind=operation.evidence_kind,
                capture_ordinal=operation.capture_ordinal,
                capture_fingerprint=reopened.fingerprint,
            )
            self._advance()
            return reopened

    async def capture_next_with_runtime(
        self,
        port: CommissioningRuntimePort,
        *,
        config_dir: str,
    ) -> AdmittedRegionCapture | None:
        """Run one operation through the bounded live transaction and commit it.

        This is the production join: the host chooses the semantic operation,
        graph, volume, and concrete admitted producer.  The only injected edge
        is bounded raw-WAV capture transport; its bytes must become typed,
        reopened evidence before any lifecycle progress is committed.
        """

        try:
            with self.run_store.claim_live_execution(self.run):
                return await self._capture_next_with_execution_claim(
                    port, config_dir=config_dir
                )
        except CommissioningRunConflict as exc:
            raise CommissioningHostError(
                "live_mutation_execution_in_progress",
                "another summed measurement caller still owns execution",
            ) from exc

    async def _capture_next_with_execution_claim(
        self,
        port: CommissioningRuntimePort,
        *,
        config_dir: str,
    ) -> AdmittedRegionCapture | None:
        """Execute while the run store's crash-released mutex is held."""

        await self._recover_pending_live_mutation(port, config_dir=config_dir)
        restored = self._current_live_mutation()
        if restored is not None and restored.status == "restored":
            self._recover_restored_capture_commit(restored)
        operation = self.next_operation()
        if operation is None:
            return None
        restored_by_caller: Callable[[], bool] = lambda: False
        try:
            if self._raw_capture_transport is None:
                raise CommissioningHostError(
                    "raw_capture_transport_unavailable",
                    "real summed capture transport is not composed in Wave 3",
                )
            request, snapshot = self._runtime_request(operation)
            baseline_fingerprint = NormalizedActiveRawIdentity(
                yaml.safe_load(request.normal_active_raw)
            ).active_raw_fingerprint

            def load_capture_authority() -> CurrentCaptureAuthority:
                current, current_raw = self._current_authority_snapshot()
                if (
                    NormalizedActiveRawIdentity(
                        yaml.safe_load(current_raw)
                    ).active_raw_fingerprint
                    != baseline_fingerprint
                ):
                    raise CommissioningHostError(
                        "fresh_authority_stale",
                        "applied baseline changed during summed capture",
                    )
                return CurrentCaptureAuthority(
                    safety_profile=current.safety_profile,
                    calibration=current.calibration,
                )

            producer = SummedCaptureProducer(
                authority=self.plan.authority,
                plan_fingerprint=self.plan.fingerprint,
                topology=snapshot.topology,
                evidence_store=self.evidence_store,
                load_current_authority=load_capture_authority,
                raw_transport=self._raw_capture_transport,
                alsa_device=DEFAULT_ALSA_DEVICE,
                playback_timeout_s=CROSSOVER_CAPTURE_PLAY_DEADLINE_S,
            )
            mutation_journal, restored_by_caller = self._runtime_mutation_journal(
                operation
            )
            result = await run_summed_capture(
                port,
                request,
                producer.callback_for(operation),
                topology=self.topology,
                mutation_journal=mutation_journal,
                config_dir=config_dir,
            )
        except CommissioningRuntimeFailure as exc:
            mutation = self._pending_live_mutation()
            if mutation is not None and (
                exc.side_effects.restore_succeeded is not True
                or exc.code == "restore_record_failed"
            ):
                self._block_live_state_unknown(mutation, reason=exc.code)
            else:
                self._abort_restored_execution(
                    operation,
                    failure_code=exc.code,
                    restored_by_caller=restored_by_caller(),
                )
                self._release_unstarted_execution(operation)
            raise
        except CommissioningRuntimeCancelled:
            self._abort_restored_execution(
                operation,
                failure_code="capture_cancelled",
                restored_by_caller=restored_by_caller(),
            )
            self._release_unstarted_execution(operation)
            raise
        except (
            CommissioningHostError,
            CommissioningRunError,
            SummedCaptureProducerError,
        ):
            self._abort_restored_execution(
                operation,
                failure_code="host_execution_failed",
                restored_by_caller=restored_by_caller(),
            )
            self._release_unstarted_execution(operation)
            raise
        except (ValueError, yaml.YAMLError) as exc:
            self._abort_restored_execution(
                operation,
                failure_code="host_execution_invalid",
                restored_by_caller=restored_by_caller(),
            )
            self._release_unstarted_execution(operation)
            raise CommissioningHostError(
                "host_execution_invalid",
                "server-owned summed execution input became invalid",
            ) from exc
        capture = result.capture.payload
        generation_proof = result.capture.generation.admission.protection_evidence
        if (
            not isinstance(capture, AdmittedRegionCapture)
            or capture.graph_fingerprint != result.graph_fingerprint
            or capture.admission_id != result.admission_id
            or capture.generation_artifact != result.capture.generation.artifact
            or capture.playback_artifact != result.capture.playback.artifact
            or capture.stimulus != result.capture.stimulus
            or capture.generation_admission
            != result.capture.generation.admission
            or capture.playback_admission != result.capture.playback.admission
            or generation_proof is None
            or generation_proof.evidence_fingerprint
            != capture.generation_protection_evidence_fingerprint
            or result.capture.protection_evidence.evidence_fingerprint
            != capture.playback_protection_evidence_fingerprint
        ):
            self._abort_restored_execution(
                operation,
                failure_code="runtime_capture_mismatch",
                restored_by_caller=restored_by_caller(),
            )
            raise CommissioningHostError(
                "runtime_capture_mismatch",
                "admitted capture does not equal the exact live transaction proof",
            )
        try:
            return self.commit_capture(operation, capture)
        except CommissioningHostError:
            self._abort_restored_execution(
                operation,
                failure_code="capture_commit_refused",
                restored_by_caller=restored_by_caller(),
            )
            raise

    def status(self) -> dict[str, Any]:
        """Return compact validated status; polling emits no events."""

        with self._lock:
            self._require_current()
            state = self.run_store.lifecycle_state(self.run)
            if state == "measured":
                complete = self._complete or self._recover_complete()
                if complete is None:
                    raise CommissioningHostError(
                        "complete_evidence_missing",
                        "measured lifecycle has no durable complete evidence",
                    )
                artifact = self.evidence_store.identify_artifact(
                    complete_relative_path(self.run.run_id)
                )
                self._require_measured_transition(artifact.fingerprint)
                self._complete = complete
            attempts = self.run_store.attempts(self.run)
            live_mutation = self._current_live_mutation()
            return {
                "schema_version": 1,
                "kind": "jts_active_commissioning_evidence_host_status",
                "session_id": self.run.session_id,
                "run_id": self.run.run_id,
                "owner_generation": self.run.owner_generation,
                "lifecycle_state": state,
                "plan_fingerprint": self.plan.fingerprint,
                "attempt_count": len(attempts),
                "complete": self._complete is not None,
                "capture_transport_configured": (
                    self._raw_capture_transport is not None
                ),
                "hardware_capture_status": "wave4_hardware_required",
                "live_mutation_status": (
                    live_mutation.status if live_mutation is not None else None
                ),
                "live_mutation_recovery_required": bool(
                    live_mutation is not None
                    and live_mutation.status in {"mutation_pending", "restored"}
                ),
            }
