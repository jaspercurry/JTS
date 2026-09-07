# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from jasper.active_speaker import commissioning_apply as apply_module
from jasper.active_speaker import commissioning_isolated_producer as producer_module
from jasper.active_speaker import commissioning_service as service_module
from jasper.active_speaker.baseline_profile import (
    build_baseline_profile_candidate,
    recompose_applied_baseline_yaml,
)
from jasper.active_speaker.commissioning_lifecycle import CommissioningTransition
from jasper.audio_measurement.evidence_identity import ExactDspStateIdentity
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    ValidationStatus,
)
from tests.test_active_speaker_commissioning_receipt import _proof
from tests.test_active_speaker_commissioning_service import (
    _complete_candidate_evidence,
    _service_harness,
)


def _valid(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


def _candidate_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    harness = _service_harness(
        tmp_path,
        monkeypatch,
        candidate_evidence=True,
    )
    _complete_candidate_evidence(harness)
    harness.service.publish_candidate()
    current = harness.service._current()
    candidate, _ = harness.service._reopen_candidate(
        current,
        require_transition=True,
    )
    return harness, current, candidate


def test_production_compiler_uses_exact_measured_candidate_corrections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jasper.active_speaker import baseline_profile as baseline_module

    harness, current, candidate = _candidate_harness(tmp_path, monkeypatch)
    monkeypatch.setattr(
        baseline_module,
        "compile_preset_from_crossover_preview",
        lambda _topology, _preview: (candidate.source_preset, [], []),
    )
    config_path = tmp_path / "strict-candidate.yml"
    payload = build_baseline_profile_candidate(
        current.authority.topology,
        design_draft={},
        crossover_preview={
            "kind": "jts_active_speaker_crossover_preview",
            "status": "ready_for_protected_staging",
            "permissions": {"may_prepare_protected_startup_config": True},
        },
        measurements={},
        write=True,
        state_path=tmp_path / "strict-candidate.json",
        config_path=config_path,
        tuning_owner="automatic",
        measured_candidate=candidate,
        validate=_valid,
    )

    assert payload["status"] == "ready_to_apply", payload["issues"]
    assert payload["permissions"]["may_apply"] is True
    assert payload["corrections"] == candidate.driver_corrections()
    assert payload["source"]["measured_candidate_fingerprint"] == (
        candidate.fingerprint
    )
    assert payload["recomposition_snapshot"]["preset"] == (
        candidate.source_preset.to_dict()
    )
    assert payload["verification"]["driver_target_proof_source"] == (
        "measured_candidate"
    )
    # #1666: the candidate lands on its own source-fingerprinted sibling next
    # to config_path, never config_path itself.
    assert not config_path.exists()
    assert Path(payload["config"]["path"]).exists()
    assert Path(payload["config"]["path"]) != config_path


def test_known_restore_in_candidate_ready_is_retryable_not_restore_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, current, _candidate = _candidate_harness(tmp_path, monkeypatch)
    candidate, artifact = harness.service._reopen_candidate(
        current,
        require_transition=True,
    )
    run = harness.plan.authority.run
    issuance = harness.run_store.issue_live_mutation(
        run,
        purpose=apply_module.APPLY_PURPOSE,
        operation_fingerprint="b" * 64,
    )
    predecessor = harness.evidence_store.publish_json_artifact(
        apply_module._source_path(run, issuance.issuance_id, "predecessor.json"),
        {
            "schema_version": 1,
            "kind": "placeholder",
            "candidate_fingerprint": candidate.fingerprint,
        },
    )
    pending = harness.run_store.record_live_mutation_intent(
        run,
        issuance,
        rollback_artifact_path=predecessor.relative_path,
        rollback_artifact_fingerprint=predecessor.fingerprint,
    )
    restored = harness.run_store.record_live_mutation_restored(
        run,
        pending,
        restoration_evidence_fingerprint="c" * 64,
    )
    harness.run_store.record_live_mutation_aborted(
        run,
        restored,
        failure_evidence_fingerprint="d" * 64,
    )

    status = harness.service.status()

    assert status["status"] == "candidate_ready"
    assert status["candidate"]["fingerprint"] == candidate.fingerprint
    transition = harness.run_store.lifecycle_transition(run)
    assert transition is not None
    assert transition.evidence_fingerprint == artifact.fingerprint


@pytest.mark.parametrize(
    ("lifecycle", "mutation_status", "expected"),
    [
        ("candidate_ready", "mutation_pending", "restore_required"),
        ("blocked_live_state_unknown", "restored", "restore_finalization_required"),
        ("candidate_ready", "retained", "apply_finalization_required"),
        ("applied_unverified", "retained", "applied_unverified"),
    ],
)
def test_banked_apply_state_remains_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: str,
    mutation_status: str,
    expected: str,
) -> None:
    harness, current, candidate = _candidate_harness(tmp_path, monkeypatch)
    run = harness.plan.authority.run
    issuance = harness.run_store.issue_live_mutation(
        run,
        purpose=apply_module.APPLY_PURPOSE,
        operation_fingerprint="b" * 64,
    )
    target_plan = harness.service._required_target_plan(current)
    raw, issues = recompose_applied_baseline_yaml(
        current.authority.topology,
        applied_profile=current.authority.applied_profile,
    )
    assert raw is not None and not issues
    predecessor = ExactDspStateIdentity(
        {
            "active_raw": raw,
            "normalized_active_raw": yaml.safe_load(raw),
            "config_path": "/etc/camilladsp/predecessor.yml",
            "listening_volume_db": -36.0,
        }
    )
    proof = replace(
        _proof(target_plan),
        operation_id=issuance.issuance_id,
        mutation_fingerprint=issuance.operation_fingerprint,
        candidate_fingerprint=candidate.fingerprint,
        safety_profile_fingerprint=harness.plan.authority.protected_safety_profile_fingerprint,
        predecessor_state=predecessor,
    )
    store = harness.evidence_store
    source = f"runs/{run.run_id}/generations/{run.owner_generation}/candidate-apply/{issuance.issuance_id}"
    prior_artifact = store.publish_json_artifact(
        f"{source}/predecessor.json", predecessor.to_dict()
    )
    pending = harness.run_store.record_live_mutation_intent(
        run,
        issuance,
        rollback_artifact_path=prior_artifact.relative_path,
        rollback_artifact_fingerprint=prior_artifact.fingerprint,
    )
    proof_artifact = store.publish_json_artifact(
        f"{source}/applied-proof.json", proof.to_dict()
    )
    if mutation_status == "retained":
        harness.run_store.record_live_mutation_retained(
            run,
            pending,
            applied_proof_fingerprint=proof_artifact.fingerprint,
        )
    elif mutation_status == "restored":
        harness.run_store.record_live_mutation_restored(
            run,
            pending,
            restoration_evidence_fingerprint="c" * 64,
        )
    if lifecycle != "candidate_ready":
        assert harness.run_store.transition(
            run,
            CommissioningTransition(
                from_state="candidate_ready",
                to_state=lifecycle,
                evidence_kind=(
                    "applied_candidate_proof"
                    if lifecycle == "applied_unverified"
                    else "uncertain_mutation_evidence"
                ),
                evidence_fingerprint=proof_artifact.fingerprint,
                failure_code=(
                    "mutation_outcome_unknown"
                    if lifecycle == "blocked_live_state_unknown"
                    else None
                ),
            ),
        )
    if mutation_status == "retained":
        changed_profile = deepcopy(current.authority.applied_profile)
        changed_profile["recomposition_snapshot"]["corrections"]["tweeter"][
            "gain_db"
        ] -= 1.0
        changed_raw, issues = recompose_applied_baseline_yaml(
            current.authority.topology,
            applied_profile=changed_profile,
        )
        assert changed_raw is not None and not issues and changed_raw != raw
        harness.service.load_current_authority = lambda: replace(
            current.authority,
            applied_profile=changed_profile,
        )
        monkeypatch.setattr(
            producer_module,
            "active_region_threshold_profile_fingerprint",
            lambda: harness.plan.authority.threshold_profile_fingerprint,
        )
        monkeypatch.setattr(
            service_module,
            "reopen_region_evidence_plan_for_baseline",
            producer_module.reopen_region_evidence_plan_for_baseline,
        )
        monkeypatch.setattr(
            service_module,
            "current_region_evidence_plan",
            lambda **_kwargs: pytest.fail("retained status must use banked authority"),
        )
    else:

        def unavailable():
            raise ValueError("current product authority is unavailable")

        harness.service.load_current_authority = unavailable
    state_before = harness.run_store.snapshot()
    status = harness.service.status()
    assert status["status"] == expected
    assert harness.run_store.snapshot() == state_before
    if mutation_status == "retained":
        assert (
            status["profile_context_id"]
            == harness.authority.comparison_set["profile_context_id"]
        )
    if lifecycle == "applied_unverified":
        assert status["applied_candidate"]["proof_fingerprint"] == proof.fingerprint
        assert (
            status["applied_candidate"]["candidate_fingerprint"]
            == candidate.fingerprint
        )
