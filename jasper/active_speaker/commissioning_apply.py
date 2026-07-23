# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Apply one exact measured Active candidate through the existing DSP path."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
    json_fingerprint,
)
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    DspApplyState,
    DspWriterLockTimeout,
    apply_dsp_config,
    dsp_apply_lock_path,
    dsp_writer_lock,
    record_dsp_apply_state,
    validate_camilla_config,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from .baseline_profile import (
    baseline_candidate_fingerprint,
    baseline_config_path,
    baseline_profile_state_path,
    build_baseline_profile_candidate,
    persist_applied_baseline_profile,
    promote_applied_baseline_candidate,
)
from .commissioning_evidence_store import EVIDENCE_ROOT, CommissioningEvidenceStore
from .commissioning_lifecycle import CommissioningTransition
from .commissioning_receipt import (
    AppliedCandidateProof,
    CommissioningRollbackEvidence,
    RequiredTargetPlan,
)
from .commissioning_run import (
    CommissioningLiveMutation,
    CommissioningRunHandle,
    CommissioningRunStore,
)
from .commissioning_runtime import (
    CommissioningRuntimePort,
    LoadConfigPath,
    restore_exact_dsp_state_locked,
    snapshot_exact_dsp_state,
)
from .measured_candidate import MeasuredElectricalCandidate
from .runtime_contract import classify_active_bass_extension_graph

APPLY_PURPOSE = "measured_candidate_apply"
APPLY_SOURCE = "active_speaker_measured_candidate_apply"
logger = logging.getLogger(__name__)


class CommissioningApplyError(RuntimeError):
    """One exact candidate apply could not reach a truthful durable outcome."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _operation_fingerprint(
    *,
    run: CommissioningRunHandle,
    candidate: MeasuredElectricalCandidate,
    target_plan: RequiredTargetPlan,
    safety_profile_fingerprint: str,
) -> str:
    return json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_measured_candidate_apply_operation",
            "run": {
                "session_id": run.session_id,
                "run_id": run.run_id,
                "owner_generation": run.owner_generation,
            },
            "candidate_fingerprint": candidate.fingerprint,
            "target_plan_fingerprint": target_plan.fingerprint,
            "safety_profile_fingerprint": safety_profile_fingerprint,
        }
    )


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


def _publish_json(
    store: CommissioningEvidenceStore,
    run: CommissioningRunHandle,
    issuance_id: str,
    filename: str,
    payload: Mapping[str, Any],
    *,
    owner_generation: int | None = None,
) -> ArtifactIdentity:
    value = dict(payload)
    artifact = store.publish_json_artifact(
        _source_path(
            run,
            issuance_id,
            filename,
            owner_generation=owner_generation,
        ),
        value,
    )
    if store.reopen_json_artifact(artifact) != value:
        raise CommissioningApplyError(
            "apply_artifact_readback_mismatch",
            f"{filename} changed on exact readback",
        )
    return artifact


def _normalized_config(path: str | Path) -> NormalizedActiveRawIdentity:
    try:
        parsed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CommissioningApplyError(
            "candidate_apply_failed_before_mutation",
            "compiled candidate YAML could not be reopened",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise CommissioningApplyError(
            "candidate_apply_failed_before_mutation",
            "compiled candidate YAML is not an object",
        )
    return NormalizedActiveRawIdentity(dict(parsed))


def _candidate_path(candidate: Mapping[str, Any]) -> str:
    config = candidate.get("config")
    path = str(config.get("path") or "") if isinstance(config, Mapping) else ""
    if not path:
        raise CommissioningApplyError(
            "candidate_apply_failed_before_mutation",
            "compiled candidate has no config path",
        )
    return path


def _candidate_sha(candidate: Mapping[str, Any]) -> str:
    config = candidate.get("config")
    value = str(config.get("sha256") or "") if isinstance(config, Mapping) else ""
    if len(value) != 64:
        raise CommissioningApplyError(
            "candidate_apply_failed_before_mutation",
            "compiled candidate has no exact config digest",
        )
    return value


def _exact_graph(state: ExactDspStateIdentity) -> NormalizedActiveRawIdentity:
    normalized = state.state.get("normalized_active_raw")
    if not isinstance(normalized, Mapping):
        raise CommissioningApplyError(
            "fresh_readback_failed", "fresh DSP state omitted its normalized graph"
        )
    return NormalizedActiveRawIdentity(dict(normalized))


def _same_volume(
    predecessor: ExactDspStateIdentity,
    observed: ExactDspStateIdentity,
) -> bool:
    before = predecessor.state.get("listening_volume_db")
    after = observed.state.get("listening_volume_db")
    return (
        isinstance(before, (int, float))
        and not isinstance(before, bool)
        and isinstance(after, (int, float))
        and not isinstance(after, bool)
        and math.isclose(float(before), float(after), rel_tol=0.0, abs_tol=1e-6)
    )


def _writer_lock_fingerprint(config_dir: Path, issuance_id: str) -> str:
    return json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_candidate_writer_lock_scope",
            "source": APPLY_SOURCE,
            "issuance_id": issuance_id,
            "lock_path": str(dsp_apply_lock_path(config_dir)),
        }
    )


def _protection_fingerprint(
    *,
    topology: OutputTopology,
    graph: NormalizedActiveRawIdentity,
    decision: Mapping[str, Any],
    safety_profile_fingerprint: str,
) -> str:
    return json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_applied_graph_protection_proof",
            "topology_id": topology.topology_id,
            "topology": topology.to_dict(),
            "graph_fingerprint": graph.fingerprint,
            "safety_profile_fingerprint": safety_profile_fingerprint,
            "runtime_contract": dict(decision),
        }
    )


def _rollback_artifact(
    *,
    store: CommissioningEvidenceStore,
    run: CommissioningRunHandle,
    issuance_id: str,
    filename: str,
    evidence: CommissioningRollbackEvidence,
) -> ArtifactIdentity:
    return _publish_json(store, run, issuance_id, filename, evidence.to_dict())


def _record_no_mutation_failure(
    *,
    run: CommissioningRunHandle,
    store: CommissioningEvidenceStore,
    issuance: CommissioningLiveMutation,
    failure_code: str,
) -> tuple[CommissioningRollbackEvidence, ArtifactIdentity]:
    evidence = CommissioningRollbackEvidence(
        mutation_state="not_attempted",
        status="not_applicable",
        evidence_kind="no_mutation",
        operation_id=issuance.issuance_id,
        failure_code=failure_code,
    )
    artifact = _rollback_artifact(
        store=store,
        run=run,
        issuance_id=issuance.issuance_id,
        filename="apply-failure.json",
        evidence=evidence,
    )
    return evidence, artifact


def _pre_mutation_block(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    issuance: CommissioningLiveMutation,
    failure_code: str,
) -> dict[str, Any]:
    run_store.release_live_mutation(run, issuance)
    evidence, artifact = _record_no_mutation_failure(
        run=run,
        store=store,
        issuance=issuance,
        failure_code=failure_code,
    )
    if not run_store.transition(
        run,
        CommissioningTransition(
            from_state="candidate_ready",
            to_state="blocked",
            evidence_kind="failure_evidence",
            evidence_fingerprint=artifact.fingerprint,
            failure_code=failure_code,
        ),
    ):
        raise CommissioningApplyError(
            "run_generation_stale", "candidate apply lost current run ownership"
        )
    return {
        "status": "blocked",
        "failure_code": failure_code,
        "rollback": evidence.to_dict(),
    }


def _pre_mutation_retry(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    issuance: CommissioningLiveMutation,
    failure_code: str,
) -> dict[str, Any]:
    """Release a transient refusal without discarding measured authority."""

    run_store.release_live_mutation(run, issuance)
    evidence, artifact = _record_no_mutation_failure(
        run=run,
        store=store,
        issuance=issuance,
        failure_code=failure_code,
    )
    log_event(
        logger,
        "correction.active_commissioning_candidate_apply_deferred",
        session=run.session_id,
        run_id=run.run_id,
        owner_generation=run.owner_generation,
        issuance_id=issuance.issuance_id,
        failure_code=failure_code,
        evidence_fingerprint=artifact.fingerprint,
    )
    return {
        "status": "candidate_ready",
        "failure_code": failure_code,
        "rollback": evidence.to_dict(),
    }


def _record_exact_restore_apply_state(
    *,
    apply_state: DspApplyState | None,
    issuance: CommissioningLiveMutation,
    predecessor: ExactDspStateIdentity,
    candidate_config_path: str,
    failure_code: str,
    rollback_succeeded: bool,
    rollback_error: str | None = None,
    source: str = APPLY_SOURCE,
) -> None:
    state = apply_state
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if state is None:
        state = DspApplyState(
            schema_version=1,
            op_id=issuance.issuance_id,
            source=source,
            phase="rollback",
            result="in_progress",
            started_at=now,
            finished_at=None,
            prior_config_path=str(predecessor.state["config_path"]),
            candidate_config_path=candidate_config_path,
        )
    state.phase = "done"
    state.result = (
        f"active_wrapper_{failure_code}_rolled_back"
        if rollback_succeeded
        else f"active_wrapper_{failure_code}_rollback_failed"
    )
    state.finished_at = now
    state.active_config_path = (
        str(predecessor.state["config_path"]) if rollback_succeeded else None
    )
    state.rollback_attempted = True
    state.rollback_succeeded = rollback_succeeded
    state.rollback_error = rollback_error
    record_dsp_apply_state(state)


async def _restore_failed_mutation_locked(
    *,
    error: BaseException,
    failure_code: str,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    issuance: CommissioningLiveMutation,
    pending: CommissioningLiveMutation,
    operation_fingerprint: str,
    predecessor: ExactDspStateIdentity,
    runtime_port: CommissioningRuntimePort,
    load_config_path: LoadConfigPath,
    candidate_config_path: str,
    apply_state: DspApplyState | None,
    observed_graph: NormalizedActiveRawIdentity | None,
    restore_started: asyncio.Event,
) -> dict[str, Any]:
    """Exactly restore, then durably resolve the pending apply before unlock."""

    if isinstance(error, DspApplyError):
        failure_code = "mutation_outcome_unknown"
    elif isinstance(error, CommissioningApplyError):
        failure_code = error.code
    unknown = CommissioningRollbackEvidence(
        mutation_state="applied" if apply_state is not None else "attempted",
        status="unknown",
        evidence_kind="uncertain_mutation",
        operation_id=issuance.issuance_id,
        mutation_fingerprint=operation_fingerprint,
        observed_applied_graph_fingerprint=(
            observed_graph.fingerprint if observed_graph is not None else None
        ),
        predecessor_state=predecessor,
        failure_code=failure_code,
    )
    try:
        await _shielded_restore_locked(
            runtime_port,
            predecessor,
            load_config_path=load_config_path,
            restore_started=restore_started,
        )
    except BaseException as restore_exc:  # noqa: BLE001 - evidence for cancellation too
        observed_apply_state = (
            error.state
            if apply_state is None and isinstance(error, DspApplyError)
            else apply_state
        )
        _record_exact_restore_apply_state(
            apply_state=observed_apply_state,
            issuance=issuance,
            predecessor=predecessor,
            candidate_config_path=candidate_config_path,
            failure_code=failure_code,
            rollback_succeeded=False,
            rollback_error=str(restore_exc),
        )
        restore_failure = CommissioningRollbackEvidence(
            mutation_state="unknown",
            status="unknown",
            evidence_kind="uncertain_mutation",
            operation_id=issuance.issuance_id,
            mutation_fingerprint=operation_fingerprint,
            observed_applied_graph_fingerprint=(
                observed_graph.fingerprint if observed_graph is not None else None
            ),
            predecessor_state=predecessor,
            failure_code="rollback_readback_failed",
        )
        _rollback_artifact(
            store=store,
            run=run,
            issuance_id=issuance.issuance_id,
            filename="restore-failure.json",
            evidence=restore_failure,
        )
        raise CommissioningApplyError(
            "candidate_restore_required",
            "candidate apply failed and exact predecessor restore is not proved",
        ) from restore_exc

    observed_apply_state = (
        error.state
        if apply_state is None and isinstance(error, DspApplyError)
        else apply_state
    )
    _record_exact_restore_apply_state(
        apply_state=observed_apply_state,
        issuance=issuance,
        predecessor=predecessor,
        candidate_config_path=candidate_config_path,
        failure_code=failure_code,
        rollback_succeeded=True,
    )
    unknown_artifact = _rollback_artifact(
        store=store,
        run=run,
        issuance_id=issuance.issuance_id,
        filename="uncertain-mutation.json",
        evidence=unknown,
    )
    lifecycle = run_store.lifecycle_state(run)
    if lifecycle == "candidate_ready":
        if not run_store.transition(
            run,
            CommissioningTransition(
                from_state="candidate_ready",
                to_state="blocked_live_state_unknown",
                evidence_kind="uncertain_mutation_evidence",
                evidence_fingerprint=unknown_artifact.fingerprint,
                failure_code=failure_code,
            ),
        ):
            raise CommissioningApplyError(
                "run_generation_stale", "uncertain candidate mutation lost ownership"
            )
    elif lifecycle != "blocked_live_state_unknown":
        raise CommissioningApplyError(
            "apply_lifecycle_stale",
            f"restored candidate mutation cannot resolve from {lifecycle}",
        )

    restored = CommissioningRollbackEvidence(
        mutation_state="applied" if apply_state is not None else "attempted",
        status="restored",
        evidence_kind="exact_restore",
        operation_id=issuance.issuance_id,
        mutation_fingerprint=operation_fingerprint,
        observed_applied_graph_fingerprint=(
            observed_graph.fingerprint if observed_graph is not None else None
        ),
        predecessor_state=predecessor,
        restored_state=predecessor,
        failure_code=failure_code,
    )
    restored_artifact = _rollback_artifact(
        store=store,
        run=run,
        issuance_id=issuance.issuance_id,
        filename="restored.json",
        evidence=restored,
    )
    restored_mutation = run_store.record_live_mutation_restored(
        run,
        pending,
        restoration_evidence_fingerprint=restored_artifact.fingerprint,
    )
    run_store.record_live_mutation_aborted(
        run,
        restored_mutation,
        failure_evidence_fingerprint=unknown_artifact.fingerprint,
    )
    if not run_store.transition(
        run,
        CommissioningTransition(
            from_state="blocked_live_state_unknown",
            to_state="rolled_back",
            evidence_kind="exact_restore_evidence",
            evidence_fingerprint=restored_artifact.fingerprint,
        ),
    ):
        raise CommissioningApplyError(
            "run_generation_stale", "exact candidate restore lost run ownership"
        )
    return {
        "status": "rolled_back",
        "failure_code": failure_code,
        "rollback": restored.to_dict(),
    }


async def _shielded_restore_locked(
    runtime_port: CommissioningRuntimePort,
    predecessor: ExactDspStateIdentity,
    *,
    load_config_path: LoadConfigPath,
    restore_started: asyncio.Event,
) -> None:
    """Restore in the lock-owning task while its caller shields that task."""

    restore_started.set()
    await restore_exact_dsp_state_locked(
        runtime_port,
        predecessor,
        load_config_path=load_config_path,
    )


async def _shielded_pending_restore(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    runtime_port: CommissioningRuntimePort,
    predecessor: ExactDspStateIdentity,
    mutation: CommissioningLiveMutation,
    load_config_path: LoadConfigPath,
    config_path: str | Path | None,
) -> bool:
    """Drain writer admission plus exact restart recovery despite cancellation."""

    async def restore() -> None:
        with run_store.claim_live_execution(run):
            async with dsp_writer_lock(
                baseline_config_path(config_path).parent,
                source=f"{APPLY_SOURCE}_recovery",
            ):
                try:
                    await restore_exact_dsp_state_locked(
                        runtime_port,
                        predecessor,
                        load_config_path=load_config_path,
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    _record_exact_restore_apply_state(
                        apply_state=None,
                        issuance=mutation,
                        predecessor=predecessor,
                        candidate_config_path=str(
                            predecessor.state["config_path"]
                        ),
                        failure_code="restart_recovery",
                        rollback_succeeded=False,
                        rollback_error=str(exc),
                        source=f"{APPLY_SOURCE}_recovery",
                    )
                    raise
                _record_exact_restore_apply_state(
                    apply_state=None,
                    issuance=mutation,
                    predecessor=predecessor,
                    candidate_config_path=str(predecessor.state["config_path"]),
                    failure_code="restart_recovery",
                    rollback_succeeded=True,
                    source=f"{APPLY_SOURCE}_recovery",
                )

    task = asyncio.create_task(restore())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


def _record_retained_or_reopen(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    pending: CommissioningLiveMutation,
    applied_proof_fingerprint: str,
) -> CommissioningLiveMutation:
    """Resolve an ambiguous sidecar write only from its exact persisted value."""

    try:
        return run_store.record_live_mutation_retained(
            run,
            pending,
            applied_proof_fingerprint=applied_proof_fingerprint,
        )
    except (OSError, RuntimeError):
        persisted = run_store.current_live_mutation(run)
        if (
            persisted is not None
            and persisted.status == "retained"
            and persisted.issuance_id == pending.issuance_id
            and persisted.operation_fingerprint == pending.operation_fingerprint
            and persisted.terminal_evidence_fingerprint == applied_proof_fingerprint
        ):
            return persisted
        raise


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


def finalize_retained_candidate_apply(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    mutation: CommissioningLiveMutation,
    candidate: MeasuredElectricalCandidate,
    target_plan: RequiredTargetPlan,
    safety_profile_fingerprint: str,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Finish only durable bookkeeping after a proven retained graph."""

    proof, artifact = reopen_applied_candidate_proof(
        store=store,
        run=run,
        mutation=mutation,
        candidate=candidate,
        target_plan=target_plan,
        safety_profile_fingerprint=safety_profile_fingerprint,
    )
    baseline_artifact = _identify(
        store,
        run,
        mutation.issuance_id,
        "baseline-candidate.json",
        owner_generation=mutation.started_owner_generation,
    )
    baseline = store.reopen_json_artifact(baseline_artifact)
    apply_state_artifact = _identify(
        store,
        run,
        mutation.issuance_id,
        "dsp-apply-state.json",
        owner_generation=mutation.started_owner_generation,
    )
    apply_state = store.reopen_json_artifact(apply_state_artifact)
    if (
        (baseline.get("source") or {}).get("measured_candidate_fingerprint")
        != candidate.fingerprint
        or baseline_candidate_fingerprint(baseline)
        != baseline.get("candidate_fingerprint")
        or _normalized_config(_candidate_path(baseline)).fingerprint
        != proof.expected_normalized_graph.fingerprint
        or apply_state.get("result") != "success"
    ):
        raise CommissioningApplyError(
            "applied_baseline_stale",
            "retained apply artifacts do not equal the reviewed candidate graph",
        )
    applied = persist_applied_baseline_profile(
        baseline,
        apply_state=apply_state,
        state_path=state_path,
    )
    # #1666: build_baseline_profile_candidate now writes every candidate to a
    # content-addressed sibling, never the canonical baseline_config_path()
    # name directly -- the same treatment as the /correction/ apply/restore
    # seam in baseline_profile.py, since commissioning rides the identical
    # write-then-apply-then-persist shape (and is, on a fresh speaker, the
    # apply that would otherwise leave the canonical file never created at
    # all). Fail-soft; never raises.
    promote_applied_baseline_candidate(applied, config_path=config_path)
    lifecycle = run_store.lifecycle_state(run)
    if lifecycle == "candidate_ready":
        if not run_store.transition(
            run,
            CommissioningTransition(
                from_state="candidate_ready",
                to_state="applied_unverified",
                evidence_kind="applied_candidate_proof",
                evidence_fingerprint=artifact.fingerprint,
            ),
        ):
            raise CommissioningApplyError(
                "run_generation_stale", "applied candidate lost current run ownership"
            )
    elif lifecycle != "applied_unverified":
        raise CommissioningApplyError(
            "apply_lifecycle_stale",
            f"retained apply cannot finalize from {lifecycle}",
        )
    return {
        "status": "applied_unverified",
        "candidate_fingerprint": candidate.fingerprint,
        "applied_candidate_proof": proof.to_dict(),
        "applied_profile": applied,
    }


async def restore_pending_candidate_apply(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    runtime_port: CommissioningRuntimePort,
    load_config_path: LoadConfigPath,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Recover one crash-interrupted candidate mutation from its exact anchor."""

    mutation = run_store.current_live_mutation(run)
    if mutation is None or mutation.purpose != APPLY_PURPOSE:
        raise CommissioningApplyError(
            "candidate_restore_not_required",
            "there is no pending candidate apply to restore",
        )
    if mutation.status == "issued":
        run_store.release_live_mutation(run, mutation)
        return {"status": "candidate_ready", "mutation_state": "not_attempted"}
    if mutation.status not in {"mutation_pending", "restored", "aborted"}:
        raise CommissioningApplyError(
            "candidate_restore_not_required",
            f"candidate apply state {mutation.status} does not require restore",
        )
    if (
        mutation.rollback_artifact_path is None
        or mutation.rollback_artifact_fingerprint is None
    ):
        raise CommissioningApplyError(
            "candidate_restore_anchor_invalid",
            "pending candidate apply has no exact predecessor pointer",
        )
    predecessor_artifact = store.identify_artifact(mutation.rollback_artifact_path)
    if predecessor_artifact.fingerprint != mutation.rollback_artifact_fingerprint:
        raise CommissioningApplyError(
            "candidate_restore_anchor_invalid",
            "pending predecessor pointer changed",
        )
    predecessor = ExactDspStateIdentity.from_mapping(
        store.reopen_json_artifact(predecessor_artifact)
    )
    lifecycle = run_store.lifecycle_state(run)
    if mutation.status in {"restored", "aborted"}:
        return _finalize_restored_recovery(
            run=run,
            run_store=run_store,
            store=store,
            mutation=mutation,
            predecessor=predecessor,
            lifecycle=lifecycle,
        )
    if lifecycle == "candidate_ready":
        unknown = CommissioningRollbackEvidence(
            mutation_state="unknown",
            status="unknown",
            evidence_kind="uncertain_mutation",
            operation_id=mutation.issuance_id,
            mutation_fingerprint=mutation.operation_fingerprint,
            predecessor_state=predecessor,
            failure_code="mutation_outcome_unknown",
        )
        unknown_artifact = None
        failure_code = "mutation_outcome_unknown"
    elif lifecycle == "blocked_live_state_unknown":
        transition = run_store.lifecycle_transition(run)
        if transition is None or transition.failure_code is None:
            raise CommissioningApplyError(
                "candidate_restore_anchor_invalid",
                "uncertain lifecycle has no exact failure evidence",
            )
        failure_code = transition.failure_code
        unknown_artifact = _identify(
            store,
            run,
            mutation.issuance_id,
            "uncertain-mutation.json",
            owner_generation=mutation.started_owner_generation,
        )
        unknown = CommissioningRollbackEvidence.from_mapping(
            store.reopen_json_artifact(unknown_artifact)
        )
        if (
            transition.evidence_fingerprint != unknown_artifact.fingerprint
            or unknown.mutation_fingerprint != mutation.operation_fingerprint
            or unknown.predecessor_state != predecessor
        ):
            raise CommissioningApplyError(
                "candidate_restore_anchor_invalid",
                "uncertain mutation evidence does not equal its predecessor",
            )
    else:
        raise CommissioningApplyError(
            "candidate_restore_not_required",
            f"pending candidate restore is incompatible with {lifecycle}",
        )

    try:
        cancelled = await _shielded_pending_restore(
            run=run,
            run_store=run_store,
            runtime_port=runtime_port,
            predecessor=predecessor,
            mutation=mutation,
            load_config_path=load_config_path,
            config_path=config_path,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        restore_failure = CommissioningRollbackEvidence(
            mutation_state="unknown",
            status="unknown",
            evidence_kind="uncertain_mutation",
            operation_id=mutation.issuance_id,
            mutation_fingerprint=mutation.operation_fingerprint,
            predecessor_state=predecessor,
            failure_code="rollback_readback_failed",
        )
        _publish_json(
            store,
            run,
            mutation.issuance_id,
            "restore-failure.json",
            restore_failure.to_dict(),
            owner_generation=mutation.started_owner_generation,
        )
        raise CommissioningApplyError(
            "candidate_restore_required",
            "the exact previous crossover could not be restored",
        ) from exc

    if unknown_artifact is None:
        unknown_artifact = _publish_json(
            store,
            run,
            mutation.issuance_id,
            "uncertain-mutation.json",
            unknown.to_dict(),
            owner_generation=mutation.started_owner_generation,
        )
        if not run_store.transition(
            run,
            CommissioningTransition(
                from_state="candidate_ready",
                to_state="blocked_live_state_unknown",
                evidence_kind="uncertain_mutation_evidence",
                evidence_fingerprint=unknown_artifact.fingerprint,
                failure_code="mutation_outcome_unknown",
            ),
        ):
            raise CommissioningApplyError(
                "run_generation_stale", "candidate recovery lost run ownership"
            )

    restored = CommissioningRollbackEvidence(
        mutation_state="unknown",
        status="restored",
        evidence_kind="exact_restore",
        operation_id=mutation.issuance_id,
        mutation_fingerprint=mutation.operation_fingerprint,
        predecessor_state=predecessor,
        restored_state=predecessor,
        failure_code=failure_code,
    )
    restored_artifact = _publish_json(
        store,
        run,
        mutation.issuance_id,
        "restored.json",
        restored.to_dict(),
        owner_generation=mutation.started_owner_generation,
    )
    restored_mutation = run_store.record_live_mutation_restored(
        run,
        mutation,
        restoration_evidence_fingerprint=restored_artifact.fingerprint,
    )
    run_store.record_live_mutation_aborted(
        run,
        restored_mutation,
        failure_evidence_fingerprint=unknown_artifact.fingerprint,
    )
    if not run_store.transition(
        run,
        CommissioningTransition(
            from_state="blocked_live_state_unknown",
            to_state="rolled_back",
            evidence_kind="exact_restore_evidence",
            evidence_fingerprint=restored_artifact.fingerprint,
        ),
    ):
        raise CommissioningApplyError(
            "run_generation_stale", "restored candidate lost current run ownership"
        )
    if cancelled:
        raise asyncio.CancelledError()
    return {"status": "rolled_back", "rollback": restored.to_dict()}


def _finalize_restored_recovery(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    mutation: CommissioningLiveMutation,
    predecessor: ExactDspStateIdentity,
    lifecycle: str,
) -> dict[str, Any]:
    """Finish bookkeeping for a restore already proved before a crash."""

    if lifecycle != "blocked_live_state_unknown":
        raise CommissioningApplyError(
            "candidate_restore_not_required",
            f"restored candidate recovery is incompatible with {lifecycle}",
        )
    unknown_artifact = _identify(
        store,
        run,
        mutation.issuance_id,
        "uncertain-mutation.json",
        owner_generation=mutation.started_owner_generation,
    )
    restored_artifact = _identify(
        store,
        run,
        mutation.issuance_id,
        "restored.json",
        owner_generation=mutation.started_owner_generation,
    )
    unknown = CommissioningRollbackEvidence.from_mapping(
        store.reopen_json_artifact(unknown_artifact)
    )
    restored = CommissioningRollbackEvidence.from_mapping(
        store.reopen_json_artifact(restored_artifact)
    )
    transition = run_store.lifecycle_transition(run)
    if (
        mutation.restoration_evidence_fingerprint != restored_artifact.fingerprint
        or unknown.mutation_fingerprint != mutation.operation_fingerprint
        or unknown.predecessor_state != predecessor
        or restored.mutation_fingerprint != mutation.operation_fingerprint
        or restored.predecessor_state != predecessor
        or restored.restored_state != predecessor
        or transition is None
        or transition.evidence_fingerprint != unknown_artifact.fingerprint
    ):
        raise CommissioningApplyError(
            "candidate_restore_anchor_invalid",
            "restored candidate evidence does not equal its exact pending mutation",
        )
    if mutation.status == "restored":
        mutation = run_store.record_live_mutation_aborted(
            run,
            mutation,
            failure_evidence_fingerprint=unknown_artifact.fingerprint,
        )
    elif mutation.terminal_evidence_fingerprint != unknown_artifact.fingerprint:
        raise CommissioningApplyError(
            "candidate_restore_anchor_invalid",
            "aborted candidate mutation does not retain its failure evidence",
        )
    if not run_store.transition(
        run,
        CommissioningTransition(
            from_state="blocked_live_state_unknown",
            to_state="rolled_back",
            evidence_kind="exact_restore_evidence",
            evidence_fingerprint=restored_artifact.fingerprint,
        ),
    ):
        raise CommissioningApplyError(
            "run_generation_stale", "restored candidate lost current run ownership"
        )
    return {"status": "rolled_back", "rollback": restored.to_dict()}


async def _apply_measured_candidate_owned(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    candidate: MeasuredElectricalCandidate,
    target_plan: RequiredTargetPlan,
    safety_profile_fingerprint: str,
    topology: OutputTopology,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    runtime_port: CommissioningRuntimePort,
    load_config_path: LoadConfigPath,
    verify_current: Callable[[], None],
    restore_started: asyncio.Event,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Compile, apply, read back, retain, or exactly restore one candidate."""

    operation = _operation_fingerprint(
        run=run,
        candidate=candidate,
        target_plan=target_plan,
        safety_profile_fingerprint=safety_profile_fingerprint,
    )
    current_mutation = run_store.current_live_mutation(run)
    if (
        current_mutation is not None
        and current_mutation.status == "issued"
        and current_mutation.purpose == APPLY_PURPOSE
    ):
        run_store.release_live_mutation(run, current_mutation)
        current_mutation = None
    if current_mutation is not None and current_mutation.status == "retained":
        return finalize_retained_candidate_apply(
            run=run,
            run_store=run_store,
            store=store,
            mutation=current_mutation,
            candidate=candidate,
            target_plan=target_plan,
            safety_profile_fingerprint=safety_profile_fingerprint,
            state_path=state_path,
            config_path=config_path,
        )
    if current_mutation is not None and current_mutation.status not in {
        "aborted",
        "committed",
        "released",
    }:
        raise CommissioningApplyError(
            "candidate_restore_required",
            "a prior live mutation must be restored before applying",
        )

    issuance = run_store.issue_live_mutation(
        run,
        purpose=APPLY_PURPOSE,
        operation_fingerprint=operation,
    )
    pending: CommissioningLiveMutation | None = None
    predecessor: ExactDspStateIdentity | None = None
    apply_state: DspApplyState | None = None
    observed_graph: NormalizedActiveRawIdentity | None = None
    config_dir = baseline_config_path(config_path).parent
    failure_code = "mutation_outcome_unknown"

    try:
        with run_store.claim_live_execution(run):
            async with dsp_writer_lock(config_dir, source=APPLY_SOURCE):
                verify_current()
                baseline = build_baseline_profile_candidate(
                    topology,
                    design_draft=design_draft,
                    crossover_preview=crossover_preview,
                    measurements=measurements,
                    write=True,
                    state_path=state_path,
                    config_path=config_path,
                    tuning_owner="automatic",
                    measured_candidate=candidate,
                    validate=validate,
                )
                if baseline.get("permissions", {}).get("may_apply") is not True:
                    return _pre_mutation_block(
                        run=run,
                        run_store=run_store,
                        store=store,
                        issuance=issuance,
                        failure_code="candidate_apply_failed_before_mutation",
                    )
                baseline_artifact = _publish_json(
                    store,
                    run,
                    issuance.issuance_id,
                    "baseline-candidate.json",
                    baseline,
                )
                if store.reopen_json_artifact(baseline_artifact) != baseline:
                    raise CommissioningApplyError(
                        "candidate_apply_failed_before_mutation",
                        "compiled baseline candidate changed on readback",
                    )
                expected_graph = _normalized_config(_candidate_path(baseline))
                predecessor = await snapshot_exact_dsp_state(runtime_port)
                predecessor_artifact = _publish_json(
                    store,
                    run,
                    issuance.issuance_id,
                    "predecessor.json",
                    predecessor.to_dict(),
                )
                pending = run_store.record_live_mutation_intent(
                    run,
                    issuance,
                    rollback_artifact_path=predecessor_artifact.relative_path,
                    rollback_artifact_fingerprint=predecessor_artifact.fingerprint,
                )
                try:
                    apply_state = await apply_dsp_config(
                        source=APPLY_SOURCE,
                        candidate_path=_candidate_path(baseline),
                        load_config=load_config_path,
                        prior_config_path=str(predecessor.state["config_path"]),
                        get_current_config_path=runtime_port.read_config_path,
                        acquire_lock=False,
                        expected_candidate_sha256=_candidate_sha(baseline),
                        validate=validate,
                    )
                    try:
                        observed = await snapshot_exact_dsp_state(runtime_port)
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        failure_code = "fresh_readback_failed"
                        raise CommissioningApplyError(
                            failure_code, "fresh applied-state readback failed"
                        ) from exc
                    observed_graph = _exact_graph(observed)
                    if (
                        observed_graph.fingerprint != expected_graph.fingerprint
                        or observed.state.get("config_path")
                        != _candidate_path(baseline)
                        or not _same_volume(predecessor, observed)
                    ):
                        failure_code = "candidate_readback_mismatch"
                        raise CommissioningApplyError(
                            failure_code,
                            "fresh graph, path, or listening-volume readback mismatched",
                        )
                    from jasper.active_speaker.environment import (
                        DEFAULT_CAMILLA_STATEFILE,
                    )
                    from jasper.active_speaker.staging import staged_metadata_path
                    from jasper.bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH
                    from jasper.bass_extension.profile import DEFAULT_PROFILE_PATH

                    graph_safety = await classify_active_bass_extension_graph(
                        topology,
                        statefile_path=Path(DEFAULT_CAMILLA_STATEFILE),
                        read_active_graph_text=runtime_port.read_active_raw,
                        applied_baseline_path=baseline_profile_state_path(
                            state_path
                        ),
                        profile_path=DEFAULT_PROFILE_PATH,
                        intent_path=BASS_EXTENSION_APPLY_INTENT_PATH,
                        staged_metadata_path=staged_metadata_path(),
                    )
                    if not graph_safety.allowed:
                        failure_code = "protection_proof_failed"
                        raise CommissioningApplyError(
                            failure_code,
                            "fresh applied graph failed the protected topology contract",
                        )
                    proof = AppliedCandidateProof(
                        operation_id=issuance.issuance_id,
                        target_plan_fingerprint=target_plan.fingerprint,
                        safety_profile_fingerprint=safety_profile_fingerprint,
                        candidate_fingerprint=candidate.fingerprint,
                        predecessor_state=predecessor,
                        expected_normalized_graph=expected_graph,
                        observed_fresh_readback_graph=observed_graph,
                        writer_lock_fingerprint=_writer_lock_fingerprint(
                            config_dir, issuance.issuance_id
                        ),
                        mutation_fingerprint=operation,
                        fresh_readback_fingerprint=observed.fingerprint,
                        protection_proof_fingerprint=_protection_fingerprint(
                            topology=topology,
                            graph=observed_graph,
                            decision=graph_safety.to_dict(),
                            safety_profile_fingerprint=safety_profile_fingerprint,
                        ),
                    )
                    proof_artifact = _publish_json(
                        store,
                        run,
                        issuance.issuance_id,
                        "applied-proof.json",
                        proof.to_dict(),
                    )
                    _publish_json(
                        store,
                        run,
                        issuance.issuance_id,
                        "dsp-apply-state.json",
                        apply_state.to_dict(),
                    )
                    retained = _record_retained_or_reopen(
                        run=run,
                        run_store=run_store,
                        pending=pending,
                        applied_proof_fingerprint=proof_artifact.fingerprint,
                    )
                except BaseException as exc:  # noqa: BLE001 - restore cancellation too
                    result = await _restore_failed_mutation_locked(
                        error=exc,
                        failure_code=failure_code,
                        run=run,
                        run_store=run_store,
                        store=store,
                        issuance=issuance,
                        pending=pending,
                        operation_fingerprint=operation,
                        predecessor=predecessor,
                        runtime_port=runtime_port,
                        load_config_path=load_config_path,
                        candidate_config_path=_candidate_path(baseline),
                        apply_state=apply_state,
                        observed_graph=observed_graph,
                        restore_started=restore_started,
                    )
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return result

                try:
                    result = finalize_retained_candidate_apply(
                        run=run,
                        run_store=run_store,
                        store=store,
                        mutation=retained,
                        candidate=candidate,
                        target_plan=target_plan,
                        safety_profile_fingerprint=safety_profile_fingerprint,
                        state_path=state_path,
                        config_path=config_path,
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    raise CommissioningApplyError(
                        "candidate_apply_finalization_required",
                        "the graph is applied and proved; retry to finish durable state",
                    ) from exc
                log_event(
                    logger,
                    "correction.active_commissioning_candidate_applied",
                    session=run.session_id,
                    run_id=run.run_id,
                    owner_generation=run.owner_generation,
                    issuance_id=issuance.issuance_id,
                    candidate_fingerprint=candidate.fingerprint,
                    applied_proof_fingerprint=proof.fingerprint,
                    baseline_candidate_fingerprint=baseline.get(
                        "candidate_fingerprint"
                    ),
                )
                return result
    except DspWriterLockTimeout:
        return _pre_mutation_retry(
            run=run,
            run_store=run_store,
            store=store,
            issuance=issuance,
            failure_code="writer_lock_unavailable",
        )
    except asyncio.CancelledError:
        if pending is None:
            run_store.release_live_mutation(run, issuance)
        raise
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        if pending is None:
            if isinstance(exc, CommissioningApplyError):
                detail = exc.detail
            else:
                detail = str(exc)
            try:
                return _pre_mutation_block(
                    run=run,
                    run_store=run_store,
                    store=store,
                    issuance=issuance,
                    failure_code="candidate_apply_failed_before_mutation",
                )
            except (OSError, RuntimeError, TypeError, ValueError) as block_exc:
                raise CommissioningApplyError(
                    "candidate_apply_failed_before_mutation", detail
                ) from block_exc
        raise


async def apply_measured_candidate(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    store: CommissioningEvidenceStore,
    candidate: MeasuredElectricalCandidate,
    target_plan: RequiredTargetPlan,
    safety_profile_fingerprint: str,
    topology: OutputTopology,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    runtime_port: CommissioningRuntimePort,
    load_config_path: LoadConfigPath,
    verify_current: Callable[[], None],
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Run apply and exact rollback in one shielded writer-lock owner task."""

    restore_started = asyncio.Event()
    task = asyncio.create_task(
        _apply_measured_candidate_owned(
            run=run,
            run_store=run_store,
            store=store,
            candidate=candidate,
            target_plan=target_plan,
            safety_profile_fingerprint=safety_profile_fingerprint,
            topology=topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            runtime_port=runtime_port,
            load_config_path=load_config_path,
            verify_current=verify_current,
            restore_started=restore_started,
            state_path=state_path,
            config_path=config_path,
            validate=validate,
        )
    )
    cancelled = False
    cancellation_forwarded = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if (
                not restore_started.is_set()
                and not cancellation_forwarded
                and not task.done()
            ):
                task.cancel()
                cancellation_forwarded = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result
