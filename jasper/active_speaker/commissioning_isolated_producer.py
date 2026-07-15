# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Promote production fixed-axis driver captures into strict evidence.

The browser relay and legacy repeat controller remain transport/UI adapters.
This module owns the only promotion boundary: a newly admitted one-shot WAV is
bound to the exact durable run, protected graph, preset, calibration, and
physical driver. Historical or fail-soft records are never read here.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Mapping
from typing import Any

import yaml

from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.evidence_identity import (
    CaptureIdentity,
    NormalizedActiveRawIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_artifacts import (
    read_generation_admission,
    read_playback_admission,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from .baseline_profile import recompose_applied_baseline_yaml
from .commissioning_admission import ActiveCaptureAdmissionHandoff
from .commissioning_evidence import (
    ACTIVE_ISOLATED_DRIVER_EVIDENCE_CONSUMER_ID,
    ACTIVE_ISOLATED_DRIVER_MEASUREMENT_KIND,
    REFERENCE_AXIS_GEOMETRY_ID,
    STATIONARY_CAPTURE_COUNT,
    AdmittedIsolatedDriverCapture,
    CompleteIsolatedDriverEvidence,
    DriverEvidenceTarget,
    IsolatedDriverEvidence,
    RegionEvidencePlan,
    active_region_context_fingerprint,
    active_region_threshold_profile_fingerprint,
    derive_region_evidence_plan,
    isolated_capture_context_base_fingerprint,
    isolated_capture_context_fingerprint,
    isolated_driver_attempt_target_id,
    isolated_driver_evidence_target_fingerprint,
)
from .commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
)
from .commissioning_lifecycle import CommissioningTransition
from .commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningRunHandle,
    CommissioningRunStore,
)
from .capture_geometry import comparison_set_valid
from .crossover_contract import (
    DRIVER_EXCITATION_MATCH_TOLERANCE_DB,
    preset_matches_applied_profile,
    verified_driver_excitation,
)
from .driver_acoustics import DRIVER_ACOUSTIC_KIND
from .measured_candidate import (
    ISOLATED_ANALYSIS_KIND,
    ISOLATED_ANALYZER_ID,
    ISOLATED_ANALYZER_VERSION,
    ISOLATED_QUALITY_KIND,
)
from .profile import ActiveSpeakerPreset


class IsolatedCapturePromotionError(ValueError):
    """One relay capture cannot enter automatic commissioning authority."""


logger = logging.getLogger(__name__)


def _missing(error: CommissioningEvidenceStoreError) -> bool:
    return error.code == CommissioningEvidenceStoreErrorCode.MISSING


def _current_plan(
    *,
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    comparison_set: Mapping[str, Any],
    applied_profile: Mapping[str, Any],
    calibration_id: str,
    calibration: CalibrationCurve,
    protected_safety_profile_fingerprint: str,
    run: CommissioningRunHandle,
    evidence_store: CommissioningEvidenceStore,
) -> RegionEvidencePlan:
    if not preset_matches_applied_profile(preset, applied_profile):
        raise IsolatedCapturePromotionError(
            "capture preset does not equal the protected applied profile"
        )
    normal_raw, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied_profile,
    )
    if normal_raw is None or issues:
        raise IsolatedCapturePromotionError(
            "protected applied profile cannot be re-emitted exactly"
        )
    try:
        baseline = NormalizedActiveRawIdentity(
            yaml.safe_load(normal_raw)
        ).active_raw_fingerprint
        context = active_region_context_fingerprint(
            baseline_active_raw_fingerprint=baseline,
            calibration_id=calibration_id,
            calibration=calibration,
        )
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise IsolatedCapturePromotionError(
            "capture baseline or calibration context is invalid"
        ) from exc
    expected = derive_region_evidence_plan(
        preset,
        topology,
        run=run,
        protected_safety_profile_fingerprint=(protected_safety_profile_fingerprint),
        comparison_set_fingerprint=str(comparison_set.get("fingerprint") or ""),
        threshold_profile_fingerprint=(active_region_threshold_profile_fingerprint()),
        context_fingerprint=context,
    )
    try:
        existing = evidence_store.reopen_region_evidence_plan(run=run)
    except CommissioningEvidenceStoreError as exc:
        if not _missing(exc):
            raise
        evidence_store.publish_region_evidence_plan(expected)
        existing = evidence_store.reopen_region_evidence_plan(run=run)
    if existing != expected:
        raise IsolatedCapturePromotionError(
            "current capture authority differs from the durable commissioning plan"
        )
    return existing


def _placement_fingerprint(
    plan: RegionEvidencePlan,
    *,
    speaker_group_id: str,
) -> str:
    """Name the stationary fixed-axis placement shared by one speaker group."""

    return json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_isolated_fixed_axis_placement",
            "plan_fingerprint": plan.fingerprint,
            "speaker_group_id": speaker_group_id,
            "geometry_id": REFERENCE_AXIS_GEOMETRY_ID,
            "policy_id": "operator_stationary_reference_axis_v1",
        }
    )


def _quality_issues(
    acoustic: Mapping[str, Any],
    excitation: object,
    *,
    role: str,
    admitted_effective_peak_dbfs: float,
    has_calibration: bool,
) -> list[str]:
    issues: list[str] = []
    snr = acoustic.get("snr")
    gating = acoustic.get("gating")
    if not has_calibration or acoustic.get("calibrated") is not True:
        issues.append("calibration_required")
    if acoustic.get("capture_geometry") != REFERENCE_AXIS_GEOMETRY_ID:
        issues.append("reference_axis_required")
    if acoustic.get("mic_clipping") is not False:
        issues.append("mic_clipping")
    if not isinstance(gating, Mapping) or gating.get("applied") is not True:
        issues.append("gated_capture_required")
    if (
        not isinstance(snr, Mapping)
        or snr.get("decision_class") != "magnitude"
        or snr.get("verdict") != "ok"
    ):
        issues.append("magnitude_snr_insufficient")
    if (
        acoustic.get("kind") != DRIVER_ACOUSTIC_KIND
        or acoustic.get("present") is not True
    ):
        issues.append("driver_response_missing")
    overlaps = acoustic.get("overlap_levels")
    if not isinstance(overlaps, list) or not overlaps:
        issues.append("crossover_overlap_missing")
    elif any(
        not isinstance(item, Mapping)
        or item.get("usable") is not True
        or item.get("above_validity_floor") is not True
        or item.get("near_validity_floor") is not False
        or item.get("snr_verdict") != "ok"
        for item in overlaps
    ):
        issues.append("crossover_overlap_unusable")
    verified = verified_driver_excitation(excitation)
    if (
        verified is None
        or verified.get("role") != role
        or not math.isclose(
            float(verified.get("effective_peak_dbfs") or math.inf),
            admitted_effective_peak_dbfs,
            rel_tol=0.0,
            abs_tol=DRIVER_EXCITATION_MATCH_TOLERANCE_DB,
        )
    ):
        issues.append("excitation_mismatch")
    return issues


def _repeatability_payload(
    captures: tuple[AdmittedIsolatedDriverCapture, ...],
    evidence_store: CommissioningEvidenceStore,
) -> dict[str, Any]:
    levels_by_fc: dict[float, list[float]] = {}
    for capture in captures:
        analysis = evidence_store.reopen_json_artifact(
            capture.capture.analysis_input_artifact
        )
        acoustic = analysis["acoustic"]
        admitted_level = capture.generation_admission.request.effective_peak_dbfs
        for item in acoustic["overlap_levels"]:
            fc = float(item["fc_hz"])
            levels_by_fc.setdefault(fc, []).append(
                float(item["level_db"]) - admitted_level
            )
    rows = []
    for fc, values in sorted(levels_by_fc.items()):
        rows.append(
            {
                "fc_hz": fc,
                "median_level_db": statistics.median(values),
                "spread_db": max(values) - min(values),
            }
        )
    return {
        "schema_version": 1,
        "kind": "jts_active_isolated_driver_repeatability",
        "algorithm_id": ISOLATED_ANALYZER_ID,
        "algorithm_version": ISOLATED_ANALYZER_VERSION,
        "capture_fingerprints": [item.fingerprint for item in captures],
        "capture_count": len(captures),
        "overlap_levels": rows,
    }


def _complete_if_ready(
    plan: RegionEvidencePlan,
    evidence_store: CommissioningEvidenceStore,
) -> CompleteIsolatedDriverEvidence | None:
    drivers = []
    for target in plan.driver_targets:
        try:
            drivers.append(
                evidence_store.reopen_isolated_driver_evidence(
                    run=plan.authority.run,
                    speaker_group_id=target.speaker_group_id,
                    role=target.role,
                )
            )
        except CommissioningEvidenceStoreError as exc:
            if _missing(exc):
                return None
            raise
    complete = CompleteIsolatedDriverEvidence(plan=plan, drivers=tuple(drivers))
    evidence_store.publish_complete_isolated_driver_evidence(complete)
    return evidence_store.reopen_complete_isolated_driver_evidence(
        run_id=plan.authority.run.run_id
    )


def _finalize_driver_if_ready(
    plan: RegionEvidencePlan,
    target: DriverEvidenceTarget,
    attempt: CommissioningAttemptHandle,
    captures: tuple[AdmittedIsolatedDriverCapture, ...],
    evidence_store: CommissioningEvidenceStore,
) -> IsolatedDriverEvidence | None:
    """Idempotently derive one driver's anchors from its typed captures."""

    if len(captures) < STATIONARY_CAPTURE_COUNT:
        return None
    if len(captures) != STATIONARY_CAPTURE_COUNT:
        raise IsolatedCapturePromotionError(
            "isolated driver capture count exceeded its bounded contract"
        )
    if not isinstance(attempt, CommissioningAttemptHandle):
        raise TypeError("attempt must be CommissioningAttemptHandle")
    canonical_captures = tuple(sorted(captures, key=lambda item: item.canonical_key))
    first = canonical_captures[0]
    repeatability = evidence_store.publish_json_artifact(
        f"isolated/{attempt.attempt_id}/repeatability.json",
        _repeatability_payload(canonical_captures, evidence_store),
    )
    evidence_store.publish_isolated_driver_evidence(
        IsolatedDriverEvidence(
            authority=plan.authority,
            plan_fingerprint=plan.fingerprint,
            speaker_group_id=target.speaker_group_id,
            role=target.role,
            evidence_target_fingerprint=first.evidence_target_fingerprint,
            driver_target_id=target.driver_target_id,
            driver_target_fingerprint=target.driver_target_fingerprint,
            attempt=attempt,
            placement_fingerprint=first.placement_fingerprint,
            context_base_fingerprint=first.context_base_fingerprint,
            graph_fingerprint=first.graph_fingerprint,
            captures=canonical_captures,
            repeatability_artifact=repeatability,
        )
    )
    return evidence_store.reopen_isolated_driver_evidence(
        run=plan.authority.run,
        speaker_group_id=target.speaker_group_id,
        role=target.role,
    )


def resume_isolated_evidence(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    evidence_store: CommissioningEvidenceStore,
) -> CompleteIsolatedDriverEvidence | None:
    """Finish write-once derived anchors left incomplete by a prior request."""

    if not run_store.callback_is_current(run):
        raise IsolatedCapturePromotionError("commissioning run ownership changed")
    try:
        plan = evidence_store.reopen_region_evidence_plan(run=run)
    except CommissioningEvidenceStoreError as exc:
        if _missing(exc):
            return None
        raise
    try:
        evidence_store.complete_isolated_driver_evidence_fingerprint(
            run_id=run.run_id
        )
    except CommissioningEvidenceStoreError as exc:
        if not _missing(exc):
            raise
    else:
        return None
    attempts = {item.target_id: item for item in run_store.attempts(run)}
    repaired = False
    all_driver_anchors = True
    for target in plan.driver_targets:
        try:
            published = evidence_store.isolated_driver_evidence_is_published(
                run=run,
                speaker_group_id=target.speaker_group_id,
                role=target.role,
            )
        except CommissioningEvidenceStoreError as exc:
            if not _missing(exc):
                raise
            published = False
        if published:
            continue
        evidence_target = isolated_driver_evidence_target_fingerprint(
            plan.authority,
            plan_fingerprint=plan.fingerprint,
            speaker_group_id=target.speaker_group_id,
            role=target.role,
            driver_target_id=target.driver_target_id,
            driver_target_fingerprint=target.driver_target_fingerprint,
        )
        attempt = attempts.get(isolated_driver_attempt_target_id(evidence_target))
        if attempt is None:
            all_driver_anchors = False
            continue
        capture_count = evidence_store.isolated_attempt_capture_count(
            attempt.attempt_id
        )
        if capture_count < STATIONARY_CAPTURE_COUNT:
            all_driver_anchors = False
            continue
        if capture_count != STATIONARY_CAPTURE_COUNT:
            raise IsolatedCapturePromotionError(
                "isolated driver capture count exceeded its bounded contract"
            )
        captures = evidence_store.reopen_isolated_attempt_captures(
            attempt.attempt_id
        )
        if _finalize_driver_if_ready(
            plan,
            target,
            attempt,
            captures,
            evidence_store,
        ) is not None:
            repaired = True
        else:
            all_driver_anchors = False
    complete = (
        _complete_if_ready(plan, evidence_store)
        if all_driver_anchors
        else None
    )
    if repaired or complete is not None:
        log_event(
            logger,
            "active_speaker.isolated_evidence_resumed",
            run_id=run.run_id,
            repaired=repaired,
            complete=complete is not None,
            complete_fingerprint=(
                complete.fingerprint if complete is not None else None
            ),
        )
    return complete


def isolated_evidence_status(
    *,
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    evidence_store: CommissioningEvidenceStore,
) -> dict[str, Any]:
    """Project resumable isolated progress from strict authority only."""

    if not run_store.callback_is_current(run):
        return {"status": "stale", "reason": "commissioning_run_changed"}
    try:
        plan = evidence_store.reopen_region_evidence_plan(run=run)
    except CommissioningEvidenceStoreError as exc:
        if _missing(exc):
            return {"status": "not_started", "reason": "evidence_plan_missing"}
        raise
    attempts = run_store.attempts(run)
    attempts_by_target = {item.target_id: item for item in attempts}
    drivers = []
    for target in plan.driver_targets:
        evidence_target = isolated_driver_evidence_target_fingerprint(
            plan.authority,
            plan_fingerprint=plan.fingerprint,
            speaker_group_id=target.speaker_group_id,
            role=target.role,
            driver_target_id=target.driver_target_id,
            driver_target_fingerprint=target.driver_target_fingerprint,
        )
        attempt = attempts_by_target.get(
            isolated_driver_attempt_target_id(evidence_target)
        )
        accepted = 0
        if attempt is not None:
            accepted = evidence_store.isolated_attempt_capture_count(attempt.attempt_id)
        try:
            driver_complete = evidence_store.isolated_driver_evidence_is_published(
                run=run,
                speaker_group_id=target.speaker_group_id,
                role=target.role,
            )
        except CommissioningEvidenceStoreError as exc:
            if not _missing(exc):
                raise
            driver_complete = False
        drivers.append(
            {
                "speaker_group_id": target.speaker_group_id,
                "role": target.role,
                "accepted": accepted,
                "required": STATIONARY_CAPTURE_COUNT,
                "complete": driver_complete,
            }
        )
    try:
        complete_fingerprint = (
            evidence_store.complete_isolated_driver_evidence_fingerprint(
                run_id=run.run_id
            )
        )
    except CommissioningEvidenceStoreError as exc:
        if not _missing(exc):
            raise
        complete_fingerprint = None
    return {
        "status": "complete" if complete_fingerprint is not None else "collecting",
        "plan_fingerprint": plan.fingerprint,
        "drivers": drivers,
        "complete_fingerprint": complete_fingerprint,
    }


def promote_isolated_driver_capture(
    *,
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    comparison_set: Mapping[str, Any],
    applied_profile: Mapping[str, Any],
    calibration_id: str | None,
    calibration: CalibrationCurve | None,
    speaker_group_id: str,
    role: str,
    capture_geometry: str,
    wav_bytes: bytes,
    sweep_meta: Mapping[str, Any],
    provisional: Mapping[str, Any],
    admission_handoff: Mapping[str, Any],
    run: CommissioningRunHandle,
    run_store: CommissioningRunStore,
    evidence_store: CommissioningEvidenceStore,
) -> dict[str, Any]:
    """Persist one promotable relay capture and advance exact durable progress."""

    if capture_geometry != REFERENCE_AXIS_GEOMETRY_ID:
        raise IsolatedCapturePromotionError(
            "only fixed-axis driver captures can enter commissioning evidence"
        )
    if not isinstance(calibration_id, str) or not calibration_id or calibration is None:
        raise IsolatedCapturePromotionError(
            "automatic commissioning requires the current mic calibration"
        )
    if not run_store.callback_is_current(run):
        raise IsolatedCapturePromotionError("commissioning run ownership changed")
    if (
        evidence_store.session_id != run.session_id
        or not comparison_set_valid(comparison_set)
        or comparison_set.get("bundle_session_id") != run.session_id
        or comparison_set.get("fingerprint") != run.session_fingerprint
        or comparison_set.get("topology_id") != topology.topology_id
        or comparison_set.get("calibration_id") != calibration_id
    ):
        raise IsolatedCapturePromotionError(
            "capture comparison, topology, calibration, or bundle authority changed"
        )
    state = run_store.lifecycle_state(run)
    if state not in {"unconfigured", "protected"}:
        raise IsolatedCapturePromotionError(
            "commissioning run is not collecting isolated-driver evidence"
        )
    handoff = ActiveCaptureAdmissionHandoff.from_mapping(admission_handoff)
    if (
        handoff.session_id != run.session_id
        or handoff.comparison_set_id != comparison_set.get("comparison_set_id")
        or handoff.comparison_set_fingerprint != run.session_fingerprint
    ):
        raise IsolatedCapturePromotionError(
            "capture handoff does not equal the exact commissioning run"
        )
    generation = read_generation_admission(
        evidence_store.admission_authority,
        handoff.generation_artifact,
    )
    playback = read_playback_admission(
        evidence_store.admission_authority,
        generation,
        handoff.playback_artifact,
    )
    generation_proof = generation.admission.protection_evidence
    playback_proof = playback.admission.protection_evidence
    safety_fingerprint = (
        generation_proof.safety_profile_fingerprint
        if generation_proof is not None
        else None
    )
    if (
        generation.admission_id != handoff.admission_id
        or playback.admission.to_dict() != dict(handoff.admission)
        or generation_proof is None
        or generation_proof.current is not True
        or generation_proof.evidence_fingerprint is None
        or playback_proof is None
        or playback_proof.current is not True
        or playback_proof.evidence_fingerprint is None
        or not isinstance(safety_fingerprint, str)
        or playback_proof.evidence_fingerprint != handoff.graph_evidence_fingerprint
    ):
        raise IsolatedCapturePromotionError(
            "capture admissions do not retain current exact protection proof"
        )
    plan = _current_plan(
        topology=topology,
        preset=preset,
        comparison_set=comparison_set,
        applied_profile=applied_profile,
        calibration_id=calibration_id,
        calibration=calibration,
        protected_safety_profile_fingerprint=safety_fingerprint,
        run=run,
        evidence_store=evidence_store,
    )
    target = next(
        (
            item
            for item in plan.driver_targets
            if item.speaker_group_id == speaker_group_id and item.role == role
        ),
        None,
    )
    if target is None or (
        target.driver_target_id != handoff.target_id
        or target.driver_target_fingerprint != handoff.target_fingerprint
    ):
        raise IsolatedCapturePromotionError(
            "capture target does not equal the durable commissioning plan"
        )
    evidence_target = isolated_driver_evidence_target_fingerprint(
        plan.authority,
        plan_fingerprint=plan.fingerprint,
        speaker_group_id=speaker_group_id,
        role=role,
        driver_target_id=target.driver_target_id,
        driver_target_fingerprint=target.driver_target_fingerprint,
    )
    attempt = run_store.reserve_attempt(
        run,
        target_id=isolated_driver_attempt_target_id(evidence_target),
        target_fingerprint=evidence_target,
        reuse_existing=True,
    )
    existing = evidence_store.reopen_isolated_attempt_captures(attempt.attempt_id)
    if len(existing) >= STATIONARY_CAPTURE_COUNT:
        _finalize_driver_if_ready(
            plan,
            target,
            attempt,
            existing,
            evidence_store,
        )
        resumed_complete = _complete_if_ready(plan, evidence_store)
        return {
            "status": "complete" if resumed_complete is not None else "collecting",
            "accepted": len(existing),
            "required": STATIONARY_CAPTURE_COUNT,
            "driver_complete": True,
            "complete": resumed_complete is not None,
            "attempt_id": attempt.attempt_id,
            "capture_fingerprint": None,
            "complete_fingerprint": (
                resumed_complete.fingerprint
                if resumed_complete is not None
                else None
            ),
            "resumed": True,
        }
    if any(item.admission_id == handoff.admission_id for item in existing):
        raise IsolatedCapturePromotionError("capture admission was already consumed")
    placement_fingerprint = _placement_fingerprint(
        plan,
        speaker_group_id=speaker_group_id,
    )
    context_base = isolated_capture_context_base_fingerprint(
        plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        evidence_target_fingerprint=evidence_target,
        driver_target_id=target.driver_target_id,
        driver_target_fingerprint=target.driver_target_fingerprint,
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=handoff.graph_fingerprint,
    )
    context = isolated_capture_context_fingerprint(
        plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        evidence_target_fingerprint=evidence_target,
        driver_target_id=target.driver_target_id,
        driver_target_fingerprint=target.driver_target_fingerprint,
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=handoff.graph_fingerprint,
        generation_protection_evidence_fingerprint=(
            generation_proof.evidence_fingerprint
        ),
        playback_protection_evidence_fingerprint=(playback_proof.evidence_fingerprint),
    )
    acoustic = provisional.get("acoustic")
    excitation = provisional.get("excitation")
    if not isinstance(acoustic, Mapping):
        raise IsolatedCapturePromotionError("driver analysis is missing")
    issues = _quality_issues(
        acoustic,
        excitation,
        role=role,
        admitted_effective_peak_dbfs=(generation.admission.request.effective_peak_dbfs),
        has_calibration=True,
    )
    if issues:
        raise IsolatedCapturePromotionError(
            "driver capture is not promotable: " + ",".join(issues)
        )
    issuance_id = handoff.admission_id
    operation_fingerprint = json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_isolated_driver_capture_operation",
            "plan_fingerprint": plan.fingerprint,
            "attempt_id": attempt.attempt_id,
            "issuance_id": issuance_id,
            "context_fingerprint": context,
        }
    )
    prefix = f"isolated/{attempt.attempt_id}/{issuance_id}"
    raw_artifact = evidence_store.publish_raw_artifact(
        f"{prefix}/raw.wav",
        wav_bytes,
    )
    calibration_payload = calibration.to_dict()
    analysis_payload = {
        "schema_version": 1,
        "kind": ISOLATED_ANALYSIS_KIND,
        "algorithm_id": ISOLATED_ANALYZER_ID,
        "algorithm_version": ISOLATED_ANALYZER_VERSION,
        "threshold_profile_fingerprint": (
            active_region_threshold_profile_fingerprint()
        ),
        "operation_fingerprint": operation_fingerprint,
        "issuance_id": issuance_id,
        "plan_fingerprint": plan.fingerprint,
        "evidence_target_fingerprint": evidence_target,
        "driver_target_id": target.driver_target_id,
        "driver_target_fingerprint": target.driver_target_fingerprint,
        "context_fingerprint": context,
        "graph_fingerprint": handoff.graph_fingerprint,
        "raw_artifact": raw_artifact.to_dict(),
        "stimulus": handoff.stimulus.to_dict(),
        "generation_artifact": generation.artifact.to_dict(),
        "playback_artifact": playback.artifact.to_dict(),
        "sweep_meta": dict(sweep_meta),
        "calibration": {
            "fingerprint": json_fingerprint(
                {"schema_version": 1, "curve": calibration_payload}
            ),
            "curve": calibration_payload,
        },
        "capture_geometry": REFERENCE_AXIS_GEOMETRY_ID,
        "excitation": excitation,
        "acoustic": dict(acoustic),
    }
    analysis_artifact = evidence_store.publish_json_artifact(
        f"{prefix}/analysis.json",
        analysis_payload,
    )
    quality_payload = {
        "schema_version": 1,
        "kind": ISOLATED_QUALITY_KIND,
        "algorithm_id": ISOLATED_ANALYZER_ID,
        "algorithm_version": ISOLATED_ANALYZER_VERSION,
        "threshold_profile_fingerprint": (
            active_region_threshold_profile_fingerprint()
        ),
        "operation_fingerprint": operation_fingerprint,
        "issuance_id": issuance_id,
        "raw_artifact_fingerprint": raw_artifact.fingerprint,
        "analysis_artifact_fingerprint": analysis_artifact.fingerprint,
        "accepted": True,
        "issues": [],
    }
    quality_artifact = evidence_store.publish_json_artifact(
        f"{prefix}/quality.json",
        quality_payload,
    )
    capture_identity = CaptureIdentity(
        consumer_id=ACTIVE_ISOLATED_DRIVER_EVIDENCE_CONSUMER_ID,
        measurement_kind=ACTIVE_ISOLATED_DRIVER_MEASUREMENT_KIND,
        capture_id=f"capture-{issuance_id}",
        raw_artifact=raw_artifact,
        analysis_input_artifact=analysis_artifact,
        target_fingerprint=target.driver_target_fingerprint,
        context_fingerprint=context,
        geometry_id=REFERENCE_AXIS_GEOMETRY_ID,
        placement_fingerprint=placement_fingerprint,
        quality_artifact=quality_artifact,
        admission_artifact=playback.artifact,
    )
    admitted_capture = AdmittedIsolatedDriverCapture(
        authority=plan.authority,
        plan_fingerprint=plan.fingerprint,
        attempt=attempt,
        speaker_group_id=speaker_group_id,
        role=role,
        evidence_target_fingerprint=evidence_target,
        driver_target_id=target.driver_target_id,
        driver_target_fingerprint=target.driver_target_fingerprint,
        context_base_fingerprint=context_base,
        context_fingerprint=context,
        placement_fingerprint=placement_fingerprint,
        graph_fingerprint=handoff.graph_fingerprint,
        generation_protection_evidence_fingerprint=(
            generation_proof.evidence_fingerprint
        ),
        playback_protection_evidence_fingerprint=(playback_proof.evidence_fingerprint),
        admission_id=issuance_id,
        capture=capture_identity,
        stimulus=handoff.stimulus,
        generation_artifact=generation.artifact,
        playback_artifact=playback.artifact,
        generation_admission=generation.admission,
        playback_admission=playback.admission,
    )
    evidence_store.publish_admitted_isolated_driver_capture(
        admitted_capture,
        ordinal=len(existing),
    )
    captures = evidence_store.reopen_isolated_attempt_captures(attempt.attempt_id)
    if state == "unconfigured":
        if not run_store.transition(
            run,
            CommissioningTransition(
                from_state="unconfigured",
                to_state="protected",
                evidence_kind="protection_evidence",
                evidence_fingerprint=handoff.graph_evidence_fingerprint,
            ),
            attempt=attempt,
        ):
            raise IsolatedCapturePromotionError(
                "capture lost durable commissioning ownership"
            )
    complete: CompleteIsolatedDriverEvidence | None = None
    driver_complete = len(captures) == STATIONARY_CAPTURE_COUNT
    if driver_complete:
        _finalize_driver_if_ready(
            plan,
            target,
            attempt,
            captures,
            evidence_store,
        )
        complete = _complete_if_ready(plan, evidence_store)
    result = {
        "status": "complete" if complete is not None else "collecting",
        "accepted": len(captures),
        "required": STATIONARY_CAPTURE_COUNT,
        "driver_complete": driver_complete,
        "complete": complete is not None,
        "attempt_id": attempt.attempt_id,
        "capture_fingerprint": admitted_capture.fingerprint,
        "complete_fingerprint": complete.fingerprint if complete is not None else None,
    }
    log_event(
        logger,
        "active_speaker.isolated_driver_evidence_promoted",
        status=result["status"],
        run_id=run.run_id,
        attempt_id=attempt.attempt_id,
        speaker_group_id=speaker_group_id,
        role=role,
        accepted=len(captures),
        required=STATIONARY_CAPTURE_COUNT,
        driver_complete=driver_complete,
        complete=complete is not None,
        capture_fingerprint=admitted_capture.fingerprint,
        complete_fingerprint=result["complete_fingerprint"],
    )
    return result
