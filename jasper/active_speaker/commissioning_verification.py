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

import datetime
import errno
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from jasper.audio_measurement.bundles import BundleError
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.log_event import log_event
from jasper.os_fault import root_os_error

from ._common import (
    ROOM_AUTHORITY_RECEIPT_ABSENT,
    ROOM_AUTHORITY_RECEIPT_MALFORMED,
    ROOM_AUTHORITY_RECEIPT_STALE,
    ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
    ROOM_AUTHORITY_RECEIPT_UNREADABLE,
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
    PROVEN_AT_FORMAT,
    RECEIPT_KIND,
    RECEIPT_SCHEMA_VERSION,
    AdmittedCaptureProof,
    AppliedCandidateProof,
    CommissioningEligibilityReceipt,
    CommissioningHardwareIdentity,
    CommissioningProofProvenance,
    CommissioningRollbackEvidence,
    PostApplyTargetVerification,
    RequiredTargetPlan,
    RequiredVerificationTarget,
    commissioning_context_fingerprint,
)
from .commissioning_run import (
    DEFAULT_STATE_PATH,
    CommissioningLiveMutation,
    CommissioningRunConflict,
    CommissioningRunHandle,
    CommissioningRunLockTimeout,
    CommissioningRunStore,
)

if TYPE_CHECKING:
    from .commissioning_evidence import RegionEvidencePlan

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


def _bundle_forensics(bundle_dir: Path) -> tuple[str | None, str | None, str | None]:
    """``(build_sha, mic_calibration_id, mic_calibration_sha256)`` for a bundle.

    Best-effort like the ``bundles`` write that produced them: a bundle that
    will not parse yields three ``None``s, so an unreadable forensic mirror
    cannot stop a receipt the strict evidence chain has earned (ruling S10).
    """

    from .bundles import summarize_bundle

    def _present(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    try:
        info = summarize_bundle(bundle_dir)
    except (OSError, BundleError):
        return None, None, None
    fingerprints = info.get("fingerprints")
    if not isinstance(fingerprints, Mapping):
        return None, None, None
    mic = fingerprints.get("mic")
    if not isinstance(mic, Mapping):
        mic = {}
    return (
        _present(fingerprints.get("build_sha")),
        _present(mic.get("calibration_id")),
        _present(mic.get("calibration_sha256")),
    )


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
        build_sha, calibration_id, calibration_sha = _bundle_forensics(
            self.evidence_store.bundle_dir
        )
        expected = CommissioningEligibilityReceipt(
            target_plan=self.target_plan,
            applied_candidate=self.applied_candidate,
            commissioning_context_fingerprint=self.context_fingerprint,
            post_apply_targets=targets,
            rollback=rollback,
            provenance=CommissioningProofProvenance(
                proven_at=datetime.datetime.now(datetime.timezone.utc).strftime(
                    PROVEN_AT_FORMAT
                ),
                proven_by_build=build_sha,
                capture_refs=tuple(
                    proof.capture.raw_artifact
                    for target in targets
                    for proof in target.admitted_captures
                ),
                hardware_identity=CommissioningHardwareIdentity(
                    topology_id=self.target_plan.topology_id,
                    topology_fingerprint=self.target_plan.topology_fingerprint,
                    mic_calibration_id=calibration_id,
                    mic_calibration_sha256=calibration_sha,
                ),
            ),
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


#: The last (reason, cause) this process disclosed. A denial is the STEADY
#: state for most speakers and this reader is polled by the dashboard, the
#: wizard and every LAN client, so an unconditional WARNING per read is a
#: journal storm on a 1 GB Pi. The transition is the event; the repeat is not.
#: See ADR-0196.
_LAST_DISCLOSED: tuple[str, str] | None = None


def _deny(reason: str, cause: str) -> dict[str, Any]:
    """One un-vouched receipt answer, disclosed loudly and never enforced.

    Ruling S10: an unproven fact is a WARNING, not a stop. ``cause`` rides the
    answer as well as the log, so the file or errno behind it reaches the
    operator without a journal read.
    """

    global _LAST_DISCLOSED

    changed = _LAST_DISCLOSED != (reason, cause)
    _LAST_DISCLOSED = (reason, cause)
    log_event(
        logger,
        "active_speaker.commissioning_receipt_unvouched",
        level=logging.WARNING if changed else logging.DEBUG,
        reason=reason,
        cause=cause,
    )
    return {
        "allowed": False,
        "authority": "automatic_verified_receipt",
        "reason": reason,
        "cause": cause,
        "receipt_fingerprint": None,
    }


def _os_cause(exc: OSError) -> str:
    """Name the fault the way an operator can act on: class, errno, path."""

    code = errno.errorcode.get(exc.errno or 0, str(exc.errno or ""))
    return f"{type(exc).__name__}:{code}:{exc.filename or ''}"


#: How the evidence store's own structured codes answer. Classified by what
#: the household must DO: nothing was minted, the record's bytes cannot be
#: trusted, or the machine could not produce it. Codes are the store's
#: vocabulary, so nothing here sniffs an exception chain. See ADR-0196.
_STORE_ABSENT_CODES = frozenset({CommissioningEvidenceStoreErrorCode.MISSING})
_STORE_CONTENT_CODES = frozenset({
    CommissioningEvidenceStoreErrorCode.INVALID_PATH,
    CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
    CommissioningEvidenceStoreErrorCode.NOT_REGULAR,
    CommissioningEvidenceStoreErrorCode.TOO_LARGE,
    CommissioningEvidenceStoreErrorCode.TOTAL_TOO_LARGE,
    CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
    CommissioningEvidenceStoreErrorCode.NOT_CANONICAL,
    CommissioningEvidenceStoreErrorCode.MALFORMED,
})


def read_commissioning_room_authority(
    topology: Any,
    *,
    run_state_path: str | Path = DEFAULT_STATE_PATH,
    sessions_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read Active's exact verified-receipt decision without claiming ownership.

    The denials are distinguishable on purpose and none of them stops room
    correction (see :func:`_deny`); what each means is ADR-0196. A lifecycle
    that is not ``verified`` is ABSENT — a true state, not a damaged one.
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
        # Both reads answer through the store-code map below, so a receipt the
        # retention sweep removed between them reads as ABSENT either way.
        artifact = store.identify_artifact(
            _artifact_relative_path(receipt_source_path(run))
        )
        payload = store.reopen_json_artifact(artifact)
        # A receipt this JTS would have to grow fields to read is not corrupt:
        # an upgrade moved the schema under an honestly minted proof. Strict
        # parsing is unchanged for the current version.
        declared = payload.get("schema_version")
        if (
            payload.get("kind") == RECEIPT_KIND
            and type(declared) is int
            and declared < RECEIPT_SCHEMA_VERSION
        ):
            return _deny(
                ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
                f"receipt schema {declared} predates {RECEIPT_SCHEMA_VERSION}",
            )
        receipt = CommissioningEligibilityReceipt.from_mapping(payload)
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
    except CommissioningEvidenceStoreError as exc:
        if exc.code in _STORE_ABSENT_CODES:
            return _deny(ROOM_AUTHORITY_RECEIPT_ABSENT, str(exc.code))
        if exc.code in _STORE_CONTENT_CODES:
            return _deny(ROOM_AUTHORITY_RECEIPT_MALFORMED, str(exc.code))
        os_fault = root_os_error(exc)
        return _deny(
            ROOM_AUTHORITY_RECEIPT_UNREADABLE,
            _os_cause(os_fault) if os_fault is not None else str(exc.code),
        )
    except CommissioningRunLockTimeout as exc:
        return _deny(ROOM_AUTHORITY_RECEIPT_UNREADABLE, type(exc).__name__)
    except CommissioningRunConflict as exc:
        # A generation moved under the read — a peer claimed the run while this
        # one was several file reads in. STALE is that fact's own answer.
        return _deny(ROOM_AUTHORITY_RECEIPT_STALE, type(exc).__name__)
    except OSError as exc:
        return _deny(ROOM_AUTHORITY_RECEIPT_UNREADABLE, _os_cause(exc))
    except (RuntimeError, TypeError, ValueError, KeyError) as exc:
        os_fault = root_os_error(exc)
        if os_fault is not None:
            return _deny(ROOM_AUTHORITY_RECEIPT_UNREADABLE, _os_cause(os_fault))
        return _deny(ROOM_AUTHORITY_RECEIPT_MALFORMED, type(exc).__name__)
