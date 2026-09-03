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
import statistics
from collections.abc import Mapping
from typing import Any

import yaml

from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.evidence_identity import (
    NormalizedActiveRawIdentity,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from .baseline_profile import recompose_applied_baseline_yaml
from .commissioning_evidence import (
    STATIONARY_CAPTURE_COUNT,
    AdmittedIsolatedDriverCapture,
    CompleteIsolatedDriverEvidence,
    DriverEvidenceTarget,
    IsolatedDriverEvidence,
    RegionEvidencePlan,
    active_region_context_fingerprint,
    active_region_threshold_profile_fingerprint,
    derive_region_evidence_plan,
    isolated_driver_attempt_target_id,
    isolated_driver_evidence_target_fingerprint,
)
from .commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    is_missing,
)
from .commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningRunHandle,
    CommissioningRunStore,
)
from .crossover_contract import (
    preset_matches_applied_profile,
)
from .measured_candidate import (
    ISOLATED_ANALYZER_ID,
    ISOLATED_ANALYZER_VERSION,
)
from .profile import ActiveSpeakerPreset


class IsolatedCapturePromotionError(ValueError):
    """One relay capture cannot enter automatic commissioning authority."""


logger = logging.getLogger(__name__)


def _reopen_region_evidence_plan_for_baseline(
    *,
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    comparison_set: Mapping[str, Any],
    calibration_id: str,
    calibration: CalibrationCurve,
    protected_safety_profile_fingerprint: str,
    baseline_active_raw_fingerprint: str,
    run: CommissioningRunHandle,
    evidence_store: CommissioningEvidenceStore,
    publish_if_missing: bool,
) -> RegionEvidencePlan:
    try:
        context = active_region_context_fingerprint(
            baseline_active_raw_fingerprint=baseline_active_raw_fingerprint,
            calibration_id=calibration_id,
            calibration=calibration,
        )
        expected = derive_region_evidence_plan(
            preset,
            topology,
            run=run,
            protected_safety_profile_fingerprint=(
                protected_safety_profile_fingerprint
            ),
            comparison_set_fingerprint=str(comparison_set.get("fingerprint") or ""),
            threshold_profile_fingerprint=(
                active_region_threshold_profile_fingerprint()
            ),
            context_fingerprint=context,
        )
    except (TypeError, ValueError) as exc:
        raise IsolatedCapturePromotionError(
            "capture baseline or calibration context is invalid"
        ) from exc
    try:
        existing = evidence_store.reopen_region_evidence_plan(run=run)
    except CommissioningEvidenceStoreError as exc:
        if not publish_if_missing or not is_missing(exc):
            raise
        evidence_store.publish_region_evidence_plan(expected)
        existing = evidence_store.reopen_region_evidence_plan(run=run)
    if existing != expected:
        raise IsolatedCapturePromotionError(
            "current capture authority differs from the durable commissioning plan"
        )
    return existing


def reopen_region_evidence_plan_for_baseline(
    *,
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    comparison_set: Mapping[str, Any],
    calibration_id: str,
    calibration: CalibrationCurve,
    protected_safety_profile_fingerprint: str,
    baseline_active_raw_fingerprint: str,
    run: CommissioningRunHandle,
    evidence_store: CommissioningEvidenceStore,
) -> RegionEvidencePlan:
    """Reopen a durable plan against its exact captured baseline identity."""

    return _reopen_region_evidence_plan_for_baseline(
        topology=topology,
        preset=preset,
        comparison_set=comparison_set,
        calibration_id=calibration_id,
        calibration=calibration,
        protected_safety_profile_fingerprint=protected_safety_profile_fingerprint,
        baseline_active_raw_fingerprint=baseline_active_raw_fingerprint,
        run=run,
        evidence_store=evidence_store,
        publish_if_missing=False,
    )


def current_region_evidence_plan(
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
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise IsolatedCapturePromotionError(
            "capture baseline or calibration context is invalid"
        ) from exc
    return _reopen_region_evidence_plan_for_baseline(
        topology=topology,
        preset=preset,
        comparison_set=comparison_set,
        calibration_id=calibration_id,
        calibration=calibration,
        protected_safety_profile_fingerprint=protected_safety_profile_fingerprint,
        baseline_active_raw_fingerprint=baseline,
        run=run,
        evidence_store=evidence_store,
        publish_if_missing=True,
    )


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
            if is_missing(exc):
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
        if is_missing(exc):
            return None
        raise
    try:
        evidence_store.complete_isolated_driver_evidence_fingerprint(
            run_id=run.run_id
        )
    except CommissioningEvidenceStoreError as exc:
        if not is_missing(exc):
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
            if not is_missing(exc):
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
        if is_missing(exc):
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
            if not is_missing(exc):
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
        if not is_missing(exc):
            raise
        complete_fingerprint = None
    return {
        "status": "complete" if complete_fingerprint is not None else "collecting",
        "plan_fingerprint": plan.fingerprint,
        "drivers": drivers,
        "complete_fingerprint": complete_fingerprint,
    }


