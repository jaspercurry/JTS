# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.bundles import BUNDLE_KIND
from jasper.active_speaker.commissioning_evidence import (
    ACTIVE_ISOLATED_DRIVER_EVIDENCE_CONSUMER_ID,
    ACTIVE_ISOLATED_DRIVER_MEASUREMENT_KIND,
    ACTIVE_REGION_DELAY_NULL_MEASUREMENT_KIND,
    ACTIVE_REGION_EVIDENCE_CONSUMER_ID,
    ACTIVE_REGION_NORMAL_MEASUREMENT_KIND,
    ACTIVE_REGION_REVERSE_MEASUREMENT_KIND,
    DELAY_WALK_ALGORITHM_ID,
    DELAY_WALK_ALGORITHM_VERSION,
    AdmittedIsolatedDriverCapture,
    AdmittedRegionCapture,
    CommissioningEvidenceError,
    CompleteIsolatedDriverEvidence,
    CompleteCommissioningEvidence,
    DelayPointEvidence,
    DelayWalkEvidence,
    EvidenceKind,
    IsolatedDriverEvidence,
    RegionCommissioningEvidence,
    RegionEvidencePlan,
    RegionGeometryAttestation,
    StationaryRegionEvidence,
    capture_attempt_context_fingerprint,
    delay_point_context_base_fingerprint,
    delay_point_target_fingerprint,
    derive_region_evidence_plan,
    evidence_attempt_target_id,
    isolated_capture_context_base_fingerprint,
    isolated_capture_context_fingerprint,
    isolated_driver_attempt_target_id,
    isolated_driver_evidence_target_fingerprint,
)
from jasper.active_speaker.commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningRunStore,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
from jasper.audio_measurement.evidence_identity import ArtifactIdentity, CaptureIdentity
from jasper.audio_measurement.excitation_admission import (
    ExcitationAdmission,
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)
from jasper.audio_measurement.excitation_artifacts import (
    GENERATION_PATH_PREFIX,
    PLAYBACK_PATH_PREFIX,
    canonical_admission_bytes,
)
from jasper.audio_measurement.null_walk import (
    BoundedNullWalkSchedule,
    NullWalkError,
    NullWalkSpec,
)
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_profile import _three_way_preset


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _preset(*, layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_three_way_preset(layout=layout))


def _stereo_three_way_topology(
    *,
    include_right: bool = True,
    right_mode: str = "active_3_way",
) -> OutputTopology:
    raw = mono_output_topology(mode="active_3_way").to_dict()
    source = raw["speaker_groups"][0]
    left = copy.deepcopy(source)
    left.update({"id": "left", "label": "Left cabinet", "kind": "left"})
    groups = [left]
    if include_right:
        right = copy.deepcopy(source)
        right.update(
            {
                "id": "right",
                "label": "Right cabinet",
                "kind": "right",
                "mode": right_mode,
            }
        )
        if right_mode == "active_2_way":
            right["channels"] = [
                channel
                for channel in right["channels"]
                if channel["role"] in {"woofer", "tweeter"}
            ]
        for index, channel in enumerate(right["channels"], start=3):
            channel["physical_output_index"] = index
            channel["human_output_label"] = f"DAC output {index + 1}"
        groups.append(right)
    raw["speaker_groups"] = groups
    raw["routing"] = {
        "main_left_group_id": "left",
        "main_right_group_id": "right" if include_right else None,
        "mono_group_id": None,
        "subwoofer_group_ids": [],
    }
    return OutputTopology.from_mapping(raw)


@dataclass(frozen=True)
class _Harness:
    store: CommissioningRunStore
    plan: RegionEvidencePlan


def _harness(
    tmp_path: Path,
    *,
    name: str = "base",
    bounded_regions: bool = False,
) -> _Harness:
    session_fingerprint = _hash(f"session:{name}")
    store = CommissioningRunStore(
        path=tmp_path / f"{name}.json",
        owner_id=_hash(f"owner:{name}")[:32],
    )
    run = store.start(
        session_id=f"session-{name}",
        session_fingerprint=session_fingerprint,
    )
    preset = _preset()
    if bounded_regions:
        preset = replace(
            preset,
            crossover_regions=(
                replace(preset.crossover_regions[0], fc_hz=1_000.0),
                preset.crossover_regions[1],
            ),
        )
    plan = derive_region_evidence_plan(
        preset,
        mono_output_topology(mode="active_3_way"),
        run=run,
        protected_safety_profile_fingerprint=_hash("profile"),
        comparison_set_fingerprint=session_fingerprint,
        threshold_profile_fingerprint=_hash("thresholds"),
        context_fingerprint=_hash("context"),
    )
    return _Harness(store=store, plan=plan)


def _artifact(
    session_id: str,
    path: str,
    *,
    content_token: str,
    bundle_kind: str = BUNDLE_KIND,
) -> ArtifactIdentity:
    content = content_token.encode()
    return ArtifactIdentity(
        bundle_kind=bundle_kind,
        bundle_id=session_id,
        relative_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )


def _admission_artifact(
    session_id: str,
    path: str,
    admission: ExcitationAdmission,
) -> ArtifactIdentity:
    content = canonical_admission_bytes(admission)
    return ArtifactIdentity(
        bundle_kind=BUNDLE_KIND,
        bundle_id=session_id,
        relative_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )


def _allowed_admission(
    plan: RegionEvidencePlan,
    target_fingerprint: str,
    *,
    proof_token: str | None = None,
) -> ExcitationAdmission:
    excitation_plan = _hash(f"excitation:{target_fingerprint}")
    requirement = _hash(f"protection:{target_fingerprint}")
    limits = ExcitationLimits(
        permitted_band=FrequencyBand(100.0, 8_000.0),
        maximum_effective_peak_dbfs=-6.0,
        maximum_duration_s=2.0,
        maximum_repeat_count=1,
        target_fingerprint=target_fingerprint,
        safety_profile_fingerprint=(
            plan.authority.protected_safety_profile_fingerprint
        ),
        protection_requirement_fingerprint=requirement,
        excitation_plan_fingerprint=excitation_plan,
    )
    request = ExcitationRequest(
        band=FrequencyBand(200.0, 5_000.0),
        effective_peak_dbfs=-12.0,
        duration_s=1.0,
        repeat_count=1,
        target_fingerprint=target_fingerprint,
        safety_profile_fingerprint=limits.safety_profile_fingerprint,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
    )
    admission = admit_excitation(
        request,
        limits,
        protection_evidence=ProtectionEvidence(
            target_fingerprint=target_fingerprint,
            safety_profile_fingerprint=limits.safety_profile_fingerprint,
            protection_requirement_fingerprint=requirement,
            authority_fingerprint=limits.fingerprint,
            excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
            evidence_fingerprint=_hash(
                f"graph-proof:{proof_token or target_fingerprint}"
            ),
            current=True,
        ),
    )
    assert admission.allowed
    return admission


def _reserve(
    harness: _Harness,
    *,
    evidence_kind: EvidenceKind,
    target_fingerprint: str,
) -> CommissioningAttemptHandle:
    return harness.store.reserve_attempt(
        harness.plan.authority.run,
        target_id=evidence_attempt_target_id(
            evidence_kind,
            target_fingerprint,
        ),
        target_fingerprint=target_fingerprint,
    )


def _capture(
    harness: _Harness,
    *,
    target_index: int,
    evidence_kind: EvidenceKind,
    attempt: CommissioningAttemptHandle,
    index: int,
    placement_fingerprint: str,
    graph_fingerprint: str,
    target_fingerprint: str | None = None,
    context_base_fingerprint: str | None = None,
    raw_token: str | None = None,
) -> AdmittedRegionCapture:
    plan = harness.plan
    target = plan.targets[target_index]
    target_fp = target_fingerprint or target.target_fingerprint_for(evidence_kind)
    context_base_fp = context_base_fingerprint or target.context_base_fingerprint_for(
        evidence_kind
    )
    admission_id = f"{target.region_id}-{evidence_kind}-{index}"
    admission = _allowed_admission(plan, target_fp)
    assert admission.protection_evidence is not None
    proof_fingerprint = admission.protection_evidence.evidence_fingerprint
    assert proof_fingerprint is not None
    context_fp = capture_attempt_context_fingerprint(
        plan.authority,
        attempt=attempt,
        evidence_kind=evidence_kind,
        target_fingerprint=target_fp,
        context_base_fingerprint=context_base_fp,
        graph_fingerprint=graph_fingerprint,
        generation_protection_evidence_fingerprint=proof_fingerprint,
        playback_protection_evidence_fingerprint=proof_fingerprint,
    )
    session_id = plan.authority.commissioning_session_id
    generation = _admission_artifact(
        session_id,
        f"{GENERATION_PATH_PREFIX}/{admission_id}.json",
        admission,
    )
    playback = _admission_artifact(
        session_id,
        f"{PLAYBACK_PATH_PREFIX}/{admission_id}.json",
        admission,
    )
    stimulus = GeneratedExcitationWav(
        generation_artifact_fingerprint=generation.fingerprint,
        excitation_plan_fingerprint=admission.limits.excitation_plan_fingerprint,
        artifact=_artifact(
            session_id,
            f"excitation/{admission_id}.wav",
            content_token=f"stimulus:{target.region_id}:{evidence_kind}:{index}",
        ),
    )
    prefix = f"captures/{target.region_id}/{evidence_kind}/{index}"
    capture = CaptureIdentity(
        consumer_id=ACTIVE_REGION_EVIDENCE_CONSUMER_ID,
        measurement_kind={
            "normal": ACTIVE_REGION_NORMAL_MEASUREMENT_KIND,
            "reverse": ACTIVE_REGION_REVERSE_MEASUREMENT_KIND,
            "delay_null": ACTIVE_REGION_DELAY_NULL_MEASUREMENT_KIND,
        }[evidence_kind],
        capture_id=f"capture-{target.region_id}-{evidence_kind}-{index}",
        raw_artifact=_artifact(
            session_id,
            f"{prefix}/raw.wav",
            content_token=raw_token
            or f"raw:{target.region_id}:{evidence_kind}:{index}",
        ),
        analysis_input_artifact=_artifact(
            session_id,
            f"{prefix}/analysis.json",
            content_token=f"analysis:{target.region_id}:{evidence_kind}:{index}",
        ),
        target_fingerprint=target_fp,
        context_fingerprint=context_fp,
        geometry_id="reference_axis",
        placement_fingerprint=placement_fingerprint,
        quality_artifact=_artifact(
            session_id,
            f"{prefix}/quality.json",
            content_token=f"quality:{target.region_id}:{evidence_kind}:{index}",
        ),
        admission_artifact=playback,
    )
    return AdmittedRegionCapture(
        authority=plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        speaker_group_id=target.speaker_group_id,
        region_id=target.region_id,
        evidence_kind=evidence_kind,
        target_fingerprint=target_fp,
        context_base_fingerprint=context_base_fp,
        context_fingerprint=context_fp,
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=graph_fingerprint,
        generation_protection_evidence_fingerprint=proof_fingerprint,
        playback_protection_evidence_fingerprint=proof_fingerprint,
        admission_id=admission_id,
        capture=capture,
        stimulus=stimulus,
        generation_artifact=generation,
        playback_artifact=playback,
        generation_admission=admission,
        playback_admission=admission,
    )


def _stationary(
    harness: _Harness,
    *,
    target_index: int,
    evidence_kind: EvidenceKind,
    placement_fingerprint: str,
    graph_fingerprint: str,
) -> StationaryRegionEvidence:
    plan = harness.plan
    target = plan.targets[target_index]
    target_fp = target.target_fingerprint_for(evidence_kind)
    attempt = _reserve(
        harness,
        evidence_kind=evidence_kind,
        target_fingerprint=target_fp,
    )
    captures = tuple(
        _capture(
            harness,
            target_index=target_index,
            evidence_kind=evidence_kind,
            attempt=attempt,
            index=index,
            placement_fingerprint=placement_fingerprint,
            graph_fingerprint=graph_fingerprint,
        )
        for index in range(3)
    )
    return StationaryRegionEvidence(
        authority=plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        speaker_group_id=target.speaker_group_id,
        region_id=target.region_id,
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        target_fingerprint=target_fp,
        context_base_fingerprint=target.context_base_fingerprint_for(evidence_kind),
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=graph_fingerprint,
        captures=captures,
    )


def _delay_walk(
    harness: _Harness,
    *,
    target_index: int,
    placement_fingerprint: str,
    geometry_seed_us: float = 37.5,
) -> DelayWalkEvidence:
    plan = harness.plan
    target = plan.targets[target_index]
    spec = NullWalkSpec(
        crossover_fc_hz=target.electrical_fc_hz,
        geometry_seed_us=geometry_seed_us,
        positive_delay_target=target.upper_role,
        negative_delay_target=target.lower_role,
        step_us=100.0,
    )
    schedule = BoundedNullWalkSchedule(
        spec,
        refinement_anchor_us=spec.geometry_seed_us,
    )
    points: list[DelayPointEvidence] = []
    for point_index, relative_delay_us in enumerate(schedule.scheduled_delays_us):
        graph = _hash(f"{target.region_id}:delay-graph:{relative_delay_us}")
        target_fp = delay_point_target_fingerprint(target, spec, relative_delay_us)
        context_base_fp = delay_point_context_base_fingerprint(
            target,
            spec,
            relative_delay_us,
            graph,
        )
        attempt = _reserve(
            harness,
            evidence_kind="delay_null",
            target_fingerprint=target_fp,
        )
        points.append(
            DelayPointEvidence(
                authority=plan.authority,
                plan_fingerprint=plan.fingerprint,
                attempt=attempt,
                speaker_group_id=target.speaker_group_id,
                region_id=target.region_id,
                relative_delay_us=relative_delay_us,
                target_fingerprint=target_fp,
                context_base_fingerprint=context_base_fp,
                placement_fingerprint=placement_fingerprint,
                graph_fingerprint=graph,
                captures=tuple(
                    _capture(
                        harness,
                        target_index=target_index,
                        evidence_kind="delay_null",
                        attempt=attempt,
                        index=point_index * 10 + repeat,
                        placement_fingerprint=placement_fingerprint,
                        graph_fingerprint=graph,
                        target_fingerprint=target_fp,
                        context_base_fingerprint=context_base_fp,
                    )
                    for repeat in range(5)
                ),
            )
        )
    session_id = plan.authority.commissioning_session_id
    attestation = RegionGeometryAttestation(
        speaker_group_id=target.speaker_group_id,
        region_id=target.region_id,
        region_target_fingerprint=target.fingerprint,
        signed_geometry_seed_us=geometry_seed_us,
        provenance_kind="operator_attested",
        provenance_id=f"geometry-{target.region_id}",
        attestation_artifact=_artifact(
            session_id,
            f"evidence/{target.region_id}/geometry-attestation.json",
            content_token=f"geometry:{target.region_id}:{geometry_seed_us}",
        ),
    )
    return DelayWalkEvidence(
        authority=plan.authority,
        plan_fingerprint=plan.fingerprint,
        speaker_group_id=target.speaker_group_id,
        region_id=target.region_id,
        algorithm_id=DELAY_WALK_ALGORITHM_ID,
        algorithm_version=DELAY_WALK_ALGORITHM_VERSION,
        geometry_attestation=attestation,
        spec=spec,
        schedule=schedule,
        placement_fingerprint=placement_fingerprint,
        points=tuple(points),
        repeatability_artifact=_artifact(
            session_id,
            f"evidence/{target.region_id}/delay-repeatability.json",
            content_token=f"repeatability:{target.region_id}",
        ),
    )


def _region(
    harness: _Harness,
    *,
    target_index: int,
    placement_fingerprint: str,
) -> RegionCommissioningEvidence:
    normal = _stationary(
        harness,
        target_index=target_index,
        evidence_kind="normal",
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=_hash(f"normal-graph:{target_index}"),
    )
    reverse = _stationary(
        harness,
        target_index=target_index,
        evidence_kind="reverse",
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=_hash(f"reverse-graph:{target_index}"),
    )
    return RegionCommissioningEvidence(
        plan=harness.plan,
        target=harness.plan.targets[target_index],
        normal=normal,
        reverse=reverse,
        delay_walk=_delay_walk(
            harness,
            target_index=target_index,
            placement_fingerprint=placement_fingerprint,
        ),
    )


def _isolated_capture(
    harness: _Harness,
    *,
    speaker_group_id: str,
    role: str,
    driver_target_id: str,
    driver_target_fingerprint: str,
    evidence_target_fingerprint: str,
    attempt: CommissioningAttemptHandle,
    index: int,
    placement_fingerprint: str,
    graph_fingerprint: str,
) -> AdmittedIsolatedDriverCapture:
    plan = harness.plan
    admission_id = f"isolated-{speaker_group_id}-{role}-{index}"
    generation_admission = _allowed_admission(
        plan,
        driver_target_fingerprint,
        proof_token=f"{admission_id}:generation",
    )
    playback_admission = _allowed_admission(
        plan,
        driver_target_fingerprint,
        proof_token=f"{admission_id}:playback",
    )
    assert generation_admission.protection_evidence is not None
    assert playback_admission.protection_evidence is not None
    generation_proof = (
        generation_admission.protection_evidence.evidence_fingerprint
    )
    playback_proof = playback_admission.protection_evidence.evidence_fingerprint
    context_base = isolated_capture_context_base_fingerprint(
        plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        evidence_target_fingerprint=evidence_target_fingerprint,
        driver_target_id=driver_target_id,
        driver_target_fingerprint=driver_target_fingerprint,
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=graph_fingerprint,
    )
    context = isolated_capture_context_fingerprint(
        plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        evidence_target_fingerprint=evidence_target_fingerprint,
        driver_target_id=driver_target_id,
        driver_target_fingerprint=driver_target_fingerprint,
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=graph_fingerprint,
        generation_protection_evidence_fingerprint=generation_proof,
        playback_protection_evidence_fingerprint=playback_proof,
    )
    session_id = plan.authority.commissioning_session_id
    generation = _admission_artifact(
        session_id,
        f"{GENERATION_PATH_PREFIX}/{admission_id}.json",
        generation_admission,
    )
    playback = _admission_artifact(
        session_id,
        f"{PLAYBACK_PATH_PREFIX}/{admission_id}.json",
        playback_admission,
    )
    stimulus = GeneratedExcitationWav(
        generation_artifact_fingerprint=generation.fingerprint,
        excitation_plan_fingerprint=(
            generation_admission.limits.excitation_plan_fingerprint
        ),
        artifact=_artifact(
            session_id,
            f"stimulus-fixture/{admission_id}.wav",
            content_token=f"stimulus:{admission_id}",
        ),
    )
    prefix = f"isolated/{speaker_group_id}/{role}/{index}"
    capture = CaptureIdentity(
        consumer_id=ACTIVE_ISOLATED_DRIVER_EVIDENCE_CONSUMER_ID,
        measurement_kind=ACTIVE_ISOLATED_DRIVER_MEASUREMENT_KIND,
        capture_id=f"isolated-{speaker_group_id}-{role}-{index}",
        raw_artifact=_artifact(
            session_id,
            f"{prefix}/raw.wav",
            content_token=f"raw:{speaker_group_id}:{role}:{index}",
        ),
        analysis_input_artifact=_artifact(
            session_id,
            f"{prefix}/analysis.json",
            content_token=f"analysis:{speaker_group_id}:{role}:{index}",
        ),
        target_fingerprint=driver_target_fingerprint,
        context_fingerprint=context,
        geometry_id="reference_axis",
        placement_fingerprint=placement_fingerprint,
        quality_artifact=_artifact(
            session_id,
            f"{prefix}/quality.json",
            content_token=f"quality:{speaker_group_id}:{role}:{index}",
        ),
        admission_artifact=playback,
    )
    return AdmittedIsolatedDriverCapture(
        authority=plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        speaker_group_id=speaker_group_id,
        role=role,
        evidence_target_fingerprint=evidence_target_fingerprint,
        driver_target_id=driver_target_id,
        driver_target_fingerprint=driver_target_fingerprint,
        context_base_fingerprint=context_base,
        context_fingerprint=context,
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=graph_fingerprint,
        generation_protection_evidence_fingerprint=generation_proof,
        playback_protection_evidence_fingerprint=playback_proof,
        admission_id=admission_id,
        capture=capture,
        stimulus=stimulus,
        generation_artifact=generation,
        playback_artifact=playback,
        generation_admission=generation_admission,
        playback_admission=playback_admission,
    )


def _isolated_driver(
    harness: _Harness,
    *,
    speaker_group_id: str,
    role: str,
    driver_target_id: str | None = None,
    driver_target_fingerprint: str | None = None,
    placement_fingerprint: str | None = None,
) -> IsolatedDriverEvidence:
    plan = harness.plan
    planned_target = next(
        target
        for target in plan.driver_targets
        if target.canonical_key == (speaker_group_id, role)
    )
    target_id = driver_target_id or planned_target.driver_target_id
    target_fingerprint = (
        driver_target_fingerprint or planned_target.driver_target_fingerprint
    )
    evidence_target = isolated_driver_evidence_target_fingerprint(
        plan.authority,
        plan_fingerprint=plan.fingerprint,
        speaker_group_id=speaker_group_id,
        role=role,
        driver_target_id=target_id,
        driver_target_fingerprint=target_fingerprint,
    )
    attempt = harness.store.reserve_attempt(
        plan.authority.run,
        target_id=isolated_driver_attempt_target_id(evidence_target),
        target_fingerprint=evidence_target,
    )
    placement = placement_fingerprint or _hash("isolated-placement")
    graph = _hash(f"isolated-graph:{speaker_group_id}:{role}")
    captures = tuple(
        _isolated_capture(
            harness,
            speaker_group_id=speaker_group_id,
            role=role,
            driver_target_id=target_id,
            driver_target_fingerprint=target_fingerprint,
            evidence_target_fingerprint=evidence_target,
            attempt=attempt,
            index=index,
            placement_fingerprint=placement,
            graph_fingerprint=graph,
        )
        for index in range(3)
    )
    return IsolatedDriverEvidence(
        authority=plan.authority,
        plan_fingerprint=plan.fingerprint,
        speaker_group_id=speaker_group_id,
        role=role,
        evidence_target_fingerprint=evidence_target,
        driver_target_id=target_id,
        driver_target_fingerprint=target_fingerprint,
        attempt=attempt,
        placement_fingerprint=placement,
        context_base_fingerprint=captures[0].context_base_fingerprint,
        graph_fingerprint=graph,
        captures=captures,
        repeatability_artifact=_artifact(
            plan.authority.commissioning_session_id,
            f"isolated/{speaker_group_id}/{role}/repeatability.json",
            content_token=f"repeatability:{speaker_group_id}:{role}",
        ),
    )


def _complete_isolated(harness: _Harness) -> CompleteIsolatedDriverEvidence:
    return CompleteIsolatedDriverEvidence(
        plan=harness.plan,
        drivers=tuple(
            _isolated_driver(
                harness,
                speaker_group_id=speaker_group_id,
                role=role,
            )
            for speaker_group_id, role in (
                target.canonical_key for target in harness.plan.driver_targets
            )
        ),
    )
def test_complete_isolated_driver_evidence_is_strict_run_bound_and_round_trips(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, name="isolated")
    complete = _complete_isolated(harness)

    assert [driver.canonical_key for driver in complete.drivers] == [
        ("mono", "woofer"),
        ("mono", "mid"),
        ("mono", "tweeter"),
    ]
    assert all(len(driver.captures) == 3 for driver in complete.drivers)
    for driver in complete.drivers:
        assert {
            capture.context_base_fingerprint for capture in driver.captures
        } == {driver.context_base_fingerprint}
        assert len(
            {capture.context_fingerprint for capture in driver.captures}
        ) == 3
        assert len(
            {
                proof
                for capture in driver.captures
                for proof in (
                    capture.generation_protection_evidence_fingerprint,
                    capture.playback_protection_evidence_fingerprint,
                )
            }
        ) == 6
    assert all(
        driver.attempt.run == complete.plan.authority.run
        and driver.attempt.target_fingerprint
        == driver.evidence_target_fingerprint
        and all(
            capture.capture.target_fingerprint
            == driver.driver_target_fingerprint
            for capture in driver.captures
        )
        for driver in complete.drivers
    )
    assert CompleteIsolatedDriverEvidence.from_mapping(complete.to_dict()) == complete

    historical = complete.to_dict()
    historical["mutable"] = True
    with pytest.raises(CommissioningEvidenceError, match="unknown or missing"):
        CompleteIsolatedDriverEvidence.from_mapping(historical)


def test_complete_isolated_driver_evidence_requires_exact_plan_roles_and_attempts(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, name="isolated-shape")
    complete = _complete_isolated(harness)

    with pytest.raises(CommissioningEvidenceError, match="exactly cover"):
        replace(complete, drivers=complete.drivers[:-1])
    with pytest.raises(CommissioningEvidenceError, match="exactly cover"):
        replace(complete, drivers=tuple(reversed(complete.drivers)))

    driver = complete.drivers[0]
    wrong_attempt = harness.store.reserve_attempt(
        harness.plan.authority.run,
        target_id="active:isolated_driver:wrong",
        target_fingerprint=driver.evidence_target_fingerprint,
    )
    with pytest.raises(CommissioningEvidenceError, match="reserved semantic target"):
        replace(driver, attempt=wrong_attempt)
    with pytest.raises(CommissioningEvidenceError, match="reserved semantic target"):
        replace(driver.captures[0], attempt=wrong_attempt)


def test_complete_isolated_driver_evidence_requires_planned_driver_identities(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, name="isolated-driver-binding")
    complete = _complete_isolated(harness)
    first = complete.drivers[0]
    unplanned = _isolated_driver(
        harness,
        speaker_group_id=first.speaker_group_id,
        role=first.role,
        driver_target_id="driver:unplanned",
        driver_target_fingerprint=_hash("driver:unplanned"),
    )

    with pytest.raises(CommissioningEvidenceError, match="exact plan"):
        replace(complete, drivers=(unplanned, *complete.drivers[1:]))


def test_complete_isolated_driver_evidence_requires_one_group_placement(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, name="isolated-placement")
    complete = _complete_isolated(harness)
    second = complete.drivers[1]
    moved = _isolated_driver(
        harness,
        speaker_group_id=second.speaker_group_id,
        role=second.role,
        placement_fingerprint=_hash("moved-placement"),
    )

    with pytest.raises(CommissioningEvidenceError, match="share one placement"):
        replace(
            complete,
            drivers=(complete.drivers[0], moved, *complete.drivers[2:]),
        )


def test_complete_isolated_driver_evidence_rejects_context_and_global_replay(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, name="isolated-replay")
    complete = _complete_isolated(harness)
    first_driver, second_driver = complete.drivers[:2]
    first_capture = first_driver.captures[0]
    second_capture = second_driver.captures[0]

    with pytest.raises(CommissioningEvidenceError, match="exact graph and attempt"):
        replace(first_capture, graph_fingerprint=_hash("changed-graph"))

    graph_replayed_captures = []
    for capture in second_driver.captures:
        context_base = isolated_capture_context_base_fingerprint(
            capture.authority,
            plan_fingerprint=capture.plan_fingerprint,
            attempt=capture.attempt,
            evidence_target_fingerprint=capture.evidence_target_fingerprint,
            driver_target_id=capture.driver_target_id,
            driver_target_fingerprint=capture.driver_target_fingerprint,
            placement_fingerprint=capture.placement_fingerprint,
            graph_fingerprint=first_driver.graph_fingerprint,
        )
        context = isolated_capture_context_fingerprint(
            capture.authority,
            plan_fingerprint=capture.plan_fingerprint,
            attempt=capture.attempt,
            evidence_target_fingerprint=capture.evidence_target_fingerprint,
            driver_target_id=capture.driver_target_id,
            driver_target_fingerprint=capture.driver_target_fingerprint,
            placement_fingerprint=capture.placement_fingerprint,
            graph_fingerprint=first_driver.graph_fingerprint,
            generation_protection_evidence_fingerprint=(
                capture.generation_protection_evidence_fingerprint
            ),
            playback_protection_evidence_fingerprint=(
                capture.playback_protection_evidence_fingerprint
            ),
        )
        graph_replayed_captures.append(
            replace(
                capture,
                graph_fingerprint=first_driver.graph_fingerprint,
                context_base_fingerprint=context_base,
                context_fingerprint=context,
                capture=replace(
                    capture.capture,
                    context_fingerprint=context,
                ),
            )
        )
    graph_replayed_driver = replace(
        second_driver,
        graph_fingerprint=first_driver.graph_fingerprint,
        context_base_fingerprint=(
            graph_replayed_captures[0].context_base_fingerprint
        ),
        captures=tuple(graph_replayed_captures),
    )
    with pytest.raises(CommissioningEvidenceError, match="graph fingerprints"):
        replace(
            complete,
            drivers=(
                first_driver,
                graph_replayed_driver,
                *complete.drivers[2:],
            ),
        )

    replayed_raw = replace(
        second_capture.capture.raw_artifact,
        sha256=first_capture.capture.raw_artifact.sha256,
        byte_size=first_capture.capture.raw_artifact.byte_size,
    )
    replayed_capture = replace(
        second_capture,
        capture=replace(second_capture.capture, raw_artifact=replayed_raw),
    )
    replayed_driver = replace(
        second_driver,
        captures=(replayed_capture, *second_driver.captures[1:]),
    )
    with pytest.raises(CommissioningEvidenceError, match="globally unique raw bytes"):
        replace(
            complete,
            drivers=(first_driver, replayed_driver, *complete.drivers[2:]),
        )

    replayed_repeatability = replace(
        second_driver.repeatability_artifact,
        relative_path=first_driver.repeatability_artifact.relative_path,
    )
    with pytest.raises(CommissioningEvidenceError, match="artifact paths"):
        replace(
            complete,
            drivers=(
                first_driver,
                replace(
                    second_driver,
                    repeatability_artifact=replayed_repeatability,
                ),
                *complete.drivers[2:],
            ),
        )


def test_complete_isolated_driver_evidence_rejects_cross_run_authority(
    tmp_path: Path,
) -> None:
    original = _complete_isolated(_harness(tmp_path, name="isolated-run-a"))
    other = _complete_isolated(_harness(tmp_path, name="isolated-run-b"))

    with pytest.raises(CommissioningEvidenceError, match="exact plan authority"):
        replace(original, drivers=(other.drivers[0], *original.drivers[1:]))


def test_plan_consumes_exact_durable_run_handle_and_round_trips(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    plan = harness.plan

    assert len(plan.authority.run.run_id) == 32
    assert plan.authority.run.owner_generation == 1
    assert plan.authority.commissioning_session_id == plan.authority.run.session_id
    assert [target.region_id for target in plan.targets] == [
        "woofer_mid",
        "mid_tweeter",
    ]
    assert [
        (
            target.driver_target_id,
            target.driver_target_fingerprint,
        )
        for target in plan.driver_targets
    ] == [
        (
            target["target_id"],
            target["target_fingerprint"],
        )
        for target in active_driver_targets(
            mono_output_topology(mode="active_3_way")
        )
    ]
    assert RegionEvidencePlan.from_mapping(plan.to_dict()) == plan

    tampered = copy.deepcopy(plan.to_dict())
    tampered["authority"]["run"]["run_id"] = "synthetic-run"
    with pytest.raises(CommissioningEvidenceError, match="UUID hex"):
        RegionEvidencePlan.from_mapping(tampered)

    with pytest.raises(CommissioningEvidenceError, match="durable run session"):
        derive_region_evidence_plan(
            _preset(),
            mono_output_topology(mode="active_3_way"),
            run=plan.authority.run,
            protected_safety_profile_fingerprint=_hash("profile"),
            comparison_set_fingerprint=_hash("different-comparison"),
            threshold_profile_fingerprint=_hash("thresholds"),
            context_fingerprint=_hash("context"),
        )


def test_plan_requires_exact_preset_layout_and_active_group_set(tmp_path: Path) -> None:
    run = _harness(tmp_path, name="layout-authority").plan.authority.run

    def derive(
        preset: ActiveSpeakerPreset,
        topology: OutputTopology,
    ) -> RegionEvidencePlan:
        return derive_region_evidence_plan(
            preset,
            topology,
            run=run,
            protected_safety_profile_fingerprint=_hash("profile"),
            comparison_set_fingerprint=run.session_fingerprint,
            threshold_profile_fingerprint=_hash("thresholds"),
            context_fingerprint=_hash("context"),
        )

    with pytest.raises(CommissioningEvidenceError, match="stereo capture preset"):
        derive(
            _preset(layout="stereo"),
            mono_output_topology(mode="active_3_way"),
        )
    with pytest.raises(CommissioningEvidenceError, match="mono capture preset"):
        derive(_preset(layout="mono"), _stereo_three_way_topology())
    with pytest.raises(CommissioningEvidenceError, match="left and right"):
        derive(
            _preset(layout="stereo"),
            _stereo_three_way_topology(include_right=False),
        )
    with pytest.raises(CommissioningEvidenceError, match="active group modes"):
        derive(
            _preset(layout="stereo"),
            _stereo_three_way_topology(right_mode="active_2_way"),
        )

    stereo = derive(_preset(layout="stereo"), _stereo_three_way_topology())
    assert [
        (target.speaker_group_id, target.region_id) for target in stereo.targets
    ] == [
        ("left", "woofer_mid"),
        ("left", "mid_tweeter"),
        ("right", "woofer_mid"),
        ("right", "mid_tweeter"),
    ]


def test_plan_identity_changes_with_real_run_generation_and_region(
    tmp_path: Path,
) -> None:
    base = _harness(tmp_path, name="base")
    other = _harness(tmp_path, name="other")
    assert other.plan.fingerprint != base.plan.fingerprint

    restarted = CommissioningRunStore(
        path=tmp_path / "base.json",
        owner_id="f" * 32,
    )
    claimed = restarted.claim_owner()
    assert claimed is not None and claimed.owner_generation == 2
    changed = derive_region_evidence_plan(
        _preset(),
        mono_output_topology(mode="active_3_way"),
        run=claimed,
        protected_safety_profile_fingerprint=_hash("profile"),
        comparison_set_fingerprint=claimed.session_fingerprint,
        threshold_profile_fingerprint=_hash("thresholds"),
        context_fingerprint=_hash("context"),
    )
    assert changed.fingerprint != base.plan.fingerprint

    raw = _three_way_preset(layout="mono")
    raw["crossover_regions"][0]["id"] = "lower_region_v2"
    changed_preset = derive_region_evidence_plan(
        ActiveSpeakerPreset.from_mapping(raw),
        mono_output_topology(mode="active_3_way"),
        run=claimed,
        protected_safety_profile_fingerprint=_hash("profile"),
        comparison_set_fingerprint=claimed.session_fingerprint,
        threshold_profile_fingerprint=_hash("thresholds"),
        context_fingerprint=_hash("context"),
    )
    assert changed_preset.targets[0].region_id == "lower_region_v2"


def test_public_kind_lookups_reject_unsupported_values(tmp_path: Path) -> None:
    target = _harness(tmp_path).plan.targets[0]
    with pytest.raises(CommissioningEvidenceError, match="unsupported"):
        target.target_fingerprint_for("typo")  # type: ignore[arg-type]
    with pytest.raises(CommissioningEvidenceError, match="unsupported"):
        target.context_base_fingerprint_for("typo")  # type: ignore[arg-type]
    with pytest.raises(CommissioningEvidenceError, match="unsupported"):
        evidence_attempt_target_id("typo", _hash("target"))  # type: ignore[arg-type]


def test_stationary_evidence_retains_minted_attempt_and_one_shots(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    evidence = _stationary(
        harness,
        target_index=0,
        evidence_kind="normal",
        placement_fingerprint=_hash("placement"),
        graph_fingerprint=_hash("normal-graph"),
    )

    assert evidence.attempt.run == harness.plan.authority.run
    assert evidence.attempt.attempt_number == 1
    assert evidence.attempt.target_id == evidence_attempt_target_id(
        "normal", evidence.target_fingerprint
    )
    assert all(capture.attempt == evidence.attempt for capture in evidence.captures)
    assert all(
        capture.playback_admission.request.repeat_count == 1
        for capture in evidence.captures
    )
    assert StationaryRegionEvidence.from_mapping(evidence.to_dict()) == evidence

    wrong_attempt = harness.store.reserve_attempt(
        harness.plan.authority.run,
        target_id="active:normal:wrong",
        target_fingerprint=evidence.target_fingerprint,
    )
    with pytest.raises(CommissioningEvidenceError, match="reserved attempt target"):
        replace(evidence, attempt=wrong_attempt)
    with pytest.raises(CommissioningEvidenceError, match="reserved attempt target"):
        replace(evidence.captures[0], attempt=wrong_attempt)


@pytest.mark.parametrize(
    ("artifact_field", "message"),
    (
        ("raw_artifact", "raw paths"),
        ("analysis_input_artifact", "analysis-input paths"),
        ("quality_artifact", "quality paths"),
    ),
)
def test_stationary_rejects_all_capture_role_path_replay(
    tmp_path: Path,
    artifact_field: str,
    message: str,
) -> None:
    harness = _harness(tmp_path)
    evidence = _stationary(
        harness,
        target_index=0,
        evidence_kind="normal",
        placement_fingerprint=_hash("placement"),
        graph_fingerprint=_hash("graph"),
    )
    first = evidence.captures[0]
    second = evidence.captures[1]
    replay_path = getattr(first.capture, artifact_field).relative_path
    replacement = _artifact(
        harness.plan.authority.commissioning_session_id,
        replay_path,
        content_token=f"different:{artifact_field}",
    )
    changes: Any = {artifact_field: replacement}
    replayed_capture_identity = replace(second.capture, **changes)
    replayed = replace(second, capture=replayed_capture_identity)
    with pytest.raises(CommissioningEvidenceError, match=message):
        replace(evidence, captures=(first, replayed, evidence.captures[2]))


def test_capture_rejects_synthetic_attempt_projection_and_multirepeat(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    evidence = _stationary(
        harness,
        target_index=0,
        evidence_kind="normal",
        placement_fingerprint=_hash("placement"),
        graph_fingerprint=_hash("graph"),
    )
    raw = evidence.captures[0].to_dict()
    raw["attempt"]["attempt_id"] = "not-a-uuid"
    with pytest.raises(CommissioningEvidenceError, match="UUID hex"):
        AdmittedRegionCapture.from_mapping(raw)

    raw = evidence.captures[0].to_dict()
    raw["playback_admission"]["request"]["repeat_count"] = 3
    raw["playback_admission"]["request"]["fingerprint"] = _hash("tampered")
    with pytest.raises(CommissioningEvidenceError):
        AdmittedRegionCapture.from_mapping(raw)


def test_stationary_rejects_cross_capture_cross_role_path_replay(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    evidence = _stationary(
        harness,
        target_index=0,
        evidence_kind="normal",
        placement_fingerprint=_hash("placement"),
        graph_fingerprint=_hash("graph"),
    )
    first = evidence.captures[0]
    second = evidence.captures[1]
    replayed_analysis = replace(
        second.capture.analysis_input_artifact,
        relative_path=first.capture.raw_artifact.relative_path,
    )
    replayed = replace(
        second,
        capture=replace(
            second.capture,
            analysis_input_artifact=replayed_analysis,
        ),
    )
    with pytest.raises(CommissioningEvidenceError, match="artifact role paths"):
        replace(evidence, captures=(first, replayed, evidence.captures[2]))


def test_delay_walk_requires_explicit_signed_geometry_and_fresh_points(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    walk = _delay_walk(
        harness,
        target_index=1,
        placement_fingerprint=_hash("placement"),
        geometry_seed_us=-37.5,
    )

    assert walk.spec.geometry_seed_us == -37.5
    assert walk.geometry_attestation.signed_geometry_seed_us == -37.5
    assert walk.schedule.refinement_anchor_us == -37.5
    assert walk.spec.candidate_delays_us() == (
        -237.5,
        -137.5,
        -37.5,
        62.5,
        162.5,
    )
    assert [point.attempt.attempt_number for point in walk.points] == [1, 2, 3, 4, 5]
    assert DelayWalkEvidence.from_mapping(walk.to_dict()) == walk

    with pytest.raises(CommissioningEvidenceError, match="requires explicit"):
        replace(walk, geometry_attestation=None)  # type: ignore[arg-type]
    with pytest.raises(CommissioningEvidenceError, match="not bound"):
        replace(
            walk,
            geometry_attestation=replace(
                walk.geometry_attestation,
                signed_geometry_seed_us=0.0,
            ),
        )

    with pytest.raises(CommissioningEvidenceError, match="exact shared spec"):
        replace(
            walk,
            schedule=BoundedNullWalkSchedule(
                replace(walk.spec, geometry_seed_us=0.0),
                refinement_anchor_us=0.0,
            ),
        )

    first_capture = walk.points[0].captures[0]
    second_capture = walk.points[1].captures[0]
    replayed_raw_path = replace(
        second_capture.capture.raw_artifact,
        relative_path=first_capture.capture.raw_artifact.relative_path,
    )
    replayed_capture = replace(
        second_capture,
        capture=replace(second_capture.capture, raw_artifact=replayed_raw_path),
    )
    replayed_point = replace(
        walk.points[1],
        captures=(replayed_capture, *walk.points[1].captures[1:]),
    )
    with pytest.raises(CommissioningEvidenceError, match="globally fresh"):
        replace(walk, points=(walk.points[0], replayed_point, *walk.points[2:]))


def test_zero_geometry_requires_and_retains_explicit_attestation(
    tmp_path: Path,
) -> None:
    walk = _delay_walk(
        _harness(tmp_path),
        target_index=1,
        placement_fingerprint=_hash("placement"),
        geometry_seed_us=0.0,
    )
    assert walk.geometry_attestation.provenance_kind == "operator_attested"
    assert walk.geometry_attestation.signed_geometry_seed_us == 0.0
    assert walk.spec.geometry_seed_us == 0.0


def test_shipped_350_hz_region_uses_bounded_schedule_without_weakening_grid(
    tmp_path: Path,
) -> None:
    walk = _delay_walk(
        _harness(tmp_path, name="shipped-lower-region"),
        target_index=0,
        placement_fingerprint=_hash("placement"),
        geometry_seed_us=0.0,
    )

    assert walk.spec.crossover_fc_hz == 350.0
    assert walk.spec.candidate_count == 29
    with pytest.raises(NullWalkError, match="candidate budget"):
        walk.spec.candidate_delays_us()
    assert len(walk.schedule.coarse_delays_us) == 15
    assert walk.schedule.coarse_delays_us[0] == -1400.0
    assert walk.schedule.coarse_delays_us[-1] == 1400.0
    assert walk.schedule.refinement_delays_us == (-100.0, 100.0)
    assert len(walk.schedule.scheduled_delays_us) == 17
    assert tuple(point.relative_delay_us for point in walk.points) == (
        walk.schedule.scheduled_delays_us
    )
    assert DelayWalkEvidence.from_mapping(walk.to_dict()) == walk


def test_region_rejects_cross_phase_analysis_quality_and_admission_replay(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    region = _region(
        harness,
        target_index=1,
        placement_fingerprint=_hash("placement"),
    )
    assert RegionCommissioningEvidence.from_mapping(region.to_dict()) == region

    first_normal = region.normal.captures[0]
    first_reverse = region.reverse.captures[0]
    for field_name in ("analysis_input_artifact", "quality_artifact"):
        replayed_identity = replace(
            first_reverse.capture,
            **{field_name: getattr(first_normal.capture, field_name)},
        )
        replayed_capture = replace(first_reverse, capture=replayed_identity)
        replayed_reverse = replace(
            region.reverse,
            captures=(replayed_capture, *region.reverse.captures[1:]),
        )
        with pytest.raises(CommissioningEvidenceError, match="replayed as reverse"):
            replace(region, reverse=replayed_reverse)

    replayed_generation = replace(
        first_reverse.generation_artifact,
        relative_path=first_normal.generation_artifact.relative_path,
    )
    replayed_playback = replace(
        first_reverse.playback_artifact,
        relative_path=first_normal.playback_artifact.relative_path,
    )
    replayed_capture = replace(
        first_reverse,
        admission_id=first_normal.admission_id,
        generation_artifact=replayed_generation,
        playback_artifact=replayed_playback,
        stimulus=replace(
            first_reverse.stimulus,
            generation_artifact_fingerprint=replayed_generation.fingerprint,
        ),
        capture=replace(
            first_reverse.capture,
            admission_artifact=replayed_playback,
        ),
    )
    replayed_reverse = replace(
        region.reverse,
        captures=(replayed_capture, *region.reverse.captures[1:]),
    )
    with pytest.raises(CommissioningEvidenceError, match="replayed as reverse"):
        replace(region, reverse=replayed_reverse)


def test_complete_plan_requires_every_region_and_round_trips(tmp_path: Path) -> None:
    harness = _harness(tmp_path, bounded_regions=True)
    regions = tuple(
        _region(
            harness,
            target_index=index,
            placement_fingerprint=_hash("placement"),
        )
        for index in range(len(harness.plan.targets))
    )
    complete = CompleteCommissioningEvidence(plan=harness.plan, regions=regions)

    assert [region.target for region in complete.regions] == list(harness.plan.targets)
    assert CompleteCommissioningEvidence.from_mapping(complete.to_dict()) == complete
    with pytest.raises(CommissioningEvidenceError, match="exactly one"):
        CompleteCommissioningEvidence(plan=harness.plan, regions=regions[:1])
    with pytest.raises(CommissioningEvidenceError, match="exactly one"):
        CompleteCommissioningEvidence(plan=harness.plan, regions=regions[::-1])


def test_complete_three_way_plan_rejects_cross_region_role_replay(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, bounded_regions=True)
    lower = _region(
        harness,
        target_index=0,
        placement_fingerprint=_hash("placement"),
    )
    upper = _region(
        harness,
        target_index=1,
        placement_fingerprint=_hash("placement"),
    )
    lower_capture = lower.normal.captures[0]
    upper_capture = upper.normal.captures[0]

    replayed_identity = replace(
        upper_capture.capture,
        raw_artifact=replace(
            upper_capture.capture.raw_artifact,
            relative_path=lower_capture.capture.analysis_input_artifact.relative_path,
        ),
    )
    replayed_capture = replace(upper_capture, capture=replayed_identity)
    replayed_normal = replace(
        upper.normal,
        captures=(replayed_capture, *upper.normal.captures[1:]),
    )
    replayed_upper = replace(upper, normal=replayed_normal)
    with pytest.raises(CommissioningEvidenceError, match="globally unique"):
        CompleteCommissioningEvidence(
            plan=harness.plan,
            regions=(lower, replayed_upper),
        )

    raw_bytes_replay = replace(
        upper_capture.capture.raw_artifact,
        sha256=lower_capture.capture.raw_artifact.sha256,
        byte_size=lower_capture.capture.raw_artifact.byte_size,
    )
    replayed_identity = replace(
        upper_capture.capture,
        raw_artifact=raw_bytes_replay,
    )
    replayed_capture = replace(upper_capture, capture=replayed_identity)
    replayed_upper = replace(
        upper,
        normal=replace(
            upper.normal,
            captures=(replayed_capture, *upper.normal.captures[1:]),
        ),
    )
    with pytest.raises(CommissioningEvidenceError, match="raw bytes"):
        CompleteCommissioningEvidence(
            plan=harness.plan,
            regions=(lower, replayed_upper),
        )
