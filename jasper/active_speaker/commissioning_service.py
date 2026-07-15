# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Production composition for one real summed-region commissioning capture.

The durable host already owns operation order, graphs, admission, analysis,
restore, and lifecycle progress.  This module supplies only the product state
needed to construct that host: the exact current run/plan, an explicit signed
geometry attestation for every region, the calibrated fixed-axis placement,
and a CamillaDSP runtime port.  Browser and relay adapters never choose a
region, polarity, delay coordinate, graph, attempt, or capture ordinal.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from jasper.audio_hardware.dac import by_id as dac_profile_by_id
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.null_walk import NullWalkError
from jasper.log_event import log_event
from .alignment_walk import driver_delay_walk_spec
from .commissioning_capture_producer import RawCaptureTransport
from .commissioning_evidence import (
    REFERENCE_AXIS_GEOMETRY_ID,
    CompleteIsolatedDriverEvidence,
    RegionEvidencePlan,
    RegionEvidenceTarget,
    RegionGeometryAttestation,
)
from .commissioning_evidence_store import (
    EVIDENCE_ROOT,
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
    complete_relative_path,
    isolated_driver_evidence_relative_path,
)
from .commissioning_host import (
    CommissioningEvidenceHost,
    CommissioningHostAuthoritySnapshot,
    RegionCommissioningInputs,
    commissioning_program_key,
)
from .commissioning_isolated_producer import current_region_evidence_plan
from .commissioning_lifecycle import CommissioningTransition
from .commissioning_receipt import RequiredTargetPlan
from .commissioning_run import CommissioningRunHandle, CommissioningRunStore
from .driver_safety import evaluate_driver_safety_profile
from .measured_candidate import (
    CANDIDATE_ALGORITHM_ID,
    CANDIDATE_ALGORITHM_VERSION,
    MeasuredElectricalCandidate,
    MeasuredCandidateEvaluationError,
    evaluate_measured_candidate,
)
from .profile import required_driver_roles

GEOMETRY_ATTESTATION_KIND = "jts_active_region_geometry_attestation_source"
GEOMETRY_PROVENANCE_KIND: Literal["operator_attested"] = "operator_attested"
GEOMETRY_PROVENANCE_ID = "operator-signed-acoustic-path-v1"
REGION_PLACEMENT_POLICY_ID = "summed_reference_axis_v1"
CANDIDATE_FAILURE_KIND = "jts_active_measured_candidate_failure"

logger = logging.getLogger(__name__)


class CommissioningServiceError(ValueError):
    """The current production commissioning composition cannot progress."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


CurrentAuthorityLoader = Callable[[], CommissioningHostAuthoritySnapshot]


def _missing(error: CommissioningEvidenceStoreError) -> bool:
    return error.code == CommissioningEvidenceStoreErrorCode.MISSING


def _finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommissioningServiceError(
            "geometry_invalid", f"{field_name} must be a finite number"
        )
    result = float(value)
    if not math.isfinite(result):
        raise CommissioningServiceError(
            "geometry_invalid", f"{field_name} must be a finite number"
        )
    return 0.0 if result == 0.0 else result


def _geometry_source_path(
    run: CommissioningRunHandle,
    target: RegionEvidenceTarget,
) -> str:
    return (
        f"runs/{run.run_id}/generations/{run.owner_generation}/regions/"
        f"{target.fingerprint}/geometry-attestation.json"
    )


def _geometry_artifact_relative_path(
    run: CommissioningRunHandle,
    target: RegionEvidenceTarget,
) -> str:
    return f"{EVIDENCE_ROOT}/artifacts/{_geometry_source_path(run, target)}"


def _candidate_source_path(run: CommissioningRunHandle) -> str:
    return (
        f"runs/{run.run_id}/generations/{run.owner_generation}/"
        "measured-candidate.json"
    )


def _candidate_artifact_relative_path(run: CommissioningRunHandle) -> str:
    return f"{EVIDENCE_ROOT}/artifacts/{_candidate_source_path(run)}"


def _candidate_failure_source_path(run: CommissioningRunHandle) -> str:
    return (
        f"runs/{run.run_id}/generations/{run.owner_generation}/"
        "measured-candidate-failure.json"
    )


def _candidate_failure_artifact_relative_path(
    run: CommissioningRunHandle,
) -> str:
    return f"{EVIDENCE_ROOT}/artifacts/{_candidate_failure_source_path(run)}"


def _placement_fingerprint(
    plan: RegionEvidencePlan,
    target: RegionEvidenceTarget,
    geometry: RegionGeometryAttestation,
) -> str:
    return json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_region_fixed_axis_placement",
            "plan_fingerprint": plan.fingerprint,
            "target_fingerprint": target.fingerprint,
            "speaker_group_id": target.speaker_group_id,
            "region_id": target.region_id,
            "geometry_id": REFERENCE_AXIS_GEOMETRY_ID,
            "placement_policy_id": REGION_PLACEMENT_POLICY_ID,
            "geometry_attestation_fingerprint": geometry.fingerprint,
        }
    )


@dataclass(frozen=True, slots=True)
class _CurrentComposition:
    authority: CommissioningHostAuthoritySnapshot
    plan: RegionEvidencePlan
    isolated: CompleteIsolatedDriverEvidence


class CommissioningCaptureService:
    """One exact run's product-owned geometry, progress, and live host join."""

    def __init__(
        self,
        *,
        run: CommissioningRunHandle,
        run_store: CommissioningRunStore,
        evidence_store: CommissioningEvidenceStore,
        load_current_authority: CurrentAuthorityLoader,
    ) -> None:
        if not isinstance(run, CommissioningRunHandle):
            raise CommissioningServiceError("run_unavailable", "run is unavailable")
        if not isinstance(run_store, CommissioningRunStore):
            raise TypeError("run_store must be CommissioningRunStore")
        if not isinstance(evidence_store, CommissioningEvidenceStore):
            raise TypeError("evidence_store must be CommissioningEvidenceStore")
        if evidence_store.session_id != run.session_id:
            raise CommissioningServiceError(
                "run_store_mismatch", "evidence store does not belong to the run"
            )
        if not callable(load_current_authority):
            raise TypeError("load_current_authority must be callable")
        self.run = run
        self.run_store = run_store
        self.evidence_store = evidence_store
        self.load_current_authority = load_current_authority

    def _current(
        self,
        *,
        verify_child_evidence: bool = True,
    ) -> _CurrentComposition:
        if not self.run_store.callback_is_current(self.run):
            raise CommissioningServiceError(
                "run_generation_stale", "commissioning run ownership changed"
            )
        authority = self.load_current_authority()
        if not isinstance(authority, CommissioningHostAuthoritySnapshot):
            raise CommissioningServiceError(
                "authority_unavailable", "current product authority is unavailable"
            )
        dac_profile = dac_profile_by_id(authority.topology.hardware.device_id)
        if (
            authority.preset.way_count != 2
            or dac_profile is None
            or not dac_profile.supports_active_crossover_commissioning
        ):
            raise CommissioningServiceError(
                "launch_scope_unsupported",
                "automatic commissioning currently requires a DAC8x active 2-way speaker",
            )
        safety = evaluate_driver_safety_profile(
            authority.safety_profile, authority.topology
        )
        if not safety.confirmed_and_current or safety.profile_fingerprint is None:
            raise CommissioningServiceError(
                "authority_stale", "driver safety authority is no longer current"
            )
        try:
            plan = current_region_evidence_plan(
                topology=authority.topology,
                preset=authority.preset,
                comparison_set=authority.comparison_set,
                applied_profile=authority.applied_profile,
                calibration_id=authority.calibration_id,
                calibration=authority.calibration,
                protected_safety_profile_fingerprint=safety.profile_fingerprint,
                run=self.run,
                evidence_store=self.evidence_store,
            )
            reopen_isolated = (
                self.evidence_store.reopen_complete_isolated_driver_evidence
                if verify_child_evidence
                else self.evidence_store.reopen_complete_isolated_driver_evidence_anchor
            )
            isolated = reopen_isolated(run_id=self.run.run_id)
        except CommissioningEvidenceStoreError as exc:
            if _missing(exc):
                raise CommissioningServiceError(
                    "isolated_evidence_incomplete",
                    "complete fixed-axis driver evidence is required first",
                ) from exc
            raise
        if commissioning_program_key(isolated.plan) != commissioning_program_key(plan):
            raise CommissioningServiceError(
                "isolated_evidence_stale",
                "fixed-axis driver evidence does not equal the current program",
            )
        return _CurrentComposition(authority, plan, isolated)

    def _geometry_payload(
        self,
        plan: RegionEvidencePlan,
        target: RegionEvidenceTarget,
        *,
        signed_path_difference_m: float,
    ) -> tuple[dict[str, Any], Any]:
        spec = driver_delay_walk_spec(
            crossover_fc_hz=target.electrical_fc_hz,
            positive_delay_target_role=target.upper_role,
            negative_delay_target_role=target.lower_role,
            signed_acoustic_path_difference_m=signed_path_difference_m,
        )
        try:
            # The write-once attestation must be runnable before it becomes
            # durable.  The coarse schedule contains both fine-grid endpoints,
            # so materializing it proves every later coordinate stays inside
            # the existing CamillaDSP 20 ms delay ceiling.
            spec.coarse_candidate_delays_us()
        except NullWalkError as exc:
            raise CommissioningServiceError(
                "geometry_out_of_bounds",
                "signed geometry exceeds the bounded crossover delay range",
            ) from exc
        payload = {
            "schema_version": 1,
            "kind": GEOMETRY_ATTESTATION_KIND,
            "plan_fingerprint": plan.fingerprint,
            "target_fingerprint": target.fingerprint,
            "speaker_group_id": target.speaker_group_id,
            "region_id": target.region_id,
            "lower_role": target.lower_role,
            "upper_role": target.upper_role,
            "signed_path_semantics": (
                "lower_driver_path_minus_upper_driver_path"
            ),
            "signed_acoustic_path_difference_m": signed_path_difference_m,
            "signed_geometry_seed_us": spec.geometry_seed_us,
            "provenance_kind": GEOMETRY_PROVENANCE_KIND,
            "provenance_id": GEOMETRY_PROVENANCE_ID,
            "delay_walk_spec": spec.to_dict(),
        }
        return payload, spec

    def _reopen_geometry(
        self,
        plan: RegionEvidencePlan,
        target: RegionEvidenceTarget,
    ) -> tuple[RegionCommissioningInputs, float] | None:
        path = _geometry_artifact_relative_path(self.run, target)
        try:
            artifact = self.evidence_store.identify_artifact(path)
        except CommissioningEvidenceStoreError as exc:
            if _missing(exc):
                return None
            raise
        payload = self.evidence_store.reopen_json_artifact(artifact)
        expected_keys = {
            "schema_version",
            "kind",
            "plan_fingerprint",
            "target_fingerprint",
            "speaker_group_id",
            "region_id",
            "lower_role",
            "upper_role",
            "signed_path_semantics",
            "signed_acoustic_path_difference_m",
            "signed_geometry_seed_us",
            "provenance_kind",
            "provenance_id",
            "delay_walk_spec",
        }
        signed_path = _finite_number(
            payload.get("signed_acoustic_path_difference_m"),
            field_name="signed_acoustic_path_difference_m",
        )
        expected, spec = self._geometry_payload(
            plan, target, signed_path_difference_m=signed_path
        )
        if set(payload) != expected_keys or payload != expected:
            raise CommissioningServiceError(
                "geometry_stale",
                "region geometry does not equal the current plan and delay policy",
            )
        geometry = RegionGeometryAttestation(
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            region_target_fingerprint=target.fingerprint,
            signed_geometry_seed_us=spec.geometry_seed_us,
            provenance_kind=GEOMETRY_PROVENANCE_KIND,
            provenance_id=GEOMETRY_PROVENANCE_ID,
            attestation_artifact=artifact,
        )
        return (
            RegionCommissioningInputs(
                target_fingerprint=target.fingerprint,
                placement_fingerprint=_placement_fingerprint(plan, target, geometry),
                geometry=geometry,
                null_walk_spec=spec,
            ),
            signed_path,
        )

    def _region_inputs(
        self, plan: RegionEvidencePlan
    ) -> tuple[RegionCommissioningInputs, ...] | None:
        values: list[RegionCommissioningInputs] = []
        for target in plan.targets:
            reopened = self._reopen_geometry(plan, target)
            if reopened is None:
                return None
            values.append(reopened[0])
        return tuple(values)

    def _required_target_plan(
        self,
        current: _CurrentComposition,
    ) -> RequiredTargetPlan:
        inputs = self._region_inputs(current.plan)
        if inputs is None:
            raise CommissioningServiceError(
                "geometry_incomplete",
                "fixed-axis geometry is required before candidate apply",
            )
        placements: dict[str, str] = {}
        by_target = {item.target_fingerprint: item for item in inputs}
        for target in current.plan.targets:
            item = by_target[target.fingerprint]
            previous = placements.setdefault(
                target.speaker_group_id, item.placement_fingerprint
            )
            if previous != item.placement_fingerprint:
                raise CommissioningServiceError(
                    "launch_scope_unsupported",
                    "one fixed-axis placement per speaker group is required",
                )
        return RequiredTargetPlan.from_topology(
            current.authority.topology,
            placement_fingerprints=placements,
        )

    def _reopen_candidate(
        self,
        current: _CurrentComposition,
        *,
        require_transition: bool,
    ) -> tuple[MeasuredElectricalCandidate, ArtifactIdentity]:
        artifact = self.evidence_store.identify_artifact(
            _candidate_artifact_relative_path(self.run)
        )
        candidate = MeasuredElectricalCandidate.from_mapping(
            self.evidence_store.reopen_json_artifact(artifact)
        )
        isolated_artifact = self.evidence_store.identify_artifact(
            isolated_driver_evidence_relative_path(self.run.run_id)
        )
        summed_artifact = self.evidence_store.identify_artifact(
            complete_relative_path(self.run.run_id)
        )
        if (
            candidate.run != self.run
            or candidate.plan_fingerprint != current.plan.fingerprint
            or candidate.source_preset != current.authority.preset
            or candidate.isolated_evidence_artifact != isolated_artifact
            or candidate.summed_evidence_artifact != summed_artifact
        ):
            raise CommissioningServiceError(
                "candidate_stale",
                "measured candidate does not equal the current commissioning program",
            )
        if require_transition:
            transition = self.run_store.lifecycle_transition(self.run)
            if (
                transition is None
                or transition.to_state != "candidate_ready"
                or transition.evidence_kind != "candidate_artifact"
                or transition.evidence_fingerprint != artifact.fingerprint
            ):
                raise CommissioningServiceError(
                    "candidate_transition_stale",
                    "candidate lifecycle does not name the exact persisted artifact",
                )
        return candidate, artifact

    def _candidate_failure_payload(
        self,
        current: _CurrentComposition,
        *,
        isolated_artifact: ArtifactIdentity,
        summed_artifact: ArtifactIdentity,
        refusal_code: str,
        refusal_detail: str,
    ) -> dict[str, Any]:
        if not refusal_code or not refusal_detail:
            raise CommissioningServiceError(
                "candidate_failure_invalid",
                "candidate refusal must name its exact reason",
            )
        return {
            "schema_version": 1,
            "kind": CANDIDATE_FAILURE_KIND,
            "session_id": self.run.session_id,
            "session_fingerprint": self.run.session_fingerprint,
            "run_id": self.run.run_id,
            "owner_generation": self.run.owner_generation,
            "plan_fingerprint": current.plan.fingerprint,
            "isolated_evidence_artifact": isolated_artifact.to_dict(),
            "summed_evidence_artifact": summed_artifact.to_dict(),
            "evaluator": {
                "id": CANDIDATE_ALGORITHM_ID,
                "version": CANDIDATE_ALGORITHM_VERSION,
            },
            "refusal_code": refusal_code,
            "refusal_detail": refusal_detail,
        }

    def _reopen_candidate_failure(
        self,
        current: _CurrentComposition,
        *,
        require_transition: bool,
    ) -> tuple[dict[str, Any], ArtifactIdentity]:
        artifact = self.evidence_store.identify_artifact(
            _candidate_failure_artifact_relative_path(self.run)
        )
        payload = self.evidence_store.reopen_json_artifact(artifact)
        expected_keys = {
            "schema_version",
            "kind",
            "session_id",
            "session_fingerprint",
            "run_id",
            "owner_generation",
            "plan_fingerprint",
            "isolated_evidence_artifact",
            "summed_evidence_artifact",
            "evaluator",
            "refusal_code",
            "refusal_detail",
        }
        refusal_code = payload.get("refusal_code")
        refusal_detail = payload.get("refusal_detail")
        if (
            set(payload) != expected_keys
            or not isinstance(refusal_code, str)
            or not isinstance(refusal_detail, str)
        ):
            raise CommissioningServiceError(
                "candidate_failure_stale",
                "candidate refusal evidence is malformed",
            )
        isolated_artifact = self.evidence_store.identify_artifact(
            isolated_driver_evidence_relative_path(self.run.run_id)
        )
        summed_artifact = self.evidence_store.identify_artifact(
            complete_relative_path(self.run.run_id)
        )
        expected = self._candidate_failure_payload(
            current,
            isolated_artifact=isolated_artifact,
            summed_artifact=summed_artifact,
            refusal_code=refusal_code,
            refusal_detail=refusal_detail,
        )
        if payload != expected:
            raise CommissioningServiceError(
                "candidate_failure_stale",
                "candidate refusal does not equal the current commissioning program",
            )
        if require_transition:
            transition = self.run_store.lifecycle_transition(self.run)
            if (
                transition is None
                or transition.to_state != "blocked"
                or transition.evidence_kind != "failure_evidence"
                or transition.evidence_fingerprint != artifact.fingerprint
                or transition.failure_code != "candidate_scoring_failed"
            ):
                raise CommissioningServiceError(
                    "candidate_failure_transition_stale",
                    "candidate refusal lifecycle does not name the exact evidence",
                )
        return payload, artifact

    def _block_candidate_refusal(
        self,
        current: _CurrentComposition,
        *,
        isolated_artifact: ArtifactIdentity,
        summed_artifact: ArtifactIdentity,
        error: MeasuredCandidateEvaluationError,
    ) -> None:
        payload = self._candidate_failure_payload(
            current,
            isolated_artifact=isolated_artifact,
            summed_artifact=summed_artifact,
            refusal_code=error.code,
            refusal_detail=error.detail,
        )
        artifact = self.evidence_store.publish_json_artifact(
            _candidate_failure_source_path(self.run), payload
        )
        if self.evidence_store.reopen_json_artifact(artifact) != payload:
            raise CommissioningServiceError(
                "candidate_failure_readback_mismatch",
                "candidate refusal evidence changed on exact readback",
            )
        committed = self.run_store.transition(
            self.run,
            CommissioningTransition(
                from_state="measured",
                to_state="blocked",
                evidence_kind="failure_evidence",
                evidence_fingerprint=artifact.fingerprint,
                failure_code="candidate_scoring_failed",
            ),
        )
        if not committed:
            raise CommissioningServiceError(
                "run_generation_stale",
                "candidate refusal lost current run ownership",
            )
        log_event(
            logger,
            "correction.active_commissioning_candidate_refused",
            session=self.run.session_id,
            run_id=self.run.run_id,
            owner_generation=self.run.owner_generation,
            plan_fingerprint=current.plan.fingerprint,
            refusal_code=error.code,
            failure_artifact_fingerprint=artifact.fingerprint,
            isolated_artifact_fingerprint=isolated_artifact.fingerprint,
            summed_artifact_fingerprint=summed_artifact.fingerprint,
        )

    @staticmethod
    def _candidate_review(
        candidate: MeasuredElectricalCandidate,
        artifact: ArtifactIdentity,
    ) -> dict[str, Any]:
        preset = candidate.source_preset
        roles = required_driver_roles(preset.way_count)
        corrections = candidate.driver_corrections()
        return {
            "fingerprint": candidate.fingerprint,
            "artifact_fingerprint": artifact.fingerprint,
            "preset_id": preset.preset_id,
            "preset_name": preset.name,
            "retained_crossover_regions": [
                {
                    "region_id": region.id,
                    "lower_role": region.lower_driver,
                    "upper_role": region.upper_driver,
                    "fc_hz": region.fc_hz,
                    "filter_family": region.target_type,
                    "order": region.order,
                    "lower_polarity": region.lower_polarity,
                    "upper_polarity": region.upper_polarity,
                    "polarity_evidence": "normal_retained_by_reverse_null",
                }
                for region in preset.crossover_regions
            ],
            "drivers": [
                {
                    "role": role,
                    "attenuation_db": corrections[role]["gain_db"],
                    "delay_ms": corrections[role]["delay_ms"],
                    "polarity": (
                        "inverted"
                        if corrections[role]["inverted"]
                        else "non-inverted"
                    ),
                }
                for role in roles
            ],
            "evidence": {
                "plan_fingerprint": candidate.plan_fingerprint,
                "isolated_artifact": candidate.isolated_evidence_artifact.to_dict(),
                "summed_artifact": candidate.summed_evidence_artifact.to_dict(),
                "algorithm_id": CANDIDATE_ALGORITHM_ID,
                "algorithm_version": CANDIDATE_ALGORITHM_VERSION,
            },
        }

    def publish_candidate(self) -> dict[str, Any]:
        """Evaluate, persist, reopen, and bind one exact measured candidate."""

        current = self._current()
        lifecycle_state = self.run_store.lifecycle_state(self.run)
        if lifecycle_state == "candidate_ready":
            candidate, artifact = self._reopen_candidate(
                current, require_transition=True
            )
            return self._candidate_review(candidate, artifact)
        if lifecycle_state != "measured":
            raise CommissioningServiceError(
                "candidate_not_measured",
                f"candidate evaluation requires measured, not {lifecycle_state}",
            )
        host_status = self._host(
            current,
            raw_capture_transport=None,
        ).status()
        if host_status.get("complete") is not True:
            raise CommissioningServiceError(
                "complete_evidence_unavailable",
                "measured lifecycle has no exact complete evidence",
            )
        isolated_artifact = self.evidence_store.identify_artifact(
            isolated_driver_evidence_relative_path(self.run.run_id)
        )
        summed_artifact = self.evidence_store.identify_artifact(
            complete_relative_path(self.run.run_id)
        )
        try:
            candidate = evaluate_measured_candidate(
                store=self.evidence_store,
                run=self.run,
                reviewed_preset=current.authority.preset,
                isolated_evidence_artifact=isolated_artifact,
                summed_evidence_artifact=summed_artifact,
            )
        except MeasuredCandidateEvaluationError as exc:
            self._block_candidate_refusal(
                current,
                isolated_artifact=isolated_artifact,
                summed_artifact=summed_artifact,
                error=exc,
            )
            raise CommissioningServiceError(
                "candidate_scoring_failed",
                "exact measured evidence could not authorize a candidate",
            ) from exc
        artifact = self.evidence_store.publish_json_artifact(
            _candidate_source_path(self.run), candidate.to_dict()
        )
        reopened, reopened_artifact = self._reopen_candidate(
            current, require_transition=False
        )
        if reopened != candidate or reopened_artifact != artifact:
            raise CommissioningServiceError(
                "candidate_readback_mismatch",
                "measured candidate changed on exact readback",
            )
        committed = self.run_store.transition(
            self.run,
            CommissioningTransition(
                from_state="measured",
                to_state="candidate_ready",
                evidence_kind="candidate_artifact",
                evidence_fingerprint=artifact.fingerprint,
            ),
        )
        if not committed:
            raise CommissioningServiceError(
                "run_generation_stale",
                "measured candidate lost current run ownership",
            )
        log_event(
            logger,
            "correction.active_commissioning_candidate_ready",
            session=self.run.session_id,
            run_id=self.run.run_id,
            owner_generation=self.run.owner_generation,
            plan_fingerprint=current.plan.fingerprint,
            candidate_fingerprint=candidate.fingerprint,
            candidate_artifact_fingerprint=artifact.fingerprint,
            isolated_artifact_fingerprint=isolated_artifact.fingerprint,
            summed_artifact_fingerprint=summed_artifact.fingerprint,
        )
        return self._candidate_review(candidate, artifact)

    async def apply_candidate(
        self,
        *,
        expected_candidate_fingerprint: str,
        runtime_port: Any,
        load_config_path: Callable[[str], Any],
    ) -> dict[str, Any]:
        """Apply only the exact reviewed candidate through the Active owner."""

        from .commissioning_apply import apply_measured_candidate
        from .commissioning_runtime import CommissioningRuntimePort
        from .crossover_preview import load_crossover_preview
        from .design_draft import load_design_draft
        from .measurement import load_measurement_state

        if not isinstance(runtime_port, CommissioningRuntimePort):
            raise TypeError("runtime_port must be CommissioningRuntimePort")
        expected = expected_candidate_fingerprint.strip()
        if not expected:
            raise CommissioningServiceError(
                "candidate_review_required",
                "the reviewed candidate fingerprint is required",
            )
        current = self._current()
        lifecycle = self.run_store.lifecycle_state(self.run)
        if lifecycle == "rolled_back":
            candidate, artifact = self._reopen_candidate(
                current, require_transition=False
            )
            if not self.run_store.transition(
                self.run,
                CommissioningTransition(
                    from_state="rolled_back",
                    to_state="candidate_ready",
                    evidence_kind="candidate_artifact",
                    evidence_fingerprint=artifact.fingerprint,
                ),
            ):
                raise CommissioningServiceError(
                    "run_generation_stale", "candidate retry lost run ownership"
                )
        elif lifecycle in {"candidate_ready", "applied_unverified"}:
            candidate, _artifact = self._reopen_candidate(
                current,
                require_transition=lifecycle == "candidate_ready",
            )
        else:
            raise CommissioningServiceError(
                "candidate_not_ready",
                f"candidate apply requires candidate_ready, not {lifecycle}",
            )
        if candidate.fingerprint != expected:
            raise CommissioningServiceError(
                "candidate_review_stale",
                "the candidate changed after review; refresh before applying",
            )
        target_plan = self._required_target_plan(current)
        safety = evaluate_driver_safety_profile(
            current.authority.safety_profile,
            current.authority.topology,
        )
        if not safety.confirmed_and_current or safety.profile_fingerprint is None:
            raise CommissioningServiceError(
                "authority_stale", "driver safety authority is no longer current"
            )
        topology = current.authority.topology
        draft = load_design_draft(topology=topology)
        preview = load_crossover_preview(current_design_draft=draft)
        measurements = load_measurement_state(topology)

        def verify_current() -> None:
            refreshed = self._current()
            reopened, _ = self._reopen_candidate(
                refreshed,
                require_transition=(
                    self.run_store.lifecycle_state(self.run) == "candidate_ready"
                ),
            )
            if (
                refreshed != current
                or reopened != candidate
                or self._required_target_plan(refreshed) != target_plan
            ):
                raise CommissioningServiceError(
                    "candidate_review_stale",
                    "candidate authority changed before writer-lock admission",
                )

        return await apply_measured_candidate(
            run=self.run,
            run_store=self.run_store,
            store=self.evidence_store,
            candidate=candidate,
            target_plan=target_plan,
            safety_profile_fingerprint=safety.profile_fingerprint,
            topology=topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            runtime_port=runtime_port,
            load_config_path=load_config_path,
            verify_current=verify_current,
        )

    async def restore_candidate(
        self,
        *,
        runtime_port: Any,
        load_config_path: Callable[[str], Any],
    ) -> dict[str, Any]:
        """Restore a crash-interrupted candidate apply from its exact pointer."""

        from .commissioning_apply import restore_pending_candidate_apply
        from .commissioning_runtime import CommissioningRuntimePort

        if not isinstance(runtime_port, CommissioningRuntimePort):
            raise TypeError("runtime_port must be CommissioningRuntimePort")
        return await restore_pending_candidate_apply(
            run=self.run,
            run_store=self.run_store,
            store=self.evidence_store,
            runtime_port=runtime_port,
            load_config_path=load_config_path,
        )

    def attest_geometry(
        self,
        *,
        expected_target_fingerprint: str,
        signed_acoustic_path_difference_mm: Any,
    ) -> dict[str, Any]:
        """Persist one explicit signed geometry value for a server plan target."""

        current = self._current()
        target = next(
            (
                item
                for item in current.plan.targets
                if item.fingerprint == expected_target_fingerprint
            ),
            None,
        )
        if target is None:
            raise CommissioningServiceError(
                "geometry_target_stale",
                "the region changed; refresh before confirming its geometry",
            )
        first_missing = next(
            (
                item
                for item in current.plan.targets
                if self._reopen_geometry(current.plan, item) is None
            ),
            None,
        )
        existing = self._reopen_geometry(current.plan, target)
        signed_mm = _finite_number(
            signed_acoustic_path_difference_mm,
            field_name="signed_acoustic_path_difference_mm",
        )
        signed_m = signed_mm / 1000.0
        if existing is not None:
            if not math.isclose(existing[1], signed_m, rel_tol=0.0, abs_tol=1e-12):
                raise CommissioningServiceError(
                    "geometry_already_attested",
                    "this run already has a different signed geometry attestation",
                )
            return {
                "status": "accepted",
                "target_fingerprint": target.fingerprint,
                "geometry_fingerprint": existing[0].geometry.fingerprint,
                "already_present": True,
            }
        if first_missing != target:
            raise CommissioningServiceError(
                "geometry_target_not_current",
                "confirm the server's current crossover region first",
            )
        payload, _spec = self._geometry_payload(
            current.plan,
            target,
            signed_path_difference_m=signed_m,
        )
        self.evidence_store.publish_json_artifact(
            _geometry_source_path(self.run, target), payload
        )
        reopened = self._reopen_geometry(current.plan, target)
        if reopened is None:
            raise CommissioningServiceError(
                "geometry_readback_failed", "geometry did not reopen after persistence"
            )
        log_event(
            logger,
            "active_speaker.commissioning_geometry_attested",
            run_id=self.run.run_id,
            owner_generation=self.run.owner_generation,
            group=target.speaker_group_id,
            region=target.region_id,
            target_fingerprint=target.fingerprint,
            geometry_fingerprint=reopened[0].geometry.fingerprint,
        )
        return {
            "status": "accepted",
            "target_fingerprint": target.fingerprint,
            "geometry_fingerprint": reopened[0].geometry.fingerprint,
            "already_present": False,
        }

    def status(self) -> dict[str, Any]:
        """Return one current state without reserving attempts or live mutations."""

        from .commissioning_apply import APPLY_PURPOSE

        if not self.run_store.callback_is_current(self.run):
            raise CommissioningServiceError(
                "run_generation_stale", "commissioning run ownership changed"
            )
        lifecycle_state = self.run_store.lifecycle_state(self.run)
        live_mutation = self.run_store.current_live_mutation(self.run)
        if (
            live_mutation is not None
            and live_mutation.purpose == APPLY_PURPOSE
            and live_mutation.status in {"mutation_pending", "restored", "aborted"}
            and lifecycle_state in {
                "candidate_ready",
                "blocked_live_state_unknown",
            }
        ):
            return {
                "schema_version": 1,
                "kind": "jts_active_region_commissioning_status",
                "status": "restore_required",
                "run_id": self.run.run_id,
                "owner_generation": self.run.owner_generation,
                "lifecycle_state": lifecycle_state,
                "plan_fingerprint": None,
                "isolated_evidence_fingerprint": None,
                "geometry": [],
                "next_geometry": None,
                "next_capture": None,
                "candidate": None,
                "candidate_failure": None,
                "applied_candidate": None,
                "live_mutation": {
                    "status": live_mutation.status,
                    "purpose": live_mutation.purpose,
                    "issuance_id": live_mutation.issuance_id,
                },
                "detail": (
                    "The live DSP outcome is uncertain. Restore the exact "
                    "previous crossover before continuing."
                ),
            }
        current = self._current(verify_child_evidence=False)
        geometry_rows: list[dict[str, Any]] = []
        for target in current.plan.targets:
            reopened = self._reopen_geometry(current.plan, target)
            geometry_rows.append(
                {
                    "speaker_group_id": target.speaker_group_id,
                    "region_id": target.region_id,
                    "target_fingerprint": target.fingerprint,
                    "fc_hz": target.electrical_fc_hz,
                    "lower_role": target.lower_role,
                    "upper_role": target.upper_role,
                    "attested": reopened is not None,
                    "signed_acoustic_path_difference_mm": (
                        reopened[1] * 1000.0 if reopened is not None else None
                    ),
                }
            )
        missing_geometry = next(
            (item for item in geometry_rows if not item["attested"]), None
        )
        candidate_review = None
        candidate_failure = None
        applied_candidate = None
        if lifecycle_state == "measured":
            # Lifecycle is not a second evidence authority.  Reuse the host's
            # exact complete-artifact reopen + transition-fingerprint check
            # before presenting this run as measured at the Active boundary.
            host_status = self._host(
                current,
                raw_capture_transport=None,
            ).status()
            if host_status.get("complete") is not True:
                raise CommissioningServiceError(
                    "complete_evidence_unavailable",
                    "measured lifecycle has no exact complete evidence",
                )
            status = "measured"
        elif lifecycle_state == "candidate_ready":
            candidate, artifact = self._reopen_candidate(
                current, require_transition=True
            )
            candidate_review = self._candidate_review(candidate, artifact)
            if live_mutation is not None and live_mutation.status == "retained":
                status = "apply_finalization_required"
            elif live_mutation is not None and live_mutation.status in {
                "mutation_pending",
                "restored",
            }:
                status = "restore_required"
            else:
                status = "candidate_ready"
        elif lifecycle_state == "applied_unverified":
            from .commissioning_apply import reopen_applied_candidate_proof

            candidate, artifact = self._reopen_candidate(
                current, require_transition=False
            )
            candidate_review = self._candidate_review(candidate, artifact)
            if live_mutation is None or live_mutation.status != "retained":
                raise CommissioningServiceError(
                    "applied_proof_stale",
                    "applied lifecycle has no retained live-mutation proof",
                )
            safety = evaluate_driver_safety_profile(
                current.authority.safety_profile,
                current.authority.topology,
            )
            if not safety.confirmed_and_current or safety.profile_fingerprint is None:
                raise CommissioningServiceError(
                    "authority_stale", "driver safety authority is no longer current"
                )
            proof, proof_artifact = reopen_applied_candidate_proof(
                store=self.evidence_store,
                run=self.run,
                mutation=live_mutation,
                candidate=candidate,
                target_plan=self._required_target_plan(current),
                safety_profile_fingerprint=safety.profile_fingerprint,
            )
            applied_candidate = {
                "proof_fingerprint": proof.fingerprint,
                "artifact_fingerprint": proof_artifact.fingerprint,
                "candidate_fingerprint": proof.candidate_fingerprint,
                "target_plan_fingerprint": proof.target_plan_fingerprint,
                "fresh_graph_fingerprint": (
                    proof.observed_fresh_readback_graph.fingerprint
                ),
            }
            status = "applied_unverified"
        elif lifecycle_state == "rolled_back":
            candidate, artifact = self._reopen_candidate(
                current, require_transition=False
            )
            candidate_review = self._candidate_review(candidate, artifact)
            status = "apply_rolled_back"
        elif lifecycle_state == "blocked_live_state_unknown":
            status = "restore_required"
        elif lifecycle_state == "blocked":
            transition = self.run_store.lifecycle_transition(self.run)
            if (
                transition is not None
                and transition.failure_code == "candidate_scoring_failed"
            ):
                failure, artifact = self._reopen_candidate_failure(
                    current, require_transition=True
                )
                candidate_failure = {
                    "artifact_fingerprint": artifact.fingerprint,
                    "reason": failure["refusal_code"],
                    "detail": failure["refusal_detail"],
                    "isolated_artifact": failure[
                        "isolated_evidence_artifact"
                    ],
                    "summed_artifact": failure[
                        "summed_evidence_artifact"
                    ],
                    "evaluator": failure["evaluator"],
                }
                status = "candidate_refused"
            else:
                status = "unavailable"
        elif missing_geometry is not None:
            status = "needs_geometry"
        elif lifecycle_state in {"unconfigured", "protected"}:
            status = "collecting"
        else:
            status = "unavailable"
        next_capture = (
            {
                "evidence_kind": "server_selected",
            }
            if status == "collecting"
            else None
        )
        return {
            "schema_version": 1,
            "kind": "jts_active_region_commissioning_status",
            "status": status,
            "run_id": self.run.run_id,
            "owner_generation": self.run.owner_generation,
            "lifecycle_state": lifecycle_state,
            "plan_fingerprint": current.plan.fingerprint,
            "isolated_evidence_fingerprint": current.isolated.fingerprint,
            "geometry": geometry_rows,
            "next_geometry": missing_geometry,
            "next_capture": next_capture,
            "candidate": candidate_review,
            "candidate_failure": candidate_failure,
            "applied_candidate": applied_candidate,
            "live_mutation": (
                {
                    "status": live_mutation.status,
                    "purpose": live_mutation.purpose,
                    "issuance_id": live_mutation.issuance_id,
                }
                if live_mutation is not None
                else None
            ),
            "detail": (
                (
                    "Exact measured evidence could not authorize a candidate; "
                    "restart the driver and alignment measurements."
                )
                if status == "candidate_refused"
                else (
                    (
                        "The candidate graph is applied and read back; retry to "
                        "finish its durable state."
                        if status == "apply_finalization_required"
                        else (
                            "The previous crossover was restored exactly. Retry "
                            "the reviewed candidate when ready."
                            if status == "apply_rolled_back"
                            else (
                                "The live DSP outcome is uncertain. Restore the "
                                "exact previous crossover before continuing."
                                if status == "restore_required"
                                else None
                            )
                        )
                    )
                    if status != "unavailable"
                    else (
                        "commissioning cannot collect from lifecycle "
                        f"{lifecycle_state}"
                    )
                )
            ),
        }

    def _host(
        self,
        current: _CurrentComposition,
        *,
        raw_capture_transport: RawCaptureTransport | None,
    ) -> CommissioningEvidenceHost:
        inputs = self._region_inputs(current.plan)
        if inputs is None:
            raise CommissioningServiceError(
                "geometry_incomplete",
                "confirm signed geometry for every crossover region first",
            )
        return CommissioningEvidenceHost(
            plan=current.plan,
            topology=current.authority.topology,
            run_store=self.run_store,
            evidence_store=self.evidence_store,
            region_inputs=inputs,
            load_current_authority=self.load_current_authority,
            raw_capture_transport=raw_capture_transport,
        )

    async def capture_next(
        self,
        port: Any,
        *,
        raw_capture_transport: RawCaptureTransport,
        config_dir: str,
    ) -> Any:
        """Execute exactly one host-selected real recorder capture."""

        current = self._current()
        host = self._host(current, raw_capture_transport=raw_capture_transport)
        return await host.capture_next_with_runtime(port, config_dir=config_dir)


def commissioning_runtime_port(camilla: Any) -> Any:
    """Adapt one Camilla controller to the existing exact runtime port."""

    from .commissioning_runtime import CommissioningRuntimePort

    return CommissioningRuntimePort(
        read_active_raw=lambda: camilla.get_active_config_raw(best_effort=False),
        apply_active_raw=lambda raw: camilla.set_active_config_raw(
            raw, best_effort=False
        ),
        read_config_path=lambda: camilla.get_config_file_path(best_effort=False),
        read_listening_volume_db=lambda: camilla.get_volume_db(best_effort=False),
        set_listening_volume_db=lambda db: camilla.set_volume_db(
            db, best_effort=False
        ),
    )
