# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.commissioning_evidence import (
    ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID,
    ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION,
    CompleteCommissioningEvidence,
    CompleteIsolatedDriverEvidence,
    active_region_threshold_profile_fingerprint,
)
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.commissioning_run import CommissioningRunHandle
from jasper.active_speaker.level_trim import (
    LevelTrimError,
    attenuation_from_group_deltas,
)
from jasper.active_speaker.measured_candidate import (
    ISOLATED_ANALYSIS_KIND,
    ISOLATED_ANALYZER_ID,
    ISOLATED_ANALYZER_VERSION,
    ISOLATED_QUALITY_KIND,
    MeasuredCandidateEvaluationError,
    MeasuredCandidateInputContract,
    MeasuredCandidateReadiness,
    MeasuredCandidateRefusal,
    MeasuredElectricalCandidate,
    _level,
    evaluate_measured_candidate,
    legacy_measured_candidate_readiness,
    measured_candidate_input_contract,
    wave2_measured_candidate_readiness,
)
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_artifacts import canonical_admission_bytes
from jasper.audio_measurement.null_walk import select_scheduled_delay
from tests.test_active_speaker_commissioning_evidence import (
    _complete_isolated,
    _hash,
    _preset,
    _region,
)
from tests.test_active_speaker_commissioning_evidence_store import (
    _harness_for_store,
    _open_store,
    _write_exact,
)


def test_wave2_states_remain_non_authoritative() -> None:
    legacy = legacy_measured_candidate_readiness().to_dict()
    wave2 = wave2_measured_candidate_readiness().to_dict()
    assert legacy["ready"] is wave2["ready"] is False
    assert legacy["candidate_authority"] is wave2["candidate_authority"] is False
    assert MeasuredCandidateRefusal.CAPTURE_NOT_ADMITTED.value in legacy["refusals"]
    assert wave2["refusals"][-1] == (
        MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED.value
    )


def test_input_contract_and_factories_are_pinned() -> None:
    contract = measured_candidate_input_contract().to_dict()
    assert contract["stationary_capture_count_per_target"] == 3
    assert contract["null_capture_count_per_delay"] == 5
    assert contract["delay_step_range_us"] == [50, 100]
    assert contract["candidate_output_enabled"] is False
    with pytest.raises(TypeError):
        MeasuredCandidateInputContract()
    with pytest.raises(TypeError):
        MeasuredCandidateReadiness()
    with pytest.raises(TypeError):
        MeasuredElectricalCandidate()


def test_legacy_trim_order_clamps_each_group_before_averaging() -> None:
    assert attenuation_from_group_deltas(
        ("woofer", "tweeter"),
        (
            (("woofer", "tweeter", 100.0),),
            (("woofer", "tweeter", 20.0),),
        ),
        minimum_db=-60.0,
    ) == {"woofer": 0.0, "tweeter": -40.0}
    with pytest.raises(LevelTrimError, match="authority bound"):
        attenuation_from_group_deltas(
            ("woofer", "tweeter"),
            (
                (("woofer", "tweeter", 100.0),),
                (("woofer", "tweeter", 20.0),),
            ),
            reject_below_db=-60.0,
        )


def _calibration() -> dict[str, object]:
    curve = {"points": [[20.0, 0.0], [20_000.0, 0.0]]}
    return {
        "fingerprint": json_fingerprint({"schema_version": 1, "curve": curve}),
        "curve": curve,
    }


def _materialize_base(
    store: CommissioningEvidenceStore,
    capture: Any,
    *,
    prefix: str,
) -> Any:
    item = capture
    raw = store.publish_raw_artifact(f"{prefix}/raw.wav", f"raw:{prefix}".encode())
    stimulus_raw = f"stimulus:{prefix}".encode()
    stimulus_artifact = ArtifactIdentity(
        bundle_kind=item.stimulus.artifact.bundle_kind,
        bundle_id=item.stimulus.artifact.bundle_id,
        relative_path=f"stimuli/{item.admission_id}.wav",
        sha256=hashlib.sha256(stimulus_raw).hexdigest(),
        byte_size=len(stimulus_raw),
    )
    _write_exact(store.bundle_dir, stimulus_artifact, stimulus_raw)
    _write_exact(
        store.bundle_dir,
        item.generation_artifact,
        canonical_admission_bytes(item.generation_admission),
    )
    _write_exact(
        store.bundle_dir,
        item.playback_artifact,
        canonical_admission_bytes(item.playback_admission),
    )
    old_id = item.capture.capture_id
    issued_id = old_id.removeprefix("capture-")
    return replace(
        item,
        capture=replace(
            item.capture,
            capture_id=f"capture-{issued_id}",
            raw_artifact=raw,
        ),
        stimulus=replace(item.stimulus, artifact=stimulus_artifact),
    )


def _publish_capture(
    store: CommissioningEvidenceStore,
    capture: Any,
    *,
    prefix: str,
    isolated: bool,
    level_db: float = 0.0,
    depth_db: float = 30.0,
    expect_null: bool = False,
    fc_hz: float = 1_000.0,
    duplicate_fc: bool = False,
    quality_mismatch: bool = False,
    excitation_mismatch: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    item = _materialize_base(store, capture, prefix=prefix)
    issuance = item.capture.capture_id.removeprefix("capture-")
    operation = item.capture.fingerprint
    if isolated:
        admitted_effective = (
            item.generation_admission.request.effective_peak_dbfs
        )
        ledger_effective = admitted_effective + (
            1.0 if excitation_mismatch == "level" else 0.0
        )
        overlaps = [
            {
                "fc_hz": region.fc_hz,
                "level_db": level_db + admitted_effective,
                "bins": 100,
                "usable": True,
                "snr_verdict": "ok",
                "above_validity_floor": True,
                "near_validity_floor": False,
            }
            for region in _preset().crossover_regions
        ]
        if duplicate_fc:
            overlaps.append(dict(overlaps[0]))
        acoustic = {
            "kind": "jts_active_speaker_driver_acoustics",
            "present": True,
            "calibrated": True,
            "capture_geometry": "reference_axis",
            "mic_clipping": False,
            "gating": {"applied": True},
            "snr": {"decision_class": "magnitude", "verdict": "ok"},
            "overlap_levels": overlaps,
        }
        extra = {
            "plan_fingerprint": item.plan_fingerprint,
            "evidence_target_fingerprint": item.evidence_target_fingerprint,
            "driver_target_id": item.driver_target_id,
            "driver_target_fingerprint": item.driver_target_fingerprint,
            "excitation": {
                "schema_version": 1,
                "scope": "sweep_plus_role_varying_commission_gain",
                "sweep_peak_dbfs": ledger_effective,
                "commissioning_gain_db": 0.0,
                "effective_peak_dbfs": ledger_effective,
                "role": (
                    "wrong-role" if excitation_mismatch == "role" else item.role
                ),
            },
        }
        analysis_kind = ISOLATED_ANALYSIS_KIND
        quality_kind = ISOLATED_QUALITY_KIND
        algorithm_id = ISOLATED_ANALYZER_ID
        algorithm_version = ISOLATED_ANALYZER_VERSION
    else:
        acoustic = {
            "kind": "jts_active_speaker_summed_acoustics",
            "null_depth_db": depth_db,
            "crossover_fc_hz": fc_hz,
            "expect_null": expect_null,
            "calibrated": True,
            "capture_geometry": "reference_axis",
            "mic_clipping": False,
            "gating": {"applied": True},
            "snr": {"decision_class": "alignment", "verdict": "ok"},
            "above_validity_floor": True,
            "near_validity_floor": False,
            "null_depth_capped": False,
        }
        extra = {"target_fingerprint": item.capture.target_fingerprint}
        analysis_kind = "jts_active_summed_capture_analysis"
        quality_kind = "jts_active_summed_capture_quality"
        algorithm_id = ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID
        algorithm_version = ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION
    analysis_payload = {
        "schema_version": 1,
        "kind": analysis_kind,
        "algorithm_id": algorithm_id,
        "algorithm_version": algorithm_version,
        "threshold_profile_fingerprint": active_region_threshold_profile_fingerprint(),
        "operation_fingerprint": operation,
        "issuance_id": issuance,
        "context_fingerprint": item.context_fingerprint,
        "graph_fingerprint": item.graph_fingerprint,
        "raw_artifact": item.capture.raw_artifact.to_dict(),
        "stimulus": item.stimulus.to_dict(),
        "generation_artifact": item.generation_artifact.to_dict(),
        "playback_artifact": item.playback_artifact.to_dict(),
        "calibration": _calibration(),
        "capture_geometry": "reference_axis",
        "acoustic": acoustic,
        **extra,
    }
    analysis = store.publish_json_artifact(f"{prefix}/analysis.json", analysis_payload)
    quality = store.publish_json_artifact(
        f"{prefix}/quality.json",
        {
            "schema_version": 1,
            "kind": quality_kind,
            "algorithm_id": algorithm_id,
            "algorithm_version": algorithm_version,
            "threshold_profile_fingerprint": active_region_threshold_profile_fingerprint(),
            "operation_fingerprint": "0" * 64 if quality_mismatch else operation,
            "issuance_id": "wrong-issuance" if quality_mismatch else issuance,
            "raw_artifact_fingerprint": item.capture.raw_artifact.fingerprint,
            "analysis_artifact_fingerprint": analysis.fingerprint,
            "accepted": True,
            "issues": [],
            "quality": {},
        },
    )
    return replace(
        item,
        capture=replace(
            item.capture,
            analysis_input_artifact=analysis,
            quality_artifact=quality,
        ),
    ), acoustic


def _materialize_isolated(
    store: CommissioningEvidenceStore,
    complete: CompleteIsolatedDriverEvidence,
    *,
    spread: float = 0.0,
    out_of_range: bool = False,
    duplicate_fc: bool = False,
    quality_mismatch: bool = False,
    excitation_mismatch: str | None = None,
) -> CompleteIsolatedDriverEvidence:
    role_levels = {"woofer": 0.0, "mid": 3.0, "tweeter": 70.0 if out_of_range else 6.0}
    drivers = []
    for driver in complete.drivers:
        captures = tuple(
            _publish_capture(
                store,
                capture,
                prefix=f"candidate/isolated/{driver.role}/{index}",
                isolated=True,
                level_db=role_levels[driver.role] + (spread if index == 2 else 0.0),
                duplicate_fc=(duplicate_fc and driver.role == "woofer" and index == 0),
                quality_mismatch=(
                    quality_mismatch and driver.role == "woofer" and index == 0
                ),
                excitation_mismatch=(
                    excitation_mismatch
                    if driver.role == "woofer" and index == 0
                    else None
                ),
            )[0]
            for index, capture in enumerate(driver.captures)
        )
        repeatability = store.publish_json_artifact(
            f"candidate/isolated/{driver.role}/repeatability.json",
            {"schema_version": 1, "capture_count": len(captures)},
        )
        drivers.append(
            replace(driver, captures=captures, repeatability_artifact=repeatability)
        )
    return replace(complete, drivers=tuple(drivers))


def _materialize_summed(
    store: CommissioningEvidenceStore,
    complete: CompleteCommissioningEvidence,
    *,
    stationary_spread: float = 0.0,
    delay_spread: float = 0.0,
) -> CompleteCommissioningEvidence:
    regions = []
    for region in complete.regions:
        fc = region.target.electrical_fc_hz
        normal = tuple(
            _publish_capture(
                store,
                capture,
                prefix=f"candidate/summed/{region.target.region_id}/normal/{index}",
                isolated=False,
                depth_db=10.0 + (stationary_spread if index == 2 else 0.0),
                expect_null=False,
                fc_hz=fc,
            )[0]
            for index, capture in enumerate(region.normal.captures)
        )
        reverse = tuple(
            _publish_capture(
                store,
                capture,
                prefix=f"candidate/summed/{region.target.region_id}/reverse/{index}",
                isolated=False,
                depth_db=35.0,
                expect_null=True,
                fc_hz=fc,
            )[0]
            for index, capture in enumerate(region.reverse.captures)
        )
        point_rows = {}
        points = []
        for point_index, point in enumerate(region.delay_walk.points):
            depth = (
                35.0
                - abs(point.relative_delay_us - region.delay_walk.spec.geometry_seed_us)
                / 1_000.0
            )
            captures_and_rows = [
                _publish_capture(
                    store,
                    capture,
                    prefix=(
                        f"candidate/summed/{region.target.region_id}/"
                        f"delay/{point_index}/{index}"
                    ),
                    isolated=False,
                    depth_db=depth + (
                        delay_spread
                        if index == 2
                        and point.relative_delay_us
                        not in region.delay_walk.schedule.coarse_delays_us
                        else 0.0
                    ),
                    expect_null=True,
                    fc_hz=fc,
                )
                for index, capture in enumerate(point.captures)
            ]
            points.append(
                replace(point, captures=tuple(item[0] for item in captures_and_rows))
            )
            point_rows[point.relative_delay_us] = [
                item[1] for item in captures_and_rows
            ]
        selection = select_scheduled_delay(
            region.delay_walk.spec, region.delay_walk.schedule, point_rows
        )
        repeatability = store.publish_json_artifact(
            f"candidate/summed/{region.target.region_id}/repeatability.json",
            selection,
        )
        geometry = store.publish_json_artifact(
            f"candidate/summed/{region.target.region_id}/geometry.json",
            {"schema_version": 1, "seed_us": region.delay_walk.spec.geometry_seed_us},
        )
        regions.append(
            replace(
                region,
                normal=replace(region.normal, captures=normal),
                reverse=replace(region.reverse, captures=reverse),
                delay_walk=replace(
                    region.delay_walk,
                    points=tuple(points),
                    repeatability_artifact=repeatability,
                    geometry_attestation=replace(
                        region.delay_walk.geometry_attestation,
                        attestation_artifact=geometry,
                    ),
                ),
            )
        )
    return replace(complete, regions=tuple(regions))


def _authority(
    tmp_path: Path,
    *,
    isolated_spread: float = 0.0,
    stationary_spread: float = 0.0,
    out_of_range: bool = False,
    duplicate_fc: bool = False,
    quality_mismatch: bool = False,
    excitation_mismatch: str | None = None,
    delay_spread: float = 0.0,
) -> tuple[
    CommissioningEvidenceStore,
    CommissioningRunHandle,
    ArtifactIdentity,
    ArtifactIdentity,
]:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    placement = _hash("isolated-placement")
    isolated = _materialize_isolated(
        store,
        _complete_isolated(harness),
        spread=isolated_spread,
        out_of_range=out_of_range,
        duplicate_fc=duplicate_fc,
        quality_mismatch=quality_mismatch,
        excitation_mismatch=excitation_mismatch,
    )
    summed = _materialize_summed(
        store,
        CompleteCommissioningEvidence(
            plan=harness.plan,
            regions=tuple(
                _region(harness, target_index=index, placement_fingerprint=placement)
                for index in range(len(harness.plan.targets))
            ),
        ),
        stationary_spread=stationary_spread,
        delay_spread=delay_spread,
    )
    return (
        store,
        harness.plan.authority.run,
        store.publish_complete_isolated_driver_evidence(isolated),
        store.publish_complete_commissioning_evidence(summed),
    )


def _evaluate(
    authority: tuple[
        CommissioningEvidenceStore,
        CommissioningRunHandle,
        ArtifactIdentity,
        ArtifactIdentity,
    ],
    *,
    run: CommissioningRunHandle | None = None,
) -> MeasuredElectricalCandidate:
    store, current, isolated, summed = authority
    return evaluate_measured_candidate(
        store=store,
        run=run or current,
        reviewed_preset=_preset(),
        isolated_evidence_artifact=isolated,
        summed_evidence_artifact=summed,
    )


def test_real_store_complete_evidence_authorizes_deterministic_candidate(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    first = _evaluate(authority)
    second = _evaluate(authority)
    assert first == second
    assert dict(first.role_attenuations_db) == {
        "woofer": 0.0,
        "mid": -3.0,
        "tweeter": -6.0,
    }
    assert first.source_preset == _preset()
    delays = dict(first.role_delays_ms)
    assert delays == {
        "woofer": 0.0,
        "mid": 0.0375,
        "tweeter": 0.075,
    }
    assert delays["mid"] - delays["woofer"] == pytest.approx(0.0375)
    assert delays["tweeter"] - delays["mid"] == pytest.approx(0.0375)
    assert first.to_dict() == second.to_dict()


def test_exact_current_owner_generation_is_required(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    run = replace(authority[1], owner_generation=authority[1].owner_generation + 1)
    with pytest.raises(MeasuredCandidateEvaluationError) as caught:
        _evaluate(authority, run=run)
    assert caught.value.code == "evidence_run_mismatch"


def test_quality_operation_and_issuance_must_match_analysis(tmp_path: Path) -> None:
    with pytest.raises(MeasuredCandidateEvaluationError) as caught:
        _evaluate(_authority(tmp_path, quality_mismatch=True))
    assert caught.value.code == "capture_quality_refused"


@pytest.mark.parametrize("mismatch", ["level", "role"])
def test_isolated_excitation_must_match_the_admitted_request(
    tmp_path: Path,
    mismatch: str,
) -> None:
    with pytest.raises(MeasuredCandidateEvaluationError) as caught:
        _evaluate(_authority(tmp_path, excitation_mismatch=mismatch))
    assert caught.value.code == "isolated_capture_unsafe"


def test_overlap_level_must_match_the_reviewed_crossover_within_one_hz() -> None:
    with pytest.raises(MeasuredCandidateEvaluationError) as caught:
        _level({1_000.0: -3.0}, 1_010.0)
    assert caught.value.code == "isolated_overlap_missing"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"isolated_spread": 2.0}, "isolated_repeat_spread"),
        ({"stationary_spread": 2.0}, "stationary_repeat_spread"),
        ({"delay_spread": 2.0}, "delay_selection_refused"),
        ({"out_of_range": True}, "candidate_attenuation_out_of_range"),
        ({"duplicate_fc": True}, "isolated_overlap_duplicate"),
    ],
)
def test_unsafe_candidate_evidence_is_refused(
    tmp_path: Path,
    kwargs: dict[str, Any],
    code: str,
) -> None:
    with pytest.raises(MeasuredCandidateEvaluationError) as caught:
        _evaluate(_authority(tmp_path, **kwargs))
    assert caught.value.code == code
