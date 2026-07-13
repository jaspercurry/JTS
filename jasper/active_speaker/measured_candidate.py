# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed Wave 2 boundary for future measured crossover selection.

Historical B2b captures predate production excitation admission and can never
enter this boundary.  Wave 2 also lacks the Shared persisted-admission and
fresh-protection API needed to authenticate new captures against Active's exact
current safety plan.  This module therefore publishes the complete evidence
contract and typed non-readiness states, but deliberately has no scoring,
candidate construction, persistence, apply, or playback entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jasper.audio_measurement.evidence_identity import json_fingerprint

from .commissioning_receipt import REFERENCE_AXIS_GEOMETRY_ID

SCHEMA_VERSION = 1
INPUT_CONTRACT_KIND = "jts_active_measured_candidate_input_contract"
READINESS_KIND = "jts_active_measured_candidate_readiness"


class MeasuredCandidateError(ValueError):
    """A measured-candidate readiness value is malformed or unsafe."""


class MeasuredCandidateRefusal(str, Enum):
    """Stable Wave 2 reasons that prevent any measured candidate score."""

    CAPTURE_NOT_ADMITTED = "measured_candidate_capture_not_admitted"
    SHARED_PERSISTED_ADMISSION_UNAVAILABLE = (
        "measured_candidate_shared_persisted_admission_unavailable"
    )
    CURRENT_PROTECTION_PROOF_MISSING = (
        "measured_candidate_current_protection_proof_missing"
    )
    DRIVER_CAPTURES_MISSING = "measured_candidate_driver_captures_missing"
    MEASURED_VALIDITY_BAND_MISSING = (
        "measured_candidate_measured_validity_band_missing"
    )
    DELAY_WALK_MISSING = "measured_candidate_delay_walk_missing"
    NORMAL_EVIDENCE_MISSING = "measured_candidate_normal_evidence_missing"
    REVERSE_EVIDENCE_MISSING = "measured_candidate_reverse_evidence_missing"
    NULL_EVIDENCE_MISSING = "measured_candidate_null_evidence_missing"
    TOPOLOGY_GRAPH_PROOF_MISSING = (
        "measured_candidate_topology_graph_proof_missing"
    )
    CANDIDATE_PUBLICATION_DISABLED = (
        "measured_candidate_publication_disabled_in_wave2"
    )


@dataclass(frozen=True, init=False)
class MeasuredCandidateInputContract:
    """Versioned prerequisites that must all precede frequency/family scoring.

    This is a declaration, not an admission implementation.  In particular it
    does not accept capture objects or protection evidence, so no caller can
    use it to turn a content fingerprint into live authority.
    """

    fixed_axis_geometry_id: str
    stationary_evidence_roles: tuple[str, ...]
    stationary_capture_count_per_target: int
    null_capture_count_per_delay: int
    capture_distinctness: str
    delay_step_range_us: tuple[int, int]
    delay_bound: str
    measured_search_band_rule: str
    placement_scope: str
    graph_scope: str
    admission_scope: str
    candidate_output_enabled: bool
    fingerprint: str = field(init=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("use measured_candidate_input_contract")

    @classmethod
    def _wave2(cls) -> "MeasuredCandidateInputContract":
        self = object.__new__(cls)
        object.__setattr__(self, "fixed_axis_geometry_id", REFERENCE_AXIS_GEOMETRY_ID)
        object.__setattr__(
            self,
            "stationary_evidence_roles",
            ("isolated_driver", "combined_normal", "combined_reverse"),
        )
        object.__setattr__(self, "stationary_capture_count_per_target", 3)
        object.__setattr__(self, "null_capture_count_per_delay", 5)
        object.__setattr__(
            self,
            "capture_distinctness",
            "unique_capture_and_artifact_fingerprints_within_run",
        )
        object.__setattr__(self, "delay_step_range_us", (50, 100))
        object.__setattr__(self, "delay_bound", "declared_geometry_plus_minus_half_period")
        object.__setattr__(
            self,
            "measured_search_band_rule",
            "profile_intersection_tightened_by_per_band_validity_and_snr",
        )
        object.__setattr__(
            self,
            "placement_scope",
            "one_exact_fixed_axis_placement_per_topology_derived_group",
        )
        object.__setattr__(
            self,
            "graph_scope",
            "exact_topology_wide_routing_filters_gain_protection_and_nonpositive_volume",
        )
        object.__setattr__(
            self,
            "admission_scope",
            "fresh_persisted_planner_and_playback_recheck_against_current_active_safety_plan",
        )
        object.__setattr__(self, "candidate_output_enabled", False)
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))
        return self

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": INPUT_CONTRACT_KIND,
            "fixed_axis_geometry_id": self.fixed_axis_geometry_id,
            "stationary_evidence_roles": list(self.stationary_evidence_roles),
            "stationary_capture_count_per_target": (
                self.stationary_capture_count_per_target
            ),
            "null_capture_count_per_delay": self.null_capture_count_per_delay,
            "capture_distinctness": self.capture_distinctness,
            "delay_step_range_us": list(self.delay_step_range_us),
            "delay_bound": self.delay_bound,
            "measured_search_band_rule": self.measured_search_band_rule,
            "placement_scope": self.placement_scope,
            "graph_scope": self.graph_scope,
            "admission_scope": self.admission_scope,
            "candidate_output_enabled": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


def measured_candidate_input_contract() -> MeasuredCandidateInputContract:
    """Return the immutable Wave 2 evidence contract."""

    return MeasuredCandidateInputContract._wave2()


@dataclass(frozen=True, init=False)
class MeasuredCandidateReadiness:
    """A permanently non-authoritative Wave 2 readiness projection."""

    source_classification: str
    refusals: tuple[MeasuredCandidateRefusal, ...]
    input_contract: MeasuredCandidateInputContract
    fingerprint: str = field(init=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("use a measured candidate readiness factory")

    @classmethod
    def _not_ready(
        cls,
        *,
        source_classification: str,
        refusals: tuple[MeasuredCandidateRefusal, ...],
    ) -> "MeasuredCandidateReadiness":
        if (
            not isinstance(source_classification, str)
            or not source_classification
            or source_classification != source_classification.strip()
        ):
            raise MeasuredCandidateError(
                "source_classification must be non-empty trimmed text"
            )
        if (
            type(refusals) is not tuple
            or not refusals
            or any(not isinstance(reason, MeasuredCandidateRefusal) for reason in refusals)
            or len(set(refusals)) != len(refusals)
            or MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED not in refusals
        ):
            raise MeasuredCandidateError(
                "non-ready state requires unique typed refusals and publication block"
            )
        self = object.__new__(cls)
        object.__setattr__(self, "source_classification", source_classification)
        object.__setattr__(self, "refusals", refusals)
        object.__setattr__(self, "input_contract", measured_candidate_input_contract())
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))
        return self

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": READINESS_KIND,
            "source_classification": self.source_classification,
            "input_contract": self.input_contract.to_dict(),
            "ready": False,
            "score_available": False,
            "candidate_authority": False,
            "persistable_candidate": False,
            "apply_authority": False,
            "receipt_authority": False,
            "refusals": [reason.value for reason in self.refusals],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


def legacy_measured_candidate_readiness() -> MeasuredCandidateReadiness:
    """Classify historical B2b evidence as permanently non-admitted."""

    return MeasuredCandidateReadiness._not_ready(
        source_classification="historical_legacy_non_admitted",
        refusals=(
            MeasuredCandidateRefusal.CAPTURE_NOT_ADMITTED,
            MeasuredCandidateRefusal.DRIVER_CAPTURES_MISSING,
            MeasuredCandidateRefusal.MEASURED_VALIDITY_BAND_MISSING,
            MeasuredCandidateRefusal.DELAY_WALK_MISSING,
            MeasuredCandidateRefusal.NORMAL_EVIDENCE_MISSING,
            MeasuredCandidateRefusal.REVERSE_EVIDENCE_MISSING,
            MeasuredCandidateRefusal.NULL_EVIDENCE_MISSING,
            MeasuredCandidateRefusal.TOPOLOGY_GRAPH_PROOF_MISSING,
            MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED,
        ),
    )


def wave2_measured_candidate_readiness() -> MeasuredCandidateReadiness:
    """Report the pending Shared boundary without accepting substitute proof."""

    return MeasuredCandidateReadiness._not_ready(
        source_classification="wave2_shared_boundary_pending",
        refusals=(
            MeasuredCandidateRefusal.SHARED_PERSISTED_ADMISSION_UNAVAILABLE,
            MeasuredCandidateRefusal.CURRENT_PROTECTION_PROOF_MISSING,
            MeasuredCandidateRefusal.TOPOLOGY_GRAPH_PROOF_MISSING,
            MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED,
        ),
    )
