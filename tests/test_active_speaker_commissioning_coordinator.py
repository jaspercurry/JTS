# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.active_speaker.commissioning_coordinator import (
    build_commissioning_view,
    summed_test_failure_message,
)

from tests.active_speaker_fixtures import mono_output_topology as _topology


def _ready_design() -> dict:
    return {
        "kind": "jts_active_speaker_design_draft",
        "status": "ready_for_review",
        "summary": {
            "missing_driver_info_roles": [],
            "missing_crossover_candidate_pairs": [],
        },
    }


def _ready_preview() -> dict:
    return {
        "kind": "jts_active_speaker_crossover_preview",
        "status": "ready_for_protected_staging",
        "permissions": {"may_prepare_protected_startup_config": True},
    }


def test_summed_test_failure_message_prioritizes_artifact_permission_failure():
    message = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "tone_backend_failed",
            "message": "Permission denied",
        },
        {
            "severity": "blocker",
            "code": "summed_test_output_mismatch",
            "message": "wrong outputs",
        },
    ])

    assert "could not prepare the combined test audio" in message
    assert "Confirm outputs" not in message


def test_commissioning_view_exposes_combined_test_as_next_action():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "validated_summed_group_count": 0,
                "required_summed_group_count": 1,
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
    )

    assert view["status"] == "needs_combined_check"
    assert view["next_action"]["id"] == "start_combined_test"
    group = view["combined_groups"][0]
    assert group["status"] == "ready_to_test"
    assert group["actions"]["start_combined_test"]["enabled"] is True
    assert group["actions"]["start_combined_test"]["body"]["level_dbfs"] == -80.0
    assert group["actions"]["start_combined_test"]["body"]["stimulus"] == "speech"
    assert group["actions"]["start_combined_test"]["body"]["duration_ms"] == 12000


def test_commissioning_view_records_result_after_audible_combined_test():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "validated_summed_group_count": 0,
                "required_summed_group_count": 1,
                "latest_summed_tests": {
                    "mono": {
                        "captured": True,
                        "audio_emitted": True,
                        "summed_test_id": "summed-playback-audible",
                        "tone": {"level_dbfs": -74.0},
                        "issues": [],
                    },
                },
                "latest_summed_validations": {},
            },
        },
        calibration_level={
            "test_signal": {
                "requested_level_dbfs": -68.0,
                "min_level_dbfs": -80.0,
                "max_level_dbfs": 0.0,
                "step_db": 1.0,
            },
            "software_gain_guard": {
                "upward_step_limit_db": 6.0,
            },
        },
    )

    assert view["next_action"]["id"] == "record_combined_result"
    assert view["test_level"]["requested_level_dbfs"] == -74.0
    group = view["combined_groups"][0]
    assert group["status"] == "ready_to_record"
    assert group["test_level"]["requested_level_dbfs"] == -74.0
    assert group["actions"]["record_combined_result"]["enabled"] is True
    assert group["actions"]["record_combined_result"]["body"] == {
        "speaker_group_id": "mono",
        "summed_test_id": "summed-playback-audible",
        "operator_listening_check": True,
    }


def test_commissioning_view_does_not_reoffer_record_for_validated_combined_test():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": True,
                "validated_summed_group_count": 1,
                "required_summed_group_count": 1,
                "latest_summed_tests": {
                    "mono": {
                        "captured": True,
                        "audio_emitted": True,
                        "summed_test_id": "summed-playback-audible",
                        "issues": [],
                    },
                },
                "latest_summed_validations": {
                    "mono": {
                        "validated": True,
                        "summed_test_id": "summed-playback-audible",
                    },
                },
            },
        },
    )

    group = view["combined_groups"][0]
    assert group["status"] == "validated"
    assert group["actions"]["record_combined_result"]["enabled"] is False
    assert view["next_action"]["id"] == "save_profile"
    assert (
        view["next_action"]["endpoint"]
        == "./active-speaker/baseline-profile/save-and-apply"
    )


def test_commissioning_view_ignores_stale_combined_validation_for_newer_test():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": True,
                "validated_summed_group_count": 1,
                "required_summed_group_count": 1,
                "latest_summed_tests": {
                    "mono": {
                        "captured": True,
                        "audio_emitted": True,
                        "summed_test_id": "summed-playback-newer",
                        "issues": [],
                    },
                },
                "latest_summed_validations": {
                    "mono": {
                        "validated": True,
                        "summed_test_id": "summed-playback-audible",
                    },
                },
            },
        },
    )

    assert view["status"] == "needs_combined_check"
    assert view["next_action"]["id"] == "record_combined_result"
    group = view["combined_groups"][0]
    assert group["status"] == "ready_to_record"
    assert group["validated"] is False
    assert group["actions"]["record_combined_result"]["enabled"] is True
    assert group["actions"]["record_combined_result"]["body"]["summed_test_id"] == (
        "summed-playback-newer"
    )


def test_commissioning_view_surfaces_superseded_profile_revalidation():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "validated_summed_group_count": 0,
                "required_summed_group_count": 1,
                "latest_summed_tests": {
                    "mono": {
                        "captured": True,
                        "audio_emitted": True,
                        "summed_test_id": "summed-playback-newer",
                        "issues": [],
                    },
                },
                "latest_summed_validations": {
                    "mono": {
                        "validated": True,
                        "summed_test_id": "summed-playback-audible",
                    },
                },
            },
        },
        baseline_profile={
            "status": "blocked",
            "revalidation": {
                "required": True,
                "reason": "applied_profile_superseded",
                "next_step": "combined_check",
            },
        },
    )

    assert view["status"] == "needs_revalidation"
    assert view["revalidation"]["required"] is True
    assert view["next_action"]["id"] == "record_combined_result"
    profile_step = next(step for step in view["steps"] if step["id"] == "profile")
    assert "Save and apply a fresh profile" in profile_step["message"]


def test_commissioning_view_allows_applied_profile_edit_to_revalidate():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": False,
                "driver_measurements_complete": False,
                "captured_driver_check_count": 0,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "validated_summed_group_count": 0,
                "required_summed_group_count": 1,
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
        baseline_profile={
            "status": "blocked",
            "revalidation": {
                "required": True,
                "reason": "applied_profile_superseded",
                "next_step": "combined_check",
                "superseded_profile": {"status": "applied"},
            },
        },
    )

    assert view["status"] == "needs_revalidation"
    assert view["current_step"] == "safety"
    assert view["driver_target_proof"]["complete"] is True
    assert view["driver_target_proof"]["source"] == "applied_profile_revalidation"
    assert view["driver_checks"]["complete"] is True
    assert view["driver_checks"]["source"] == "applied_profile_revalidation"
    safety_step = next(step for step in view["steps"] if step["id"] == "safety")
    assert safety_step["status"] == "active"
    profile_step = next(step for step in view["steps"] if step["id"] == "profile")
    assert profile_step["status"] == "todo"
    group = view["combined_groups"][0]
    assert group["status"] == "ready_to_test"
    assert group["actions"]["start_combined_test"]["enabled"] is True
    assert view["next_action"]["id"] == "start_combined_test"


def test_commissioning_view_new_setup_stays_on_confirm_outputs_until_driver_proof():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": False,
                "driver_measurements_complete": False,
                "captured_driver_check_count": 0,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "validated_summed_group_count": 0,
                "required_summed_group_count": 1,
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
    )

    assert view["status"] == "needs_driver_target_proof"
    assert view["current_step"] == "map"
    assert view["driver_target_proof"]["complete"] is False
    assert view["driver_target_proof"]["source"] == "missing"
    assert view["driver_checks"]["complete"] is False
    assert view["driver_checks"]["source"] == "missing"
    profile_step = next(step for step in view["steps"] if step["id"] == "profile")
    assert profile_step["status"] == "todo"
    group = view["combined_groups"][0]
    assert group["status"] == "blocked"
    assert group["actions"]["start_combined_test"]["enabled"] is False
    assert view["next_action"]["id"] == "confirm_outputs"


def test_commissioning_view_setup_check_revalidation_stays_on_confirm_outputs():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": False,
                "driver_measurements_complete": False,
                "summed_validation_complete": False,
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
        baseline_profile={
            "status": "blocked",
            "revalidation": {
                "required": True,
                "reason": "applied_profile_superseded",
                "next_step": "setup_checks",
                "superseded_profile": {"status": "applied"},
            },
        },
    )

    assert view["current_step"] == "map"
    assert view["driver_target_proof"]["complete"] is False
    assert view["driver_checks"]["complete"] is False
    assert view["next_action"]["id"] == "confirm_outputs"


def test_commissioning_view_topology_change_invalidates_applied_profile_driver_proof():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": False,
                "driver_measurements_complete": False,
                "summed_validation_complete": False,
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
        baseline_profile={
            "status": "blocked",
            "revalidation": {
                "required": True,
                "reason": "applied_profile_superseded",
                "next_step": "combined_check",
                "changed": ["topology_fingerprint", "measurement_summary_fingerprint"],
                "superseded_profile": {"status": "applied"},
            },
        },
    )

    assert view["current_step"] == "map"
    assert view["driver_target_proof"]["complete"] is False
    assert view["driver_target_proof"]["source"] == "missing"
    assert view["next_action"]["id"] == "confirm_outputs"


def test_commissioning_view_uses_backend_failure_copy_for_combined_group():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "latest_summed_tests": {
                    "mono": {
                        "captured": False,
                        "audio_emitted": False,
                        "issues": [
                            {
                                "severity": "blocker",
                                "code": "tone_backend_failed",
                                "message": "Permission denied",
                            },
                            {
                                "severity": "blocker",
                                "code": "summed_test_output_mismatch",
                                "message": "wrong outputs",
                            },
                        ],
                    },
                },
                "latest_summed_validations": {},
            },
        },
    )

    group = view["combined_groups"][0]
    assert group["status"] == "test_failed"
    assert "could not prepare the combined test audio" in group["message"]
    assert "Confirm outputs" not in group["message"]


def test_commissioning_view_blocks_output_confirmation_until_values_are_ready():
    view = build_commissioning_view(
        _topology(),
        design_draft={
            "kind": "jts_active_speaker_design_draft",
            "status": "needs_research",
            "summary": {
                "missing_driver_info_roles": ["tweeter"],
                "missing_crossover_candidate_pairs": [["woofer", "tweeter"]],
            },
        },
        crossover_preview={"status": "not_prepared"},
        measurements={
            "summary": {
                "driver_checks_complete": False,
                "summed_validation_complete": False,
            },
        },
    )

    assert view["status"] == "needs_driver_values"
    assert view["current_step"] == "research"
    assert view["driver_values"]["complete"] is False
    assert view["next_action"]["id"] == "save_driver_values"
    map_step = next(step for step in view["steps"] if step["id"] == "map")
    assert map_step["status"] == "todo"


def test_commissioning_view_requires_crossover_preview_after_saved_values():
    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview={"status": "not_prepared"},
        measurements={
            "summary": {
                "driver_checks_complete": False,
                "summed_validation_complete": False,
            },
        },
    )

    assert view["status"] == "needs_driver_values"
    assert view["current_step"] == "research"
    assert view["driver_values"]["design_ready"] is True
    assert view["driver_values"]["preview_ready"] is False
    assert view["next_action"]["id"] == "preview_crossover"


def test_research_next_action_endpoints_match_frontend_footer_dispatch():
    """Pin the Python↔JS seam the /sound/ research footer depends on.

    deploy/assets/sound-profile/js/active-speaker-ui.js (nextActionAct) maps a
    research-step next_action to a click `data-act` purely by substring-matching
    the endpoint: "/design-draft" -> save the draft, "/crossover-preview" ->
    prepare the preview. If the coordinator ever renames these endpoints the
    footer would silently fall back to "Continue", so assert the substrings
    here (the JS side is pinned by tests/js/active_speaker_ui_test.mjs).
    """

    save_values = build_commissioning_view(
        _topology(),
        design_draft={"status": "not_saved"},
        measurements={"summary": {"driver_checks_complete": False}},
    )["next_action"]
    assert save_values["id"] == "save_driver_values"
    assert "/design-draft" in save_values["endpoint"]

    preview = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview={"status": "not_prepared"},
        measurements={"summary": {"driver_checks_complete": False}},
    )["next_action"]
    assert preview["id"] == "preview_crossover"
    assert "/crossover-preview" in preview["endpoint"]
