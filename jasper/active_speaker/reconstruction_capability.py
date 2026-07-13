# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Typed, fail-closed low-frequency reconstruction capability boundaries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .legacy_replay import LEGACY_EVIDENCE_CLASSIFICATION

RECONSTRUCTION_MODEL_ID = "sealed_single_radiator_v1"
RECONSTRUCTION_THRESHOLD_SET_ID = RECONSTRUCTION_MODEL_ID


class ReconstructionRefusal(StrEnum):
    PROFILE_UNCONFIRMED = "reconstruction_profile_unconfirmed"
    PROFILE_STALE = "reconstruction_profile_stale"
    TARGET_MISMATCH = "reconstruction_target_mismatch"
    TOPOLOGY_MISMATCH = "reconstruction_topology_mismatch"
    GEOMETRY_BINDING_MISMATCH = "reconstruction_geometry_binding_mismatch"
    PLACEMENT_BINDING_MISMATCH = "reconstruction_placement_binding_mismatch"
    APPLIED_CROSSOVER_MISMATCH = "reconstruction_applied_crossover_mismatch"
    CALIBRATION_MISMATCH = "reconstruction_calibration_mismatch"
    CAPTURE_NOT_ADMITTED = "reconstruction_capture_not_admitted"
    CAPTURE_QUALITY_REFUSED = "reconstruction_capture_quality_refused"
    CAPTURE_SNR_INSUFFICIENT = "reconstruction_capture_snr_insufficient"
    ENCLOSURE_UNSUPPORTED = "reconstruction_enclosure_unsupported"
    SOURCE_COUNT_UNSUPPORTED = "reconstruction_source_count_unsupported"
    GEOMETRY_MISSING = "reconstruction_geometry_missing"
    GEOMETRY_MODEL_DOMAIN_UNSUPPORTED = (
        "reconstruction_geometry_model_domain_unsupported"
    )
    NEAR_FIELD_DISTANCE_MISSING = "reconstruction_near_field_distance_missing"
    NEAR_FIELD_DISTANCE_OUT_OF_RANGE = (
        "reconstruction_near_field_distance_out_of_range"
    )
    FAR_FIELD_DISTANCE_MISSING = "reconstruction_far_field_distance_missing"
    FAR_FIELD_DISTANCE_OUT_OF_RANGE = (
        "reconstruction_far_field_distance_out_of_range"
    )
    FAR_FIELD_VALIDITY_FLOOR_UNKNOWN = (
        "reconstruction_far_field_validity_floor_unknown"
    )
    OVERLAP_MISSING = "reconstruction_overlap_missing"
    OVERLAP_TOO_NARROW = "reconstruction_overlap_too_narrow"
    OVERLAP_TRANSITION_UNCOVERED = "reconstruction_overlap_transition_uncovered"
    OVERLAP_NON_FINITE = "reconstruction_overlap_non_finite"
    OVERLAP_RMS_EXCEEDED = "reconstruction_overlap_rms_exceeded"
    OVERLAP_PEAK_EXCEEDED = "reconstruction_overlap_peak_exceeded"
    OVERLAP_SLOPE_EXCEEDED = "reconstruction_overlap_slope_exceeded"
    DECISION_BAND_UNCOVERED = "reconstruction_decision_band_uncovered"


@dataclass(frozen=True)
class ReconstructionCapability:
    """A classification result, never an apply or playback capability."""

    refusals: tuple[ReconstructionRefusal, ...]
    evidence_classification: str

    @property
    def ready(self) -> bool:
        return not self.refusals

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": RECONSTRUCTION_MODEL_ID,
            "threshold_set_id": RECONSTRUCTION_THRESHOLD_SET_ID,
            "ready": self.ready,
            "refusals": [item.value for item in self.refusals],
            "evidence_classification": self.evidence_classification,
            "authoritative": False,
            "authorizes_splice": False,
            "authorizes_candidate": False,
            "authorizes_apply": False,
            "authorizes_verification": False,
            "authorizes_receipt": False,
            "authorizes_playback": False,
        }


def cabinet_refusals(
    cabinet: Mapping[str, Any] | None,
) -> tuple[ReconstructionRefusal, ...]:
    """Return what the frozen safety-profile cabinet block cannot prove."""

    if not isinstance(cabinet, Mapping):
        return (ReconstructionRefusal.GEOMETRY_MISSING,)
    enclosure = cabinet.get("enclosure_kind")
    if enclosure != "sealed":
        return (ReconstructionRefusal.ENCLOSURE_UNSUPPORTED,)
    if type(cabinet.get("radiator_count")) is not int or cabinet.get(
        "radiator_count"
    ) != 1:
        return (ReconstructionRefusal.SOURCE_COUNT_UNSUPPORTED,)
    diameter = cabinet.get("effective_radiating_diameter_mm")
    width = cabinet.get("baffle_width_mm")
    if (
        isinstance(diameter, bool)
        or not isinstance(diameter, (int, float))
        or not math.isfinite(float(diameter))
        or float(diameter) <= 0.0
        or isinstance(width, bool)
        or not isinstance(width, (int, float))
        or not math.isfinite(float(width))
        or float(width) <= 0.0
    ):
        return (ReconstructionRefusal.GEOMETRY_MISSING,)
    # Baffle height is intentionally absent from the frozen profile. A
    # separately versioned, target-bound geometry artifact is not shipped yet.
    return (ReconstructionRefusal.GEOMETRY_MISSING,)


def legacy_reconstruction_capability(
    cabinet: Mapping[str, Any] | None,
) -> ReconstructionCapability:
    """Classify a historical replay without ever upgrading its evidence."""

    refusals = list(cabinet_refusals(cabinet))
    if ReconstructionRefusal.CAPTURE_NOT_ADMITTED not in refusals:
        refusals.append(ReconstructionRefusal.CAPTURE_NOT_ADMITTED)
    return ReconstructionCapability(
        refusals=tuple(refusals),
        evidence_classification=LEGACY_EVIDENCE_CLASSIFICATION,
    )
