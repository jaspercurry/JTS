# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from jasper.active_speaker import commissioning_host as commissioning_host_module
from jasper.active_speaker.commissioning_evidence import (
    AdmittedRegionCapture,
    CompleteCommissioningEvidence,
    RegionGeometryAttestation,
    active_region_context_fingerprint,
    delay_point_context_base_fingerprint,
    derive_region_evidence_plan,
)
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
    attempt_capture_relative_path,
    complete_relative_path,
)
from jasper.active_speaker.commissioning_host import (
    CommissioningEvidenceHost,
    CommissioningHostAuthoritySnapshot,
    CommissioningHostError,
    RegionCaptureOperation,
    RegionCommissioningInputs,
)
from jasper.active_speaker.commissioning_lifecycle import CommissioningTransition
from jasper.active_speaker.commissioning_run import CommissioningRunStore
from jasper.active_speaker.baseline_profile import (
    recompose_applied_baseline_yaml,
    topology_config_fingerprint,
)
from jasper.active_speaker.capture_geometry import comparison_set_fingerprint
from jasper.active_speaker.driver_safety import (
    build_driver_safety_profile,
    evaluate_driver_safety_profile,
)
from jasper.active_speaker.measurement import (
    active_driver_targets,
    start_active_comparison_set,
)
from jasper.active_speaker import commissioning_runtime as runtime
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    CaptureIdentity,
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
)
from jasper.audio_measurement.excitation_artifacts import (
    GenerationAdmissionArtifact,
    PlaybackAdmissionArtifact,
    canonical_admission_bytes,
)
from jasper.audio_measurement.null_walk import (
    BoundedNullWalkSchedule,
    NullWalkError,
    NullWalkSpec,
)
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_commissioning_evidence import (
    _Harness,
    _capture,
    _hash,
    _region,
)
from tests.test_active_speaker_commissioning_evidence_store import (
    _materialize_complete,
    _open_store,
    _write_exact,
)
from tests.test_active_speaker_commissioning_runtime import FakePort, _request
from tests.test_active_speaker_driver_safety import _manual_settings
from tests.test_active_speaker_profile import _two_way_preset


@dataclass
class _HostHarness:
    evidence_store: CommissioningEvidenceStore
    run_store: CommissioningRunStore
    evidence: _Harness
    topology: OutputTopology
    authority: CommissioningHostAuthoritySnapshot
    inputs: tuple[RegionCommissioningInputs, ...]
    host: CommissioningEvidenceHost
    transport: _SyntheticRawCaptureTransport
    capture_index: int = 0


class _SyntheticRawCaptureTransport:
    """Callable transport seam used by host tests while producer tests own WAVs."""

    def __init__(self) -> None:
        self.harness: _HostHarness | None = None
        self.operation_transform: Callable[
            [RegionCaptureOperation], RegionCaptureOperation
        ] = lambda operation: operation
        self.before_capture: Callable[[], Awaitable[None]] | None = None
        self.last_capture: AdmittedRegionCapture | None = None

    async def __call__(self, _begin_playback):
        pytest.fail("host unit tests replace the concrete WAV producer")

    def callback_for(self, operation: RegionCaptureOperation):
        assert self.harness is not None
        callback = _runtime_callback_for(self.harness)(
            self.operation_transform(operation)
        )

        async def synthetic(context: runtime.CommissioningLiveContext):
            if self.before_capture is not None:
                await self.before_capture()
            result = await callback(context)
            self.last_capture = result.payload
            return result

        return synthetic


class _HostTestSummedCaptureProducer:
    """Narrow host-unit double; the concrete producer has its own test suite."""

    def __init__(self, **kwargs) -> None:
        assert isinstance(kwargs["alsa_device"], str) and kwargs["alsa_device"]
        assert float(kwargs["playback_timeout_s"]) > 0.0
        assert callable(kwargs["load_current_authority"])
        self.transport = kwargs["raw_transport"]

    def callback_for(self, operation: RegionCaptureOperation):
        return self.transport.callback_for(operation)


@pytest.fixture(autouse=True)
def _use_host_unit_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        commissioning_host_module,
        "SummedCaptureProducer",
        _HostTestSummedCaptureProducer,
    )


def _plan(
    tmp_path: Path,
    evidence_store: CommissioningEvidenceStore,
    *,
    owner_id: str,
    run_store: CommissioningRunStore | None = None,
    authority: CommissioningHostAuthoritySnapshot | None = None,
):
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    topology = mono_output_topology(mode="active_2_way")
    if authority is None:
        safety_profile = build_driver_safety_profile(
            topology,
            manual_settings=_manual_settings(),
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-14T12:00:00Z",
        )
        safety = evaluate_driver_safety_profile(safety_profile, topology)
        assert safety.confirmed_and_current
        locks = {
            target["target_id"]: {
                "target_id": target["target_id"],
                "speaker_group_id": target["speaker_group_id"],
                "role": target["role"],
                "tone_frequency_hz": (
                    250.0 if target["role"] == "woofer" else 6250.0
                ),
                "tone_peak_dbfs": -24.0,
                "commissioning_gain_db": 0.0,
                "locked_main_volume_db": (
                    -22.0 if target["role"] == "woofer" else -32.0
                ),
            }
            for target in active_driver_targets(topology)
        }
        comparison_set = start_active_comparison_set(
            topology,
            profile_context_id=str(safety.profile_fingerprint),
            setup_sha256=_hash("setup"),
            device_sha256=_hash("device"),
            calibration_id="test-calibration",
            driver_level_locks=locks,
            bundle_session_id=evidence_store.session_id,
            state_path=tmp_path / "authority-measurements.json",
            now="2026-07-14T12:00:01Z",
        )
        applied_profile = {
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_baseline_profile_candidate",
            "status": "applied",
            "baseline_id": "host-baseline",
            "recomposition_snapshot": {
                "schema_version": 1,
                "domain": "full",
                "topology_id": topology.topology_id,
                "topology_fingerprint": topology_config_fingerprint(topology),
                "preset": preset.to_dict(),
                "playback_device": "hw:Loopback,0",
                "corrections": {
                    "woofer": {
                        "gain_db": 0.0,
                        "delay_ms": 0.0,
                        "inverted": False,
                    },
                    "tweeter": {
                        "gain_db": -6.0,
                        "delay_ms": 0.0,
                        "inverted": False,
                    },
                },
            },
        }
        authority = CommissioningHostAuthoritySnapshot(
            topology=topology,
            preset=preset,
            safety_profile=safety_profile,
            comparison_set=comparison_set,
            applied_profile=applied_profile,
            calibration_id="test-calibration",
            calibration=CalibrationCurve(
                freqs_hz=[20.0, 20_000.0],
                correction_db=[0.0, 0.0],
            ),
        )
    else:
        assert authority.topology == topology
        preset = authority.preset
    safety = evaluate_driver_safety_profile(authority.safety_profile, topology)
    assert safety.profile_fingerprint is not None
    store = run_store or CommissioningRunStore(
        path=tmp_path / "run.json", owner_id=owner_id
    )
    run = store.claim_owner() if run_store is not None else store.start(
        session_id=evidence_store.session_id,
        session_fingerprint=str(authority.comparison_set["fingerprint"]),
    )
    assert run is not None
    normal_active_raw, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=authority.applied_profile,
    )
    assert normal_active_raw is not None and issues == []
    context_fingerprint = active_region_context_fingerprint(
        baseline_active_raw_fingerprint=NormalizedActiveRawIdentity(
            yaml.safe_load(normal_active_raw)
        ).active_raw_fingerprint,
        calibration_id=authority.calibration_id,
        calibration=authority.calibration,
    )
    plan = derive_region_evidence_plan(
        preset,
        topology,
        run=run,
        protected_safety_profile_fingerprint=safety.profile_fingerprint,
        comparison_set_fingerprint=str(authority.comparison_set["fingerprint"]),
        threshold_profile_fingerprint=_hash("thresholds"),
        context_fingerprint=context_fingerprint,
    )
    return store, topology, plan, authority


def _region_inputs(
    evidence_store: CommissioningEvidenceStore,
    plan,
) -> tuple[RegionCommissioningInputs, ...]:
    values: list[RegionCommissioningInputs] = []
    for target in plan.targets:
        seed = 0.0
        attestation_artifact = evidence_store.publish_json_artifact(
            (
                f"geometry/{plan.authority.run.owner_generation}/"
                f"{target.speaker_group_id}/{target.region_id}.json"
            ),
            {
                "provenance": "operator_attested",
                "region_target_fingerprint": target.fingerprint,
                "signed_geometry_seed_us": seed,
            },
        )
        geometry = RegionGeometryAttestation(
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            region_target_fingerprint=target.fingerprint,
            signed_geometry_seed_us=seed,
            provenance_kind="operator_attested",
            provenance_id=f"geometry-{target.region_id}",
            attestation_artifact=attestation_artifact,
        )
        values.append(
            RegionCommissioningInputs(
                target_fingerprint=target.fingerprint,
                placement_fingerprint=_hash(
                    f"placement:{target.speaker_group_id}:{target.region_id}"
                ),
                geometry=geometry,
                null_walk_spec=NullWalkSpec(
                    crossover_fc_hz=target.electrical_fc_hz,
                    geometry_seed_us=seed,
                    positive_delay_target=target.upper_role,
                    negative_delay_target=target.lower_role,
                    step_us=100.0,
                ),
            )
        )
    return tuple(values)


def _host_harness(tmp_path: Path) -> _HostHarness:
    evidence_store = _open_store(tmp_path)
    run_store, topology, plan, authority = _plan(
        tmp_path, evidence_store, owner_id="1" * 32
    )
    evidence = _Harness(store=run_store, plan=plan)
    inputs = _region_inputs(evidence_store, plan)
    transport = _SyntheticRawCaptureTransport()
    host = CommissioningEvidenceHost(
        plan=plan,
        topology=topology,
        run_store=run_store,
        evidence_store=evidence_store,
        region_inputs=inputs,
        load_current_authority=lambda: authority,
        raw_capture_transport=transport,
    )
    harness = _HostHarness(
        evidence_store,
        run_store,
        evidence,
        topology,
        authority,
        inputs,
        host,
        transport,
    )
    transport.harness = harness
    return harness


def _admitted_capture(
    harness: _HostHarness,
    operation: RegionCaptureOperation,
    *,
    graph_fingerprint: str | None = None,
    zero_delay_uses_reverse_graph: bool = False,
):
    plan = harness.evidence.plan
    target_index = plan.targets.index(operation.target)
    index = harness.capture_index
    harness.capture_index += 1
    coordinate = operation.relative_delay_us
    graph = graph_fingerprint
    if graph is None and zero_delay_uses_reverse_graph and coordinate == 0.0:
        graph = _hash(f"graph:{operation.target.fingerprint}:reverse:None")
    graph = graph or _hash(
        f"graph:{operation.target.fingerprint}:{operation.graph_kind}:{coordinate}"
    )
    if operation.evidence_kind == "delay_null":
        assert operation.null_walk_spec is not None
        assert coordinate is not None
        context_base = delay_point_context_base_fingerprint(
            operation.target,
            operation.null_walk_spec,
            coordinate,
            graph,
        )
    else:
        context_base = operation.target.context_base_fingerprint_for(
            operation.evidence_kind
        )
    capture = _capture(
        harness.evidence,
        target_index=target_index,
        evidence_kind=operation.evidence_kind,
        attempt=operation.attempt,
        index=index,
        placement_fingerprint=operation.placement_fingerprint,
        graph_fingerprint=graph,
        target_fingerprint=operation.target_fingerprint,
        context_base_fingerprint=context_base,
    )
    store = harness.evidence_store
    raw = store.publish_raw_artifact(
        f"captures/{operation.attempt.attempt_id}/{operation.capture_ordinal}/raw.wav",
        f"raw:{index}".encode(),
    )
    null_depth = 20.0 - abs(float(coordinate or 0.0)) / 1000.0
    analysis = store.publish_json_artifact(
        f"captures/{operation.attempt.attempt_id}/{operation.capture_ordinal}/analysis.json",
        {
            "acoustic": {
                "above_validity_floor": True,
                "calibrated": True,
                "crossover_fc_hz": operation.target.electrical_fc_hz,
                "expect_null": operation.evidence_kind == "delay_null",
                "gating": {"applied": True},
                "mic_clipping": False,
                "null_depth_capped": False,
                "null_depth_db": null_depth + (operation.capture_ordinal * 0.1),
                "snr": {"decision_class": "alignment", "verdict": "ok"},
            }
        },
    )
    quality = store.publish_json_artifact(
        f"captures/{operation.attempt.attempt_id}/{operation.capture_ordinal}/quality.json",
        {"accepted": True, "capture_index": index},
    )
    identity = CaptureIdentity(
        consumer_id=capture.capture.consumer_id,
        measurement_kind=capture.capture.measurement_kind,
        capture_id=capture.capture.capture_id,
        raw_artifact=raw,
        analysis_input_artifact=analysis,
        target_fingerprint=capture.capture.target_fingerprint,
        context_fingerprint=capture.capture.context_fingerprint,
        geometry_id=capture.capture.geometry_id,
        placement_fingerprint=capture.capture.placement_fingerprint,
        quality_artifact=quality,
        admission_artifact=capture.playback_artifact,
    )
    stimulus_raw = (
        f"stimulus:{capture.region_id}:{capture.evidence_kind}:{index}"
    ).encode()
    stimulus_artifact = ArtifactIdentity(
        bundle_kind=capture.stimulus.artifact.bundle_kind,
        bundle_id=capture.stimulus.artifact.bundle_id,
        relative_path=f"stimuli/{capture.admission_id}.wav",
        sha256=hashlib.sha256(stimulus_raw).hexdigest(),
        byte_size=len(stimulus_raw),
    )
    capture = replace(
        capture,
        capture=identity,
        stimulus=replace(capture.stimulus, artifact=stimulus_artifact),
    )
    _write_exact(
        store.bundle_dir,
        capture.stimulus.artifact,
        stimulus_raw,
    )
    _write_exact(
        store.bundle_dir,
        capture.generation_artifact,
        canonical_admission_bytes(capture.generation_admission),
    )
    _write_exact(
        store.bundle_dir,
        capture.playback_artifact,
        canonical_admission_bytes(capture.playback_admission),
    )
    return capture


def _runtime_callback_for(harness: _HostHarness):
    def callback_for(operation: RegionCaptureOperation):
        async def callback(context: runtime.CommissioningLiveContext):
            capture = _admitted_capture(
                harness,
                operation,
                graph_fingerprint=context.graph.active_raw_fingerprint,
            )
            generation = GenerationAdmissionArtifact(
                authority=harness.evidence_store.admission_authority,
                admission_id=capture.admission_id,
                admission=capture.generation_admission,
                artifact=capture.generation_artifact,
            )
            playback = PlaybackAdmissionArtifact(
                generation=generation,
                admission=capture.playback_admission,
                artifact=capture.playback_artifact,
            )
            proof = capture.playback_admission.protection_evidence
            assert proof is not None
            return runtime.AdmittedCaptureCallbackResult(
                generation=generation,
                playback=playback,
                stimulus=capture.stimulus,
                protection_evidence=proof,
                payload=capture,
            )

        return callback

    return callback_for


def _host_from(
    harness: _HostHarness,
    *,
    run_store: CommissioningRunStore | None = None,
    authority: CommissioningHostAuthoritySnapshot | None = None,
) -> CommissioningEvidenceHost:
    current_authority = authority or harness.authority
    return CommissioningEvidenceHost(
        plan=harness.evidence.plan,
        topology=harness.topology,
        run_store=run_store or harness.run_store,
        evidence_store=harness.evidence_store,
        region_inputs=harness.inputs,
        load_current_authority=lambda: current_authority,
        raw_capture_transport=harness.transport,
    )


def _restore_synthetic_operation(
    harness: _HostHarness,
    operation: RegionCaptureOperation,
):
    issued = harness.run_store.current_live_mutation(operation.attempt.run)
    assert issued is not None
    assert issued.status == "issued"
    assert issued.issuance_id == operation.issuance_id
    assert issued.operation_fingerprint == operation.fingerprint
    raw = _request("normal").normal_active_raw
    normalized = NormalizedActiveRawIdentity(yaml.safe_load(raw))
    predecessor = ExactDspStateIdentity(
        {
            "active_raw": raw,
            "normalized_active_raw": normalized.normalized_active_raw,
            "config_path": "/tmp/synthetic-active.yml",
            "listening_volume_db": -28.0,
        }
    )
    base = (
        f"runtime-rollback/{operation.attempt.run.run_id}/"
        f"{issued.started_owner_generation}/{issued.issuance_id}"
    )
    anchor = harness.evidence_store.publish_json_artifact(
        f"{base}/predecessor.json",
        predecessor.to_dict(),
    )
    reopened = ExactDspStateIdentity.from_mapping(
        harness.evidence_store.reopen_json_artifact(anchor)
    )
    assert reopened == predecessor
    pending = harness.run_store.record_live_mutation_intent(
        operation.attempt.run,
        issued,
        rollback_artifact_path=anchor.relative_path,
        rollback_artifact_fingerprint=anchor.fingerprint,
    )
    restore_payload = {
        "schema_version": 1,
        "kind": "jts_active_summed_measurement_restore",
        "issuance_id": issued.issuance_id,
        "operation_fingerprint": operation.fingerprint,
        "predecessor_fingerprint": predecessor.fingerprint,
        "restored_graph_fingerprint": normalized.active_raw_fingerprint,
        "restored_config_path": "/tmp/synthetic-active.yml",
        "restored_listening_volume_db": -28.0,
    }
    marker = harness.evidence_store.publish_json_artifact(
        f"{base}/restored.json",
        restore_payload,
    )
    assert harness.evidence_store.reopen_json_artifact(marker) == restore_payload
    return harness.run_store.record_live_mutation_restored(
        operation.attempt.run,
        pending,
        restoration_evidence_fingerprint=marker.fingerprint,
    )


def _commit_synthetic_capture(
    harness: _HostHarness,
    operation: RegionCaptureOperation,
    capture,
):
    _restore_synthetic_operation(harness, operation)
    return harness.host.commit_capture(operation, capture)


@pytest.mark.asyncio
async def test_production_join_holds_runtime_transaction_through_host_commit(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    fake = FakePort()
    predecessor_raw = fake.raw
    predecessor_volume = fake.volume

    committed = await harness.host.capture_next_with_runtime(
        fake.port(),
        config_dir=str(tmp_path),
    )

    assert committed is not None
    assert committed.graph_fingerprint == NormalizedActiveRawIdentity(
        yaml.safe_load(fake.apply_calls[0])
    ).active_raw_fingerprint
    assert fake.raw == predecessor_raw
    assert fake.volume == predecessor_volume
    assert fake.volume_calls[0] == -32.0
    assert harness.run_store.pending_live_mutation(harness.evidence.plan.authority.run) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [False, True])
async def test_runtime_restore_failure_durably_blocks_unknown_live_state(
    tmp_path: Path,
    protected: bool,
) -> None:
    harness = _host_harness(tmp_path)
    if protected:
        assert harness.run_store.transition(
            harness.evidence.plan.authority.run,
            CommissioningTransition(
                from_state="unconfigured",
                to_state="protected",
                evidence_kind="protection_evidence",
                evidence_fingerprint=_hash("protection"),
            ),
        )
    fake = FakePort()
    fake.fail_apply_call = 2

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await harness.host.capture_next_with_runtime(
            fake.port(),
            config_dir=str(tmp_path),
        )

    assert raised.value.code == "restore_failed"
    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "blocked_live_state_unknown"
    )
    assert harness.run_store.pending_live_mutation(
        harness.evidence.plan.authority.run
    ) is not None
    assert harness.host.status()["live_mutation_recovery_required"] is True

    fake.fail_apply_call = None
    with pytest.raises(CommissioningHostError) as recovered:
        await harness.host.capture_next_with_runtime(
            fake.port(),
            config_dir=str(tmp_path),
        )
    assert recovered.value.code == "lifecycle_not_collecting"
    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "rolled_back"
    )
    assert harness.run_store.pending_live_mutation(
        harness.evidence.plan.authority.run
    ) is None


@pytest.mark.asyncio
async def test_restart_recovers_pending_predecessor_before_next_operation(
    tmp_path: Path,
) -> None:
    original = _host_harness(tmp_path)
    fake = FakePort()
    predecessor_raw = fake.raw
    predecessor_graph = NormalizedActiveRawIdentity(yaml.safe_load(predecessor_raw))
    predecessor = ExactDspStateIdentity(
        {
            "active_raw": predecessor_raw,
            "normalized_active_raw": predecessor_graph.normalized_active_raw,
            "config_path": fake.path,
            "listening_volume_db": fake.volume,
        }
    )
    operation = original.host.next_operation()
    assert operation is not None
    issued = original.run_store.current_live_mutation(
        original.evidence.plan.authority.run
    )
    assert issued is not None and issued.status == "issued"
    assert issued.issuance_id == operation.issuance_id
    anchor = original.evidence_store.publish_json_artifact(
        (
            f"runtime-rollback/{operation.attempt.run.run_id}/"
            f"{issued.started_owner_generation}/{issued.issuance_id}/"
            "predecessor.json"
        ),
        predecessor.to_dict(),
    )
    original.run_store.record_live_mutation_intent(
        original.evidence.plan.authority.run,
        issued,
        rollback_artifact_path=anchor.relative_path,
        rollback_artifact_fingerprint=anchor.fingerprint,
    )
    fake.raw = _request("normal").normal_active_raw
    fake.volume = -32.0

    restarted_store = CommissioningRunStore(
        path=tmp_path / "run.json", owner_id="7" * 32
    )
    restarted_store, topology, fresh_plan, authority = _plan(
        tmp_path,
        original.evidence_store,
        owner_id="7" * 32,
        run_store=restarted_store,
        authority=original.authority,
    )
    fresh_inputs = _region_inputs(original.evidence_store, fresh_plan)
    fresh_transport = _SyntheticRawCaptureTransport()
    fresh_host = CommissioningEvidenceHost(
        plan=fresh_plan,
        topology=topology,
        run_store=restarted_store,
        evidence_store=original.evidence_store,
        region_inputs=fresh_inputs,
        load_current_authority=lambda: authority,
        raw_capture_transport=fresh_transport,
    )
    fresh = _HostHarness(
        evidence_store=original.evidence_store,
        run_store=restarted_store,
        evidence=_Harness(store=restarted_store, plan=fresh_plan),
        topology=topology,
        authority=authority,
        inputs=fresh_inputs,
        host=fresh_host,
        transport=fresh_transport,
    )
    fresh_transport.harness = fresh

    committed = await fresh.host.capture_next_with_runtime(
        fake.port(),
        config_dir=str(tmp_path),
    )

    assert committed is not None
    assert yaml.safe_load(fake.apply_calls[0]) == yaml.safe_load(predecessor_raw)
    assert fake.raw == predecessor_raw
    assert fake.volume == -28.0
    assert restarted_store.pending_live_mutation(fresh_plan.authority.run) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority_kind",
    ["preset", "comparison", "safety", "applied"],
)
async def test_production_join_refuses_stale_authority_before_mutation(
    tmp_path: Path,
    authority_kind: str,
) -> None:
    harness = _host_harness(tmp_path)
    snapshot = harness.authority
    if authority_kind == "preset":
        raw = deepcopy(snapshot.preset.to_dict())
        raw["preset_id"] = "stale-preset"
        stale = replace(
            snapshot,
            preset=ActiveSpeakerPreset.from_mapping(raw),
        )
    elif authority_kind == "comparison":
        comparison: dict[str, Any] = deepcopy(dict(snapshot.comparison_set))
        comparison["comparison_set_id"] = "f" * 32
        comparison["fingerprint"] = comparison_set_fingerprint(comparison)
        stale = replace(snapshot, comparison_set=comparison)
    elif authority_kind == "safety":
        safety: dict[str, Any] = deepcopy(dict(snapshot.safety_profile))
        safety["profile_fingerprint"] = "f" * 64
        stale = replace(snapshot, safety_profile=safety)
    else:
        applied: dict[str, Any] = deepcopy(dict(snapshot.applied_profile))
        applied["status"] = "candidate"
        stale = replace(snapshot, applied_profile=applied)
    host = _host_from(harness, authority=stale)
    fake = FakePort()

    with pytest.raises(CommissioningHostError) as raised:
        await host.capture_next_with_runtime(
            fake.port(),
            config_dir=str(tmp_path),
        )

    assert raised.value.code == "fresh_authority_stale"
    assert fake.apply_calls == []
    terminal = harness.run_store.current_live_mutation(
        harness.evidence.plan.authority.run
    )
    assert terminal is not None and terminal.status == "released"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context_kind", ["applied_baseline", "profile_context", "calibration"]
)
async def test_production_join_refuses_program_context_drift_between_captures(
    tmp_path: Path,
    context_kind: str,
) -> None:
    harness = _host_harness(tmp_path)
    fake = FakePort()
    committed = await harness.host.capture_next_with_runtime(
        fake.port(),
        config_dir=str(tmp_path),
    )
    assert committed is not None
    apply_count = len(fake.apply_calls)

    if context_kind == "applied_baseline":
        applied: dict[str, Any] = deepcopy(dict(harness.authority.applied_profile))
        applied["recomposition_snapshot"]["corrections"]["tweeter"]["gain_db"] = -5.0
        stale = replace(harness.authority, applied_profile=applied)
    elif context_kind == "profile_context":
        comparison: dict[str, Any] = deepcopy(dict(harness.authority.comparison_set))
        comparison["profile_context_id"] = _hash("changed-profile-context")
        comparison["fingerprint"] = comparison_set_fingerprint(comparison)
        stale = replace(harness.authority, comparison_set=comparison)
    else:
        stale = replace(
            harness.authority,
            calibration=CalibrationCurve(
                freqs_hz=[20.0, 20_000.0],
                correction_db=[0.0, 1.0],
            ),
        )
    stale_host = _host_from(harness, authority=stale)

    with pytest.raises(CommissioningHostError) as raised:
        await stale_host.capture_next_with_runtime(
            fake.port(),
            config_dir=str(tmp_path),
        )

    assert raised.value.code == "fresh_authority_stale"
    assert len(fake.apply_calls) == apply_count
    terminal = harness.run_store.current_live_mutation(
        harness.evidence.plan.authority.run
    )
    assert terminal is not None and terminal.status == "released"


@pytest.mark.asyncio
async def test_restored_failure_aborts_exact_issuance_and_retry_is_fresh(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    original = harness.host.next_operation()
    assert original is not None and original.issuance_id is not None
    harness.transport.operation_transform = lambda operation: replace(
            operation,
            placement_fingerprint=_hash("wrong-runtime-placement"),
        )

    with pytest.raises(CommissioningHostError) as raised:
        await harness.host.capture_next_with_runtime(
            FakePort().port(),
            config_dir=str(tmp_path),
        )
    assert raised.value.code == "capture_operation_mismatch"
    aborted = harness.run_store.current_live_mutation(
        harness.evidence.plan.authority.run
    )
    assert aborted is not None and aborted.status == "aborted"
    assert aborted.issuance_id == original.issuance_id
    assert aborted.restoration_evidence_fingerprint is not None
    assert aborted.terminal_evidence_fingerprint is not None
    assert aborted.rollback_artifact_path is not None
    assert f"/{aborted.issuance_id}/" in aborted.rollback_artifact_path

    retried = harness.host.next_operation()
    assert retried is not None
    assert retried.fingerprint == original.fingerprint
    assert retried.issuance_id != original.issuance_id
    late_capture = harness.transport.last_capture
    assert late_capture is not None
    with pytest.raises(CommissioningHostError) as late:
        harness.host.commit_capture(
            original,
            late_capture,
        )
    assert late.value.code == "operation_not_restored"


def test_plan_persistence_does_not_invent_live_protection_evidence(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)

    harness.host.prepare()
    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "unconfigured"
    )
    operation = harness.host.next_operation()
    assert operation is not None
    capture = _admitted_capture(harness, operation)
    _commit_synthetic_capture(harness, operation, capture)

    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "protected"
    )
    snapshot = harness.run_store.snapshot()["current"]
    assert snapshot["transition_journal"][0]["transition"]["evidence_fingerprint"] == (
        capture.generation_artifact.fingerprint
    )


def test_host_progresses_only_exact_operations_to_durable_measured(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    observed: list[RegionCaptureOperation] = []

    while (operation := harness.host.next_operation()) is not None:
        observed.append(operation)
        _commit_synthetic_capture(
            harness,
            operation, _admitted_capture(harness, operation)
        )

    complete = harness.evidence_store.reopen_complete_commissioning_evidence(
        run_id=harness.evidence.plan.authority.run.run_id
    )
    assert isinstance(complete, CompleteCommissioningEvidence)
    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "measured"
    )
    assert harness.host.status()["complete"] is True
    assert [item.evidence_kind for item in observed[:6]] == [
        "normal",
        "normal",
        "normal",
        "reverse",
        "reverse",
        "reverse",
    ]
    assert all(
        len(region.normal.captures) == 3
        and len(region.reverse.captures) == 3
        and all(len(point.captures) == 5 for point in region.delay_walk.points)
        for region in complete.regions
    )
    assert all(
        region.delay_walk.schedule
        == region.delay_walk.schedule.from_coarse_evidence(
            region.delay_walk.spec,
            {
                point.relative_delay_us: tuple(
                    harness.evidence_store.reopen_json_artifact(
                        capture.capture.analysis_input_artifact
                    )
                    for capture in point.captures
                )
                for point in region.delay_walk.points
                if point.relative_delay_us
                in region.delay_walk.schedule.coarse_delays_us
            },
        )
        for region in complete.regions
    )


def test_zero_delay_cannot_reuse_the_reverse_baseline_graph(tmp_path: Path) -> None:
    harness = _host_harness(tmp_path)

    with pytest.raises(CommissioningHostError) as raised:
        while (operation := harness.host.next_operation()) is not None:
            _commit_synthetic_capture(
                harness,
                operation,
                _admitted_capture(
                    harness,
                    operation,
                    zero_delay_uses_reverse_graph=True,
                ),
            )

    assert raised.value.code == "graph_identity_replayed"
    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "protected"
    )


def test_same_process_retry_reuses_attempt_and_replayed_capture_is_refused(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    first = harness.host.next_operation()
    retried = harness.host.next_operation()
    assert first is not None and retried == first
    assert len(harness.run_store.attempts(harness.evidence.plan.authority.run)) == 1

    capture = _admitted_capture(harness, first)
    _commit_synthetic_capture(harness, first, capture)
    second = harness.host.next_operation()
    assert second is not None
    assert second.attempt == first.attempt
    assert second.capture_ordinal == 1
    with pytest.raises(CommissioningHostError) as raised:
        _commit_synthetic_capture(harness, second, capture)
    assert raised.value.code == "capture_identity_replayed"


def test_restart_aborts_restored_execution_without_capture_commit_marker(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    operation = harness.host.next_operation()
    assert operation is not None and operation.issuance_id is not None
    _restore_synthetic_operation(harness, operation)
    assert harness.host.status()["live_mutation_recovery_required"] is True

    peer_store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id=harness.evidence.plan.authority.run.owner_id,
    )
    peer = _host_from(harness, run_store=peer_store)
    retried = peer.next_operation()

    assert retried is not None
    assert retried.fingerprint == operation.fingerprint
    assert retried.issuance_id != operation.issuance_id
    current = peer_store.current_live_mutation(harness.evidence.plan.authority.run)
    assert current is not None and current.status == "issued"


def test_restart_commits_reopened_capture_after_crash_before_sidecar_commit(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    operation = harness.host.next_operation()
    assert operation is not None and operation.issuance_id is not None
    capture = _admitted_capture(harness, operation)
    restored = _restore_synthetic_operation(harness, operation)
    capture_path = attempt_capture_relative_path(
        operation.attempt.attempt_id,
        operation.capture_ordinal,
    )
    commit_marker = {
        "schema_version": 1,
        "kind": "jts_active_summed_measurement_capture_commit",
        "issuance_id": restored.issuance_id,
        "operation_fingerprint": operation.fingerprint,
        "capture_fingerprint": capture.fingerprint,
        "capture_relative_path": capture_path,
    }
    harness.evidence_store.publish_json_artifact(
        harness.host._capture_commit_marker_path(restored),
        commit_marker,
    )
    capture_artifact = harness.evidence_store.publish_admitted_region_capture(
        capture,
        ordinal=operation.capture_ordinal,
    )

    peer_store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id=harness.evidence.plan.authority.run.owner_id,
    )
    peer = _host_from(harness, run_store=peer_store)
    advanced = peer.next_operation()

    assert advanced is not None and advanced.capture_ordinal == 1
    terminal = peer_store.current_live_mutation(harness.evidence.plan.authority.run)
    assert terminal is not None and terminal.status == "issued"
    assert terminal.issuance_id == advanced.issuance_id
    # The prior committed terminal is overwritten only by the freshly issued
    # next operation, so the canonical capture itself is the durable progress.
    reopened = harness.evidence_store.reopen_admitted_region_capture(
        capture_artifact
    )
    assert reopened == capture


@pytest.mark.asyncio
async def test_two_independent_hosts_allow_only_one_issuance_to_execute(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    peer_store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id=harness.evidence.plan.authority.run.owner_id,
    )
    peer_host = _host_from(harness, run_store=peer_store)
    first_operation = harness.host.next_operation()
    peer_operation = peer_host.next_operation()
    assert first_operation is not None and peer_operation == first_operation
    first_fake = FakePort()
    capture_started = asyncio.Event()
    release_capture = asyncio.Event()
    callback_count = 0

    async def before_capture() -> None:
        nonlocal callback_count
        callback_count += 1
        capture_started.set()
        await release_capture.wait()

    harness.transport.before_capture = before_capture

    first = asyncio.create_task(
        harness.host.capture_next_with_runtime(
            first_fake.port(),
            config_dir=str(tmp_path),
        )
    )
    await capture_started.wait()
    with pytest.raises(CommissioningHostError) as raised:
        peer_host._runtime_mutation_journal(peer_operation)
    assert raised.value.code == "operation_stale"
    release_capture.set()
    assert await first is not None
    assert callback_count == 1


@pytest.mark.asyncio
async def test_restored_in_flight_capture_cannot_be_recovered_by_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _host_harness(tmp_path)
    peer_store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id=harness.evidence.plan.authority.run.owner_id,
    )
    peer = _host_from(harness, run_store=peer_store)
    operation = harness.host.next_operation()
    assert operation is not None
    assert peer.next_operation() == operation
    runtime_restored = asyncio.Event()
    allow_host_commit = asyncio.Event()
    original_run = commissioning_host_module.run_summed_capture

    async def pause_after_restore(*args, **kwargs):
        result = await original_run(*args, **kwargs)
        mutation = harness.run_store.current_live_mutation(operation.attempt.run)
        assert mutation is not None and mutation.status == "restored"
        runtime_restored.set()
        await allow_host_commit.wait()
        return result

    monkeypatch.setattr(
        commissioning_host_module,
        "run_summed_capture",
        pause_after_restore,
    )
    first = asyncio.create_task(
        harness.host.capture_next_with_runtime(
            FakePort().port(),
            config_dir=str(tmp_path),
        )
    )
    await runtime_restored.wait()

    with pytest.raises(CommissioningHostError) as planned:
        peer.next_operation()
    assert planned.value.code == "live_mutation_execution_in_progress"
    with pytest.raises(CommissioningHostError) as executed:
        await peer.capture_next_with_runtime(
            FakePort().port(),
            config_dir=str(tmp_path),
        )
    assert executed.value.code == "live_mutation_execution_in_progress"
    still_restored = peer_store.current_live_mutation(operation.attempt.run)
    assert still_restored is not None and still_restored.status == "restored"

    allow_host_commit.set()
    assert await first is not None
    committed = peer_store.current_live_mutation(operation.attempt.run)
    assert committed is not None and committed.status == "committed"


def test_equal_normal_reverse_graph_refuses_before_delay_attempt(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    repeated_graph = _hash("same-normal-reverse-graph")

    with pytest.raises(CommissioningHostError) as raised:
        while (operation := harness.host.next_operation()) is not None:
            _commit_synthetic_capture(
                harness,
                operation,
                _admitted_capture(
                    harness,
                    operation,
                    graph_fingerprint=repeated_graph,
                ),
            )

    assert raised.value.code == "graph_identity_replayed"
    assert len(harness.run_store.attempts(harness.evidence.plan.authority.run)) == 2


def test_coarse_schedule_failure_is_durable_and_reserves_no_refinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _host_harness(tmp_path)

    def refuse_schedule(*_args, **_kwargs):
        raise NullWalkError("non-repeatable coarse evidence")

    monkeypatch.setattr(
        BoundedNullWalkSchedule,
        "from_coarse_evidence",
        refuse_schedule,
    )
    with pytest.raises(CommissioningHostError) as raised:
        while (operation := harness.host.next_operation()) is not None:
            _commit_synthetic_capture(
                harness,
                operation,
                _admitted_capture(harness, operation),
            )

    assert raised.value.code == "delay_schedule_refused"
    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "blocked"
    )
    coarse_count = len(harness.inputs[0].null_walk_spec.coarse_candidate_delays_us())
    assert len(harness.run_store.attempts(harness.evidence.plan.authority.run)) == (
        2 + coarse_count
    )


def test_restart_abandons_incomplete_generation_and_refuses_late_commit(
    tmp_path: Path,
) -> None:
    original = _host_harness(tmp_path)
    stale_operation = original.host.next_operation()
    assert stale_operation is not None

    restarted_store = CommissioningRunStore(
        path=tmp_path / "run.json", owner_id="2" * 32
    )
    restarted_store, topology, fresh_plan, authority = _plan(
        tmp_path,
        original.evidence_store,
        owner_id="2" * 32,
        run_store=restarted_store,
        authority=original.authority,
    )
    fresh_inputs = _region_inputs(original.evidence_store, fresh_plan)
    fresh = CommissioningEvidenceHost(
        plan=fresh_plan,
        topology=topology,
        run_store=restarted_store,
        evidence_store=original.evidence_store,
        region_inputs=fresh_inputs,
        load_current_authority=lambda: authority,
    )
    fresh_operation = fresh.next_operation()
    assert fresh_operation is not None
    assert fresh_operation.attempt.run.owner_generation == 2
    assert fresh_operation.attempt != stale_operation.attempt

    with pytest.raises(CommissioningHostError) as raised:
        original.host.commit_capture(
            stale_operation, _admitted_capture(original, stale_operation)
        )
    assert raised.value.code == "run_generation_stale"


def test_restart_recovers_complete_persisted_before_measured_transition(
    tmp_path: Path,
) -> None:
    original = _host_harness(tmp_path)
    original.host.prepare()
    placement = original.inputs[0].placement_fingerprint
    region = _region(
        original.evidence,
        target_index=0,
        placement_fingerprint=placement,
    )
    complete = CompleteCommissioningEvidence(
        plan=original.evidence.plan, regions=(region,)
    )
    complete = _materialize_complete(original.evidence_store, complete)
    assert original.run_store.transition(
        original.evidence.plan.authority.run,
        CommissioningTransition(
            from_state="unconfigured",
            to_state="protected",
            evidence_kind="protection_evidence",
            evidence_fingerprint=(
                complete.regions[0].normal.captures[0].generation_artifact.fingerprint
            ),
        ),
    )
    original.evidence_store.publish_complete_commissioning_evidence(complete)
    assert original.run_store.lifecycle_state(original.evidence.plan.authority.run) == (
        "protected"
    )

    restarted_store = CommissioningRunStore(
        path=tmp_path / "run.json", owner_id="3" * 32
    )
    restarted_store, topology, fresh_plan, authority = _plan(
        tmp_path,
        original.evidence_store,
        owner_id="3" * 32,
        run_store=restarted_store,
        authority=original.authority,
    )
    fresh = CommissioningEvidenceHost(
        plan=fresh_plan,
        topology=topology,
        run_store=restarted_store,
        evidence_store=original.evidence_store,
        region_inputs=_region_inputs(original.evidence_store, fresh_plan),
        load_current_authority=lambda: replace(
            authority,
            calibration=CalibrationCurve(
                freqs_hz=[20.0, 20_000.0],
                correction_db=[0.0, 1.0],
            ),
        ),
    )

    with pytest.raises(CommissioningHostError) as drifted:
        fresh.prepare()
    assert drifted.value.code == "fresh_authority_stale"
    assert restarted_store.lifecycle_state(fresh_plan.authority.run) == "protected"

    fresh._load_current_authority = lambda: authority
    assert fresh.prepare() == complete
    assert restarted_store.lifecycle_state(fresh_plan.authority.run) == "measured"
    assert fresh.next_operation() is None


def test_complete_is_not_assembled_from_regions_after_authority_drift(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    harness.host.prepare()
    region = _region(
        harness.evidence,
        target_index=0,
        placement_fingerprint=harness.inputs[0].placement_fingerprint,
    )
    complete = _materialize_complete(
        harness.evidence_store,
        CompleteCommissioningEvidence(
            plan=harness.evidence.plan,
            regions=(region,),
        ),
    )
    harness.evidence_store.publish_region_commissioning_evidence(
        complete.regions[0]
    )
    assert harness.run_store.transition(
        harness.evidence.plan.authority.run,
        CommissioningTransition(
            from_state="unconfigured",
            to_state="protected",
            evidence_kind="protection_evidence",
            evidence_fingerprint=(
                complete.regions[0].normal.captures[0].generation_artifact.fingerprint
            ),
        ),
    )
    drifted = replace(
        harness.authority,
        calibration=CalibrationCurve(
            freqs_hz=[20.0, 20_000.0],
            correction_db=[0.0, 1.0],
        ),
    )
    restarted = _host_from(harness, authority=drifted)

    with pytest.raises(CommissioningHostError) as raised:
        restarted.next_operation()

    assert raised.value.code == "fresh_authority_stale"
    assert harness.run_store.lifecycle_state(harness.evidence.plan.authority.run) == (
        "protected"
    )
    assert not (
        harness.evidence_store.bundle_dir
        / complete_relative_path(harness.evidence.plan.authority.run.run_id)
    ).exists()


def test_measured_recovery_requires_journal_to_name_exact_complete(
    tmp_path: Path,
) -> None:
    harness = _host_harness(tmp_path)
    placement = harness.inputs[0].placement_fingerprint
    complete = CompleteCommissioningEvidence(
        plan=harness.evidence.plan,
        regions=(
            _region(
                harness.evidence,
                target_index=0,
                placement_fingerprint=placement,
            ),
        ),
    )
    complete = _materialize_complete(harness.evidence_store, complete)
    harness.evidence_store.publish_complete_commissioning_evidence(complete)
    assert harness.run_store.transition(
        harness.evidence.plan.authority.run,
        CommissioningTransition(
            from_state="unconfigured",
            to_state="protected",
            evidence_kind="protection_evidence",
            evidence_fingerprint=(
                complete.regions[0].normal.captures[0].generation_artifact.fingerprint
            ),
        ),
    )
    assert harness.run_store.transition(
        harness.evidence.plan.authority.run,
        CommissioningTransition(
            from_state="protected",
            to_state="measured",
            evidence_kind="admitted_measurement_set",
            evidence_fingerprint=_hash("wrong-complete-artifact"),
        ),
    )

    with pytest.raises(CommissioningHostError) as raised:
        harness.host.prepare()
    assert raised.value.code == "complete_evidence_stale"
