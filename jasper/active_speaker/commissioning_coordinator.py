# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Backend view model for active-speaker commissioning.

This module is intentionally read-only. It composes the durable state files that
the setup flow already owns and turns them into product actions/messages for the
web UI. Sound-producing transitions still live in the existing action modules;
the coordinator is the single place that decides what the next obvious action
is and how failure evidence should be presented to a household.
"""

from __future__ import annotations

from typing import Any, Mapping

from jasper.output_topology import (
    OutputTopology,
    channel_identity_report,
    topology_is_subless_passive_mains,
)

from ._common import finite_float as _finite_float
from .measurement import active_summed_targets
from .revalidation import applied_profile_revalidation_satisfies_driver_target_proof

COORDINATOR_KIND = "jts_active_speaker_commissioning_view"

# A step this speaker's shape will never run. Distinct from "done" (which claims
# work happened) and from "todo"/"active" (which promise work still can); the
# /sound/ page renders it as an explanatory card instead of a dead end. The
# spelling is deliberate and shared: it is the word the /correction/ journey's
# closed step vocabulary already uses for a passive topology
# (docs/correction-journey-design.md §5, "this step doesn't apply") and the word
# `driver_target_proof.source` below already reports for the same shape.
STEP_STATUS_NOT_REQUIRED = "not_required"
# The matching terminal view status: the flow is finished, and finished WITHOUT
# an active-crossover commissioning ladder ever applying to this speaker.
VIEW_STATUS_NOT_REQUIRED = "not_required"

# The ordered commissioning step ids `build_commissioning_view` emits, exported
# so envelope/progress consumers derive from ONE tuple instead of re-typing the
# literals. Keep this tuple in sync with the view construction
# (`build_commissioning_view`) below.
COMMISSIONING_STEP_IDS: tuple[str, ...] = (
    "layout",
    "research",
    "map",
    "safety",
    "profile",
)

# The card titles /sound/ actually renders for those steps, mirrored here so a
# remedy can tell a household WHICH card to go back to using the words on their
# screen. Deliberately not the `label` this module puts in each step: the page
# hard-codes its own titles (`renderOutputStepCard` in
# deploy/assets/sound-profile/js/main.js) and titles the research step "Add your
# components", so quoting the label would send the household hunting for a card
# that does not exist. Pinned against the page by
# tests/test_active_speaker_commissioning_coordinator.py.
COMMISSIONING_STEP_PAGE_TITLES: dict[str, str] = {
    "layout": "Choose speaker layout",
    "research": "Add your components",
    "map": "Confirm outputs",
    "safety": "Test combined drivers",
    "profile": "Validate and apply",
}

# Ordered failure vocabulary for the combined driver test: the FIRST code
# present wins, so the most specific remedy is what a household reads. Every
# blocker a summed-test playback can carry belongs here; an unmapped code falls
# through to the blocker's OWN reason (see `summed_test_failure_message`) rather
# than to a generic "press Play combined test to retry", which prescribes the
# one action guaranteed to fail again. That generic-only fallback is how the
# whole `commission_startup_anchor_*` family stayed invisible on jts5
# (2026-08-06): the staged graph was stale for a freshly-drawn layout, the
# backend correctly refused to play, and the card said to press the button
# again.
_SUMMED_TEST_FAILURE_COPY: tuple[tuple[str, str], ...] = (
    (
        "tone_backend_failed",
        "JTS could not prepare the combined test audio. Retry after the setup "
        "finishes; if it fails again, open System status.",
    ),
    (
        "commission_startup_anchor_not_staged",
        "The crossover preview is out of date for this layout. Go back to "
        f"{COMMISSIONING_STEP_PAGE_TITLES['research']}, run the preview, then "
        "retry the combined test.",
    ),
    (
        "commission_startup_anchor_path_safety_blocked",
        "JTS could not verify the quiet crossover path for this layout. "
        f"Re-check {COMMISSIONING_STEP_PAGE_TITLES['map']}, then retry the "
        "combined test.",
    ),
    (
        "commission_startup_anchor_load_failed",
        "JTS could not load the quiet crossover setup. Press Play combined "
        "test to retry; if it fails again, open System status.",
    ),
    (
        "summed_test_driver_target_proof_missing",
        f"Finish {COMMISSIONING_STEP_PAGE_TITLES['map']} before the combined "
        "test.",
    ),
    (
        "summed_test_already_active",
        "A combined speaker test is already running. Stop it, then try again.",
    ),
    (
        "summed_commission_load_failed",
        "JTS could not open the quiet combined-test path. Press Play combined "
        "test to retry.",
    ),
    (
        "safe_session_not_armed",
        "JTS could not open the quiet combined-test session. Press Play "
        "combined test to retry.",
    ),
    (
        "summed_test_artifact_missing",
        "The combined test did not finish. Press Play combined test to retry.",
    ),
    (
        "summed_test_playback_incomplete",
        "The combined test did not finish. Press Play combined test to retry.",
    ),
    (
        "summed_test_output_mismatch",
        "The last combined test did not match the saved speaker outputs. "
        f"Re-check {COMMISSIONING_STEP_PAGE_TITLES['map']} before retrying.",
    ),
)


def issue_codes(issues: Any) -> set[str]:
    if not isinstance(issues, list):
        return set()
    return {
        str(issue.get("code") or "")
        for issue in issues
        if isinstance(issue, Mapping) and issue.get("code")
    }


def has_blocker(issues: Any) -> bool:
    return any(
        isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        for issue in (issues if isinstance(issues, list) else [])
    )


def _first_blocker_reason(issues: Any) -> str:
    """Return the first blocker's own prose, blank when it carries none."""

    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, Mapping) or issue.get("severity") != "blocker":
            continue
        if issue.get("code"):
            return str(issue.get("message") or "").strip()
    return ""


def summed_test_failure_message(issues: Any) -> str:
    """Return the one user-facing reason for a failed combined test."""

    codes = issue_codes(issues)
    for code, message in _SUMMED_TEST_FAILURE_COPY:
        if code in codes:
            return message
    # Unmapped blocker: say what the backend actually reported. Failing loud
    # with an unfamiliar sentence beats a familiar sentence that is wrong about
    # what to do next. The blocker's `message` is prose the backend already
    # wrote for a human; the `code` is NOT — raw snake_case identifiers must
    # never reach a household (the /sound/ no-jargon rule, pinned by
    # tests/test_sound_setup.py::test_active_speaker_setup_copy_has_no_backend_jargon),
    # so a blocker that carries no prose is reported as exactly that: a failure
    # JTS could not explain.
    reason = _first_blocker_reason(issues)
    if reason:
        return (
            f"The combined test did not play: {reason.rstrip('.')}. "
            "Press Play combined test to retry."
        )
    if codes:
        return (
            "The combined test did not play, and JTS did not report why. Press "
            "Play combined test to retry; if it fails again, open System status."
        )
    return ""


def _step(step_id: str, label: str, status: str, message: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "status": status,
        "message": message,
    }


def _derive_step_statuses(
    rungs: tuple[tuple[str, bool, bool], ...],
) -> dict[str, str]:
    """Turn per-rung completion into ONE ordered ladder with one live step.

    Each rung reports only the two facts it genuinely owns: whether it is
    finished, and whether this speaker's shape will ever run it. Which rung is
    *active* is a property of the LADDER, not of any rung — it is the first
    unfinished rung this speaker can still reach.

    Deriving "active" per rung instead is what put two steps live at once on
    jts5 (2026-08-06): a freshly-drawn active 2-way still carried confirmed
    outputs and captured driver checks from before the redraw, so the combined
    driver test's own readiness predicate came true three rungs before the
    crossover values were saved. It lit up, invited a click, and the graph
    fail-closed on the stale staged config.

    A finished rung still reports "done" even when an earlier rung is not,
    because hiding completed work would be the same dishonesty pointing the
    other way; it simply does not claim the baton.
    """

    statuses: dict[str, str] = {}
    baton_taken = False
    for step_id, done, not_required in rungs:
        if done:
            statuses[step_id] = "done"
        elif not_required:
            statuses[step_id] = STEP_STATUS_NOT_REQUIRED
        elif not baton_taken:
            statuses[step_id] = "active"
            baton_taken = True
        else:
            statuses[step_id] = "todo"
    return statuses


def _action(
    action_id: str,
    label: str,
    *,
    enabled: bool,
    endpoint: str | None = None,
    method: str = "POST",
    body: Mapping[str, Any] | None = None,
    message: str = "",
) -> dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "enabled": bool(enabled),
        "endpoint": endpoint,
        "method": method,
        "body": dict(body or {}),
        "message": message,
    }


def _preview_ready(crossover_preview: Mapping[str, Any] | None) -> bool:
    if not isinstance(crossover_preview, Mapping):
        return False
    permissions = (
        crossover_preview.get("permissions")
        if isinstance(crossover_preview.get("permissions"), Mapping)
        else {}
    )
    return (
        crossover_preview.get("kind") == "jts_active_speaker_crossover_preview"
        and crossover_preview.get("status") == "ready_for_protected_staging"
        and permissions.get("may_prepare_protected_startup_config") is True
    )


def _driver_values_view(
    *,
    active_setup: bool,
    design_draft: Mapping[str, Any] | None,
    crossover_preview: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the saved driver/crossover readiness contract for setup flow."""

    if not active_setup:
        return {
            "status": "not_needed",
            "complete": True,
            "design_ready": True,
            "preview_ready": True,
            "missing_driver_info_roles": [],
            "missing_crossover_candidate_pairs": [],
            "message": "No active crossover values are needed for this layout.",
        }

    draft = design_draft if isinstance(design_draft, Mapping) else {}
    summary = draft.get("summary") if isinstance(draft.get("summary"), Mapping) else {}
    design_status = str(draft.get("status") or "not_saved")
    design_ready = design_status == "ready_for_review"
    preview_ready = _preview_ready(crossover_preview)
    missing_roles = list(summary.get("missing_driver_info_roles") or [])
    missing_pairs = list(summary.get("missing_crossover_candidate_pairs") or [])
    if design_ready and preview_ready:
        status = "ready"
        message = "Driver and crossover values are saved."
    elif design_ready:
        status = "needs_preview"
        message = "Preview the crossover before confirming outputs."
    elif missing_roles or missing_pairs:
        status = "needs_values"
        message = "Save driver names and crossover points before continuing."
    else:
        status = design_status
        message = "Save the driver and crossover values before continuing."
    return {
        "status": status,
        "complete": design_ready and preview_ready,
        "design_ready": design_ready,
        "preview_ready": preview_ready,
        "missing_driver_info_roles": missing_roles,
        "missing_crossover_candidate_pairs": missing_pairs,
        "message": message,
    }


def _latest(mapping: Any, key: str) -> Mapping[str, Any]:
    if isinstance(mapping, Mapping):
        value = mapping.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _combined_test_level(
    calibration_level: Mapping[str, Any] | None,
    latest_test: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    test_signal = (
        calibration_level.get("test_signal")
        if isinstance(calibration_level, Mapping)
        and isinstance(calibration_level.get("test_signal"), Mapping)
        else {}
    )
    software_guard = (
        calibration_level.get("software_gain_guard")
        if isinstance(calibration_level, Mapping)
        and isinstance(calibration_level.get("software_gain_guard"), Mapping)
        else {}
    )
    requested = test_signal.get("requested_level_dbfs", -80.0)
    latest_tone = (
        latest_test.get("tone")
        if isinstance(latest_test, Mapping)
        and isinstance(latest_test.get("tone"), Mapping)
        else {}
    )
    latest_level = _finite_float(latest_tone.get("level_dbfs"))
    if (
        latest_level is not None
        and isinstance(latest_test, Mapping)
        and latest_test.get("captured") is True
        and latest_test.get("audio_emitted") is True
        and not has_blocker(latest_test.get("issues"))
    ):
        requested = latest_level
    return {
        "requested_level_dbfs": requested,
        "min_level_dbfs": test_signal.get("min_level_dbfs", -80.0),
        "max_level_dbfs": test_signal.get("max_level_dbfs", 0.0),
        "step_db": test_signal.get("step_db", 1.0),
        "upward_step_limit_db": software_guard.get("upward_step_limit_db", 6.0),
    }


def _combined_group_view(
    target: Mapping[str, Any],
    *,
    summary: Mapping[str, Any],
    calibration_level: Mapping[str, Any] | None,
    driver_values_complete: bool,
    driver_target_proof_complete: bool,
) -> dict[str, Any]:
    group_id = str(target.get("speaker_group_id") or "")
    label = str(target.get("speaker_group_label") or group_id or "Speaker")
    latest_tests = summary.get("latest_summed_tests")
    latest_validations = summary.get("latest_summed_validations")
    latest_test = _latest(latest_tests, group_id)
    latest_validation = _latest(latest_validations, group_id)
    test_level = _combined_test_level(calibration_level, latest_test)
    has_audible_test = (
        latest_test.get("captured") is True
        and latest_test.get("audio_emitted") is True
        and not has_blocker(latest_test.get("issues"))
    )
    latest_test_id = str(
        latest_test.get("summed_test_id") or latest_test.get("playback_id") or ""
    )
    latest_validation_test_id = str(
        latest_validation.get("summed_test_id")
        or latest_validation.get("playback_id")
        or ""
    )
    validated = bool(
        latest_validation.get("validated") is True
        and latest_test_id
        and latest_validation_test_id == latest_test_id
    )
    failure_message = (
        ""
        if has_audible_test or not latest_test
        else summed_test_failure_message(latest_test.get("issues"))
    )

    # Running the combined test needs the WHOLE ladder above it, not just the
    # driver proof: it plays through the staged crossover graph, which only
    # exists once the driver/crossover values are saved and previewed. Gating on
    # the proof alone offered the button while the staged graph was stale, so
    # the household's click could only ever be refused.
    combined_test_ready = driver_values_complete and driver_target_proof_complete

    if validated:
        status = "validated"
        status_label = "validated"
        message = "Combined crossover check is saved."
    elif has_audible_test:
        status = "ready_to_record"
        status_label = "ready to record"
        message = "Combined speaker test played. Record what you heard."
    elif combined_test_ready:
        status = "ready_to_test" if not failure_message else "test_failed"
        status_label = "next" if not failure_message else "not tested"
        message = failure_message or (
            "Run the combined speaker test. It uses the prepared crossover setup "
            "and starts at the quiet test level."
        )
    elif not driver_values_complete:
        status = "blocked"
        status_label = "after setup"
        message = (
            "Add the driver and crossover values first, then test the combined "
            "speaker."
        )
    else:
        status = "blocked"
        status_label = "after outputs"
        message = "Confirm each output and driver first, then test the combined speaker."

    actions = {
        "start_combined_test": _action(
            "start_combined_test",
            "Play combined test",
            enabled=combined_test_ready,
            endpoint="./active-speaker/summed-test",
            body={
                "speaker_group_id": group_id,
                "audio": True,
                "stimulus": "speech",
                "duration_ms": 12000,
                "level_dbfs": test_level.get("requested_level_dbfs"),
            },
        ),
        "record_combined_result": _action(
            "record_combined_result",
            "Record combined check",
            enabled=has_audible_test and not validated,
            endpoint="./active-speaker/summed-validation",
            body={
                "speaker_group_id": group_id,
                "summed_test_id": latest_test_id,
                "operator_listening_check": True,
            },
        ),
    }
    return {
        "group_id": group_id,
        "label": label,
        "mode": target.get("mode"),
        "roles": list(target.get("roles") or []),
        "status": status,
        "status_label": status_label,
        "message": message,
        "failure_message": failure_message,
        "latest_test_id": latest_test_id,
        "has_audible_test": has_audible_test,
        "validated": validated,
        "test_level": test_level,
        "actions": actions,
    }


def _first_enabled_action(groups: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for group in groups:
        actions = group.get("actions")
        if not isinstance(actions, Mapping):
            continue
        for action_id in ("record_combined_result", "start_combined_test"):
            action = actions.get(action_id)
            if isinstance(action, Mapping) and action.get("enabled") is True:
                return {
                    **dict(action),
                    "speaker_group_id": group.get("group_id"),
                    "group_label": group.get("label"),
                }
    return None


def build_commissioning_view(
    topology: OutputTopology,
    *,
    design_draft: Mapping[str, Any] | None = None,
    crossover_preview: Mapping[str, Any] | None = None,
    measurements: Mapping[str, Any] | None = None,
    commission: Mapping[str, Any] | None = None,
    startup_load: Mapping[str, Any] | None = None,
    baseline_profile: Mapping[str, Any] | None = None,
    calibration_level: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose active-speaker setup state into one UI-facing view model."""

    measurements = measurements if isinstance(measurements, Mapping) else {}
    summary = (
        measurements.get("summary")
        if isinstance(measurements.get("summary"), Mapping)
        else {}
    )
    identity = channel_identity_report(topology)
    assigned_count = int(identity.get("assigned_channel_count") or 0)
    unverified_count = int(identity.get("unverified_channel_count") or 0)
    output_identity_complete = assigned_count > 0 and unverified_count == 0
    raw_driver_checks_complete = bool(
        summary.get("driver_checks_complete")
        or summary.get("driver_measurements_complete")
    )
    profile_status = str((baseline_profile or {}).get("status") or "")
    profile_applied = profile_status == "applied"
    revalidation = (
        (baseline_profile or {}).get("revalidation")
        if isinstance((baseline_profile or {}).get("revalidation"), Mapping)
        else {}
    )
    revalidation_required = revalidation.get("required") is True
    driver_target_proof_satisfied_by_revalidation = (
        not raw_driver_checks_complete
        and output_identity_complete
        and applied_profile_revalidation_satisfies_driver_target_proof(revalidation)
    )
    driver_checks_complete = (
        raw_driver_checks_complete or driver_target_proof_satisfied_by_revalidation
    )
    captured_driver_count = int(
        summary.get("captured_driver_check_count")
        or summary.get("captured_driver_count")
        or 0
    )
    required_driver_count = int(
        summary.get("required_driver_check_count")
        or summary.get("required_driver_count")
        or 0
    )
    active_targets = active_summed_targets(topology)
    has_layout = bool(topology.speaker_groups)
    active_setup = bool(active_targets)
    driver_target_proof_complete = (
        output_identity_complete and (not active_setup or driver_checks_complete)
    )
    # Full-range passive mains with no sub: no inter-driver crossover and no
    # bass-management split, so the last two rungs of the ladder (combined
    # driver test, active speaker profile) never apply. `not active_setup` is
    # implied by the predicate — a topology whose mains are all passive and
    # which has no sub group cannot hold an active group — but it is kept as
    # the fail-CLOSED conjunct: if the two ever disagreed we would rather keep
    # asking for driver checks than silently drop a required one.
    commissioning_not_required = not active_setup and topology_is_subless_passive_mains(
        topology
    )
    driver_values = _driver_values_view(
        active_setup=active_setup,
        design_draft=design_draft,
        crossover_preview=crossover_preview,
    )
    driver_values_complete = bool(driver_values.get("complete"))
    combined_groups = [
        _combined_group_view(
            target,
            summary=summary,
            calibration_level=calibration_level,
            driver_values_complete=driver_values_complete,
            driver_target_proof_complete=driver_target_proof_complete,
        )
        for target in active_targets
    ]
    summed_complete = bool(active_targets) and all(
        group.get("validated") is True for group in combined_groups
    )
    # One ordered ladder: each rung declares only "am I finished?" and "will
    # this speaker ever run me?". `_derive_step_statuses` decides which single
    # rung holds the baton. Every message below then reads that DERIVED status
    # — never a predicate of its own. When the two disagreed, `map` sat "todo"
    # under the sentence "All assigned outputs and drivers are confirmed."
    step_status = _derive_step_statuses((
        ("layout", has_layout, False),
        ("research", driver_values_complete, False),
        # Confirming outputs is deliberately gated behind the saved values, so
        # this rung is not "done" until the ladder legitimately reached it.
        ("map", driver_values_complete and driver_target_proof_complete, False),
        ("safety", summed_complete, commissioning_not_required),
        ("profile", profile_applied, commissioning_not_required),
    ))
    steps = [
        _step(
            "layout",
            "Choose speaker layout",
            step_status["layout"],
            "Speaker layout is saved."
            if step_status["layout"] == "done"
            else "Choose what is wired.",
        ),
        _step(
            "research",
            "Add driver and crossover values",
            step_status["research"],
            str(driver_values.get("message") or "Save driver and crossover values."),
        ),
        _step(
            "map",
            "Confirm outputs",
            step_status["map"],
            (
                # Say only what was actually proven. When no driver listening
                # check was ever REQUIRED (`driver_target_proof.source` is
                # "not_required"), claiming the drivers were confirmed is a
                # verification claim nothing backs — output identity is all
                # this speaker had to prove.
                "All assigned outputs are confirmed. This layout needs no "
                "separate driver listening checks."
                if step_status["map"] == "done" and not active_setup
                else "All assigned outputs and drivers are confirmed."
                if step_status["map"] == "done"
                else "Play each assigned driver quietly, then confirm what you hear."
                if step_status["map"] == "active"
                # Not reached yet. Name what it is waiting on instead of
                # reporting work that has not been authorised to count.
                else "Add the driver and crossover values first, then confirm "
                "each output."
            ),
        ),
        _step(
            "safety",
            "Test combined drivers",
            step_status["safety"],
            (
                "Combined crossover check is saved."
                if step_status["safety"] == "done"
                # A layout with no active crossover has no combined driver test
                # to offer, whatever stage its outputs are at. Saying "confirm
                # each output and driver first" here contradicted the map step,
                # which had just reported them confirmed.
                else "No combined driver test applies to this layout."
                if not active_setup
                else "Existing active profile covers driver/output proof; "
                "revalidate the combined crossover."
                if step_status["safety"] == "active"
                and driver_target_proof_satisfied_by_revalidation
                else "Run the combined speaker test through the saved crossover."
                if step_status["safety"] == "active"
                # Waiting. Which rung it is waiting ON is the whole point: the
                # old copy always blamed the outputs, so a household whose
                # outputs were already confirmed had nowhere to go.
                else "Add the driver and crossover values first, then test the "
                "combined speaker."
                if not driver_values_complete
                else "Confirm each output and driver before the combined test."
            ),
        ),
        _step(
            "profile",
            "Validate and apply",
            step_status["profile"],
            (
                "This is now the active speaker profile."
                if step_status["profile"] == "done"
                # A subless passive speaker plays through the flat program
                # lane, so it never compiles an active speaker profile. (A
                # passive speaker WITH a sub still does — bass management —
                # so this copy is gated on the subless shape, not on
                # `not active_setup`.)
                else "No active speaker profile is needed for this layout."
                if step_status["profile"] == STEP_STATUS_NOT_REQUIRED
                else "Save and apply a fresh profile after revalidation."
                if revalidation_required
                else "Save the active speaker profile after the combined check."
            ),
        ),
    ]
    # An "active" step wins; otherwise fall back to the last step this speaker
    # can actually reach, so a terminated ladder points at Confirm outputs
    # rather than a step it will never run.
    applicable_steps = [
        step for step in steps
        if step.get("status") != STEP_STATUS_NOT_REQUIRED
    ]
    current_step = next(
        (step["id"] for step in steps if step.get("status") == "active"),
        applicable_steps[-1]["id"] if applicable_steps else "",
    )
    next_action = None
    if has_layout and not driver_values_complete:
        if driver_values.get("design_ready") and not driver_values.get("preview_ready"):
            next_action = _action(
                "preview_crossover",
                "Preview crossover",
                enabled=True,
                endpoint="./active-speaker/crossover-preview",
            )
        else:
            next_action = _action(
                "save_driver_values",
                "Save values",
                enabled=True,
                endpoint="./active-speaker/design-draft",
            )
    if next_action is None and not driver_target_proof_complete:
        next_action = _action(
            "confirm_outputs",
            "Confirm outputs",
            enabled=driver_values_complete,
            method="GET",
            message="Play each assigned driver quietly, then confirm what you hear.",
        )
    elif next_action is None and commissioning_not_required:
        # Terminal, and said so: an empty next_action reads the same as "the
        # coordinator has no idea", which is exactly the dead end this replaces.
        next_action = _action(
            "setup_complete",
            "Setup complete",
            enabled=False,
            message="This speaker is set up. No crossover checks apply to it.",
        )
    elif next_action is None and not summed_complete:
        next_action = _first_enabled_action(combined_groups)
    elif next_action is None and summed_complete and not profile_applied:
        next_action = _action(
            "save_profile",
            "Save active profile",
            enabled=True,
            endpoint="./active-speaker/baseline-profile/save-and-apply",
        )

    status = (
        "applied" if profile_applied else
        "ready_to_save_profile" if summed_complete else
        "needs_driver_values" if has_layout and not driver_values_complete else
        "needs_driver_target_proof" if driver_values_complete and not driver_target_proof_complete else
        # Outputs are confirmed and nothing further applies -- the terminal
        # state for a subless passive speaker. Sits AFTER the proof gate so an
        # unconfirmed passive layout still reports what it owes.
        VIEW_STATUS_NOT_REQUIRED if commissioning_not_required else
        "needs_revalidation" if revalidation_required else
        "needs_combined_check" if driver_target_proof_complete else
        "needs_layout"
    )
    return {
        "artifact_schema_version": 1,
        "kind": COORDINATOR_KIND,
        "status": status,
        "steps": steps,
        "current_step": current_step,
        "combined_groups": combined_groups,
        "next_action": dict(next_action or {}),
        "driver_values": driver_values,
        "output_identity": {
            "assigned_channel_count": assigned_count,
            "unverified_channel_count": unverified_count,
            "complete": output_identity_complete,
        },
        "driver_target_proof": {
            "complete": driver_target_proof_complete,
            "source": (
                "applied_profile_revalidation"
                if driver_target_proof_satisfied_by_revalidation
                else "measurements"
                if raw_driver_checks_complete
                else "not_required"
                if not active_setup
                else "missing"
            ),
            "output_identity_complete": output_identity_complete,
            "driver_checks_complete": driver_checks_complete,
            "captured": captured_driver_count,
            "required": required_driver_count,
        },
        "driver_checks": {
            "complete": driver_checks_complete,
            "source": (
                "applied_profile_revalidation"
                if driver_target_proof_satisfied_by_revalidation
                else "measurements"
                if raw_driver_checks_complete
                else "missing"
            ),
            "captured": captured_driver_count,
            "required": required_driver_count,
        },
        "summed_validation": {
            "complete": summed_complete,
            "validated": int(summary.get("validated_summed_group_count") or 0),
            "required": int(summary.get("required_summed_group_count") or 0),
        },
        "revalidation": dict(revalidation),
        "test_level": (
            dict(combined_groups[0]["test_level"])
            if combined_groups else _combined_test_level(calibration_level)
        ),
        "runtime": {
            "commission": dict(commission or {}),
            "startup_load": dict(startup_load or {}),
        },
    }


def load_commissioning_view(
    topology: OutputTopology | None = None,
    *,
    commission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """THE commissioning view of this speaker — load state, then compose.

    ``build_commissioning_view`` above is a pure composer: it never loads state
    itself, so any caller that omits an input silently degrades the view (a
    missing ``design_draft`` pins ``current_step`` to "research" forever; a
    missing ``baseline_profile`` makes "applied" unreachable). This loader is
    the single source of truth for feeding it — it loads every durable state
    input exactly the way the ``/sound/`` commissioning card always has
    (design draft → preview derived FROM that draft → measurements →
    calibration level → the write-free baseline-profile candidate →
    startup-load state) and composes the view. Both the ``/sound/`` payload and
    the ``/correction/crossover/envelope`` builder call this; neither hand-rolls
    the input set.

    ``commission`` is the one caller-supplied input: it is a runtime-only relay
    (surfaced verbatim under ``runtime.commission``, never consulted for
    steps/status/next_action) and the full payload needs an async CamillaDSP
    runtime probe that only the ``/sound/`` caller owns. Callers without a live
    probe pass ``None`` — the composed steps are identical.

    Lazy imports keep this module light for pure-composition callers/tests.
    """
    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.active_speaker.startup_load import load_startup_load_state
    from jasper.output_topology import load_output_topology

    if topology is None:
        topology = load_output_topology()
    design_draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=design_draft)
    measurements = load_measurement_state(topology)
    calibration_level = load_calibration_level_state()
    baseline = build_baseline_profile_candidate(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
    )
    return build_commissioning_view(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        commission=commission,
        startup_load={"state": load_startup_load_state()},
        baseline_profile=baseline,
        calibration_level=calibration_level,
    )
