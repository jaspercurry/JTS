# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.active_speaker.commissioning_coordinator import (
    _SUMMED_TEST_FAILURE_COPY,
    COMMISSIONING_STEP_PAGE_TITLES,
    STEP_STATUS_NOT_REQUIRED,
    VIEW_STATUS_NOT_REQUIRED,
    build_commissioning_view,
    summed_test_failure_message,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

from tests.active_speaker_fixtures import mono_output_topology as _topology


def _step(view: dict, step_id: str) -> dict:
    return next(step for step in view["steps"] if step["id"] == step_id)


def _passive_stereo_topology() -> OutputTopology:
    """Full-range passive left+right mains with no subwoofer."""

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_stereo_passive",
        "name": "Bench stereo passive",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [
                    {
                        "role": "full_range",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                ],
            },
            {
                "id": "right",
                "label": "Right",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [
                    {
                        "role": "full_range",
                        "physical_output_index": 1,
                        "identity_verified": True,
                    },
                ],
            },
        ],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    })


def _no_measurements() -> dict:
    """The measurement summary a passive speaker actually has: nothing."""

    return {
        "summary": {
            "driver_checks_complete": False,
            "driver_measurements_complete": False,
            "captured_driver_check_count": 0,
            "required_driver_check_count": 0,
            "summed_validation_complete": False,
            "validated_summed_group_count": 0,
            "required_summed_group_count": 0,
            "latest_summed_tests": {},
            "latest_summed_validations": {},
        },
    }


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
    """A backend that never played beats a mismatch measured from what played.

    `tone_backend_failed` shares the quiet-test family's sentence — the remedy
    is the same two steps, and that sentence names the control to press — so
    what is pinned here is the PRECEDENCE, not which family owns the copy.
    """

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

    assert "Press Play combined test to retry" in message
    assert "Confirm outputs" not in message
    assert "did not match the saved speaker outputs" not in message


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
    assert "Press Play combined test to retry" in group["message"]
    assert "Confirm outputs" not in group["message"]
    assert "did not match the saved speaker outputs" not in group["message"]


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


@pytest.mark.parametrize(
    "make_topology",
    [
        pytest.param(lambda: _topology(mode="full_range_passive"), id="mono"),
        pytest.param(_passive_stereo_topology, id="stereo"),
    ],
)
def test_subless_passive_layout_terminates_the_ladder_after_confirm_outputs(
    make_topology,
):
    """A full-range passive speaker finishes at Confirm outputs.

    It has no inter-driver crossover and no bass-management split, so the last
    two rungs (combined driver test, active speaker profile) can never run.
    Before this, the view dead-ended: `safety` sat "active" forever, `profile`
    sat "todo", and `next_action` was `{}` — the coordinator's shape for "no
    idea", which is what a household saw on jts4.
    """

    view = build_commissioning_view(
        make_topology(),
        design_draft={"status": "not_saved"},
        crossover_preview={"status": "not_prepared"},
        measurements=_no_measurements(),
    )

    assert view["status"] == VIEW_STATUS_NOT_REQUIRED
    assert view["driver_target_proof"]["complete"] is True
    assert view["driver_target_proof"]["source"] == "not_required"
    assert _step(view, "map")["status"] == "done"
    assert _step(view, "safety")["status"] == STEP_STATUS_NOT_REQUIRED
    assert _step(view, "profile")["status"] == STEP_STATUS_NOT_REQUIRED
    # The flow points at the last step this speaker can actually reach, not at
    # a rung it will never run.
    assert view["current_step"] == "map"
    assert view["next_action"]["id"] == "setup_complete"
    assert view["next_action"]["enabled"] is False
    assert view["combined_groups"] == []


@pytest.mark.parametrize(
    "make_topology",
    [
        pytest.param(lambda: _topology(mode="full_range_passive"), id="mono"),
        pytest.param(_passive_stereo_topology, id="stereo"),
    ],
)
def test_subless_passive_layout_never_claims_unheld_driver_evidence(make_topology):
    """No step may claim drivers were confirmed when zero checks were required.

    `driver_target_proof` reports `captured: 0` with `source: "not_required"`,
    so "All assigned outputs and drivers are confirmed" was a verification
    claim nothing backed, and the safety step then contradicted it outright.
    """

    view = build_commissioning_view(
        make_topology(),
        design_draft={"status": "not_saved"},
        crossover_preview={"status": "not_prepared"},
        measurements=_no_measurements(),
    )

    assert view["driver_target_proof"]["captured"] == 0
    map_message = _step(view, "map")["message"]
    assert "drivers are confirmed" not in map_message
    assert "needs no separate driver listening checks" in map_message
    # The safety step must not tell the household to go back and confirm
    # outputs that the map step just reported confirmed.
    safety_message = _step(view, "safety")["message"]
    assert "Confirm each output and driver" not in safety_message
    assert safety_message == "No combined driver test applies to this layout."
    assert _step(view, "profile")["message"] == (
        "No active speaker profile is needed for this layout."
    )


def test_passive_layout_still_owes_output_identity_before_it_is_complete():
    """Terminating the ladder must not skip the one check passive DOES owe."""

    view = build_commissioning_view(
        _topology(mode="full_range_passive", identity_verified=False),
        design_draft={"status": "not_saved"},
        crossover_preview={"status": "not_prepared"},
        measurements=_no_measurements(),
    )

    assert view["status"] == "needs_driver_target_proof"
    assert view["driver_target_proof"]["complete"] is False
    assert view["current_step"] == "map"
    assert view["next_action"]["id"] == "confirm_outputs"
    assert _step(view, "map")["status"] == "active"
    assert "Play each assigned driver quietly" in _step(view, "map")["message"]
    # The two rungs are still not applicable — the shape decides that, not the
    # confirmation progress — but the flow is NOT reported complete.
    assert _step(view, "safety")["status"] == STEP_STATUS_NOT_REQUIRED
    assert _step(view, "profile")["status"] == STEP_STATUS_NOT_REQUIRED


def test_active_shapes_still_require_driver_checks_and_the_combined_test():
    """The no-regression guard for the shape that DOES need the ladder.

    Mutation check: point the passive disposition at an active_2_way topology
    and this test fails — `safety` becomes not_applicable and `status` becomes
    "not_required" instead of demanding the driver checks.
    """

    for mode in ("active_2_way", "active_3_way"):
        view = build_commissioning_view(
            _topology(mode=mode),
            design_draft=_ready_design(),
            crossover_preview=_ready_preview(),
            measurements={
                "summary": {
                    "driver_checks_complete": False,
                    "driver_measurements_complete": False,
                    "captured_driver_check_count": 0,
                    "required_driver_check_count": 2,
                    "summed_validation_complete": False,
                    "latest_summed_tests": {},
                    "latest_summed_validations": {},
                },
            },
        )

        assert view["status"] == "needs_driver_target_proof", mode
        assert view["driver_target_proof"]["source"] == "missing", mode
        assert view["next_action"]["id"] == "confirm_outputs", mode
        assert _step(view, "safety")["status"] == "todo", mode
        assert _step(view, "profile")["status"] == "todo", mode
        assert _step(view, "map")["message"] == (
            "Play each assigned driver quietly, then confirm what you hear."
        ), mode
        assert view["combined_groups"], mode


def test_passive_mains_with_a_sub_keep_the_active_profile_rung():
    """A passive speaker WITH a sub is a different shape — do not terminate it.

    Bass management still compiles a (degenerate 1-way) active profile for it,
    so the profile rung stays live. Only the contradictory safety-step copy is
    repaired here; the combined test genuinely has no target either way.
    """

    view = build_commissioning_view(
        _topology(mode="full_range_passive", with_subwoofer=True),
        design_draft={"status": "not_saved"},
        crossover_preview={"status": "not_prepared"},
        measurements=_no_measurements(),
    )

    assert view["status"] != VIEW_STATUS_NOT_REQUIRED
    assert _step(view, "profile")["status"] != STEP_STATUS_NOT_REQUIRED
    assert _step(view, "safety")["status"] != STEP_STATUS_NOT_REQUIRED
    # Item 1 still applies: no step may contradict the map step.
    assert "Confirm each output and driver" not in _step(view, "safety")["message"]


def _active_steps(view: dict) -> list[str]:
    return [step["id"] for step in view["steps"] if step["status"] == "active"]


def _stale_preview_view() -> dict:
    """The exact state a household reached on jts5, 2026-08-06.

    A mono active 2-way whose layout is saved and whose outputs and drivers are
    already confirmed (measurements survive a layout redraw), but whose saved
    crossover preview is stale for the freshly-drawn layout. So
    `driver_target_proof` is complete while `driver_values` is not — the one
    combination that lets a later rung's readiness predicate come true before
    an earlier rung's does.
    """

    return build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview={"status": "not_prepared"},
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
    )


def test_active_ladder_runs_one_rung_at_a_time_when_the_preview_is_stale():
    """Exactly one step may be "active" — the one the household is on.

    Field evidence (jts5, 2026-08-06): the view reported `current_step:
    research` AND rendered `safety` as a second active step, three rungs early.
    Its card offered the combined speaker test, the household pressed it, and
    the backend fail-closed on the stale staged graph.
    """

    view = _stale_preview_view()

    assert view["status"] == "needs_driver_values"
    assert view["current_step"] == "research"
    assert _active_steps(view) == ["research"]
    assert _step(view, "safety")["status"] == "todo"
    assert _step(view, "profile")["status"] == "todo"


def test_active_ladder_never_carries_done_copy_on_an_unreached_step():
    """A step that has not been reached may not report itself finished.

    Field evidence: `map` was "todo" while its message read "All assigned
    outputs and drivers are confirmed." — done-state copy, because the status
    was gated on `driver_values_complete and driver_target_proof_complete`
    while the message was gated on `driver_target_proof_complete` alone.
    """

    view = _stale_preview_view()

    map_step = _step(view, "map")
    assert map_step["status"] == "todo"
    assert "are confirmed" not in map_step["message"]
    # It must say what it is waiting on: the rung holding the baton.
    assert COMMISSIONING_STEP_PAGE_TITLES["research"] in map_step["message"]
    # Same defect shape one rung further down: `safety` cannot advertise the
    # test it is not yet allowed to run.
    safety_step = _step(view, "safety")
    assert "Run the combined speaker test" not in safety_step["message"]
    assert COMMISSIONING_STEP_PAGE_TITLES["research"] in safety_step["message"]


def test_active_ladder_disables_the_combined_test_until_its_prerequisites_hold():
    """The invitation itself must be gated, not only the backend refusal.

    `jasper-web`'s staging refusal stays as the belt; this is the braces the
    household can see, so the click never happens three rungs early.
    """

    view = _stale_preview_view()

    group = view["combined_groups"][0]
    assert group["actions"]["start_combined_test"]["enabled"] is False
    assert group["status"] == "blocked"
    assert COMMISSIONING_STEP_PAGE_TITLES["research"] in group["message"]


def test_active_ladder_hands_the_baton_to_the_combined_test_when_ready():
    """The no-regression half: a genuinely-ready ladder still offers the test."""

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
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
    )

    assert _active_steps(view) == ["safety"]
    assert view["current_step"] == "safety"
    assert _step(view, "research")["status"] == "done"
    assert _step(view, "map")["status"] == "done"
    assert _step(view, "map")["message"] == (
        "All assigned outputs and drivers are confirmed."
    )
    assert _step(view, "safety")["message"] == (
        "Run the combined speaker test through the saved crossover."
    )
    group = view["combined_groups"][0]
    assert group["actions"]["start_combined_test"]["enabled"] is True
    assert group["status"] == "ready_to_test"


@pytest.mark.parametrize(
    "make_view",
    [
        pytest.param(_stale_preview_view, id="stale_preview"),
        pytest.param(
            lambda: build_commissioning_view(
                _topology(),
                design_draft={"status": "not_saved"},
                crossover_preview={"status": "not_prepared"},
                measurements=_no_measurements(),
            ),
            id="fresh_active_layout",
        ),
        pytest.param(
            lambda: build_commissioning_view(
                OutputTopology.from_mapping({
                    "artifact_schema_version": 1,
                    "kind": OUTPUT_TOPOLOGY_KIND,
                    "topology_id": "empty",
                    "name": "Empty",
                    "status": "draft",
                    "hardware": {
                        "device_id": "hifiberry_dac8x",
                        "device_label": "HiFiBerry DAC8x",
                        "physical_output_count": 8,
                        "card_id": "DAC8",
                    },
                    "speaker_groups": [],
                }),
                design_draft={"status": "not_saved"},
                crossover_preview={"status": "not_prepared"},
                measurements=_no_measurements(),
            ),
            id="no_layout",
        ),
        pytest.param(
            lambda: build_commissioning_view(
                _topology(mode="full_range_passive"),
                design_draft={"status": "not_saved"},
                crossover_preview={"status": "not_prepared"},
                measurements=_no_measurements(),
            ),
            id="subless_passive",
        ),
        pytest.param(
            lambda: build_commissioning_view(
                _topology(mode="full_range_passive", with_subwoofer=True),
                design_draft={"status": "not_saved"},
                crossover_preview={"status": "not_prepared"},
                measurements=_no_measurements(),
            ),
            id="passive_with_sub",
        ),
    ],
)
def test_every_commissioning_shape_shows_at_most_one_active_step(make_view):
    """The invariant, swept across the shapes this coordinator emits.

    Mutation: restore any step's independent `"active" if <own predicate>`
    branch and the stale-preview case fails here with two active steps.
    """

    view = make_view()

    assert len(_active_steps(view)) <= 1, view["steps"]
    # And the flow always points at a step it can actually reach.
    assert view["current_step"] in {step["id"] for step in view["steps"]} | {""}
    current = view["current_step"]
    if current:
        assert _step(view, current)["status"] != STEP_STATUS_NOT_REQUIRED


def test_summed_test_failure_message_sends_a_stale_staged_graph_back_to_values():
    """The failure the household actually hit must name the step to return to.

    Journal chain (jts5, 2026-08-06): `startup_anchor
    reason=staged_topology_mismatch` -> `staged_config status=blocked
    preview_status=stale blockers=1` -> `summed_test status=failed
    audio_emitted=False blockers=1`. The backend refusal is correct; the copy
    was not — the whole `commission_startup_anchor_*` family fell through to
    the generic "Press Play combined test to retry", which prescribes the one
    action guaranteed to fail again.
    """

    message = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "commission_startup_anchor_not_staged",
            "message": (
                "could not stage the silent active-speaker setup before driver "
                "testing"
            ),
        },
    ])

    # Name the card the household can actually see. /sound/ titles this step
    # "Add your components"; the coordinator's own `label` for it is different,
    # so the remedy must quote the rendered title, not the label.
    assert "Add your components" in message
    assert "preview" in message
    assert "Press Play combined test to retry" not in message


def test_summed_test_failure_message_maps_the_rest_of_the_anchor_family():
    """Each anchor blocker names its own remedy — no shared false diagnosis."""

    path_safety = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "commission_startup_anchor_path_safety_blocked",
            "message": "could not verify the silent active-speaker setup path",
        },
    ])
    assert "Confirm outputs" in path_safety

    load_failed = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "commission_startup_anchor_load_failed",
            "message": "could not load the silent active-speaker setup",
        },
    ])
    assert "quiet crossover setup" in load_failed
    assert "Add your components" not in load_failed

    proof_missing = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "summed_test_driver_target_proof_missing",
            "message": "confirm each output and driver",
        },
    ])
    assert "Confirm outputs" in proof_missing


def test_summed_test_failure_message_surfaces_an_unmapped_blocker_reason():
    """An unknown blocker fails loud: its own reason, never generic-only.

    Mutation: drop the raw-reason branch and this test reads back the generic
    retry line, which is exactly how the anchor family stayed invisible.
    """

    message = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "some_future_blocker",
            "message": "the crossover graph refused a channel remap",
        },
    ])

    assert "the crossover graph refused a channel remap" in message

    # A blocker with no showable prose still gets plain copy and a next step.
    # It must NOT echo the raw identifier: the /sound/ no-jargon rule
    # (tests/test_sound_setup.py::test_active_speaker_setup_copy_has_no_backend_jargon)
    # forbids snake_case codes in household-facing copy. The fail-loud half
    # rides `combined_groups[].failure_code` instead — see
    # test_blocked_combined_test_reports_its_blocker_code_out_of_band.
    coded_only = summed_test_failure_message([
        {"severity": "blocker", "code": "some_future_blocker"},
    ])
    assert "some_future_blocker" not in coded_only
    assert "open System status" in coded_only
    _assert_household_safe(coded_only, "coded-only blocker")


def test_no_combined_test_failure_copy_leaks_a_raw_code():
    """The mapped remedies must read as sentences, not as backend identifiers.

    The same no-jargon rule the JS copy is held to, pinned on the side that now
    owns every per-failure sentence. Mutation: put any `code` string into its
    own message above and this fails.
    """

    for code, message in _SUMMED_TEST_FAILURE_COPY:
        assert code not in message, code
        assert "_" not in message, message


# Backend vocabulary that docs/HANDOFF-sound-preferences.md promises never
# reaches this surface, plus the shapes that carry it: absolute filesystem
# paths, exception class names, raw snake_case identifiers.
_BANNED_COPY_TOKENS = (
    "camilladsp",
    "yaml",
    "alsa",
    "configfs",
    "systemd",
    "snd-aloop",
)


def _assert_household_safe(text: str, where: str) -> None:
    assert "/" not in text, f"{where}: filesystem path in household copy: {text!r}"
    assert not re.search(r"\b\w*(?:Error|Exception)\b", text), (
        f"{where}: exception class in household copy: {text!r}"
    )
    assert "_" not in text, f"{where}: raw identifier in household copy: {text!r}"
    lowered = text.lower()
    for token in _BANNED_COPY_TOKENS:
        assert token not in lowered, f"{where}: {token!r} in household copy: {text!r}"


def test_combined_test_failure_copy_never_leaks_backend_prose():
    """Real blocker prose from the load path must not reach a household verbatim.

    These three messages are produced by the live commissioning-load path and
    every one of them carries something this surface promises not to show — an
    absolute path, an exception class, or the DSP engine's name. The unmapped
    branch interpolates the blocker's own prose, so without sanitising it the
    Python side walks straight around
    tests/test_sound_setup.py::test_active_speaker_setup_copy_has_no_backend_jargon,
    which reads only the JS files.

    Mutation: drop the sanitiser and each of these fails here.
    """

    leaking = [
        (
            "rollback_anchor_missing",
            "all-muted staged rollback anchor does not exist: "
            "/var/lib/jasper/active_speaker/startup_rollback.yml",
        ),
        (
            "current_config_snapshot_failed",
            "could not read current CamillaDSP config path: OSError",
        ),
        (
            "driver_commission_load_failed",
            "CamillaDSP commissioning load failed: CamillaError",
        ),
    ]
    for code, message in leaking:
        rendered = summed_test_failure_message([
            {"severity": "blocker", "code": code, "message": message},
        ])
        assert rendered, code
        _assert_household_safe(rendered, code)


def test_unmapped_blocker_prose_is_sanitised_before_it_is_shown():
    """The guard for the FALLBACK branch, not for the mapping.

    The three codes above are now mapped, so they never reach the sanitiser —
    a test using them proves the table, not the scrubbing. These use an unmapped
    code so the interpolating branch actually runs, and each case is chosen so a
    different half of `_household_safe_reason` is what saves it.

    Mutations, each of which fails a case here: drop the path substitution;
    drop the exception substitution; drop the banned-vocabulary rejection.
    """

    def render(message: str) -> str:
        return summed_test_failure_message([
            {"severity": "blocker", "code": "some_future_blocker",
             "message": message},
        ])

    # (a) path stripping is what saves this one — no identifier, no vocabulary.
    stripped_path = render("could not open the quiet test tone at /tmp/tone.wav")
    _assert_household_safe(stripped_path, "path")
    assert "could not open the quiet test tone at" in stripped_path

    # (b) exception-class stripping is what saves this one.
    stripped_exception = render("could not start the quiet tone: RuntimeError")
    _assert_household_safe(stripped_exception, "exception")
    assert "could not start the quiet tone" in stripped_exception

    # (c) rejection is what saves this one — nothing to strip, so the whole
    # reason is dropped and the household gets the written fallback.
    rejected = render("the CamillaDSP graph refused a channel remap")
    _assert_household_safe(rejected, "vocabulary")
    assert "refused a channel remap" not in rejected
    assert "open System status" in rejected


def test_every_step_message_is_household_safe():
    """The same bar for the ladder's own copy, swept across the shapes."""

    views = [
        _stale_preview_view(),
        build_commissioning_view(
            _topology(),
            design_draft={"status": "not_saved"},
            crossover_preview={"status": "not_prepared"},
            measurements=_no_measurements(),
        ),
        build_commissioning_view(
            _topology(mode="full_range_passive"),
            design_draft={"status": "not_saved"},
            crossover_preview={"status": "not_prepared"},
            measurements=_no_measurements(),
        ),
    ]
    for view in views:
        for step in view["steps"]:
            _assert_household_safe(step["message"], f"step {step['id']}")
        for group in view["combined_groups"]:
            _assert_household_safe(group["message"], f"group {group['group_id']}")


def test_anchor_remedy_routes_without_asserting_an_unverified_cause():
    """`commission_startup_anchor_not_staged` covers ~8 distinct staging causes.

    Staging reports "staged" only when the blocker count is zero and the file
    exists, so this one code fires for an unresolved DAC route, an unresolved
    subwoofer, an incomplete guard, a failed generation, a preset bind, and
    more. The specific cause never reaches the coordinator — only the first
    load issue is forwarded — so naming one ("the crossover preview is out of
    date") is a diagnosis this module cannot make. Route without diagnosing;
    the per-cause fix is issue #2184.
    """

    message = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "commission_startup_anchor_not_staged",
            "message": (
                "could not stage the silent active-speaker setup before driver "
                "testing"
            ),
        },
    ])

    assert "Add your components" in message
    # Says what to DO, never why it failed.
    assert "out of date" not in message
    assert "stale" not in message.lower()


def test_blocked_combined_test_reports_its_blocker_code_out_of_band():
    """Fail loud without jargon: the code rides a field, not the sentence."""

    view = build_commissioning_view(
        _topology(),
        design_draft=_ready_design(),
        crossover_preview=_ready_preview(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "latest_summed_tests": {
                    "mono": {
                        "captured": False,
                        "audio_emitted": False,
                        "issues": [{
                            "severity": "blocker",
                            "code": "rollback_anchor_missing",
                            "message": "anchor does not exist: /var/lib/jasper/x.yml",
                        }],
                    },
                },
                "latest_summed_validations": {},
            },
        },
    )

    group = view["combined_groups"][0]
    assert group["failure_code"] == "rollback_anchor_missing"
    _assert_household_safe(group["failure_message"], "failure_message")


def test_waiting_copy_names_the_step_that_actually_holds_the_baton():
    """A waiting rung must point at the live rung, not always at the values.

    First-run state — no saved topology at all. `driver_values` reports
    "not needed" (there is no active layout to need them), so a waiting message
    hard-coded to the values rung told the household to finish work the card
    directly above it had just said was unnecessary. The baton is on
    Choose speaker layout, and that is what both waiting rungs must name.
    """

    view = build_commissioning_view(
        OutputTopology.from_mapping({
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "empty",
            "name": "Empty",
            "status": "draft",
            "hardware": {
                "device_id": "hifiberry_dac8x",
                "device_label": "HiFiBerry DAC8x",
                "physical_output_count": 8,
                "card_id": "DAC8",
            },
            "speaker_groups": [],
        }),
        design_draft={"status": "not_saved"},
        crossover_preview={"status": "not_prepared"},
        measurements=_no_measurements(),
    )

    assert _active_steps(view) == ["layout"]
    layout_title = COMMISSIONING_STEP_PAGE_TITLES["layout"]
    for step_id in ("map", "safety"):
        step = _step(view, step_id)
        assert step["status"] == "todo", step_id
        assert layout_title in step["message"], (step_id, step["message"])
        # It must not demand the values the research rung just called unneeded.
        assert "driver and crossover values" not in step["message"], step_id


def test_waiting_copy_names_the_values_step_when_that_is_the_live_one():
    """The same derivation, one rung along — the jts5 field state."""

    view = _stale_preview_view()

    assert _active_steps(view) == ["research"]
    research_title = COMMISSIONING_STEP_PAGE_TITLES["research"]
    assert research_title in _step(view, "map")["message"]
    assert research_title in _step(view, "safety")["message"]
    assert research_title in view["combined_groups"][0]["message"]


def test_step_labels_match_the_titles_the_page_renders():
    """One name per step. Nothing renders `label`, but a second name is a bug
    in waiting — and this module now quotes titles in remedy copy."""

    view = _stale_preview_view()

    for step in view["steps"]:
        assert step["label"] == COMMISSIONING_STEP_PAGE_TITLES[step["id"]], step["id"]


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOUND_MODULE = _REPO_ROOT / "deploy" / "assets" / "sound-profile" / "js" / "main.js"
_ACTIVE_SPEAKER_UI_MODULE = (
    _REPO_ROOT / "deploy" / "assets" / "sound-profile" / "js" / "active-speaker-ui.js"
)


def test_failure_remedies_name_the_card_titles_the_page_actually_renders():
    """A remedy that names a card must name it the way /sound/ titles it.

    Three places name these steps: this module's step `label`, the page's own
    `renderOutputStepCard` titles, and `outputStepTitle` (used by the
    "Finish the current card before opening X" guard). The page titles are the
    only ones a household reads, so `COMMISSIONING_STEP_PAGE_TITLES` mirrors
    THEM. Without this pin, a title reworded in the page would leave the
    combined-test remedy pointing at a card that no longer exists — a quieter
    version of the dead end it was added to fix.
    """

    page = _SOUND_MODULE.read_text()
    rendered = dict(
        re.findall(
            r"renderOutputStepCard\(\s*'([a-z_]+)',\s*'([^']+)',",
            page,
        )
    )

    assert rendered, "no renderOutputStepCard(<id>, <title>) calls found"
    assert rendered == COMMISSIONING_STEP_PAGE_TITLES, (
        "commissioning step titles drifted from the /sound/ page"
    )

    # The status-line guard names cards too; it must use the same words.
    guard = _ACTIVE_SPEAKER_UI_MODULE.read_text()
    guard_titles = dict(
        re.findall(
            r"^\s*(layout|research|map|safety|profile):\s*'([^']+)'",
            guard.split("export function outputStepTitle")[1].split("}")[0],
            re.MULTILINE,
        )
    )
    assert guard_titles == COMMISSIONING_STEP_PAGE_TITLES, (
        "outputStepTitle drifted from the /sound/ page titles"
    )


def test_host_error_codes_and_household_blocker_codes_stay_disjoint():
    """Two vocabularies, one namespace — keep them from colliding again.

    `CommissioningHostError` codes are INTERNAL: they name invariant violations
    in the server-owned commissioning journal, and nothing outside
    `commissioning_host.py` reads `.code` today. `_SUMMED_TEST_FAILURE_COPY`
    codes are HOUSEHOLD blockers, each mapped to a sentence prescribing a
    remedy a household can carry out.

    `rollback_anchor_missing` was spelled in BOTH — the household one meaning
    "the current CamillaDSP config path does not exist" (remedy: press Play
    combined test to retry), the internal one meaning "a restore was recorded
    with no matching intent" (no remedy; a retry cannot fix an invariant
    violation). They never collided in behaviour, because nothing wires a host
    error's code into an issues list. The first reader who does would map an
    internal defect onto a household retry, so the guarantee is asserted rather
    than left to that reader noticing.

    Mutation: revert the rename in `_runtime_mutation_journal`'s
    `record_restored` and this fails naming the shared token.
    """

    import ast

    repo = Path(__file__).resolve().parent.parent
    source = (repo / "jasper" / "active_speaker" / "commissioning_host.py").read_text(
        encoding="utf-8"
    )
    host_codes = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if name != "CommissioningHostError" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            host_codes.add(first.value)

    assert len(host_codes) > 40, (
        f"the CommissioningHostError walk found too few codes ({len(host_codes)}) "
        "— this guard would be vacuous"
    )

    household_codes = {code for code, _ in _SUMMED_TEST_FAILURE_COPY}
    shared = sorted(host_codes & household_codes)
    assert not shared, (
        "these tokens name an internal commissioning-journal failure AND a "
        "household blocker with a household remedy; wiring a host error's "
        f"`.code` into an issues list would prescribe the wrong fix: {shared}"
    )
