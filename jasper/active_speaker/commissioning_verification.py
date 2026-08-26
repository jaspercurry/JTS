# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Post-apply combined-response verification for Active commissioning.

The reviewed candidate is already the live protected graph.  Verification
therefore owns no graph transaction: it holds the existing DSP writer lock,
proves the current exact state still equals the retained apply readback, and
uses the production admitted recorder path for three fixed-axis repeats.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.log_event import log_event

from ._common import (
    ROOM_AUTHORITY_RECEIPT_ABSENT,
    ROOM_AUTHORITY_RECEIPT_MALFORMED,
    ROOM_AUTHORITY_RECEIPT_STALE,
)
from .commissioning_evidence_store import (
    EVIDENCE_ROOT,
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
)
from .commissioning_lifecycle import CommissioningTransition
from .commissioning_receipt import (
    POST_APPLY_REQUIRED_REPEATS,
    POST_APPLY_VERIFICATION_ALGORITHM_ID,
    POST_APPLY_VERIFICATION_ALGORITHM_VERSION,
    AdmittedCaptureProof,
    AppliedCandidateProof,
    CommissioningEligibilityReceipt,
    CommissioningRollbackEvidence,
    PostApplyTargetVerification,
    RequiredTargetPlan,
    RequiredVerificationTarget,
    commissioning_context_fingerprint,
)
from .commissioning_run import (
    DEFAULT_STATE_PATH,
    CommissioningAttemptHandle,
    CommissioningLiveMutation,
    CommissioningRunConflict,
    CommissioningRunHandle,
    CommissioningRunStore,
)

if TYPE_CHECKING:
    from jasper.audio_measurement.null_walk import NullWalkSpec

    from .commissioning_evidence import RegionEvidencePlan, RegionEvidenceTarget

POST_APPLY_CAPTURE_SOURCE = "active_speaker_post_apply_verification"
_PASS_VERDICT = "blend_ok"
_FAIL_VERDICT = "polarity_or_delay_problem"
logger = logging.getLogger(__name__)


class CommissioningVerificationError(RuntimeError):
    """The retained apply could not advance its exact verification proof."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _capture_source_path(
    run: CommissioningRunHandle,
    target: RequiredVerificationTarget,
    ordinal: int,
) -> str:
    return (
        f"runs/{run.run_id}/generations/{run.owner_generation}/post-apply/"
        f"{target.target_fingerprint}/repeat-{ordinal:04d}.json"
    )


def _target_source_path(
    run: CommissioningRunHandle,
    target: RequiredVerificationTarget,
) -> str:
    return (
        f"runs/{run.run_id}/generations/{run.owner_generation}/post-apply/"
        f"{target.target_fingerprint}/verification.json"
    )


def _verification_failure_source_path(
    run: CommissioningRunHandle,
    mutation: CommissioningLiveMutation,
) -> str:
    return (
        f"runs/{run.run_id}/generations/{mutation.started_owner_generation}/"
        f"candidate-apply/{mutation.issuance_id}/"
        "post-apply-verification-failed.json"
    )


def receipt_source_path(run: CommissioningRunHandle) -> str:
    # The positive receipt belongs to the durable run, not to the process
    # generation that happened to finish it. Service restart advances owner
    # generation; a generation-scoped path would silently revoke verified Room
    # authority even though the retained apply and lifecycle remain current.
    return f"runs/{run.run_id}/commissioning-eligibility-receipt.json"


def _artifact_relative_path(source_path: str) -> str:
    return f"{EVIDENCE_ROOT}/artifacts/{source_path}"


@dataclass(frozen=True, slots=True)
class PostApplyCaptureOperation:
    """One server-issued repeat for an exact topology target."""

    plan_fingerprint: str
    target: RegionEvidenceTarget
    required_target: RequiredVerificationTarget
    attempt: CommissioningAttemptHandle
    placement_fingerprint: str
    driver_target_fingerprints: tuple[str, str]
    lower_channels: tuple[int, ...]
    upper_channels: tuple[int, ...]
    capture_ordinal: int
    commissioning_context_fingerprint: str
    issuance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    evidence_kind: Literal["normal"] = "normal"
    relative_delay_us: None = None
    null_walk_spec: NullWalkSpec | None = None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.attempt.target_fingerprint != self.required_target.target_fingerprint:
            raise CommissioningVerificationError(
                "verification_attempt_stale",
                "post-apply attempt does not equal its required topology target",
            )
        if self.placement_fingerprint != self.required_target.placement_fingerprint:
            raise CommissioningVerificationError(
                "verification_placement_stale",
                "post-apply placement does not equal its required target",
            )
        if not 1 <= self.capture_ordinal <= POST_APPLY_REQUIRED_REPEATS:
            raise CommissioningVerificationError(
                "verification_ordinal_invalid", "post-apply repeat is outside its bound"
            )
        object.__setattr__(
            self,
            "fingerprint",
            json_fingerprint(
                {
                    "schema_version": 1,
                    "kind": "jts_active_post_apply_capture_operation",
                    "plan_fingerprint": self.plan_fingerprint,
                    "region_target_fingerprint": self.target.fingerprint,
                    "required_target_fingerprint": self.required_target.fingerprint,
                    "attempt_id": self.attempt.attempt_id,
                    "capture_ordinal": self.capture_ordinal,
                    "commissioning_context_fingerprint": (
                        self.commissioning_context_fingerprint
                    ),
                    "issuance_id": self.issuance_id,
                }
            ),
        )

    @property
    def target_fingerprint(self) -> str:
        return self.required_target.target_fingerprint


class CommissioningVerificationService:
    """Persist three-repeat target verdicts and the one Active receipt."""

    def __init__(
        self,
        *,
        run: CommissioningRunHandle,
        run_store: CommissioningRunStore,
        evidence_store: CommissioningEvidenceStore,
        plan: RegionEvidencePlan,
        target_plan: RequiredTargetPlan,
        applied_candidate: AppliedCandidateProof,
        retained_mutation: CommissioningLiveMutation,
        load_current_authority: Any,
    ) -> None:
        self.run = run
        self.run_store = run_store
        self.evidence_store = evidence_store
        self.plan = plan
        self.target_plan = target_plan
        self.applied_candidate = applied_candidate
        self.retained_mutation = retained_mutation
        self.load_current_authority = load_current_authority
        if (
            run_store.lifecycle_state(run) not in {"applied_unverified", "verified"}
            or retained_mutation.status != "retained"
            or retained_mutation.issuance_id != applied_candidate.operation_id
            or target_plan.fingerprint != applied_candidate.target_plan_fingerprint
        ):
            raise CommissioningVerificationError(
                "applied_proof_stale", "post-apply verification has no retained apply"
            )

    @property
    def context_fingerprint(self) -> str:
        return commissioning_context_fingerprint(
            target_plan=self.target_plan,
            applied_candidate=self.applied_candidate,
        )

    def _missing(self, error: CommissioningEvidenceStoreError) -> bool:
        return error.code == CommissioningEvidenceStoreErrorCode.MISSING

    def _reopen_capture(
        self, target: RequiredVerificationTarget, ordinal: int
    ) -> AdmittedCaptureProof | None:
        try:
            artifact = self.evidence_store.identify_artifact(
                _artifact_relative_path(_capture_source_path(self.run, target, ordinal))
            )
        except CommissioningEvidenceStoreError as exc:
            if self._missing(exc):
                return None
            raise
        proof = AdmittedCaptureProof.from_mapping(
            self.evidence_store.reopen_json_artifact(artifact)
        )
        capture = proof.capture
        if (
            proof.commissioning_session_id != self.run.session_id
            or capture.target_fingerprint != target.target_fingerprint
            or capture.placement_fingerprint != target.placement_fingerprint
            or capture.context_fingerprint != self.context_fingerprint
        ):
            raise CommissioningVerificationError(
                "verification_capture_stale",
                "stored post-apply capture does not equal the current authority",
            )
        for child in (
            capture.raw_artifact,
            capture.analysis_input_artifact,
            capture.quality_artifact,
            capture.admission_artifact,
            proof.generation_artifact,
        ):
            self.evidence_store.reopen_artifact(child)
        return proof

    def _captures(
        self, target: RequiredVerificationTarget
    ) -> tuple[AdmittedCaptureProof, ...]:
        values: list[AdmittedCaptureProof] = []
        missing_seen = False
        for ordinal in range(1, POST_APPLY_REQUIRED_REPEATS + 1):
            proof = self._reopen_capture(target, ordinal)
            if proof is None:
                missing_seen = True
            elif missing_seen:
                raise CommissioningVerificationError(
                    "verification_progress_invalid",
                    "post-apply capture repeats are not contiguous",
                )
            else:
                values.append(proof)
        return tuple(values)

    def _capture_verdict(self, proof: AdmittedCaptureProof) -> str:
        capture = proof.capture
        analysis = self.evidence_store.reopen_json_artifact(
            capture.analysis_input_artifact
        )
        quality = self.evidence_store.reopen_json_artifact(capture.quality_artifact)
        acoustic = analysis.get("acoustic")
        if (
            analysis.get("kind") != "jts_active_summed_capture_analysis"
            or analysis.get("target_fingerprint") != capture.target_fingerprint
            or analysis.get("context_fingerprint") != capture.context_fingerprint
            or not isinstance(analysis.get("raw_artifact"), Mapping)
            or analysis["raw_artifact"].get("fingerprint")
            != capture.raw_artifact.fingerprint
            or not isinstance(acoustic, Mapping)
            or quality.get("kind") != "jts_active_summed_capture_quality"
            or quality.get("accepted") is not True
            or quality.get("issues") != []
            or quality.get("analysis_artifact_fingerprint")
            != capture.analysis_input_artifact.fingerprint
            or quality.get("raw_artifact_fingerprint")
            != capture.raw_artifact.fingerprint
        ):
            raise CommissioningVerificationError(
                "verification_capture_stale",
                "stored post-apply analysis does not equal its admitted capture",
            )
        verdict = acoustic.get("verdict")
        if verdict not in {_PASS_VERDICT, _FAIL_VERDICT}:
            raise CommissioningVerificationError(
                "verification_capture_stale",
                "stored post-apply analysis has no supported acoustic verdict",
            )
        return str(verdict)

    def _verification_failure(
        self,
        target: RequiredVerificationTarget,
        captures: tuple[AdmittedCaptureProof, ...],
        verdicts: tuple[str, ...],
    ) -> tuple[dict[str, Any], ArtifactIdentity]:
        core = {
            "schema_version": 1,
            "kind": "jts_active_post_apply_verification_failure",
            "failure_code": "post_apply_verification_failed",
            "session_id": self.run.session_id,
            "run_id": self.run.run_id,
            "target_plan_fingerprint": self.target_plan.fingerprint,
            "target_fingerprint": target.target_fingerprint,
            "commissioning_context_fingerprint": self.context_fingerprint,
            "applied_candidate_proof_fingerprint": self.applied_candidate.fingerprint,
            "operation_id": self.retained_mutation.issuance_id,
            "mutation_operation_fingerprint": (
                self.retained_mutation.operation_fingerprint
            ),
            "capture_fingerprints": [proof.fingerprint for proof in captures],
            "acoustic_verdicts": list(verdicts),
        }
        expected = {**core, "fingerprint": json_fingerprint(core)}
        artifact = self.evidence_store.publish_json_artifact(
            _verification_failure_source_path(self.run, self.retained_mutation),
            expected,
        )
        if self.evidence_store.reopen_json_artifact(artifact) != expected:
            raise CommissioningVerificationError(
                "verification_readback_mismatch",
                "post-apply failure changed on exact reopen",
            )
        log_event(
            logger,
            "correction.crossover_verification_failed",
            session=self.run.session_id,
            run_id=self.run.run_id,
            owner_generation=self.run.owner_generation,
            group=target.speaker_group_id,
            target_fingerprint=target.target_fingerprint,
            applied_candidate_fingerprint=self.applied_candidate.fingerprint,
            failure_code=expected["failure_code"],
            failure_artifact_fingerprint=artifact.fingerprint,
        )
        return expected, artifact

    def _reopen_verification_failure(
        self,
    ) -> tuple[dict[str, Any], ArtifactIdentity] | None:
        try:
            artifact = self.evidence_store.identify_artifact(
                _artifact_relative_path(
                    _verification_failure_source_path(self.run, self.retained_mutation)
                )
            )
        except CommissioningEvidenceStoreError as exc:
            if self._missing(exc):
                return None
            raise
        raw = self.evidence_store.reopen_json_artifact(artifact)
        expected_fields = {
            "schema_version",
            "kind",
            "failure_code",
            "session_id",
            "run_id",
            "target_plan_fingerprint",
            "target_fingerprint",
            "commissioning_context_fingerprint",
            "applied_candidate_proof_fingerprint",
            "operation_id",
            "mutation_operation_fingerprint",
            "capture_fingerprints",
            "acoustic_verdicts",
            "fingerprint",
        }
        capture_fingerprints = raw.get("capture_fingerprints")
        declared_verdicts = raw.get("acoustic_verdicts")
        if (
            set(raw) != expected_fields
            or raw.get("schema_version") != 1
            or raw.get("kind") != "jts_active_post_apply_verification_failure"
            or raw.get("failure_code") != "post_apply_verification_failed"
            or raw.get("session_id") != self.run.session_id
            or raw.get("run_id") != self.run.run_id
            or raw.get("target_plan_fingerprint") != self.target_plan.fingerprint
            or raw.get("commissioning_context_fingerprint") != self.context_fingerprint
            or raw.get("applied_candidate_proof_fingerprint")
            != self.applied_candidate.fingerprint
            or raw.get("operation_id") != self.retained_mutation.issuance_id
            or raw.get("mutation_operation_fingerprint")
            != self.retained_mutation.operation_fingerprint
            or raw.get("target_fingerprint")
            not in {target.target_fingerprint for target in self.target_plan.targets}
            or not isinstance(capture_fingerprints, list)
            or len(capture_fingerprints) != POST_APPLY_REQUIRED_REPEATS
            or len(set(capture_fingerprints)) != POST_APPLY_REQUIRED_REPEATS
            or any(
                not isinstance(value, str) or len(value) != 64
                for value in capture_fingerprints
            )
            or not isinstance(declared_verdicts, list)
            or len(declared_verdicts) != POST_APPLY_REQUIRED_REPEATS
            or any(
                value not in {_PASS_VERDICT, _FAIL_VERDICT}
                for value in declared_verdicts
            )
            or _FAIL_VERDICT not in declared_verdicts
            or raw.get("fingerprint")
            != json_fingerprint(
                {key: value for key, value in raw.items() if key != "fingerprint"}
            )
        ):
            raise CommissioningVerificationError(
                "verification_capture_stale",
                "stored post-apply failure does not equal the retained authority",
            )
        return raw, artifact

    def _reopen_receipt(
        self,
    ) -> tuple[CommissioningEligibilityReceipt, ArtifactIdentity] | None:
        try:
            artifact = self.evidence_store.identify_artifact(
                _artifact_relative_path(receipt_source_path(self.run))
            )
        except CommissioningEvidenceStoreError as exc:
            if self._missing(exc):
                return None
            raise
        receipt = CommissioningEligibilityReceipt.from_mapping(
            self.evidence_store.reopen_json_artifact(artifact)
        )
        if (
            receipt.target_plan != self.target_plan
            or receipt.applied_candidate != self.applied_candidate
            or receipt.commissioning_context_fingerprint != self.context_fingerprint
            or any(
                self._capture_verdict(proof) != _PASS_VERDICT
                for target in receipt.post_apply_targets
                for proof in target.admitted_captures
            )
        ):
            raise CommissioningVerificationError(
                "receipt_readback_mismatch",
                "stored commissioning receipt does not equal the retained authority",
            )
        return receipt, artifact

    def _target_verification(
        self, target: RequiredVerificationTarget
    ) -> tuple[
        PostApplyTargetVerification | None,
        tuple[dict[str, Any], ArtifactIdentity] | None,
    ]:
        captures = self._captures(target)
        if len(captures) != POST_APPLY_REQUIRED_REPEATS:
            return None, None
        verdicts = tuple(self._capture_verdict(proof) for proof in captures)
        if any(verdict != _PASS_VERDICT for verdict in verdicts):
            return None, self._verification_failure(target, captures, verdicts)
        expected = PostApplyTargetVerification(
            speaker_group_id=target.speaker_group_id,
            target_id=target.target_id,
            target_fingerprint=target.target_fingerprint,
            geometry_id=target.geometry_id,
            placement_fingerprint=target.placement_fingerprint,
            commissioning_session_id=self.run.session_id,
            commissioning_context_fingerprint=self.context_fingerprint,
            verification_algorithm_id=POST_APPLY_VERIFICATION_ALGORITHM_ID,
            verification_algorithm_version=(POST_APPLY_VERIFICATION_ALGORITHM_VERSION),
            threshold_profile_fingerprint=(
                self.plan.authority.threshold_profile_fingerprint
            ),
            verdict="passed",
            admitted_captures=captures,
        )
        artifact = self.evidence_store.publish_json_artifact(
            _target_source_path(self.run, target), expected.to_dict()
        )
        reopened = PostApplyTargetVerification.from_mapping(
            self.evidence_store.reopen_json_artifact(artifact)
        )
        if reopened != expected:
            raise CommissioningVerificationError(
                "verification_readback_mismatch",
                "post-apply target verification changed on exact reopen",
            )
        return reopened, None

    def _receipt(
        self, targets: tuple[PostApplyTargetVerification, ...]
    ) -> tuple[CommissioningEligibilityReceipt, Any]:
        rollback = CommissioningRollbackEvidence(
            mutation_state="applied",
            status="not_required",
            evidence_kind="retained_apply",
            operation_id=self.applied_candidate.operation_id,
            mutation_fingerprint=self.applied_candidate.mutation_fingerprint,
            observed_applied_graph_fingerprint=(
                self.applied_candidate.observed_fresh_readback_graph.fingerprint
            ),
            predecessor_state=self.applied_candidate.predecessor_state,
        )
        expected = CommissioningEligibilityReceipt(
            target_plan=self.target_plan,
            applied_candidate=self.applied_candidate,
            commissioning_context_fingerprint=self.context_fingerprint,
            post_apply_targets=targets,
            rollback=rollback,
        )
        artifact = self.evidence_store.publish_json_artifact(
            receipt_source_path(self.run), expected.to_dict()
        )
        reopened = CommissioningEligibilityReceipt.from_mapping(
            self.evidence_store.reopen_json_artifact(artifact)
        )
        if reopened != expected:
            raise CommissioningVerificationError(
                "receipt_readback_mismatch",
                "commissioning receipt changed on exact reopen",
            )
        return reopened, artifact

    def status(self) -> dict[str, Any]:
        reopened_receipt = self._reopen_receipt()
        if reopened_receipt is not None:
            receipt, artifact = reopened_receipt
            lifecycle = self.run_store.lifecycle_state(self.run)
            expected_transition = CommissioningTransition(
                from_state="applied_unverified",
                to_state="verified",
                evidence_kind="commissioning_eligibility_receipt",
                evidence_fingerprint=artifact.fingerprint,
            )
            committed = False
            if lifecycle == "applied_unverified":
                try:
                    committed = self.run_store.transition(self.run, expected_transition)
                except CommissioningRunConflict:
                    pass
            if self.run_store.lifecycle_state(self.run) != "verified" or (
                self.run_store.lifecycle_transition(self.run) != expected_transition
            ):
                raise CommissioningVerificationError(
                    "run_generation_stale",
                    "receipt lost current run ownership",
                )
            if committed:
                log_event(
                    logger,
                    "correction.crossover_verification_passed",
                    session=self.run.session_id,
                    run_id=self.run.run_id,
                    owner_generation=self.run.owner_generation,
                    receipt_fingerprint=receipt.fingerprint,
                    receipt_artifact_fingerprint=artifact.fingerprint,
                )
            return {
                "status": "verified",
                "targets": [
                    {
                        "speaker_group_id": target.speaker_group_id,
                        "target_fingerprint": target.target_fingerprint,
                        "captured_repeats": POST_APPLY_REQUIRED_REPEATS,
                        "required_repeats": POST_APPLY_REQUIRED_REPEATS,
                        "verified": True,
                        "failed": False,
                    }
                    for target in self.target_plan.targets
                ],
                "next_target": None,
                "failure": None,
                "receipt": {
                    "fingerprint": receipt.fingerprint,
                    "artifact_fingerprint": artifact.fingerprint,
                    "target_plan_fingerprint": receipt.target_plan.fingerprint,
                    "applied_candidate_fingerprint": (
                        receipt.applied_candidate.fingerprint
                    ),
                },
            }

        persisted_failure = self._reopen_verification_failure()
        if persisted_failure is not None:
            failure_payload, artifact = persisted_failure
            return {
                "status": "verification_failed",
                "targets": [
                    {
                        "speaker_group_id": target.speaker_group_id,
                        "target_fingerprint": target.target_fingerprint,
                        "captured_repeats": (
                            POST_APPLY_REQUIRED_REPEATS
                            if target.target_fingerprint
                            == failure_payload["target_fingerprint"]
                            else 0
                        ),
                        "required_repeats": POST_APPLY_REQUIRED_REPEATS,
                        "verified": False,
                        "failed": (
                            target.target_fingerprint
                            == failure_payload["target_fingerprint"]
                        ),
                    }
                    for target in self.target_plan.targets
                ],
                "next_target": None,
                "failure": {
                    "failure_code": failure_payload["failure_code"],
                    "fingerprint": failure_payload["fingerprint"],
                    "artifact_fingerprint": artifact.fingerprint,
                },
                "receipt": None,
            }

        target_rows: list[dict[str, Any]] = []
        verified_targets: list[PostApplyTargetVerification] = []
        failed: tuple[dict[str, Any], ArtifactIdentity] | None = None
        for target in self.target_plan.targets:
            captures = self._captures(target)
            verification, target_failure = self._target_verification(target)
            if verification is not None:
                verified_targets.append(verification)
            if target_failure is not None:
                failed = target_failure
            target_rows.append(
                {
                    "speaker_group_id": target.speaker_group_id,
                    "target_fingerprint": target.target_fingerprint,
                    "captured_repeats": len(captures),
                    "required_repeats": POST_APPLY_REQUIRED_REPEATS,
                    "verified": verification is not None,
                    "failed": target_failure is not None,
                }
            )
        if failed is not None:
            return self.status()
        if len(verified_targets) == len(self.target_plan.targets):
            self._receipt(tuple(verified_targets))
            return self.status()
        return {
            "status": "applied_unverified",
            "targets": target_rows,
            "next_target": next(
                (row for row in target_rows if row["verified"] is not True), None
            ),
            "failure": None,
            "receipt": None,
        }


def _deny(reason: str, cause: str) -> dict[str, Any]:
    """One un-vouched receipt answer, disclosed loudly and never enforced.

    Ruling S10: an unproven fact is a WARNING, not a stop. The caller records
    that the automatic crossover is not receipt-backed — which keeps room
    correction from CLAIMING the verified authority — and runs anyway.
    """

    log_event(
        logger,
        "active_speaker.commissioning_receipt_unvouched",
        level=logging.WARNING,
        reason=reason,
        cause=cause,
    )
    return {
        "allowed": False,
        "authority": "automatic_verified_receipt",
        "reason": reason,
        "receipt_fingerprint": None,
    }


def read_commissioning_room_authority(
    topology: Any,
    *,
    run_state_path: str | Path = DEFAULT_STATE_PATH,
    sessions_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read Active's exact verified-receipt decision without claiming ownership.

    The three denials are distinguishable on purpose. A receipt that was never
    minted, one that no longer describes this speaker, and one whose bytes will
    not parse have three different remedies, and a disclosure that cannot say
    which of them happened is vague rather than loud. None of them stops room
    correction — see :func:`_deny`.
    """

    from .bundles import sessions_dir

    try:
        run_store = CommissioningRunStore(path=run_state_path)
        snapshot = run_store.snapshot()
        current = snapshot.get("current")
        if (
            not isinstance(current, Mapping)
            or current.get("lifecycle_state") != "verified"
        ):
            return _deny(ROOM_AUTHORITY_RECEIPT_ABSENT, "lifecycle is not verified")
        run = CommissioningRunHandle(
            session_id=str(current["session_id"]),
            session_fingerprint=str(current["session_fingerprint"]),
            run_id=str(current["run_id"]),
            owner_id=str(current["owner_id"]),
            owner_generation=int(current["owner_generation"]),
        )
        root = Path(sessions_root) if sessions_root is not None else sessions_dir()
        store = CommissioningEvidenceStore.open(
            root / run.session_id,
            expected_session_id=run.session_id,
        )
        try:
            artifact = store.identify_artifact(
                _artifact_relative_path(receipt_source_path(run))
            )
        except CommissioningEvidenceStoreError as exc:
            if exc.code == CommissioningEvidenceStoreErrorCode.MISSING:
                return _deny(
                    ROOM_AUTHORITY_RECEIPT_ABSENT, "no receipt artifact for this run"
                )
            raise
        receipt = CommissioningEligibilityReceipt.from_mapping(
            store.reopen_json_artifact(artifact)
        )
        transition = run_store.lifecycle_transition(run)
        mutation = run_store.current_live_mutation(run)
        if (
            receipt.target_plan.topology.to_dict() != topology.to_dict()
            or transition is None
            or transition.to_state != "verified"
            or transition.evidence_kind != "commissioning_eligibility_receipt"
            or transition.evidence_fingerprint != artifact.fingerprint
            or mutation is None
            or mutation.status != "retained"
            or mutation.issuance_id != receipt.applied_candidate.operation_id
            or mutation.operation_fingerprint
            != receipt.applied_candidate.mutation_fingerprint
            or mutation.terminal_evidence_fingerprint is None
        ):
            return _deny(
                ROOM_AUTHORITY_RECEIPT_STALE,
                "receipt does not describe the current topology, transition or "
                "live mutation",
            )
        return {
            "allowed": True,
            "authority": "automatic_verified_receipt",
            "reason": None,
            "receipt_fingerprint": receipt.fingerprint,
            "receipt_artifact_fingerprint": artifact.fingerprint,
        }
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        # Everything left is the receipt or its run record failing to read as
        # what it claims to be: a strict-schema reject, a torn file, a run
        # record missing a key. Naming the exception type is the whole point
        # of narrowing the two structural causes out above it.
        return _deny(ROOM_AUTHORITY_RECEIPT_MALFORMED, type(exc).__name__)
