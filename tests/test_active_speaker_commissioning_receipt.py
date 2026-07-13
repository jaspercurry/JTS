# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from jasper.active_speaker.commissioning_receipt import (
    POST_APPLY_REQUIRED_REPEATS,
    POST_APPLY_VERIFICATION_ALGORITHM_ID,
    POST_APPLY_VERIFICATION_ALGORITHM_VERSION,
    AdmittedCaptureProof,
    AppliedCandidateProof,
    CommissioningEligibilityReceipt,
    CommissioningReceiptError,
    CommissioningRollbackEvidence,
    PostApplyTargetVerification,
    RequiredTargetPlan,
    RequiredVerificationTarget,
    commissioning_context_fingerprint,
)
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    CaptureIdentity,
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
)
from jasper.audio_measurement.excitation_admission import (
    ExcitationAdmission,
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

SESSION_ID = "lane-c-session"
THRESHOLD_PROFILE = "6" * 64


def _hash(char: str) -> str:
    return char * 64


def _stereo_topology(*, include_right: bool = True) -> OutputTopology:
    groups = [
        {
            "id": "left",
            "label": "Left cabinet",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": 0,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "physical_output_index": 1,
                    "identity_verified": True,
                    "protection_required": True,
                    "protection_status": "software_guard_requested",
                },
            ],
        }
    ]
    routing: dict[str, object] = {"main_left_group_id": "left"}
    if include_right:
        groups.append(
            {
                "id": "right",
                "label": "Right cabinet",
                "kind": "right",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 2,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 3,
                        "identity_verified": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        )
        routing["main_right_group_id"] = "right"
    return OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "active-stereo-1",
            "name": "Active stereo",
            "status": "draft",
            "hardware": {
                "device_id": "hifiberry_dac8x",
                "device_label": "HiFiBerry DAC8x",
                "physical_output_count": 8,
                "card_id": "DAC8",
            },
            "speaker_groups": groups,
            "routing": routing,
        }
    )


def _placements(*, include_right: bool = True) -> dict[str, str]:
    values = {"left": _hash("3")}
    if include_right:
        values["right"] = _hash("4")
    return values


def _plan() -> RequiredTargetPlan:
    return RequiredTargetPlan.from_topology(
        _stereo_topology(),
        placement_fingerprints=_placements(),
    )


def _predecessor(marker: str = "entry") -> ExactDspStateIdentity:
    return ExactDspStateIdentity(
        {
            "config_path": "/etc/camilladsp/active.yml",
            "active_raw": {
                "devices": {"volume_limit": -12.0},
                "filters": {"marker": {"type": marker}},
            },
        }
    )


def _normalized_graph(marker: str = "candidate") -> NormalizedActiveRawIdentity:
    return NormalizedActiveRawIdentity(
        {
            "devices": {"volume_limit": -12.0},
            "filters": {"marker": {"type": marker}},
        }
    )


def _proof(plan: RequiredTargetPlan) -> AppliedCandidateProof:
    graph = _normalized_graph()
    return AppliedCandidateProof(
        operation_id="lane-c-apply-1",
        target_plan_fingerprint=plan.fingerprint,
        safety_profile_fingerprint=_hash("a"),
        candidate_fingerprint=_hash("b"),
        predecessor_state=_predecessor(),
        expected_normalized_graph=graph,
        observed_fresh_readback_graph=NormalizedActiveRawIdentity(
            graph.normalized_active_raw
        ),
        writer_lock_fingerprint=_hash("e"),
        mutation_fingerprint=_hash("f"),
        fresh_readback_fingerprint=_hash("0"),
        protection_proof_fingerprint=_hash("5"),
    )


def _retained_rollback(proof: AppliedCandidateProof) -> CommissioningRollbackEvidence:
    return CommissioningRollbackEvidence(
        mutation_state="applied",
        status="not_required",
        evidence_kind="retained_apply",
        operation_id=proof.operation_id,
        mutation_fingerprint=proof.mutation_fingerprint,
        observed_applied_graph_fingerprint=(
            proof.observed_fresh_readback_graph.fingerprint
        ),
        predecessor_state=proof.predecessor_state,
    )


def _artifact(path: str, digest: str, *, session: str = SESSION_ID) -> ArtifactIdentity:
    return ArtifactIdentity(
        bundle_kind="jts_active_speaker_commissioning_authority",
        bundle_id=session,
        relative_path=path,
        sha256=_hash(digest),
        byte_size=4096,
    )


def _admission(
    target_fingerprint: str,
    *,
    safety_profile_fingerprint: str = "a" * 64,
    protection_evidence: bool = True,
    repeat_count: int = POST_APPLY_REQUIRED_REPEATS,
) -> ExcitationAdmission:
    limits = ExcitationLimits(
        permitted_band=FrequencyBand(500, 10_000),
        maximum_effective_peak_dbfs=-12,
        maximum_duration_s=8,
        maximum_repeat_count=max(POST_APPLY_REQUIRED_REPEATS, repeat_count),
        target_fingerprint=target_fingerprint,
        safety_profile_fingerprint=safety_profile_fingerprint,
        protection_requirement_fingerprint=_hash("c"),
        excitation_plan_fingerprint=_hash("b"),
    )
    request = ExcitationRequest(
        band=FrequencyBand(1_000, 8_000),
        effective_peak_dbfs=-18,
        duration_s=4,
        repeat_count=repeat_count,
        target_fingerprint=target_fingerprint,
        safety_profile_fingerprint=safety_profile_fingerprint,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
    )
    evidence = ProtectionEvidence(
        target_fingerprint=target_fingerprint,
        safety_profile_fingerprint=safety_profile_fingerprint,
        protection_requirement_fingerprint=(limits.protection_requirement_fingerprint),
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
        evidence_fingerprint=_hash("d"),
        current=True,
    )
    return admit_excitation(
        request,
        limits,
        protection_evidence=evidence if protection_evidence else None,
    )


def _admission_artifact(
    path: str,
    admission: ExcitationAdmission,
    *,
    session: str,
) -> ArtifactIdentity:
    canonical = json.dumps(
        admission.to_dict(),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ArtifactIdentity(
        bundle_kind="jts_active_speaker_commissioning_authority",
        bundle_id=session,
        relative_path=path,
        sha256=hashlib.sha256(canonical).hexdigest(),
        byte_size=len(canonical),
    )


def _capture(
    index: int,
    *,
    target: RequiredVerificationTarget,
    context: str,
    session: str = SESSION_ID,
    capture_id: str | None = None,
    repeat_count: int = POST_APPLY_REQUIRED_REPEATS,
) -> CaptureIdentity:
    prefix = f"post_apply/{target.speaker_group_id}_{index}"
    admission = _admission(target.target_fingerprint, repeat_count=repeat_count)
    return CaptureIdentity(
        consumer_id="active_crossover",
        measurement_kind="active_crossover_post_apply",
        capture_id=capture_id or f"{target.speaker_group_id}-{index}",
        raw_artifact=_artifact(f"{prefix}.wav", f"{index:x}", session=session),
        analysis_input_artifact=_artifact(f"{prefix}_input.json", "6", session=session),
        target_fingerprint=target.target_fingerprint,
        context_fingerprint=context,
        geometry_id="reference_axis",
        placement_fingerprint=target.placement_fingerprint,
        quality_artifact=_artifact(f"{prefix}_quality.json", "7", session=session),
        admission_artifact=_admission_artifact(
            f"{prefix}_admission.json",
            admission,
            session=session,
        ),
    )


def _admitted(
    capture: CaptureIdentity,
    *,
    session: str = SESSION_ID,
    repeat_count: int = POST_APPLY_REQUIRED_REPEATS,
) -> AdmittedCaptureProof:
    return AdmittedCaptureProof(
        capture=capture,
        commissioning_session_id=session,
        admission=_admission(capture.target_fingerprint, repeat_count=repeat_count),
    )


def _target_verification(
    target: RequiredVerificationTarget,
    *,
    context: str,
    start: int,
    session: str = SESSION_ID,
    repeat_count: int = POST_APPLY_REQUIRED_REPEATS,
) -> PostApplyTargetVerification:
    captures = tuple(
        _capture(
            index,
            target=target,
            context=context,
            session=session,
            repeat_count=repeat_count,
        )
        for index in range(start, start + 3)
    )
    return PostApplyTargetVerification(
        speaker_group_id=target.speaker_group_id,
        target_id=target.target_id,
        target_fingerprint=target.target_fingerprint,
        geometry_id=target.geometry_id,
        placement_fingerprint=target.placement_fingerprint,
        commissioning_session_id=session,
        commissioning_context_fingerprint=context,
        verification_algorithm_id=POST_APPLY_VERIFICATION_ALGORITHM_ID,
        verification_algorithm_version=POST_APPLY_VERIFICATION_ALGORITHM_VERSION,
        threshold_profile_fingerprint=THRESHOLD_PROFILE,
        verdict="passed",
        admitted_captures=tuple(
            _admitted(capture, session=session, repeat_count=repeat_count)
            for capture in captures
        ),
    )


def _receipt() -> CommissioningEligibilityReceipt:
    plan = _plan()
    proof = _proof(plan)
    context = commissioning_context_fingerprint(
        target_plan=plan,
        applied_candidate=proof,
    )
    return CommissioningEligibilityReceipt(
        target_plan=plan,
        applied_candidate=proof,
        commissioning_context_fingerprint=context,
        post_apply_targets=(
            _target_verification(plan.targets[0], context=context, start=1),
            _target_verification(plan.targets[1], context=context, start=4),
        ),
        rollback=_retained_rollback(proof),
    )


def test_real_topology_factory_builds_combined_group_targets_and_compares_current():
    topology = _stereo_topology()
    plan = RequiredTargetPlan.from_topology(
        topology,
        placement_fingerprints=_placements(),
    )

    assert [target.speaker_group_id for target in plan.targets] == ["left", "right"]
    assert [target.target_id for target in plan.targets] == [
        "combined_active_group:left",
        "combined_active_group:right",
    ]
    assert plan.matches_current_topology(
        topology,
        placement_fingerprints=_placements(),
    )
    assert not plan.matches_current_topology(
        _stereo_topology(include_right=False),
        placement_fingerprints=_placements(include_right=False),
    )
    with pytest.raises(CommissioningReceiptError, match="exactly equal"):
        RequiredTargetPlan.from_topology(
            topology,
            placement_fingerprints=_placements(include_right=False),
        )


@pytest.mark.parametrize(
    ("channel_index", "change"),
    (
        (0, {"identity_verified": False}),
        (1, {"startup_muted": False}),
    ),
    ids=("identity-unverified", "tweeter-not-startup-muted"),
)
def test_target_plan_requires_verified_safe_topology(
    channel_index: int,
    change: dict[str, bool],
) -> None:
    payload = _stereo_topology().to_dict()
    payload["speaker_groups"][0]["channels"][channel_index].update(change)
    topology = OutputTopology.from_mapping(payload)

    assert topology.evaluation()["status"] != "verified"
    with pytest.raises(CommissioningReceiptError, match="verified output topology"):
        RequiredTargetPlan.from_topology(
            topology,
            placement_fingerprints=_placements(),
        )


def test_full_topology_receipt_round_trips_as_positive_room_authority():
    receipt = _receipt()
    round_trip = CommissioningEligibilityReceipt.from_mapping(receipt.to_dict())

    assert round_trip == receipt
    assert all(target.verdict == "passed" for target in round_trip.post_apply_targets)
    assert all(
        len(target.admitted_captures) == 3 for target in round_trip.post_apply_targets
    )


@pytest.mark.parametrize(
    ("path", "float_value"),
    (
        (("hardware", "physical_output_count"), 8.0),
        (
            ("speaker_groups", 0, "channels", 0, "physical_output_index"),
            0.0,
        ),
    ),
    ids=("physical-output-count", "physical-output-index"),
)
def test_topology_snapshot_rejects_int_to_float_shape_tamper(
    path: tuple[str | int, ...],
    float_value: float,
):
    payload = copy.deepcopy(_receipt().to_dict())
    node = payload["target_plan"]["topology"]
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = float_value

    with pytest.raises(CommissioningReceiptError, match="snapshot is not canonical"):
        CommissioningEligibilityReceipt.from_mapping(payload)


def test_omitting_real_right_stereo_target_cannot_unlock_room():
    receipt = _receipt()
    assert [target.speaker_group_id for target in receipt.target_plan.targets] == [
        "left",
        "right",
    ]
    with pytest.raises(CommissioningReceiptError, match="exactly equal"):
        CommissioningEligibilityReceipt(
            target_plan=receipt.target_plan,
            applied_candidate=receipt.applied_candidate,
            commissioning_context_fingerprint=receipt.commissioning_context_fingerprint,
            post_apply_targets=(receipt.post_apply_targets[0],),
            rollback=receipt.rollback,
        )


@pytest.mark.parametrize("changed_field", ("operation", "mutation", "graph"))
def test_retained_rollback_cannot_cross_apply_operations(changed_field: str) -> None:
    receipt = _receipt()
    proof = receipt.applied_candidate
    if changed_field == "operation":
        changed = replace(proof, operation_id="lane-c-apply-2")
    elif changed_field == "mutation":
        changed = replace(proof, mutation_fingerprint=_hash("1"))
    else:
        graph = _normalized_graph("other-candidate")
        changed = replace(
            proof,
            expected_normalized_graph=graph,
            observed_fresh_readback_graph=NormalizedActiveRawIdentity(
                graph.normalized_active_raw
            ),
        )
    context = commissioning_context_fingerprint(
        target_plan=receipt.target_plan,
        applied_candidate=changed,
    )

    with pytest.raises(CommissioningReceiptError, match="retained verified apply"):
        CommissioningEligibilityReceipt(
            target_plan=receipt.target_plan,
            applied_candidate=changed,
            commissioning_context_fingerprint=context,
            post_apply_targets=tuple(
                _target_verification(target, context=context, start=1 + index * 3)
                for index, target in enumerate(receipt.target_plan.targets)
            ),
            rollback=receipt.rollback,
        )


def test_self_declared_partial_plan_cannot_reuse_real_stereo_topology_authority():
    full = _plan()
    with pytest.raises(CommissioningReceiptError, match="exactly equal"):
        RequiredTargetPlan(
            topology=full.topology,
            topology_id=full.topology_id,
            topology_fingerprint=full.topology_fingerprint,
            targets=(full.targets[0],),
        )


def test_target_authority_requires_typed_admission_and_passing_verdict():
    receipt = _receipt()
    target = receipt.post_apply_targets[0]
    capture = target.captures[0]

    valid_admission = _admission(capture.target_fingerprint)
    wrong_artifact_capture = replace(
        capture,
        admission_artifact=_artifact(
            "post_apply/wrong_admission.json",
            "0",
        ),
    )
    with pytest.raises(CommissioningReceiptError, match="canonical typed admission"):
        AdmittedCaptureProof(
            capture=wrong_artifact_capture,
            commissioning_session_id=SESSION_ID,
            admission=valid_admission,
        )
    refused = _admission(capture.target_fingerprint, protection_evidence=False)
    refused_capture = replace(
        capture,
        admission_artifact=_admission_artifact(
            "post_apply/refused_admission.json",
            refused,
            session=SESSION_ID,
        ),
    )
    with pytest.raises(CommissioningReceiptError, match="allowed excitation"):
        AdmittedCaptureProof(
            capture=refused_capture,
            commissioning_session_id=SESSION_ID,
            admission=refused,
        )
    other_target_admission = _admission(_hash("e"))
    wrong_target_capture = replace(
        capture,
        admission_artifact=_admission_artifact(
            "post_apply/wrong_target_admission.json",
            other_target_admission,
            session=SESSION_ID,
        ),
    )
    with pytest.raises(CommissioningReceiptError, match="admission target"):
        AdmittedCaptureProof(
            capture=wrong_target_capture,
            commissioning_session_id=SESSION_ID,
            admission=other_target_admission,
        )
    proof = target.admitted_captures[0]
    assert proof.admission_decision_fingerprint == proof.admission.fingerprint
    assert proof.authority_fingerprint == proof.admission.limits.fingerprint
    assert (
        proof.excitation_plan_fingerprint
        == proof.admission.limits.excitation_plan_fingerprint
    )
    with pytest.raises(CommissioningReceiptError, match="passing verdict"):
        replace(target, verdict="failed")  # type: ignore[arg-type]
    with pytest.raises(CommissioningReceiptError, match="verification algorithm"):
        replace(target, verification_algorithm_version="2")


def test_target_repeats_require_one_exact_admission_and_receipt_profile():
    receipt = _receipt()
    left = receipt.post_apply_targets[0]
    capture = left.captures[1]
    other_profile_admission = _admission(
        capture.target_fingerprint,
        safety_profile_fingerprint=_hash("e"),
    )
    changed_capture = replace(
        capture,
        admission_artifact=_admission_artifact(
            "post_apply/changed_profile_admission.json",
            other_profile_admission,
            session=SESSION_ID,
        ),
    )
    changed_proof = AdmittedCaptureProof(
        capture=changed_capture,
        commissioning_session_id=SESSION_ID,
        admission=other_profile_admission,
    )
    with pytest.raises(CommissioningReceiptError, match="one exact excitation"):
        replace(
            left,
            admitted_captures=(
                left.admitted_captures[0],
                changed_proof,
                left.admitted_captures[2],
            ),
        )

    all_changed = replace(
        left,
        admitted_captures=tuple(
            AdmittedCaptureProof(
                capture=replace(
                    proof.capture,
                    admission_artifact=_admission_artifact(
                        f"post_apply/profile_{index}.json",
                        other_profile_admission,
                        session=SESSION_ID,
                    ),
                ),
                commissioning_session_id=SESSION_ID,
                admission=other_profile_admission,
            )
            for index, proof in enumerate(left.admitted_captures)
        ),
    )
    with pytest.raises(CommissioningReceiptError, match="safety profile"):
        replace(
            receipt,
            post_apply_targets=(all_changed, receipt.post_apply_targets[1]),
        )


@pytest.mark.parametrize("repeat_count", [2, 4])
def test_target_repeats_bind_exact_admission_repeat_count(repeat_count: int):
    receipt = _receipt()
    target = receipt.target_plan.targets[0]
    with pytest.raises(CommissioningReceiptError, match="admission repeat count"):
        _target_verification(
            target,
            context=receipt.commissioning_context_fingerprint,
            start=8,
            repeat_count=repeat_count,
        )


def test_target_and_receipt_enforce_unique_capture_ids_raws_and_one_session():
    receipt = _receipt()
    left = receipt.post_apply_targets[0]
    duplicated_id_capture = replace(
        left.captures[1],
        capture_id=left.captures[0].capture_id,
    )
    duplicated_id_proof = _admitted(duplicated_id_capture)
    with pytest.raises(CommissioningReceiptError, match="must be unique"):
        replace(
            left,
            admitted_captures=(
                left.admitted_captures[0],
                duplicated_id_proof,
                left.admitted_captures[2],
            ),
        )

    right = receipt.post_apply_targets[1]
    foreign = _target_verification(
        receipt.target_plan.targets[1],
        context=receipt.commissioning_context_fingerprint,
        start=4,
        session="another-session",
    )
    with pytest.raises(CommissioningReceiptError, match="one commissioning session"):
        replace(receipt, post_apply_targets=(left, foreign))

    duplicate_global = _capture(
        4,
        target=receipt.target_plan.targets[1],
        context=receipt.commissioning_context_fingerprint,
        capture_id=left.captures[0].capture_id,
    )
    duplicate_global_proof = _admitted(duplicate_global)
    changed_right = replace(
        right,
        admitted_captures=(
            duplicate_global_proof,
            right.admitted_captures[1],
            right.admitted_captures[2],
        ),
    )
    with pytest.raises(CommissioningReceiptError, match="globally unique"):
        replace(receipt, post_apply_targets=(left, changed_right))


def test_applied_candidate_uses_typed_normalized_graph_and_exact_predecessor():
    plan = _plan()
    proof = _proof(plan)

    assert proof.expected_normalized_graph == proof.observed_fresh_readback_graph
    assert (
        proof.predecessor_state.fingerprint
        != proof.expected_normalized_graph.fingerprint
    )
    with pytest.raises(CommissioningReceiptError, match="expected normalized"):
        replace(
            proof,
            observed_fresh_readback_graph=_normalized_graph("different"),
        )


def test_rollback_records_attempted_unknown_and_exact_restore_honestly():
    predecessor = _predecessor()
    no_mutation = CommissioningRollbackEvidence(
        mutation_state="not_attempted",
        status="not_applicable",
        evidence_kind="no_mutation",
        operation_id="lane-c-apply-blocked",
        failure_code="writer_lock_unavailable",
    )
    uncertain = CommissioningRollbackEvidence(
        mutation_state="attempted",
        status="unknown",
        evidence_kind="uncertain_mutation",
        operation_id="lane-c-apply-unknown",
        mutation_fingerprint=_hash("f"),
        predecessor_state=predecessor,
        failure_code="mutation_outcome_unknown",
    )
    restored = CommissioningRollbackEvidence(
        mutation_state="unknown",
        status="restored",
        evidence_kind="exact_restore",
        operation_id="lane-c-apply-restored",
        mutation_fingerprint=_hash("f"),
        predecessor_state=predecessor,
        restored_state=ExactDspStateIdentity(predecessor.state),
        failure_code="candidate_readback_mismatch",
    )

    for evidence in (no_mutation, uncertain, restored):
        assert (
            CommissioningRollbackEvidence.from_mapping(evidence.to_dict()) == evidence
        )
    with pytest.raises(CommissioningReceiptError, match="exact predecessor"):
        replace(restored, restored_state=_predecessor("wrong"))
    with pytest.raises(CommissioningReceiptError, match="unsupported rollback"):
        replace(uncertain, failure_code="free_form_failure")


@pytest.mark.parametrize(
    "failure_code",
    [
        "mutation_outcome_unknown",
        "fresh_readback_failed",
        "rollback_apply_failed",
        "rollback_readback_failed",
        "rollback_readback_mismatch",
    ],
)
def test_no_mutation_rollback_refuses_post_mutation_failure_codes(failure_code: str):
    with pytest.raises(CommissioningReceiptError, match="pre-mutation failure"):
        CommissioningRollbackEvidence(
            mutation_state="not_attempted",
            status="not_applicable",
            evidence_kind="no_mutation",
            operation_id="lane-c-apply-blocked",
            failure_code=failure_code,
        )


def test_rollback_failure_codes_match_status_and_evidence_kind():
    predecessor = _predecessor()
    with pytest.raises(CommissioningReceiptError, match="mutation failure"):
        CommissioningRollbackEvidence(
            mutation_state="applied",
            status="restored",
            evidence_kind="exact_restore",
            operation_id="lane-c-apply-restore",
            mutation_fingerprint=_hash("f"),
            predecessor_state=predecessor,
            restored_state=ExactDspStateIdentity(predecessor.state),
            failure_code="rollback_apply_failed",
        )
    with pytest.raises(CommissioningReceiptError, match="rollback-operation"):
        CommissioningRollbackEvidence(
            mutation_state="applied",
            status="failed",
            evidence_kind="uncertain_mutation",
            operation_id="lane-c-apply-failed",
            mutation_fingerprint=_hash("f"),
            predecessor_state=predecessor,
            failure_code="candidate_readback_mismatch",
        )
    with pytest.raises(CommissioningReceiptError, match="rollback-operation"):
        CommissioningRollbackEvidence(
            mutation_state="applied",
            status="failed",
            evidence_kind="uncertain_mutation",
            operation_id="lane-c-apply-failed",
            mutation_fingerprint=_hash("f"),
            predecessor_state=predecessor,
            failure_code="rollback_readback_failed",
        )
    with pytest.raises(CommissioningReceiptError, match="failure evidence"):
        CommissioningRollbackEvidence(
            mutation_state="applied",
            status="unknown",
            evidence_kind="uncertain_mutation",
            operation_id="lane-c-apply-unknown",
            mutation_fingerprint=_hash("f"),
            predecessor_state=predecessor,
            failure_code="rollback_readback_mismatch",
        )


@pytest.mark.parametrize(
    ("payload", "parser"),
    [
        (
            lambda: _receipt().target_plan.targets[0].to_dict(),
            RequiredVerificationTarget.from_mapping,
        ),
        (lambda: _receipt().target_plan.to_dict(), RequiredTargetPlan.from_mapping),
        (
            lambda: _receipt().applied_candidate.to_dict(),
            AppliedCandidateProof.from_mapping,
        ),
        (
            lambda: _receipt().rollback.to_dict(),
            CommissioningRollbackEvidence.from_mapping,
        ),
        (
            lambda: _receipt().post_apply_targets[0].admitted_captures[0].to_dict(),
            AdmittedCaptureProof.from_mapping,
        ),
        (
            lambda: _receipt().post_apply_targets[0].to_dict(),
            PostApplyTargetVerification.from_mapping,
        ),
        (lambda: _receipt().to_dict(), CommissioningEligibilityReceipt.from_mapping),
    ],
)
def test_every_active_authority_rejects_unknown_fields_and_bool_schema(
    payload,
    parser,
):
    unknown = payload()
    unknown["future_guess"] = True
    with pytest.raises(CommissioningReceiptError, match="unknown or missing fields"):
        parser(unknown)
    boolean_schema = payload()
    boolean_schema["schema_version"] = True
    with pytest.raises(CommissioningReceiptError, match="unsupported"):
        parser(boolean_schema)


def test_nested_evidence_tamper_invalidates_serialized_receipt():
    payload = copy.deepcopy(_receipt().to_dict())
    payload["post_apply_targets"][0]["admitted_captures"][0]["capture"][
        "quality_artifact"
    ]["byte_size"] += 1

    with pytest.raises(CommissioningReceiptError, match="declared fingerprint"):
        CommissioningEligibilityReceipt.from_mapping(payload)
