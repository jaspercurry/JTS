# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read the exact proof of a retained commissioning candidate."""

from __future__ import annotations

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
)

from .commissioning_evidence_store import EVIDENCE_ROOT, CommissioningEvidenceStore
from .commissioning_receipt import (
    AppliedCandidateProof,
    RequiredTargetPlan,
)
from .commissioning_run import (
    CommissioningLiveMutation,
    CommissioningRunHandle,
)
from .measured_candidate import MeasuredElectricalCandidate

APPLY_PURPOSE = "measured_candidate_apply"


class CommissioningApplyError(RuntimeError):
    """A retained candidate proof does not match its current authority."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _source_path(
    run: CommissioningRunHandle,
    issuance_id: str,
    filename: str,
    *,
    owner_generation: int | None = None,
) -> str:
    return (
        f"runs/{run.run_id}/generations/"
        f"{owner_generation or run.owner_generation}/"
        f"candidate-apply/{issuance_id}/{filename}"
    )


def _identify(
    store: CommissioningEvidenceStore,
    run: CommissioningRunHandle,
    issuance_id: str,
    filename: str,
    *,
    owner_generation: int | None = None,
) -> ArtifactIdentity:
    return store.identify_artifact(
        f"{EVIDENCE_ROOT}/artifacts/"
        + _source_path(
            run,
            issuance_id,
            filename,
            owner_generation=owner_generation,
        )
    )


def reopen_applied_candidate_proof(
    *,
    store: CommissioningEvidenceStore,
    run: CommissioningRunHandle,
    mutation: CommissioningLiveMutation,
    candidate: MeasuredElectricalCandidate,
    target_plan: RequiredTargetPlan,
    safety_profile_fingerprint: str,
) -> tuple[AppliedCandidateProof, ArtifactIdentity]:
    artifact = _identify(
        store,
        run,
        mutation.issuance_id,
        "applied-proof.json",
        owner_generation=mutation.started_owner_generation,
    )
    proof = AppliedCandidateProof.from_mapping(store.reopen_json_artifact(artifact))
    if (
        mutation.status != "retained"
        or mutation.terminal_evidence_fingerprint != artifact.fingerprint
        or proof.operation_id != mutation.issuance_id
        or proof.mutation_fingerprint != mutation.operation_fingerprint
        or proof.candidate_fingerprint != candidate.fingerprint
        or proof.target_plan_fingerprint != target_plan.fingerprint
        or proof.safety_profile_fingerprint != safety_profile_fingerprint
    ):
        raise CommissioningApplyError(
            "applied_proof_stale",
            "retained apply proof does not equal the current candidate authority",
        )
    return proof, artifact
