# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Backend view model for active-speaker commissioning.

Read-only: it composes the durable state files the setup flow already owns into
product actions and messages for the web UI. Sound-producing transitions live in
the action modules; this is the one place deciding the next obvious action and
how failure evidence reaches a household.
"""

from __future__ import annotations

import re
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
# spelling is deliberate and shared: it is the word `output_topology.py` and
# `driver_target_proof.source` below already report for the same shape.
STEP_STATUS_NOT_REQUIRED = "not_required"
# The matching terminal view status: finished WITHOUT an active-crossover
# commissioning ladder ever applying to this speaker.
VIEW_STATUS_NOT_REQUIRED = "not_required"

# The ONE name each step has: `renderOutputStepCard` and `outputStepTitle` in
# deploy/assets/sound-profile/js/main.js render these titles, and remedy copy
# below quotes them, so "go back to X" always names a heading a household sees.
COMMISSIONING_STEP_PAGE_TITLES: dict[str, str] = {
    "layout": "Choose speaker layout",
    "research": "Add your components",
    "map": "Confirm outputs",
    "safety": "Test combined drivers",
    "profile": "Validate and apply",
}

# The ordered step ids `build_commissioning_view` emits; derived from the titles
# above so envelope/progress consumers cannot disagree with the page.
COMMISSIONING_STEP_IDS: tuple[str, ...] = tuple(COMMISSIONING_STEP_PAGE_TITLES)

_RESEARCH = COMMISSIONING_STEP_PAGE_TITLES["research"]
_MAP = COMMISSIONING_STEP_PAGE_TITLES["map"]

# Household copy for a failed combined test, grouped by the ACTION available to a
# household rather than by the backend's cause, and searched in order: the FIRST
# code present wins. An unmapped code falls through to the blocker's own prose,
# which carries paths, exception classes and the DSP engine's name, so coverage
# of the reachable codes matters more than per-cause nuance. Per-cause detail
# needs the sub-code plumbed through first — issue #2184. The exact code always
# rides out of band as `combined_groups[].failure_code`.
_SUMMED_TEST_FAILURE_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    # The setup for this layout was never prepared. Route back to the values.
    (
        (
            "commission_startup_anchor_not_staged",
            "commission_active_graph_not_staged",
            "staged_candidate_not_ready",
            "staged_topology_mismatch",
            "active_startup_candidate_required",
            "commission_tone_preset_unresolved",
            "commission_tone_preset_unreadable",
        ),
        f"JTS could not prepare the crossover setup for this layout. Go back to "
        f"{_RESEARCH}, run the preview, then retry the combined test.",
    ),
    # The outputs or their safety evidence are not proven. Route back to Confirm
    # outputs.
    (
        (
            "commission_startup_anchor_path_safety_blocked",
            "path_safety_evidence_missing",
            "path_safety_evidence_invalid",
            "physical_identity_unverified",
            "no_assigned_outputs",
            "summed_test_driver_target_proof_missing",
        ),
        f"JTS could not verify the speaker outputs for this test. Re-check "
        f"{_MAP}, then retry the combined test.",
    ),
    # The speaker's output connection cannot carry the test (#2412). MUST be
    # mapped: these blockers' own prose carries an operator reconciler command
    # that `_household_safe_reason` would not strip. Kept ABOVE the quiet-test
    # family below — the two co-occur, and "retry" can never satisfy a
    # structurally unarmed path.
    (
        (
            "commissioning_transport_ends_disagree",
            "commissioning_ring_feed_unarmed",
            "commissioning_active_endpoint_unarmed",
            "ring_wire_declaration_invalid",
        ),
        "This speaker’s output connection isn’t ready for the combined test, "
        "so the test can’t run. Open System status.",
    ),
    # Kept ABOVE the retry family below: this code always rides the same
    # payload as `commission_startup_anchor_load_failed`, and retrying cannot
    # clear it. Twin of the `/sound/` JS ladder entry for the same code
    # (`active-speaker-ui.js::commissionIssueReason`); the two must agree on
    # whether retrying helps.
    (
        ("staged_startup_hold_unavailable",),
        "JTS could not hold the silent speaker setup in place, so it left the "
        "speaker as it was and played nothing. Open System status.",
    ),
    # Below every routing family above, because "go back to <step>" / "open
    # System status" is a better answer than "retry" whenever one of those codes
    # is also present, and ABOVE `summed_test_output_mismatch`:
    # `record_summed_test_artifact` extends the playback issues and THEN appends
    # the mismatch, so the two co-occur and a backend that never played beats a
    # mismatch measured from what played.
    (
        (
            "tone_backend_failed",
            "commission_tone_backend_failed",
            "commission_startup_anchor_load_failed",
            "summed_commission_load_failed",
            "driver_commission_load_failed",
            "startup_config_load_failed",
            "startup_config_missing",
            "startup_config_path_missing",
            "startup_config_unreadable",
            "startup_config_validation_not_valid",
            # Mapped because its own prose names an operator `--check`
            # invocation that `_household_safe_reason` withholds only by
            # coincidence — it rejects the banned token, not the command.
            "camilla_config_not_validated",
            "current_config_snapshot_failed",
            "current_config_snapshot_missing",
            "rollback_anchor_missing",
            "commission_rollback_anchor_missing",
            "rollback_config_missing",
            "commission_rollback_config_missing",
            "commission_live_state_stale",
            "commission_output_hardware_reconcile_failed",
            "commissioning_protection_while_audible",
            "calibration_level_guard_missing",
        ),
        "JTS could not open the quiet crossover setup. Press Play combined "
        "test to retry; if it fails again, open System status.",
    ),
    (
        ("summed_test_output_mismatch",),
        "The last combined test did not match the saved speaker outputs. "
        f"Re-check {_MAP} before retrying.",
    ),
    (
        ("summed_test_already_active",),
        "A combined speaker test is already running. Stop it, then try again.",
    ),
    (
        (
            "summed_commission_rollback_failed",
            "commission_rollback_failed",
            "commission_rollback_unavailable",
            "startup_rollback_failed",
            "startup_rollback_unavailable",
        ),
        "The combined test finished but JTS could not restore the quiet setup. "
        "Open System status before playing anything else.",
    ),
    (
        ("safe_session_not_armed",),
        "JTS could not open the quiet combined-test session. Press Play "
        "combined test to retry.",
    ),
    (
        ("summed_test_artifact_missing", "summed_test_playback_incomplete"),
        "The combined test did not finish. Press Play combined test to retry.",
    ),
)

# Flattened for lookup: what consumers and the copy guards walk.
_SUMMED_TEST_FAILURE_COPY: tuple[tuple[str, str], ...] = tuple(
    (code, message)
    for codes, message in _SUMMED_TEST_FAILURE_FAMILIES
    for code in codes
)

# The /sound/ no-jargon rule reads only the JS files, so this Python surface
# enforces it itself: no absolute path, exception class, or raw identifier may
# reach a household.
_COPY_PATH_RE = re.compile(r"(?:^|(?<=\s))~?/\S*")
_COPY_EXCEPTION_RE = re.compile(r"\b\w*(?:Error|Exception)\b")
_COPY_BANNED_TOKENS = ("camilladsp", "yaml", "alsa", "configfs", "systemd", "snd-aloop")


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


def first_blocker(issues: Any) -> tuple[str, str]:
    """Return the first blocker's ``(code, message)``, blank when there is none."""

    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, Mapping) or issue.get("severity") != "blocker":
            continue
        code = str(issue.get("code") or "")
        if code:
            return code, str(issue.get("message") or "").strip()
    return "", ""


def _household_safe_reason(text: str) -> str:
    """Return backend prose fit to show a household, or "" when it is not.

    Strips paths and exception classes, then fails closed on anything still
    holding backend vocabulary or a raw identifier.
    """

    reason = _COPY_PATH_RE.sub(" ", text)
    reason = _COPY_EXCEPTION_RE.sub(" ", reason)
    reason = re.sub(r"\s+", " ", reason).strip(" :;,.-")
    if not reason or "_" in reason:
        return ""
    lowered = reason.lower()
    if any(token in lowered for token in _COPY_BANNED_TOKENS):
        return ""
    return reason


def summed_test_failure_message(issues: Any) -> str:
    """Return the one user-facing reason for a failed combined test."""

    codes = issue_codes(issues)
    for code, message in _SUMMED_TEST_FAILURE_COPY:
        if code in codes:
            return message
    # Unmapped blocker: show the backend's own prose when it is fit to show,
    # rather than flatten a new failure mode into a familiar wrong sentence.
    reason = _household_safe_reason(first_blocker(issues)[1])
    if reason:
        return (
            f"The combined test did not play: {reason.rstrip('.')}. "
            "Press Play combined test to retry."
        )
    if codes:
        return (
            "The combined test did not play. Press Play combined test to "
            "retry; if it fails again, open System status."
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

    A rung reports only whether it is finished and whether this speaker's shape
    will ever run it; *active* is a property of the LADDER — the first
    unfinished rung still reachable — so exactly one rung holds the baton. A
    finished rung still reports "done" under an unfinished earlier rung.
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


def _waiting_message(baton_step: str, then_do: str) -> str:
    """Copy for a rung the ladder has not reached yet, naming the baton holder."""

    title = COMMISSIONING_STEP_PAGE_TITLES.get(baton_step)
    if not title:
        return f"Finish the earlier steps first, then {then_do}."
    return f"Finish {title} first, then {then_do}."


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
    blocked_by: str,
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
    reported = (
        ("", "")
        if has_audible_test or not latest_test
        else first_blocker(latest_test.get("issues"))
    )
    failure_code = reported[0]
    failure_message = (
        ""
        if has_audible_test or not latest_test
        else summed_test_failure_message(latest_test.get("issues"))
    )

    # The test plays through the staged crossover graph, which exists only once
    # the driver/crossover values are saved and previewed, so it needs the WHOLE
    # ladder above it, not just the driver proof. `blocked_by` is the first
    # unfinished prerequisite rung — empty means every one is done.
    combined_test_ready = not blocked_by

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
    else:
        status = "blocked"
        status_label = "after outputs" if blocked_by == "map" else "after setup"
        message = _waiting_message(blocked_by, "test the combined speaker")

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
        # The raw code, kept OUT of the sentence: diagnostics get the exact
        # identifier without leaking jargon onto the card.
        "failure_code": failure_code,
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
    applied_profile_verdict: str = "",
) -> dict[str, Any]:
    """Compose active-speaker setup state into one UI-facing view model.

    ``applied_profile_verdict`` is one of
    :func:`baseline_profile.applied_profile_displacement`'s verdicts, or ``""``
    for "checked, and the speaker holds it"; this composer performs no IO, so
    the loader answers it. Only ``APPLIED_PROFILE_DISPLACED`` revokes the
    profile; a "could not check" verdict discloses a caveat. See ADR-0195.
    """

    from .baseline_profile import APPLIED_PROFILE_DISPLACED

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
    revalidation = (
        (baseline_profile or {}).get("revalidation")
        if isinstance((baseline_profile or {}).get("revalidation"), Mapping)
        else {}
    )
    revalidation_required = revalidation.get("required") is True
    # The rebuild's own status cannot reach "applied" for a measured profile;
    # `applied_profile_stands` is the payload's own verdict. See ADR-0195.
    applied_profile_stands = (
        (baseline_profile or {}).get("applied_profile_stands") is True
    )
    verdict = str(applied_profile_verdict or "")
    profile_applied = applied_profile_stands and verdict != APPLIED_PROFILE_DISPLACED
    # Applied, and JTS could not confirm the speaker is holding it. Disclosed
    # rather than revoked, with the recovery door left open.
    profile_applied_caveat = verdict if profile_applied else ""
    applied_anchor = (baseline_profile or {}).get("applied_recomposition_profile")
    # What the basic save-and-apply door would NOT re-emit: it compiles the
    # chosen crossover plus driver trims, while linearization and blend come
    # only from a measured candidate.
    applied_profile_carries_correction = bool(
        isinstance(applied_anchor, Mapping)
        and (
            applied_anchor.get("linearization")
            or applied_anchor.get("blend_correction")
        )
    )
    # The builder's own compared-and-clean grant, consumed rather than
    # re-derived; it rides even a blocked payload.
    applied_profile_proves_drivers = (
        (baseline_profile or {}).get("driver_target_proof_from_applied_profile")
        is True
    )
    driver_target_proof_satisfied_by_revalidation = (
        not raw_driver_checks_complete
        and output_identity_complete
        and (
            applied_profile_revalidation_satisfies_driver_target_proof(revalidation)
            or applied_profile_proves_drivers
        )
    )
    driver_proof_source = (
        "applied_profile"
        if driver_target_proof_satisfied_by_revalidation
        and applied_profile_proves_drivers
        else "applied_profile_revalidation"
        if driver_target_proof_satisfied_by_revalidation
        else ""
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
    # bass-management split, so the last two rungs (combined driver test, active
    # speaker profile) never apply. `not active_setup` is implied by the
    # predicate but kept as the fail-CLOSED conjunct.
    commissioning_not_required = not active_setup and topology_is_subless_passive_mains(
        topology
    )
    driver_values = _driver_values_view(
        active_setup=active_setup,
        design_draft=design_draft,
        crossover_preview=crossover_preview,
    )
    driver_values_complete = bool(driver_values.get("complete"))
    # The rungs the combined driver test sits behind. Split out because the
    # groups are built before `summed_complete` exists, and because the first
    # unfinished one answers both "may the test be offered?" and "which card do
    # we name?" — one value, so button and copy cannot disagree.
    prerequisite_rungs: tuple[tuple[str, bool, bool], ...] = (
        ("layout", has_layout, False),
        ("research", driver_values_complete, False),
        # Gated behind the saved values, so this rung is not "done" until the
        # ladder legitimately reached it.
        ("map", driver_values_complete and driver_target_proof_complete, False),
    )
    blocked_by = next(
        (step_id for step_id, done, _ in prerequisite_rungs if not done), ""
    )
    combined_groups = [
        _combined_group_view(
            target,
            summary=summary,
            calibration_level=calibration_level,
            blocked_by=blocked_by,
        )
        for target in active_targets
    ]
    summed_complete = bool(active_targets) and all(
        group.get("validated") is True for group in combined_groups
    )
    # Every message below reads the DERIVED status, never a predicate of its
    # own, so a rung's copy cannot contradict the status on its card.
    step_status = _derive_step_statuses(prerequisite_rungs + (
        ("safety", summed_complete, commissioning_not_required),
        ("profile", profile_applied, commissioning_not_required),
    ))
    # The one live rung, so a waiting rung can name it rather than guess.
    baton_step = next(
        (step_id for step_id in COMMISSIONING_STEP_IDS
         if step_status[step_id] == "active"),
        "",
    )
    steps = [
        _step(
            "layout",
            COMMISSIONING_STEP_PAGE_TITLES["layout"],
            step_status["layout"],
            "Speaker layout is saved."
            if step_status["layout"] == "done"
            else "Choose what is wired.",
        ),
        _step(
            "research",
            COMMISSIONING_STEP_PAGE_TITLES["research"],
            step_status["research"],
            str(driver_values.get("message") or "Save driver and crossover values."),
        ),
        _step(
            "map",
            COMMISSIONING_STEP_PAGE_TITLES["map"],
            step_status["map"],
            (
                # A passive layout requires no driver listening check, so
                # output identity is all this speaker had to prove.
                "All assigned outputs are confirmed. This layout needs no "
                "separate driver listening checks."
                if step_status["map"] == "done" and not active_setup
                else "All assigned outputs and drivers are confirmed."
                if step_status["map"] == "done"
                else "Play each assigned driver quietly, then confirm what you hear."
                if step_status["map"] == "active"
                else _waiting_message(baton_step, "confirm each output")
            ),
        ),
        _step(
            "safety",
            COMMISSIONING_STEP_PAGE_TITLES["safety"],
            step_status["safety"],
            (
                "Combined crossover check is saved."
                if step_status["safety"] == "done"
                # A saved layout with no active crossover has no combined
                # driver test to offer, whatever stage its outputs are at.
                # Gated on a saved layout: with nothing wired yet there is no
                # "this layout" to make the claim about.
                else "No combined driver test applies to this layout."
                if has_layout and not active_setup
                else "Existing active profile covers driver/output proof; "
                "revalidate the combined crossover."
                if step_status["safety"] == "active"
                and driver_target_proof_satisfied_by_revalidation
                else "Run the combined speaker test through the saved crossover."
                if step_status["safety"] == "active"
                else _waiting_message(baton_step, "test the combined speaker")
            ),
        ),
        _step(
            "profile",
            COMMISSIONING_STEP_PAGE_TITLES["profile"],
            step_status["profile"],
            (
                "This is the active speaker profile. JTS could not confirm "
                "the speaker is playing it."
                if step_status["profile"] == "done" and profile_applied_caveat
                else "This is now the active speaker profile."
                if step_status["profile"] == "done"
                # A subless passive speaker plays through the flat program
                # lane and compiles no active speaker profile; a passive
                # speaker WITH a sub still does (bass management), so this is
                # gated on the subless shape, not on `not active_setup`.
                else "No active speaker profile is needed for this layout."
                if step_status["profile"] == STEP_STATUS_NOT_REQUIRED
                else "Save and apply a fresh profile after revalidation."
                if revalidation_required
                else "Save the active speaker profile after the combined check."
            ),
        ),
    ]
    # An "active" step wins; otherwise the last step this speaker can reach, so
    # a terminated ladder never points at a step it will never run.
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
        # Terminal, and said so: an empty next_action reads as "no idea".
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
    # The basic door compiles the chosen crossover with driver trims only. It
    # stays reachable in every state but is never the recommendation over a
    # measured tune, and never offered without saying what it replaces
    # (ADR-0195, ruling S10: disclose, do not block).
    secondary_action: dict[str, Any] | None = None
    offer_basic = applied_profile_carries_correction and (
        (profile_applied and next_action is None)
        or str((next_action or {}).get("id") or "") == "save_profile"
    )
    if offer_basic:
        secondary_action = _action(
            "save_basic_profile",
            "Replace with basic profile",
            enabled=True,
            endpoint="./active-speaker/baseline-profile/save-and-apply",
            message=(
                "Compiles the saved crossover with driver trims only. This "
                "replaces the measured profile applied now — its per-driver "
                "linearization and blend correction are not re-emitted."
            ),
        )
        if next_action is not None:
            next_action = _action(
                "remeasure_crossover",
                "Re-measure",
                enabled=True,
                method="GET",
                endpoint="/correction/crossover/",
                message=(
                    "Active speaker setup changed after the measured profile "
                    "was applied. Re-measure to carry that tune forward."
                ),
            )

    status = (
        # `not next_action`: "applied" is terminal, so it may not stand beside
        # a rung this speaker still owes.
        "applied" if profile_applied and not next_action else
        "ready_to_save_profile" if summed_complete and not profile_applied else
        "needs_driver_values" if has_layout and not driver_values_complete else
        "needs_driver_target_proof" if driver_values_complete and not driver_target_proof_complete else
        # The terminal state for a subless passive speaker. Sits AFTER the
        # proof gate so an unconfirmed passive layout still reports what it owes.
        VIEW_STATUS_NOT_REQUIRED if commissioning_not_required else
        "needs_revalidation" if revalidation_required else
        "needs_combined_check" if driver_target_proof_complete else
        "needs_layout"
    )
    return {
        "artifact_schema_version": 1,
        "kind": COORDINATOR_KIND,
        "status": status,
        "applied_profile": {
            "stands": profile_applied,
            # "" while the speaker is confirmed to hold it; otherwise the
            # verdict that qualifies the claim.
            "verdict": profile_applied_caveat,
            "carries_correction": applied_profile_carries_correction,
        },
        "steps": steps,
        "current_step": current_step,
        "combined_groups": combined_groups,
        "next_action": dict(next_action or {}),
        "secondary_action": dict(secondary_action or {}),
        "driver_values": driver_values,
        "output_identity": {
            "assigned_channel_count": assigned_count,
            "unverified_channel_count": unverified_count,
            "complete": output_identity_complete,
        },
        "driver_target_proof": {
            "complete": driver_target_proof_complete,
            "source": (
                driver_proof_source
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
                driver_proof_source
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

    The single source of truth for feeding the pure composer above: a caller
    that omits one of its inputs silently degrades the view, so both the
    ``/sound/`` payload and the ``/correction/crossover/envelope`` builder come
    through here. ``commission`` is the one caller-supplied input, a
    runtime-only view needing an async CamillaDSP probe only ``/sound/`` owns;
    ``None`` composes identical steps.
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
        applied_profile_verdict=read_applied_profile_verdict(baseline),
    )


def read_applied_profile_verdict(baseline_profile: Mapping[str, Any]) -> str:
    """Ask the speaker whether it is still playing the applied profile.

    ``""`` when it is. Otherwise one of
    :func:`baseline_profile.applied_profile_displacement`'s verdicts, or
    :data:`~jasper.active_speaker.baseline_profile.APPLIED_PROFILE_CONFIG_MISSING`
    — the record's own ``config.exists`` is frozen at apply time, so only a
    fresh stat sees the file go missing under it. Two reads at wizard cadence;
    nothing polled reaches this.
    """

    from pathlib import Path

    from .baseline_profile import (
        APPLIED_PROFILE_CONFIG_MISSING,
        applied_profile_displacement,
    )

    applied = baseline_profile.get("applied_recomposition_profile")
    if (
        not isinstance(applied, Mapping)
        or baseline_profile.get("applied_profile_stands") is not True
    ):
        return ""
    verdict = applied_profile_displacement(applied)
    if verdict:
        return verdict
    config = applied.get("config") if isinstance(applied.get("config"), Mapping) else {}
    recorded = str(config.get("path") or "")
    return "" if Path(recorded).exists() else APPLIED_PROFILE_CONFIG_MISSING
