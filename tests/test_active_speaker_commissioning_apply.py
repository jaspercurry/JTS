# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from jasper.active_speaker import commissioning_apply as apply_module
from jasper.active_speaker.baseline_profile import (
    BASELINE_PROFILE_KIND,
    baseline_candidate_fingerprint,
    build_baseline_profile_candidate,
)
from jasper.active_speaker.commissioning_apply import (
    CommissioningApplyError,
    apply_measured_candidate,
    restore_pending_candidate_apply,
)
from jasper.active_speaker.commissioning_run import CommissioningRunStore
from jasper.active_speaker.commissioning_runtime import CommissioningRuntimePort
from jasper.active_speaker.runtime_contract import GraphSafety
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
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


def _runtime(candidate_text: str, *, fail_candidate_load: bool = False):
    predecessor_text = yaml.safe_dump(
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
    monkeypatch.setattr(
        apply_module,
        "classify_camilla_graph",
        lambda **_kwargs: GraphSafety(
            classification="approved_active_runtime",
            allowed=True,
        ),
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
    port, load_path, state, predecessor_text = _runtime(
        candidate_text,
        fail_candidate_load=fail_candidate_load,
    )
    try:
        result = await apply_measured_candidate(
            run=harness.plan.authority.run,
            run_store=harness.run_store,
            store=harness.evidence_store,
            candidate=candidate,
            target_plan=harness.service._required_target_plan(current),
            safety_profile_fingerprint="a" * 64,
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
    except BaseException as exc:
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
    harness, _candidate, result, state, predecessor, _port, _load = await _apply(
        tmp_path,
        monkeypatch,
        fail_candidate_load=True,
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
        safety_profile_fingerprint="a" * 64,
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
    assert harness.service.status()["status"] == "restore_required"

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
