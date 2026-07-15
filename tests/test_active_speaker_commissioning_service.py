# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from jasper.active_speaker import commissioning_service as service_module
from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
)
from jasper.active_speaker.commissioning_evidence import (
    CompleteCommissioningEvidence,
    derive_region_evidence_plan,
)
from jasper.active_speaker.commissioning_host import (
    CommissioningHostError,
    RegionCaptureOperation,
)
from jasper.active_speaker.commissioning_lifecycle import CommissioningTransition
from jasper.active_speaker.commissioning_run import CommissioningRunStore
from jasper.active_speaker.commissioning_service import (
    CommissioningCaptureService,
    CommissioningServiceError,
    commissioning_runtime_port,
)
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_commissioning_evidence import (
    _Harness,
    _complete_isolated,
    _hash,
    _region,
)
from tests.test_active_speaker_commissioning_evidence_store import (
    _materialize_isolated,
)
from tests.test_active_speaker_commissioning_host import _plan


@dataclass(frozen=True)
class _ServiceHarness:
    service: CommissioningCaptureService
    evidence_store: CommissioningEvidenceStore
    run_store: CommissioningRunStore
    plan: object
    authority: object


def _service_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_evidence: bool = False,
) -> _ServiceHarness:
    topology = mono_output_topology(mode="active_2_way")
    bundle = open_bundle(
        topology,
        calibration_id="test-calibration",
        sessions_dir=tmp_path / "sessions",
    )
    assert bundle is not None
    evidence_store = CommissioningEvidenceStore.open(
        bundle["bundle_dir"], expected_session_id=bundle["session_id"]
    )
    run_store, returned_topology, plan, authority = _plan(
        tmp_path,
        evidence_store,
        owner_id="9" * 32,
    )
    assert returned_topology == topology
    evidence_store.publish_region_evidence_plan(plan)
    isolated_fixture = _complete_isolated(_Harness(store=run_store, plan=plan))
    if candidate_evidence:
        from tests.test_active_speaker_measured_candidate import (
            _materialize_isolated as materialize_candidate_isolated,
        )

        isolated = materialize_candidate_isolated(
            evidence_store,
            isolated_fixture,
            preset=authority.preset,
        )
    else:
        isolated = _materialize_isolated(evidence_store, isolated_fixture)
    evidence_store.publish_complete_isolated_driver_evidence(isolated)
    attempt = run_store.attempts(plan.authority.run)[0]
    assert run_store.transition(
        plan.authority.run,
        CommissioningTransition(
            from_state="unconfigured",
            to_state="protected",
            evidence_kind="protection_evidence",
            evidence_fingerprint=_hash("protected-graph"),
        ),
        attempt=attempt,
    )

    def current_plan(**kwargs):
        assert kwargs["run"] == plan.authority.run
        assert kwargs["evidence_store"] is evidence_store
        return plan

    monkeypatch.setattr(service_module, "current_region_evidence_plan", current_plan)
    monkeypatch.setattr(
        service_module,
        "reopen_region_evidence_plan_for_baseline",
        current_plan,
    )
    service = CommissioningCaptureService(
        run=plan.authority.run,
        run_store=run_store,
        evidence_store=evidence_store,
        load_current_authority=lambda: authority,
    )
    return _ServiceHarness(service, evidence_store, run_store, plan, authority)


def _complete_candidate_evidence(harness: _ServiceHarness):
    from tests.test_active_speaker_measured_candidate import _materialize_summed

    target = harness.plan.targets[0]
    harness.service.attest_geometry(
        expected_target_fingerprint=target.fingerprint,
        signed_acoustic_path_difference_mm=0.0,
    )
    summed = _materialize_summed(
        harness.evidence_store,
        CompleteCommissioningEvidence(
            plan=harness.plan,
            regions=(
                _region(
                    _Harness(store=harness.run_store, plan=harness.plan),
                    target_index=0,
                    placement_fingerprint=_hash("isolated-placement"),
                ),
            ),
        ),
    )
    artifact = harness.evidence_store.publish_complete_commissioning_evidence(
        summed
    )
    assert harness.run_store.transition(
        harness.plan.authority.run,
        CommissioningTransition(
            from_state="protected",
            to_state="measured",
            evidence_kind="admitted_measurement_set",
            evidence_fingerprint=artifact.fingerprint,
        ),
    )
    return artifact


def test_geometry_is_write_once_and_status_does_not_issue_host_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=service_module.__name__)
    harness = _service_harness(tmp_path, monkeypatch)
    target = harness.plan.targets[0]
    attempts_before = harness.run_store.attempts(harness.plan.authority.run)

    with monkeypatch.context() as status_patch:
        status_patch.setattr(
            CommissioningEvidenceStore,
            "reopen_complete_isolated_driver_evidence",
            lambda _store, **_kwargs: pytest.fail(
                "status must not deep-reopen isolated child WAV evidence"
            ),
        )
        initial = harness.service.status()

    assert initial["status"] == "needs_geometry"
    assert initial["next_geometry"]["target_fingerprint"] == target.fingerprint
    assert initial["next_capture"] is None
    assert harness.run_store.attempts(harness.plan.authority.run) == attempts_before
    assert harness.run_store.current_live_mutation(harness.plan.authority.run) is None

    accepted = harness.service.attest_geometry(
        expected_target_fingerprint=target.fingerprint,
        signed_acoustic_path_difference_mm=12.5,
    )
    assert accepted["status"] == "accepted"
    assert accepted["already_present"] is False
    assert "event=active_speaker.commissioning_geometry_attested" in caplog.text

    collecting = harness.service.status()
    assert collecting["status"] == "collecting"
    assert collecting["next_capture"] == {"evidence_kind": "server_selected"}
    assert collecting["geometry"] == [
        {
            "speaker_group_id": target.speaker_group_id,
            "region_id": target.region_id,
            "target_fingerprint": target.fingerprint,
            "fc_hz": target.electrical_fc_hz,
            "lower_role": target.lower_role,
            "upper_role": target.upper_role,
            "attested": True,
            "signed_acoustic_path_difference_mm": 12.5,
        }
    ]
    assert harness.run_store.attempts(harness.plan.authority.run) == attempts_before
    assert harness.run_store.current_live_mutation(harness.plan.authority.run) is None

    payload = harness.evidence_store.reopen_json_artifact(
        harness.evidence_store.identify_artifact(
            service_module._geometry_artifact_relative_path(
                harness.plan.authority.run, target
            )
        )
    )
    assert payload["signed_path_semantics"] == (
        "lower_driver_path_minus_upper_driver_path"
    )
    assert payload["signed_acoustic_path_difference_m"] == 0.0125
    assert payload["delay_walk_spec"]["positive_delay_target"] == target.upper_role
    assert payload["delay_walk_spec"]["negative_delay_target"] == target.lower_role

    repeated = harness.service.attest_geometry(
        expected_target_fingerprint=target.fingerprint,
        signed_acoustic_path_difference_mm=12.5,
    )
    assert repeated["already_present"] is True
    with pytest.raises(CommissioningServiceError, match="already has a different"):
        harness.service.attest_geometry(
            expected_target_fingerprint=target.fingerprint,
            signed_acoustic_path_difference_mm=-12.5,
        )


def test_out_of_bounds_geometry_is_refused_before_write_once_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _service_harness(tmp_path, monkeypatch)
    target = harness.plan.targets[0]
    artifact_path = service_module._geometry_artifact_relative_path(
        harness.plan.authority.run,
        target,
    )

    with pytest.raises(
        CommissioningServiceError,
        match="exceeds the bounded crossover delay range",
    ) as captured:
        harness.service.attest_geometry(
            expected_target_fingerprint=target.fingerprint,
            signed_acoustic_path_difference_mm=100_000.0,
        )

    assert captured.value.code == "geometry_out_of_bounds"
    with pytest.raises(CommissioningEvidenceStoreError):
        harness.evidence_store.identify_artifact(artifact_path)


def test_service_composes_geometry_into_the_existing_host_operation_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _service_harness(tmp_path, monkeypatch)
    target = harness.plan.targets[0]
    harness.service.attest_geometry(
        expected_target_fingerprint=target.fingerprint,
        signed_acoustic_path_difference_mm=-8.0,
    )

    current = harness.service._current()
    host = harness.service._host(current, raw_capture_transport=None)
    operation = host.next_operation()

    assert isinstance(operation, RegionCaptureOperation)
    assert operation.target == target
    assert operation.evidence_kind == "normal"
    assert operation.capture_ordinal == 0
    assert operation.required_capture_count == 3
    assert operation.issuance_id is not None
    assert host.next_operation() == operation


def test_measured_status_requires_exact_complete_evidence_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _service_harness(tmp_path, monkeypatch)
    target = harness.plan.targets[0]
    harness.service.attest_geometry(
        expected_target_fingerprint=target.fingerprint,
        signed_acoustic_path_difference_mm=0.0,
    )
    assert harness.run_store.transition(
        harness.plan.authority.run,
        CommissioningTransition(
            from_state="protected",
            to_state="measured",
            evidence_kind="admitted_measurement_set",
            evidence_fingerprint=_hash("missing-complete-evidence"),
        ),
    )

    with pytest.raises(
        CommissioningHostError,
        match="measured lifecycle has no durable complete evidence",
    ):
        harness.service.status()


def test_measured_candidate_is_persisted_bound_and_projected_without_rescoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=service_module.__name__)
    harness = _service_harness(
        tmp_path,
        monkeypatch,
        candidate_evidence=True,
    )
    summed_artifact = _complete_candidate_evidence(harness)

    review = harness.service.publish_candidate()

    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == (
        "candidate_ready"
    )
    assert review["retained_crossover_regions"] == [
        {
            "region_id": "woofer_tweeter",
            "lower_role": "woofer",
            "upper_role": "tweeter",
            "fc_hz": 1600.0,
            "filter_family": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "polarity_evidence": "normal_retained_by_reverse_null",
        }
    ]
    assert review["drivers"] == [
        {
            "role": "woofer",
            "attenuation_db": 0.0,
            "delay_ms": 0.0,
            "polarity": "non-inverted",
        },
        {
            "role": "tweeter",
            "attenuation_db": -6.0,
            "delay_ms": 0.0375,
            "polarity": "non-inverted",
        },
    ]
    assert review["evidence"]["summed_artifact"] == summed_artifact.to_dict()
    assert "event=correction.active_commissioning_candidate_ready" in caplog.text

    with monkeypatch.context() as status_patch:
        status_patch.setattr(
            service_module,
            "evaluate_measured_candidate",
            lambda **_kwargs: pytest.fail("status must not rescore child WAVs"),
        )
        status_patch.setattr(
            CommissioningEvidenceStore,
            "reopen_complete_commissioning_evidence",
            lambda _store, **_kwargs: pytest.fail(
                "candidate status must not deep-reopen child WAVs"
            ),
        )
        status = harness.service.status()

    assert status["status"] == "candidate_ready"
    assert status["candidate"] == review
    transition = harness.run_store.lifecycle_transition(
        harness.plan.authority.run
    )
    assert transition is not None
    assert transition.evidence_fingerprint == review["artifact_fingerprint"]
    assert harness.service.publish_candidate() == review


def test_candidate_refusal_is_persisted_blocked_and_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from jasper.active_speaker.measured_candidate import (
        MeasuredCandidateEvaluationError,
    )

    caplog.set_level(logging.INFO, logger=service_module.__name__)
    harness = _service_harness(
        tmp_path,
        monkeypatch,
        candidate_evidence=True,
    )
    summed_artifact = _complete_candidate_evidence(harness)
    calls = 0

    def refuse(**_kwargs):
        nonlocal calls
        calls += 1
        raise MeasuredCandidateEvaluationError(
            "candidate_polarity_inconclusive",
            "normal and reverse evidence did not prove one polarity",
        )

    monkeypatch.setattr(service_module, "evaluate_measured_candidate", refuse)

    with pytest.raises(CommissioningServiceError) as captured:
        harness.service.publish_candidate()

    assert captured.value.code == "candidate_scoring_failed"
    assert calls == 1
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == "blocked"
    transition = harness.run_store.lifecycle_transition(
        harness.plan.authority.run
    )
    assert transition is not None
    assert transition.evidence_kind == "failure_evidence"
    assert transition.failure_code == "candidate_scoring_failed"
    status = harness.service.status()
    assert status["status"] == "candidate_refused"
    assert status["candidate"] is None
    assert status["candidate_failure"]["reason"] == (
        "candidate_polarity_inconclusive"
    )
    assert status["candidate_failure"]["summed_artifact"] == (
        summed_artifact.to_dict()
    )
    assert status["candidate_failure"]["artifact_fingerprint"] == (
        transition.evidence_fingerprint
    )
    assert calls == 1
    assert "event=correction.active_commissioning_candidate_refused" in caplog.text

    with pytest.raises(CommissioningServiceError) as repeated:
        harness.service.publish_candidate()
    assert repeated.value.code == "candidate_not_measured"
    assert calls == 1


@pytest.mark.asyncio
async def test_runtime_port_uses_strict_camilla_readback_and_apply() -> None:
    calls: list[tuple[object, ...]] = []

    class Camilla:
        async def get_active_config_raw(self, *, best_effort: bool):
            calls.append(("read_graph", best_effort))
            return "graph"

        async def set_active_config_raw(self, raw: str, *, best_effort: bool):
            calls.append(("apply_graph", raw, best_effort))
            return True

        async def get_config_file_path(self, *, best_effort: bool):
            calls.append(("read_path", best_effort))
            return "/etc/camilladsp/current.yml"

        async def get_volume_db(self, *, best_effort: bool):
            calls.append(("read_volume", best_effort))
            return -32.0

        async def set_volume_db(self, value: float, *, best_effort: bool):
            calls.append(("set_volume", value, best_effort))
            return True

    port = commissioning_runtime_port(Camilla())

    assert await port.read_active_raw() == "graph"
    assert await port.apply_active_raw("candidate") is True
    assert await port.read_config_path() == "/etc/camilladsp/current.yml"
    assert await port.read_listening_volume_db() == -32.0
    assert await port.set_listening_volume_db(-48.0) is True
    assert calls == [
        ("read_graph", False),
        ("apply_graph", "candidate", False),
        ("read_path", False),
        ("read_volume", False),
        ("set_volume", -48.0, False),
    ]


def test_stale_run_generation_cannot_reuse_geometry_or_capture_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _service_harness(tmp_path, monkeypatch)
    target = harness.plan.targets[0]
    harness.service.attest_geometry(
        expected_target_fingerprint=target.fingerprint,
        signed_acoustic_path_difference_mm=0.0,
    )
    harness.run_store.replace_current(
        session_id=harness.plan.authority.run.session_id,
        session_fingerprint=harness.plan.authority.run.session_fingerprint,
    )

    with pytest.raises(CommissioningServiceError) as raised:
        harness.service.status()
    assert raised.value.code == "run_generation_stale"


def test_service_refuses_hardware_and_way_counts_outside_launch_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _service_harness(tmp_path, monkeypatch)

    unsupported_hardware = replace(
        harness.authority.topology.hardware,
        device_id="apple_usb_c_dongle",
    )
    unsupported_topology = replace(
        harness.authority.topology,
        hardware=unsupported_hardware,
    )
    unsupported_dac = CommissioningCaptureService(
        run=harness.plan.authority.run,
        run_store=harness.run_store,
        evidence_store=harness.evidence_store,
        load_current_authority=lambda: replace(
            harness.authority,
            topology=unsupported_topology,
        ),
    )
    with pytest.raises(CommissioningServiceError) as dac_error:
        unsupported_dac.status()
    assert dac_error.value.code == "launch_scope_unsupported"

    unsupported_way = CommissioningCaptureService(
        run=harness.plan.authority.run,
        run_store=harness.run_store,
        evidence_store=harness.evidence_store,
        load_current_authority=lambda: replace(
            harness.authority,
            preset=replace(harness.authority.preset, way_count=3),
        ),
    )
    with pytest.raises(CommissioningServiceError) as way_error:
        unsupported_way.status()
    assert way_error.value.code == "launch_scope_unsupported"


def test_new_owner_generation_reuses_only_same_program_isolated_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _service_harness(tmp_path, monkeypatch)
    old_plan = harness.plan
    old_target = old_plan.targets[0]
    harness.service.attest_geometry(
        expected_target_fingerprint=old_target.fingerprint,
        signed_acoustic_path_difference_mm=4.0,
    )

    restarted_store = CommissioningRunStore(
        path=tmp_path / "run.json", owner_id="8" * 32
    )
    claimed = restarted_store.claim_owner()
    assert claimed is not None
    assert claimed.run_id == old_plan.authority.run.run_id
    assert claimed.owner_generation == old_plan.authority.run.owner_generation + 1
    new_plan = derive_region_evidence_plan(
        harness.authority.preset,
        harness.authority.topology,
        run=claimed,
        protected_safety_profile_fingerprint=(
            old_plan.authority.protected_safety_profile_fingerprint
        ),
        comparison_set_fingerprint=(
            old_plan.authority.comparison_set_fingerprint
        ),
        threshold_profile_fingerprint=(
            old_plan.authority.threshold_profile_fingerprint
        ),
        context_fingerprint=old_plan.authority.context_fingerprint,
    )
    harness.evidence_store.publish_region_evidence_plan(new_plan)
    monkeypatch.setattr(
        service_module,
        "current_region_evidence_plan",
        lambda **_kwargs: new_plan,
    )
    restarted = CommissioningCaptureService(
        run=claimed,
        run_store=restarted_store,
        evidence_store=harness.evidence_store,
        load_current_authority=lambda: harness.authority,
    )

    status = restarted.status()

    assert status["status"] == "needs_geometry"
    assert status["owner_generation"] == claimed.owner_generation
    assert status["isolated_evidence_fingerprint"] == (
        harness.evidence_store.reopen_complete_isolated_driver_evidence(
            run_id=claimed.run_id
        ).fingerprint
    )
    assert status["next_geometry"]["target_fingerprint"] == (
        new_plan.targets[0].fingerprint
    )
