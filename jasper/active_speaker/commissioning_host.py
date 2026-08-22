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
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.null_walk import (
    NullWalkSpec,
)
from jasper.output_topology import OutputTopology

from .baseline_profile import topology_config_fingerprint
from .commissioning_evidence import (
    CompleteCommissioningEvidence,
    DelayPointEvidence,
    DelayWalkEvidence,
    EvidenceKind,
    RegionCommissioningEvidence,
    RegionEvidencePlan,
    RegionEvidenceTarget,
    RegionGeometryAttestation,
    StationaryRegionEvidence,
    delay_point_target_fingerprint,
    evidence_attempt_target_id,
)
from .commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
    complete_relative_path,
)
from .commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningLiveMutation,
    CommissioningRunHandle,
    CommissioningRunStore,
)
from .measurement import active_driver_targets
from .profile import ActiveSpeakerPreset

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


def commissioning_program_key(plan: RegionEvidencePlan) -> tuple[Any, ...]:
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


    def _current_live_mutation(self) -> CommissioningLiveMutation | None:
        return self.run_store.current_live_mutation(self.run)


    def _recover_complete_anchor(self) -> CompleteCommissioningEvidence | None:
        """Reopen the typed status anchor without hashing every child WAV."""

        try:
            complete = (
                self.evidence_store.reopen_complete_commissioning_evidence_anchor(
                    run_id=self.run.run_id
                )
            )
        except CommissioningEvidenceStoreError as exc:
            if self._missing(exc):
                return None
            raise
        self._require_complete_program(complete)
        return complete

    def _require_complete_program(
        self,
        complete: CompleteCommissioningEvidence,
    ) -> None:
        if commissioning_program_key(complete.plan) != commissioning_program_key(
            self.plan
        ):
            raise CommissioningHostError(
                "complete_evidence_stale",
                "durable complete evidence does not equal the current program",
            )

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


    def status(self) -> dict[str, Any]:
        """Return compact validated status; polling emits no events."""

        with self._lock:
            self._require_current()
            state = self.run_store.lifecycle_state(self.run)
            complete_available = self._complete is not None
            if state == "measured":
                complete = self._complete or self._recover_complete_anchor()
                if complete is None:
                    raise CommissioningHostError(
                        "complete_evidence_missing",
                        "measured lifecycle has no durable complete evidence",
                    )
                artifact = self.evidence_store.identify_artifact(
                    complete_relative_path(self.run.run_id)
                )
                self._require_measured_transition(artifact.fingerprint)
                complete_available = True
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
                "complete": complete_available,
                "hardware_capture_status": "hardware_validation_required",
                "live_mutation_status": (
                    live_mutation.status if live_mutation is not None else None
                ),
                "live_mutation_recovery_required": bool(
                    live_mutation is not None
                    and live_mutation.status in {"mutation_pending", "restored"}
                ),
            }
