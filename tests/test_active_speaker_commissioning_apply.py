# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from jasper.active_speaker import commissioning_apply as apply_module
from jasper.active_speaker import commissioning_isolated_producer as producer_module
from jasper.active_speaker import commissioning_service as service_module
from jasper.active_speaker.baseline_profile import (
    BASELINE_PROFILE_KIND,
    baseline_candidate_fingerprint,
    build_baseline_profile_candidate,
    recompose_applied_baseline_yaml,
)
from jasper.active_speaker.commissioning_apply import (
    CommissioningApplyError,
    apply_measured_candidate,
    restore_pending_candidate_apply,
)
from jasper.active_speaker.commissioning_run import CommissioningRunStore
from jasper.active_speaker.commissioning_runtime import (
    CommissioningRuntimePort,
    snapshot_exact_dsp_state,
)
from jasper.active_speaker.commissioning_service import CommissioningServiceError
from jasper.active_speaker.runtime_contract import GraphSafety
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspWriterLockTimeout,
    ValidationStatus,
    camilla_graph_mutation,
    dsp_apply_lock_path,
    last_dsp_apply_state,
)
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


def _runtime(
    candidate_text: str,
    *,
    predecessor_text: str | None = None,
    fail_candidate_load: bool = False,
):
    predecessor_text = predecessor_text or yaml.safe_dump(
        {
            "devices": {"volume_limit": -12.0},
            "filters": {"predecessor": {"type": "Gain"}},
        },
        sort_keys=True,
    )
    state: dict[str, Any] = {
        "raw": predecessor_text,
        "path": "/etc/camilladsp/predecessor.yml",
        "volume": -36.0,
    }

    async def read_raw() -> str:
        return state["raw"]

    async def apply_raw(raw: str) -> bool:
        state["raw"] = raw
        return True

    async def read_path() -> str:
        return state["path"]

    async def read_volume() -> float:
        return state["volume"]

    async def set_volume(value: float) -> bool:
        state["volume"] = value
        return True

    async def load_path(path: str) -> bool:
        if path.endswith("candidate.yml") and fail_candidate_load:
            return False
        state["path"] = path
        state["raw"] = (
            candidate_text if path.endswith("candidate.yml") else predecessor_text
        )
        return True

    return (
        CommissioningRuntimePort(
            read_active_raw=read_raw,
            apply_active_raw=apply_raw,
            read_config_path=read_path,
            read_listening_volume_db=read_volume,
            set_listening_volume_db=set_volume,
        ),
        load_path,
        state,
        predecessor_text,
    )


def _install_compiler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_path: Path,
    candidate_text: str,
) -> None:
    def build(topology, **kwargs):
        measured = kwargs["measured_candidate"]
        candidate_path.write_text(candidate_text, encoding="utf-8")
        payload = {
            "artifact_schema_version": 1,
            "kind": BASELINE_PROFILE_KIND,
            "status": "ready_to_apply",
            "baseline_id": "baseline-test",
            "source": {
                "fingerprint": "1" * 64,
                "measured_candidate_fingerprint": measured.fingerprint,
            },
            "config": {
                "path": str(candidate_path),
                "sha256": hashlib.sha256(candidate_text.encode()).hexdigest(),
            },
            "corrections": measured.driver_corrections(),
            "permissions": {"may_apply": True},
            "recomposition_snapshot": {
                "schema_version": 1,
                "topology_id": topology.topology_id,
                "preset": measured.source_preset.to_dict(),
                "corrections": measured.driver_corrections(),
                "measured_candidate_fingerprint": measured.fingerprint,
            },
        }
        payload["candidate_fingerprint"] = baseline_candidate_fingerprint(payload)
        return payload

    monkeypatch.setattr(apply_module, "build_baseline_profile_candidate", build)
    async def classify(*_args, **_kwargs):
        return GraphSafety(
            classification="approved_active_runtime",
            allowed=True,
        )

    monkeypatch.setattr(
        apply_module,
        "classify_active_bass_extension_graph",
        classify,
    )


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
    assert config_path.exists()


async def _apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_candidate_load: bool = False,
    capture_error: bool = False,
    mutation_tasks: list[asyncio.Task[Any]] | None = None,
):
    harness, current, candidate = _candidate_harness(tmp_path, monkeypatch)
    candidate_path = tmp_path / "candidate.yml"
    candidate_text = yaml.safe_dump(
        {
            "devices": {"volume_limit": -12.0},
            "filters": {"candidate": {"type": "Gain"}},
        },
        sort_keys=True,
    )
    _install_compiler(
        monkeypatch,
        candidate_path=candidate_path,
        candidate_text=candidate_text,
    )
    predecessor_text, issues = recompose_applied_baseline_yaml(
        current.authority.topology,
        applied_profile=current.authority.applied_profile,
    )
    assert predecessor_text is not None and issues == []
    port, load_path, state, predecessor_text = _runtime(
        candidate_text,
        predecessor_text=predecessor_text,
        fail_candidate_load=fail_candidate_load,
    )
    if mutation_tasks is not None:
        base_port = port
        base_load_path = load_path

        async def guarded_apply_active_raw(raw: str) -> bool:
            async with camilla_graph_mutation(
                source="test.candidate_apply_raw",
                lock_path=dsp_apply_lock_path(tmp_path),
                bass_extension_intent_path=tmp_path / "bass-intent.json",
            ):
                owner = asyncio.current_task()
                assert owner is not None
                mutation_tasks.append(owner)
                return await base_port.apply_active_raw(raw)

        async def guarded_load_config_path(path: str) -> bool:
            async with camilla_graph_mutation(
                source="test.candidate_load_path",
                lock_path=dsp_apply_lock_path(tmp_path),
                bass_extension_intent_path=tmp_path / "bass-intent.json",
            ):
                owner = asyncio.current_task()
                assert owner is not None
                mutation_tasks.append(owner)
                return await base_load_path(path)

        port = replace(port, apply_active_raw=guarded_apply_active_raw)
        load_path = guarded_load_config_path
    try:
        result = await apply_measured_candidate(
            run=harness.plan.authority.run,
            run_store=harness.run_store,
            store=harness.evidence_store,
            candidate=candidate,
            target_plan=harness.service._required_target_plan(current),
            safety_profile_fingerprint=(
                harness.plan.authority.protected_safety_profile_fingerprint
            ),
            topology=current.authority.topology,
            design_draft={},
            crossover_preview={},
            measurements={},
            runtime_port=port,
            load_config_path=load_path,
            verify_current=lambda: None,
            state_path=tmp_path / "baseline-state.json",
            config_path=candidate_path,
            validate=_valid,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if not capture_error:
            raise
        result = exc
    return harness, candidate, result, state, predecessor_text, port, load_path


@pytest.mark.asyncio
async def test_candidate_apply_persists_fresh_proof_and_retained_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, candidate, result, state, _previous, _port, _load = await _apply(
        tmp_path,
        monkeypatch,
    )

    assert result["status"] == "applied_unverified"
    assert result["candidate_fingerprint"] == candidate.fingerprint
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == (
        "applied_unverified"
    )
    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    assert mutation is not None and mutation.status == "retained"
    assert state["path"] == str(tmp_path / "candidate.yml")
    saved = json.loads((tmp_path / "baseline-state.json").read_text())
    assert saved["status"] == "applied"
    assert saved["source"]["measured_candidate_fingerprint"] == candidate.fingerprint


@pytest.mark.asyncio
async def test_failed_candidate_load_restores_exact_predecessor_before_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation_tasks: list[asyncio.Task[Any]] = []
    harness, _candidate, result, state, predecessor, _port, _load = await _apply(
        tmp_path,
        monkeypatch,
        fail_candidate_load=True,
        mutation_tasks=mutation_tasks,
    )

    assert result["status"] == "rolled_back"
    assert state == {
        "raw": predecessor,
        "path": "/etc/camilladsp/predecessor.yml",
        "volume": -36.0,
    }
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == "rolled_back"
    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    assert mutation is not None and mutation.status == "aborted"
    assert mutation.restoration_evidence_fingerprint is not None
    assert len(mutation_tasks) >= 3
    assert len({id(task) for task in mutation_tasks}) == 1


@pytest.mark.asyncio
async def test_retained_apply_retry_only_finishes_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_persist = apply_module.persist_applied_baseline_profile
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated durable-state interruption")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(apply_module, "persist_applied_baseline_profile", fail_once)
    harness, candidate, interrupted, state, _previous, port, load = await _apply(
        tmp_path,
        monkeypatch,
        capture_error=True,
    )
    assert isinstance(interrupted, CommissioningApplyError)
    assert interrupted.code == "candidate_apply_finalization_required"
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == (
        "candidate_ready"
    )
    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    assert mutation is not None and mutation.status == "retained"
    applied_path = state["path"]
    current = harness.service._current()

    result = await apply_measured_candidate(
        run=harness.plan.authority.run,
        run_store=harness.run_store,
        store=harness.evidence_store,
        candidate=candidate,
        target_plan=harness.service._required_target_plan(current),
        safety_profile_fingerprint=(
            harness.plan.authority.protected_safety_profile_fingerprint
        ),
        topology=current.authority.topology,
        design_draft={},
        crossover_preview={},
        measurements={},
        runtime_port=port,
        load_config_path=load,
        verify_current=lambda: None,
        state_path=tmp_path / "baseline-state.json",
        config_path=tmp_path / "candidate.yml",
        validate=_valid,
    )

    assert result["status"] == "applied_unverified"
    assert state["path"] == applied_path
    assert calls == 2


@pytest.mark.asyncio
async def test_retained_sidecar_failure_restores_before_writer_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsp_state_path = tmp_path / "dsp-apply-state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(dsp_state_path))

    def fail_retained(*_args, **_kwargs):
        raise OSError("simulated retained-sidecar failure")

    monkeypatch.setattr(
        CommissioningRunStore,
        "record_live_mutation_retained",
        fail_retained,
    )
    harness, _candidate, result, state, predecessor, _port, _load = await _apply(
        tmp_path,
        monkeypatch,
    )

    assert result["status"] == "rolled_back"
    assert state == {
        "raw": predecessor,
        "path": "/etc/camilladsp/predecessor.yml",
        "volume": -36.0,
    }
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == "rolled_back"
    shared_state = last_dsp_apply_state(state_path=dsp_state_path)
    assert shared_state is not None
    assert shared_state["result"] == (
        "active_wrapper_mutation_outcome_unknown_rolled_back"
    )
    assert shared_state["active_config_path"] == (
        "/etc/camilladsp/predecessor.yml"
    )
    assert shared_state["rollback_attempted"] is True
    assert shared_state["rollback_succeeded"] is True


@pytest.mark.asyncio
async def test_ambiguous_retained_sidecar_write_reopens_exact_persisted_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_retained = CommissioningRunStore.record_live_mutation_retained

    def persist_then_raise(self, *args, **kwargs):
        real_retained(self, *args, **kwargs)
        raise OSError("simulated lost retained acknowledgement")

    monkeypatch.setattr(
        CommissioningRunStore,
        "record_live_mutation_retained",
        persist_then_raise,
    )
    harness, _candidate, result, state, _previous, _port, _load = await _apply(
        tmp_path,
        monkeypatch,
    )

    assert result["status"] == "applied_unverified"
    assert state["path"] == str(tmp_path / "candidate.yml")
    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    assert mutation is not None and mutation.status == "retained"


@pytest.mark.asyncio
async def test_restart_after_proved_restore_finishes_without_mutating_dsp_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_aborted = CommissioningRunStore.record_live_mutation_aborted
    calls = 0

    def fail_once(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated crash after restored sidecar")
        return real_aborted(self, *args, **kwargs)

    monkeypatch.setattr(
        CommissioningRunStore,
        "record_live_mutation_aborted",
        fail_once,
    )
    harness, _candidate, interrupted, state, predecessor, port, load = await _apply(
        tmp_path,
        monkeypatch,
        fail_candidate_load=True,
        capture_error=True,
    )
    assert isinstance(interrupted, OSError)
    assert state == {
        "raw": predecessor,
        "path": "/etc/camilladsp/predecessor.yml",
        "volume": -36.0,
    }
    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    assert mutation is not None and mutation.status == "restored"
    harness.service.load_current_authority = lambda: (_ for _ in ()).throw(
        ValueError("simulated stale product authority")
    )
    status = harness.service.status()
    assert status["status"] == "restore_finalization_required"
    assert "already restored" in status["detail"]

    result = await restore_pending_candidate_apply(
        run=harness.plan.authority.run,
        run_store=harness.run_store,
        store=harness.evidence_store,
        runtime_port=port,
        load_config_path=load,
        config_path=tmp_path / "candidate.yml",
    )

    assert result["status"] == "rolled_back"
    assert state == {
        "raw": predecessor,
        "path": "/etc/camilladsp/predecessor.yml",
        "volume": -36.0,
    }
    assert calls == 2


@pytest.mark.asyncio
async def test_applied_profile_change_keeps_preapply_plan_for_status_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_transition = CommissioningRunStore.transition
    failed_transition = False

    def fail_applied_transition_once(self, handle, transition, **kwargs):
        nonlocal failed_transition
        if transition.to_state == "applied_unverified" and not failed_transition:
            failed_transition = True
            return False
        return real_transition(self, handle, transition, **kwargs)

    monkeypatch.setattr(
        CommissioningRunStore,
        "transition",
        fail_applied_transition_once,
    )
    harness, candidate, interrupted, state, _previous, port, load = await _apply(
        tmp_path,
        monkeypatch,
        capture_error=True,
    )
    assert isinstance(interrupted, CommissioningApplyError)
    assert interrupted.code == "candidate_apply_finalization_required"
    assert failed_transition is True
    assert state["path"] == str(tmp_path / "candidate.yml")
    saved = json.loads((tmp_path / "baseline-state.json").read_text())
    assert saved["status"] == "applied"

    harness.service.load_current_authority = lambda: replace(
        harness.authority,
        applied_profile=saved,
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
        lambda **_kwargs: pytest.fail(
            "post-apply status must not rebuild authority from the new graph"
        ),
    )

    status = harness.service.status()
    assert status["status"] == "apply_finalization_required"
    assert status["candidate"]["fingerprint"] == candidate.fingerprint
    assert status["profile_context_id"] == harness.authority.comparison_set[
        "profile_context_id"
    ]

    current = harness.service._current()
    result = await apply_measured_candidate(
        run=harness.plan.authority.run,
        run_store=harness.run_store,
        store=harness.evidence_store,
        candidate=candidate,
        target_plan=harness.service._required_target_plan(current),
        safety_profile_fingerprint=(
            harness.plan.authority.protected_safety_profile_fingerprint
        ),
        topology=current.authority.topology,
        design_draft={},
        crossover_preview={},
        measurements={},
        runtime_port=port,
        load_config_path=load,
        verify_current=lambda: pytest.fail("retained retry must not reload audio"),
        state_path=tmp_path / "baseline-state.json",
        config_path=tmp_path / "candidate.yml",
        validate=_valid,
    )

    assert result["status"] == "applied_unverified"
    assert state["path"] == str(tmp_path / "candidate.yml")
    final_status = harness.service.status()
    assert final_status["status"] == "applied_unverified"
    assert final_status["profile_context_id"] == harness.authority.comparison_set[
        "profile_context_id"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restore_succeeds",
    [True, False],
    ids=["exact_restore", "restore_failure"],
)
async def test_repeated_cancellation_preserves_exact_restore_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_succeeds: bool,
) -> None:
    dsp_state_path = tmp_path / "dsp-apply-state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(dsp_state_path))
    harness, current, candidate = _candidate_harness(tmp_path, monkeypatch)
    candidate_path = tmp_path / "candidate.yml"
    candidate_text = yaml.safe_dump(
        {
            "devices": {"volume_limit": -12.0},
            "filters": {"candidate": {"type": "Gain"}},
        },
        sort_keys=True,
    )
    _install_compiler(
        monkeypatch,
        candidate_path=candidate_path,
        candidate_text=candidate_text,
    )
    predecessor_text, issues = recompose_applied_baseline_yaml(
        current.authority.topology,
        applied_profile=current.authority.applied_profile,
    )
    assert predecessor_text is not None and issues == []
    port, real_load, state, predecessor_text = _runtime(
        candidate_text,
        predecessor_text=predecessor_text,
    )
    candidate_loaded = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    mutation_tasks: list[asyncio.Task[Any]] = []
    base_port = port

    async def guarded_apply_active_raw(raw: str) -> bool:
        async with camilla_graph_mutation(
            source="test.cancelled_candidate_apply_raw",
            lock_path=dsp_apply_lock_path(tmp_path),
            bass_extension_intent_path=tmp_path / "bass-intent.json",
        ):
            owner = asyncio.current_task()
            assert owner is not None
            mutation_tasks.append(owner)
            return await base_port.apply_active_raw(raw)

    port = replace(port, apply_active_raw=guarded_apply_active_raw)

    async def load_then_wait(path: str) -> bool:
        async with camilla_graph_mutation(
            source="test.cancelled_candidate_load_path",
            lock_path=dsp_apply_lock_path(tmp_path),
            bass_extension_intent_path=tmp_path / "bass-intent.json",
        ):
            owner = asyncio.current_task()
            assert owner is not None
            mutation_tasks.append(owner)
            if path == str(candidate_path):
                loaded = await real_load(path)
                candidate_loaded.set()
                await asyncio.Future()
                return loaded
            restore_started.set()
            await release_restore.wait()
            if not restore_succeeds:
                return False
            return await real_load(path)

    task = asyncio.create_task(
        apply_measured_candidate(
            run=harness.plan.authority.run,
            run_store=harness.run_store,
            store=harness.evidence_store,
            candidate=candidate,
            target_plan=harness.service._required_target_plan(current),
            safety_profile_fingerprint=(
                harness.plan.authority.protected_safety_profile_fingerprint
            ),
            topology=current.authority.topology,
            design_draft={},
            crossover_preview={},
            measurements={},
            runtime_port=port,
            load_config_path=load_then_wait,
            verify_current=lambda: None,
            state_path=tmp_path / "baseline-state.json",
            config_path=candidate_path,
            validate=_valid,
        )
    )
    await asyncio.wait_for(candidate_loaded.wait(), timeout=2.0)
    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    assert mutation is not None and mutation.status == "mutation_pending"

    task.cancel()
    await asyncio.wait_for(restore_started.wait(), timeout=2.0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_restore.set()
    if restore_succeeds:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CommissioningApplyError) as raised:
            await task
        assert raised.value.code == "candidate_restore_required"

    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    shared_state = last_dsp_apply_state(state_path=dsp_state_path)
    assert shared_state is not None
    if restore_succeeds:
        assert state == {
            "raw": predecessor_text,
            "path": "/etc/camilladsp/predecessor.yml",
            "volume": -36.0,
        }
        assert mutation is not None and mutation.status == "aborted"
        assert harness.run_store.lifecycle_state(harness.plan.authority.run) == (
            "rolled_back"
        )
        assert shared_state["result"].endswith("_rolled_back")
        assert shared_state["rollback_succeeded"] is True
    else:
        assert state == {
            "raw": predecessor_text,
            "path": str(candidate_path),
            "volume": -36.0,
        }
        assert mutation is not None and mutation.status == "mutation_pending"
        assert mutation.restoration_evidence_fingerprint is None
        assert harness.run_store.lifecycle_state(harness.plan.authority.run) == (
            "candidate_ready"
        )
        assert shared_state["result"].endswith("_rollback_failed")
        assert shared_state["rollback_succeeded"] is False
    assert len(mutation_tasks) == 3
    assert len({id(owner) for owner in mutation_tasks}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_writer", [False, True])
async def test_restart_with_pending_mutation_performs_live_exact_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_before_writer: bool,
) -> None:
    dsp_state_path = tmp_path / "dsp-apply-state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(dsp_state_path))
    harness, current, candidate = _candidate_harness(tmp_path, monkeypatch)
    candidate_text = yaml.safe_dump(
        {
            "devices": {"volume_limit": -12.0},
            "filters": {"candidate": {"type": "Gain"}},
        },
        sort_keys=True,
    )
    predecessor_text, issues = recompose_applied_baseline_yaml(
        current.authority.topology,
        applied_profile=current.authority.applied_profile,
    )
    assert predecessor_text is not None and issues == []
    port, load, state, predecessor_text = _runtime(
        candidate_text,
        predecessor_text=predecessor_text,
    )
    predecessor = await snapshot_exact_dsp_state(port)
    run = harness.plan.authority.run
    target_plan = harness.service._required_target_plan(current)
    operation = apply_module._operation_fingerprint(
        run=run,
        candidate=candidate,
        target_plan=target_plan,
        safety_profile_fingerprint=(
            harness.plan.authority.protected_safety_profile_fingerprint
        ),
    )
    issuance = harness.run_store.issue_live_mutation(
        run,
        purpose=apply_module.APPLY_PURPOSE,
        operation_fingerprint=operation,
    )
    predecessor_artifact = harness.evidence_store.publish_json_artifact(
        apply_module._source_path(
            run,
            issuance.issuance_id,
            "predecessor.json",
        ),
        predecessor.to_dict(),
    )
    harness.run_store.record_live_mutation_intent(
        run,
        issuance,
        rollback_artifact_path=predecessor_artifact.relative_path,
        rollback_artifact_fingerprint=predecessor_artifact.fingerprint,
    )
    state.update(
        {
            "raw": candidate_text,
            "path": str(tmp_path / "candidate.yml"),
            "volume": -18.0,
        }
    )

    assert harness.service.status()["status"] == "restore_required"
    restore = restore_pending_candidate_apply(
        run=run,
        run_store=harness.run_store,
        store=harness.evidence_store,
        runtime_port=port,
        load_config_path=load,
        config_path=tmp_path / "candidate.yml",
    )
    if cancel_before_writer:
        writer_waiting = asyncio.Event()
        admit_writer = asyncio.Event()

        @asynccontextmanager
        async def delayed_writer(*_args, **_kwargs):
            writer_waiting.set()
            await admit_writer.wait()
            yield

        monkeypatch.setattr(apply_module, "dsp_writer_lock", delayed_writer)
        task = asyncio.create_task(restore)
        await asyncio.wait_for(writer_waiting.wait(), timeout=2.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        admit_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await restore
        assert result["status"] == "rolled_back"
    assert state == {
        "raw": predecessor_text,
        "path": "/etc/camilladsp/predecessor.yml",
        "volume": -36.0,
    }
    mutation = harness.run_store.current_live_mutation(run)
    assert mutation is not None and mutation.status == "aborted"
    shared_state = last_dsp_apply_state(state_path=dsp_state_path)
    assert shared_state is not None
    assert shared_state["source"] == (
        f"{apply_module.APPLY_SOURCE}_recovery"
    )
    assert shared_state["result"] == (
        "active_wrapper_restart_recovery_rolled_back"
    )
    assert shared_state["active_config_path"] == (
        "/etc/camilladsp/predecessor.yml"
    )


@pytest.mark.asyncio
async def test_writer_lock_timeout_leaves_candidate_ready_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_writer_lock = apply_module.dsp_writer_lock

    @asynccontextmanager
    async def collide_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DspWriterLockTimeout(
                tmp_path / "dsp-writer.lock",
                timeout_s=10.0,
                waited_s=10.0,
                source=apply_module.APPLY_SOURCE,
            )
        async with real_writer_lock(*_args, **_kwargs):
            yield

    monkeypatch.setattr(apply_module, "dsp_writer_lock", collide_once)
    harness, candidate, deferred, state, _previous, port, load = await _apply(
        tmp_path,
        monkeypatch,
    )

    assert deferred["status"] == "candidate_ready"
    assert deferred["failure_code"] == "writer_lock_unavailable"
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == (
        "candidate_ready"
    )
    mutation = harness.run_store.current_live_mutation(harness.plan.authority.run)
    assert mutation is not None and mutation.status == "released"

    current = harness.service._current()
    result = await apply_measured_candidate(
        run=harness.plan.authority.run,
        run_store=harness.run_store,
        store=harness.evidence_store,
        candidate=candidate,
        target_plan=harness.service._required_target_plan(current),
        safety_profile_fingerprint=(
            harness.plan.authority.protected_safety_profile_fingerprint
        ),
        topology=current.authority.topology,
        design_draft={},
        crossover_preview={},
        measurements={},
        runtime_port=port,
        load_config_path=load,
        verify_current=lambda: None,
        state_path=tmp_path / "baseline-state.json",
        config_path=tmp_path / "candidate.yml",
        validate=_valid,
    )

    assert result["status"] == "applied_unverified"
    assert state["path"] == str(tmp_path / "candidate.yml")
    assert calls == 2


@pytest.mark.asyncio
async def test_stale_rolled_back_review_does_not_change_known_restore_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _candidate, result, _state, _previous, port, load = await _apply(
        tmp_path,
        monkeypatch,
        fail_candidate_load=True,
    )
    assert result["status"] == "rolled_back"

    with pytest.raises(CommissioningServiceError) as captured:
        await harness.service.apply_candidate(
            expected_candidate_fingerprint="0" * 64,
            runtime_port=port,
            load_config_path=load,
        )

    assert captured.value.code == "candidate_review_stale"
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == "rolled_back"
    assert harness.service.status()["status"] == "apply_rolled_back"


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
