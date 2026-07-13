# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.active_speaker.measured_candidate import (
    MeasuredCandidateInputContract,
    MeasuredCandidateReadiness,
    MeasuredCandidateRefusal,
    legacy_measured_candidate_readiness,
    measured_candidate_input_contract,
    wave2_measured_candidate_readiness,
)


def test_legacy_state_is_permanently_non_ready_and_non_authoritative():
    payload = legacy_measured_candidate_readiness().to_dict()
    assert payload["source_classification"] == "historical_legacy_non_admitted"
    assert payload["ready"] is False
    assert payload["score_available"] is False
    assert payload["candidate_authority"] is False
    assert payload["persistable_candidate"] is False
    assert payload["apply_authority"] is False
    assert payload["receipt_authority"] is False
    assert MeasuredCandidateRefusal.CAPTURE_NOT_ADMITTED.value in payload["refusals"]
    assert "candidate" not in payload
    assert "selected_evaluation" not in payload


def test_wave2_refuses_scoring_until_shared_persisted_authority_exists():
    payload = wave2_measured_candidate_readiness().to_dict()
    assert payload["source_classification"] == "wave2_shared_boundary_pending"
    assert payload["ready"] is False
    assert payload["score_available"] is False
    assert payload["input_contract"]["candidate_output_enabled"] is False
    assert payload["refusals"] == [
        MeasuredCandidateRefusal.SHARED_PERSISTED_ADMISSION_UNAVAILABLE.value,
        MeasuredCandidateRefusal.CURRENT_PROTECTION_PROOF_MISSING.value,
        MeasuredCandidateRefusal.TOPOLOGY_GRAPH_PROOF_MISSING.value,
        MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED.value,
    ]


def test_input_contract_pins_all_pre_score_evidence_classes():
    payload = measured_candidate_input_contract().to_dict()
    assert payload["stationary_evidence_roles"] == [
        "isolated_driver",
        "combined_normal",
        "combined_reverse",
    ]
    assert payload["stationary_capture_count_per_target"] == 3
    assert payload["null_capture_count_per_delay"] == 5
    assert payload["capture_distinctness"] == (
        "unique_capture_and_artifact_fingerprints_within_run"
    )
    assert payload["delay_step_range_us"] == [50, 100]
    assert payload["delay_bound"] == "declared_geometry_plus_minus_half_period"
    assert payload["measured_search_band_rule"] == (
        "profile_intersection_tightened_by_per_band_validity_and_snr"
    )
    assert payload["placement_scope"] == (
        "one_exact_fixed_axis_placement_per_topology_derived_group"
    )
    assert payload["graph_scope"] == (
        "exact_topology_wide_routing_filters_gain_protection_and_nonpositive_volume"
    )
    assert payload["admission_scope"] == (
        "fresh_persisted_planner_and_playback_recheck_against_current_active_safety_plan"
    )


def test_contract_and_readiness_types_cannot_be_directly_forged():
    with pytest.raises(TypeError, match="measured_candidate_input_contract"):
        MeasuredCandidateInputContract()
    with pytest.raises(TypeError, match="readiness factory"):
        MeasuredCandidateReadiness()
