# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace

import pytest

from jasper.active_speaker.bundles import BUNDLE_KIND
from jasper.active_speaker.commissioning_evidence import (
    ACTIVE_REGION_DELAY_NULL_MEASUREMENT_KIND,
    ACTIVE_REGION_EVIDENCE_CONSUMER_ID,
    ACTIVE_REGION_NORMAL_MEASUREMENT_KIND,
    ACTIVE_REGION_REVERSE_MEASUREMENT_KIND,
    DELAY_WALK_ALGORITHM_ID,
    DELAY_WALK_ALGORITHM_VERSION,
    AdmittedRegionCapture,
    CommissioningEvidenceError,
    DelayPointEvidence,
    DelayWalkEvidence,
    RegionCommissioningEvidence,
    RegionEvidencePlan,
    StationaryRegionEvidence,
    capture_attempt_context_fingerprint,
    delay_point_context_base_fingerprint,
    delay_point_target_fingerprint,
    derive_region_evidence_plan,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.evidence_identity import ArtifactIdentity, CaptureIdentity
from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
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
from jasper.audio_measurement.null_walk import NullWalkSpec
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_profile import _three_way_preset


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _preset() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_three_way_preset())


def _plan(**overrides: object) -> RegionEvidencePlan:
    kwargs: dict[str, object] = {
        "run_id": "run-1",
        "owner_generation": 3,
        "protected_safety_profile_fingerprint": _hash("profile"),
        "comparison_set_fingerprint": _hash("comparison"),
        "commissioning_session_id": "session-1",
        "threshold_profile_fingerprint": _hash("thresholds"),
        "context_fingerprint": _hash("context"),
    }
    kwargs.update(overrides)
    return derive_region_evidence_plan(
        _preset(),
        mono_output_topology(mode="active_3_way"),
        **kwargs,  # type: ignore[arg-type]
    )


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
    plan: RegionEvidencePlan, target_fingerprint: str
) -> ExcitationAdmission:
    plan_fingerprint = _hash(f"excitation:{target_fingerprint}")
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
        excitation_plan_fingerprint=plan_fingerprint,
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
    protection = ProtectionEvidence(
        target_fingerprint=target_fingerprint,
        safety_profile_fingerprint=limits.safety_profile_fingerprint,
        protection_requirement_fingerprint=requirement,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
        evidence_fingerprint=_hash(f"graph-proof:{target_fingerprint}"),
        current=True,
    )
    admission = admit_excitation(
        request,
        limits,
        protection_evidence=protection,
    )
    assert admission.allowed
    return admission


def _capture(
    plan: RegionEvidencePlan,
    *,
    target_index: int,
    evidence_kind: str,
    attempt_id: str,
    index: int,
    placement_fingerprint: str,
    graph_fingerprint: str,
    target_fingerprint: str | None = None,
    context_base_fingerprint: str | None = None,
    raw_token: str | None = None,
    plan_fingerprint: str | None = None,
) -> AdmittedRegionCapture:
    target = plan.targets[target_index]
    target_fp = target_fingerprint or target.target_fingerprint_for(evidence_kind)  # type: ignore[arg-type]
    context_base_fp = (
        context_base_fingerprint or target.context_base_fingerprint_for(evidence_kind)  # type: ignore[arg-type]
    )
    admission_id = f"{target.region_id}-{evidence_kind}-{index}"
    admission = _allowed_admission(plan, target_fp)
    assert admission.protection_evidence is not None
    proof_fingerprint = admission.protection_evidence.evidence_fingerprint
    assert proof_fingerprint is not None
    context_fp = capture_attempt_context_fingerprint(
        plan.authority,
        attempt_id=attempt_id,
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        target_fingerprint=target_fp,
        context_base_fingerprint=context_base_fp,
        graph_fingerprint=graph_fingerprint,
        generation_protection_evidence_fingerprint=proof_fingerprint,
        playback_protection_evidence_fingerprint=proof_fingerprint,
    )
    generation = _admission_artifact(
        plan.authority.commissioning_session_id,
        f"{GENERATION_PATH_PREFIX}/{admission_id}.json",
        admission,
    )
    playback = _admission_artifact(
        plan.authority.commissioning_session_id,
        f"{PLAYBACK_PATH_PREFIX}/{admission_id}.json",
        admission,
    )
    session_id = plan.authority.commissioning_session_id
    stimulus_artifact = _artifact(
        session_id,
        f"excitation/{admission_id}.wav",
        content_token=f"stimulus:{target.region_id}:{evidence_kind}",
    )
    stimulus = GeneratedExcitationWav(
        generation_artifact_fingerprint=generation.fingerprint,
        excitation_plan_fingerprint=admission.limits.excitation_plan_fingerprint,
        artifact=stimulus_artifact,
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
        plan_fingerprint=plan_fingerprint or plan.fingerprint,
        attempt_id=attempt_id,
        speaker_group_id=target.speaker_group_id,
        region_id=target.region_id,
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
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
    plan: RegionEvidencePlan,
    *,
    target_index: int,
    evidence_kind: str,
    attempt_id: str | None = None,
    placement_fingerprint: str,
    graph_fingerprint: str,
    plan_fingerprint: str | None = None,
) -> StationaryRegionEvidence:
    target = plan.targets[target_index]
    resolved_attempt_id = attempt_id or f"{evidence_kind}-attempt"
    captures = tuple(
        _capture(
            plan,
            target_index=target_index,
            evidence_kind=evidence_kind,
            attempt_id=resolved_attempt_id,
            index=index,
            placement_fingerprint=placement_fingerprint,
            graph_fingerprint=graph_fingerprint,
            plan_fingerprint=plan_fingerprint,
        )
        for index in range(3)
    )
    return StationaryRegionEvidence(
        authority=plan.authority,
        plan_fingerprint=plan_fingerprint or plan.fingerprint,
        attempt_id=resolved_attempt_id,
        speaker_group_id=target.speaker_group_id,
        region_id=target.region_id,
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        target_fingerprint=target.target_fingerprint_for(evidence_kind),  # type: ignore[arg-type]
        context_base_fingerprint=target.context_base_fingerprint_for(evidence_kind),  # type: ignore[arg-type]
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=graph_fingerprint,
        captures=captures,
    )


def _delay_walk(
    plan: RegionEvidencePlan,
    *,
    target_index: int,
    placement_fingerprint: str,
) -> DelayWalkEvidence:
    target = plan.targets[target_index]
    spec = NullWalkSpec(
        crossover_fc_hz=target.electrical_fc_hz,
        geometry_seed_us=0.0,
        positive_delay_target=target.upper_role,
        negative_delay_target=target.lower_role,
        step_us=100.0,
    )
    points: list[DelayPointEvidence] = []
    for point_index, relative_delay_us in enumerate(spec.candidate_delays_us()):
        graph = _hash(f"delay-graph:{relative_delay_us}")
        target_fp = delay_point_target_fingerprint(target, spec, relative_delay_us)
        context_base_fp = delay_point_context_base_fingerprint(
            target,
            spec,
            relative_delay_us,
            graph,
        )
        attempt_id = f"delay-attempt-{point_index}"
        captures = tuple(
            _capture(
                plan,
                target_index=target_index,
                evidence_kind="delay_null",
                attempt_id=attempt_id,
                index=point_index * 10 + repeat,
                placement_fingerprint=placement_fingerprint,
                graph_fingerprint=graph,
                target_fingerprint=target_fp,
                context_base_fingerprint=context_base_fp,
            )
            for repeat in range(5)
        )
        points.append(
            DelayPointEvidence(
                authority=plan.authority,
                plan_fingerprint=plan.fingerprint,
                attempt_id=attempt_id,
                speaker_group_id=target.speaker_group_id,
                region_id=target.region_id,
                relative_delay_us=relative_delay_us,
                target_fingerprint=target_fp,
                context_base_fingerprint=context_base_fp,
                placement_fingerprint=placement_fingerprint,
                graph_fingerprint=graph,
                captures=captures,
            )
        )
    return DelayWalkEvidence(
        authority=plan.authority,
        plan_fingerprint=plan.fingerprint,
        speaker_group_id=target.speaker_group_id,
        region_id=target.region_id,
        algorithm_id=DELAY_WALK_ALGORITHM_ID,
        algorithm_version=DELAY_WALK_ALGORITHM_VERSION,
        spec=spec,
        placement_fingerprint=placement_fingerprint,
        points=tuple(points),
        repeatability_artifact=_artifact(
            plan.authority.commissioning_session_id,
            f"evidence/{target.region_id}/delay-repeatability.json",
            content_token=f"repeatability:{target.region_id}",
        ),
    )


def test_three_way_plan_preserves_both_regions_and_round_trips() -> None:
    plan = _plan()

    assert [target.region_id for target in plan.targets] == [
        "woofer_mid",
        "mid_tweeter",
    ]
    assert [(target.lower_role, target.upper_role) for target in plan.targets] == [
        ("woofer", "mid"),
        ("mid", "tweeter"),
    ]
    assert len({target.region_fingerprint for target in plan.targets}) == 2
    assert all(
        len(
            {
                target.normal_target_fingerprint,
                target.reverse_target_fingerprint,
                target.delay_target_base_fingerprint,
            }
        )
        == 3
        for target in plan.targets
    )
    assert RegionEvidencePlan.from_mapping(plan.to_dict()) == plan


def test_plan_is_exact_to_run_comparison_topology_and_regions() -> None:
    base = _plan()

    assert _plan(run_id="run-2").fingerprint != base.fingerprint
    assert _plan(owner_generation=4).fingerprint != base.fingerprint
    assert (
        _plan(comparison_set_fingerprint=_hash("replacement")).fingerprint
        != base.fingerprint
    )
    raw = _three_way_preset()
    raw["crossover_regions"][0]["id"] = "lower_region_v2"
    changed_preset = ActiveSpeakerPreset.from_mapping(raw)
    changed = derive_region_evidence_plan(
        changed_preset,
        mono_output_topology(mode="active_3_way"),
        run_id="run-1",
        owner_generation=3,
        protected_safety_profile_fingerprint=_hash("profile"),
        comparison_set_fingerprint=_hash("comparison"),
        commissioning_session_id="session-1",
        threshold_profile_fingerprint=_hash("thresholds"),
        context_fingerprint=_hash("context"),
    )
    assert changed.fingerprint != base.fingerprint
    assert changed.targets[0].region_id == "lower_region_v2"


def test_plan_rejects_unknown_fields_tampering_and_nonpositive_owner() -> None:
    plan = _plan()
    unknown = plan.to_dict()
    unknown["extra"] = True
    with pytest.raises(CommissioningEvidenceError, match="unknown or missing"):
        RegionEvidencePlan.from_mapping(unknown)

    tampered = plan.to_dict()
    tampered["preset_id"] = "different"
    with pytest.raises(CommissioningEvidenceError, match="fingerprint"):
        RegionEvidencePlan.from_mapping(tampered)

    with pytest.raises(CommissioningEvidenceError, match="positive integer"):
        _plan(owner_generation=0)

    valid_preset = _preset()
    invalid_region = replace(
        valid_preset.crossover_regions[0],
        lower_driver="tweeter",
        upper_driver="woofer",
    )
    invalid_preset = replace(
        valid_preset,
        crossover_regions=(invalid_region, *valid_preset.crossover_regions[1:]),
    )
    with pytest.raises(CommissioningEvidenceError, match="capture preset is invalid"):
        derive_region_evidence_plan(
            invalid_preset,
            mono_output_topology(mode="active_3_way"),
            run_id="run-1",
            owner_generation=3,
            protected_safety_profile_fingerprint=_hash("profile"),
            comparison_set_fingerprint=_hash("comparison"),
            commissioning_session_id="session-1",
            threshold_profile_fingerprint=_hash("thresholds"),
            context_fingerprint=_hash("context"),
        )


def test_stationary_evidence_is_three_fresh_one_shots_with_canonical_artifacts() -> (
    None
):
    plan = _plan()
    evidence = _stationary(
        plan,
        target_index=0,
        evidence_kind="normal",
        placement_fingerprint=_hash("placement"),
        graph_fingerprint=_hash("normal-graph"),
    )

    assert len(evidence.captures) == 3
    assert {
        capture.playback_admission.fingerprint for capture in evidence.captures
    } == {evidence.captures[0].playback_admission.fingerprint}
    assert all(
        capture.playback_admission.request.repeat_count == 1
        for capture in evidence.captures
    )
    assert StationaryRegionEvidence.from_mapping(evidence.to_dict()) == evidence


def test_multi_repeat_or_historical_artifact_cannot_become_region_authority() -> None:
    plan = _plan()
    capture = _capture(
        plan,
        target_index=0,
        evidence_kind="normal",
        attempt_id="normal-attempt",
        index=0,
        placement_fingerprint=_hash("placement"),
        graph_fingerprint=_hash("normal-graph"),
    )
    raw = capture.to_dict()
    request = raw["playback_admission"]["request"]
    request["repeat_count"] = 3
    request["fingerprint"] = _hash("not-canonical")
    with pytest.raises(CommissioningEvidenceError):
        AdmittedRegionCapture.from_mapping(raw)

    historical = copy.deepcopy(capture.to_dict())
    for field in (
        "capture",
        "stimulus",
        "generation_artifact",
        "playback_artifact",
    ):
        artifacts = [historical[field]]
        if field == "capture":
            artifacts = [
                historical[field][name]
                for name in (
                    "raw_artifact",
                    "analysis_input_artifact",
                    "quality_artifact",
                    "admission_artifact",
                )
            ]
        elif field == "stimulus":
            artifacts = [historical[field]["artifact"]]
        for artifact in artifacts:
            artifact["bundle_kind"] = "jts_historical_capture_bundle"
            artifact["fingerprint"] = _hash("tampered-artifact")
    with pytest.raises(CommissioningEvidenceError):
        AdmittedRegionCapture.from_mapping(historical)


def test_stationary_rejects_duplicate_raw_bytes_and_reverse_replay() -> None:
    plan = _plan()
    target = plan.targets[0]
    placement = _hash("placement")
    graph = _hash("normal-graph")
    captures = tuple(
        _capture(
            plan,
            target_index=0,
            evidence_kind="normal",
            attempt_id="normal-attempt",
            index=index,
            placement_fingerprint=placement,
            graph_fingerprint=graph,
            raw_token="same-raw-bytes",
        )
        for index in range(3)
    )
    with pytest.raises(CommissioningEvidenceError, match="unique raw bytes"):
        StationaryRegionEvidence(
            authority=plan.authority,
            plan_fingerprint=plan.fingerprint,
            attempt_id="normal-attempt",
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            evidence_kind="normal",
            target_fingerprint=target.normal_target_fingerprint,
            context_base_fingerprint=target.normal_context_base_fingerprint,
            placement_fingerprint=placement,
            graph_fingerprint=graph,
            captures=captures,
        )

    normal = _stationary(
        plan,
        target_index=0,
        evidence_kind="normal",
        placement_fingerprint=placement,
        graph_fingerprint=graph,
    )
    with pytest.raises(CommissioningEvidenceError, match="do not share"):
        StationaryRegionEvidence(
            authority=plan.authority,
            plan_fingerprint=plan.fingerprint,
            attempt_id="wrong-reverse-attempt",
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            evidence_kind="reverse",
            target_fingerprint=target.reverse_target_fingerprint,
            context_base_fingerprint=target.reverse_context_base_fingerprint,
            placement_fingerprint=placement,
            graph_fingerprint=graph,
            captures=normal.captures,
        )


def test_delay_walk_uses_exact_shared_half_period_grid_and_five_fresh_captures() -> (
    None
):
    plan = _plan()
    placement = _hash("placement")
    # mid/tweeter at 2500 Hz => half-period 200 us and five 100-us grid points.
    walk = _delay_walk(plan, target_index=1, placement_fingerprint=placement)

    assert walk.spec.half_period_us == 200.0
    assert walk.spec.candidate_delays_us() == (-200.0, -100.0, 0.0, 100.0, 200.0)
    assert [point.relative_delay_us for point in walk.points] == list(
        walk.spec.candidate_delays_us()
    )
    assert all(len(point.captures) == 5 for point in walk.points)
    assert DelayWalkEvidence.from_mapping(walk.to_dict()) == walk

    tampered = walk.to_dict()
    tampered["spec"]["candidate_delays_us"][0] = -250.0
    with pytest.raises(CommissioningEvidenceError, match="exact canonical"):
        DelayWalkEvidence.from_mapping(tampered)

    first_raw = walk.points[0].captures[0].capture.raw_artifact
    path_collision = _artifact(
        plan.authority.commissioning_session_id,
        first_raw.relative_path,
        content_token="different-repeatability-content",
    )
    with pytest.raises(CommissioningEvidenceError, match="distinct artifact role"):
        replace(walk, repeatability_artifact=path_collision)


def test_complete_region_evidence_requires_distinct_normal_reverse_and_delay() -> None:
    plan = _plan()
    placement = _hash("placement")
    normal = _stationary(
        plan,
        target_index=1,
        evidence_kind="normal",
        placement_fingerprint=placement,
        graph_fingerprint=_hash("normal-graph"),
    )
    reverse = _stationary(
        plan,
        target_index=1,
        evidence_kind="reverse",
        placement_fingerprint=placement,
        graph_fingerprint=_hash("reverse-graph"),
    )
    evidence = RegionCommissioningEvidence(
        plan=plan,
        target=plan.targets[1],
        normal=normal,
        reverse=reverse,
        delay_walk=_delay_walk(plan, target_index=1, placement_fingerprint=placement),
    )

    assert RegionCommissioningEvidence.from_mapping(evidence.to_dict()) == evidence

    swapped_spec = NullWalkSpec(
        crossover_fc_hz=evidence.target.electrical_fc_hz,
        geometry_seed_us=0.0,
        positive_delay_target=evidence.target.lower_role,
        negative_delay_target=evidence.target.upper_role,
        step_us=100.0,
    )
    with pytest.raises(CommissioningEvidenceError, match="electrical crossover"):
        RegionCommissioningEvidence(
            plan=plan,
            target=plan.targets[1],
            normal=normal,
            reverse=reverse,
            delay_walk=replace(evidence.delay_walk, spec=swapped_spec),
        )

    reverse_first = reverse.captures[0]
    reused_stimulus = GeneratedExcitationWav(
        generation_artifact_fingerprint=reverse_first.generation_artifact.fingerprint,
        excitation_plan_fingerprint=(
            reverse_first.generation_admission.limits.excitation_plan_fingerprint
        ),
        artifact=normal.captures[0].stimulus.artifact,
    )
    replayed_reverse_capture = replace(reverse_first, stimulus=reused_stimulus)
    replayed_reverse = replace(
        reverse,
        captures=(replayed_reverse_capture, *reverse.captures[1:]),
    )
    with pytest.raises(CommissioningEvidenceError, match="replayed as reverse"):
        RegionCommissioningEvidence(
            plan=plan,
            target=plan.targets[1],
            normal=normal,
            reverse=replayed_reverse,
            delay_walk=evidence.delay_walk,
        )

    with pytest.raises(CommissioningEvidenceError, match="phase targets"):
        RegionCommissioningEvidence(
            plan=plan,
            target=plan.targets[1],
            normal=normal,
            reverse=StationaryRegionEvidence(
                authority=plan.authority,
                plan_fingerprint=plan.fingerprint,
                attempt_id="reverse-wrong-attempt",
                speaker_group_id=plan.targets[1].speaker_group_id,
                region_id=plan.targets[1].region_id,
                evidence_kind="reverse",
                target_fingerprint=normal.target_fingerprint,
                context_base_fingerprint=normal.context_base_fingerprint,
                placement_fingerprint=placement,
                graph_fingerprint=normal.graph_fingerprint,
                captures=tuple(
                    _capture(
                        plan,
                        target_index=1,
                        evidence_kind="reverse",
                        attempt_id="reverse-wrong-attempt",
                        index=index,
                        placement_fingerprint=placement,
                        graph_fingerprint=normal.graph_fingerprint,
                        target_fingerprint=normal.target_fingerprint,
                        context_base_fingerprint=normal.context_base_fingerprint,
                    )
                    for index in range(3)
                ),
            ),
            delay_walk=evidence.delay_walk,
        )


def test_attempt_id_is_content_bound_and_one_set_cannot_mix_retries() -> None:
    plan = _plan()
    placement = _hash("placement")
    graph = _hash("graph")
    capture = _capture(
        plan,
        target_index=0,
        evidence_kind="normal",
        attempt_id="attempt-a",
        index=0,
        placement_fingerprint=placement,
        graph_fingerprint=graph,
    )
    with pytest.raises(CommissioningEvidenceError, match="exact run attempt"):
        replace(capture, attempt_id="attempt-b")
    with pytest.raises(CommissioningEvidenceError, match="exact run attempt"):
        replace(capture, graph_fingerprint=_hash("different-graph"))

    captures = tuple(
        _capture(
            plan,
            target_index=0,
            evidence_kind="normal",
            attempt_id="attempt-b" if index == 2 else "attempt-a",
            index=index,
            placement_fingerprint=placement,
            graph_fingerprint=graph,
        )
        for index in range(3)
    )
    target = plan.targets[0]
    with pytest.raises(CommissioningEvidenceError, match="one exact target"):
        StationaryRegionEvidence(
            authority=plan.authority,
            plan_fingerprint=plan.fingerprint,
            attempt_id="attempt-a",
            speaker_group_id=target.speaker_group_id,
            region_id=target.region_id,
            evidence_kind="normal",
            target_fingerprint=target.normal_target_fingerprint,
            context_base_fingerprint=target.normal_context_base_fingerprint,
            placement_fingerprint=placement,
            graph_fingerprint=graph,
            captures=captures,
        )


def test_capture_rejects_cross_role_artifact_and_unbound_stimulus() -> None:
    plan = _plan()
    capture = _capture(
        plan,
        target_index=0,
        evidence_kind="normal",
        attempt_id="attempt-a",
        index=0,
        placement_fingerprint=_hash("placement"),
        graph_fingerprint=_hash("graph"),
    )
    cross_role_capture = replace(
        capture.capture,
        raw_artifact=capture.generation_artifact,
    )
    with pytest.raises(CommissioningEvidenceError, match="roles must be distinct"):
        replace(capture, capture=cross_role_capture)

    unbound_stimulus = GeneratedExcitationWav(
        generation_artifact_fingerprint=_hash("different-generation"),
        excitation_plan_fingerprint=capture.stimulus.excitation_plan_fingerprint,
        artifact=capture.stimulus.artifact,
    )
    with pytest.raises(CommissioningEvidenceError, match="not bound"):
        replace(capture, stimulus=unbound_stimulus)


def test_region_rejects_cross_generation_authority_and_reused_attempts() -> None:
    plan = _plan()
    stale_plan = _plan(owner_generation=4)
    placement = _hash("placement")
    normal = _stationary(
        plan,
        target_index=1,
        evidence_kind="normal",
        attempt_id="normal-attempt",
        placement_fingerprint=placement,
        graph_fingerprint=_hash("normal-graph"),
    )
    reverse = _stationary(
        plan,
        target_index=1,
        evidence_kind="reverse",
        attempt_id="reverse-attempt",
        placement_fingerprint=placement,
        graph_fingerprint=_hash("reverse-graph"),
    )
    walk = _delay_walk(plan, target_index=1, placement_fingerprint=placement)

    stale_normal = _stationary(
        stale_plan,
        target_index=1,
        evidence_kind="normal",
        attempt_id="stale-attempt",
        placement_fingerprint=placement,
        graph_fingerprint=_hash("stale-graph"),
        plan_fingerprint=plan.fingerprint,
    )
    with pytest.raises(CommissioningEvidenceError, match="authorities"):
        RegionCommissioningEvidence(
            plan=plan,
            target=plan.targets[1],
            normal=stale_normal,
            reverse=reverse,
            delay_walk=walk,
        )

    same_attempt_reverse = _stationary(
        plan,
        target_index=1,
        evidence_kind="reverse",
        attempt_id=normal.attempt_id,
        placement_fingerprint=placement,
        graph_fingerprint=_hash("reverse-graph"),
    )
    with pytest.raises(CommissioningEvidenceError, match="distinct attempts"):
        RegionCommissioningEvidence(
            plan=plan,
            target=plan.targets[1],
            normal=normal,
            reverse=same_attempt_reverse,
            delay_walk=walk,
        )
