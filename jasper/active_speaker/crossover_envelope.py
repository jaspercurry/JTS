# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure screen envelope for sequential Layer-A acoustic commissioning.

``/sound/`` owns topology, driver protection, output identity, and the safe
starting profile.  This envelope owns the distinct microphone journey:

    mic/calibration + per-driver level -> driver sweeps -> combined alignment
    -> atomic apply

It reads the already-composed crossover status payload and returns one primary
action plus any explicit alternatives. It performs no I/O and mutates no
measurement state; the correction web host supplies relay/apply adapters for
the returned action descriptors.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from ..log_event import log_event

logger = logging.getLogger(__name__)

CROSSOVER_ENVELOPE_SCHEMA_VERSION = 5

_STEP_IDS = ("speaker_setup", "microphone", "drivers", "alignment", "apply")
_STEP_LABELS = {
    "speaker_setup": "Protected speaker setup",
    "microphone": "Microphone and level",
    "drivers": "Measure each driver",
    "alignment": "Align the crossover",
    "apply": "Apply speaker profile",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _targets(status: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    from .crossover_eligibility import mapping_sequence

    return list(mapping_sequence(_mapping(status.get("targets")).get(kind)))


def _summary(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(status.get("measurements")).get("summary"))


def _driver_repeat_target_ids(target: Mapping[str, Any]) -> set[str]:
    """Return controller ids without letting a malformed status crash the UI."""

    group_id = str(target.get("speaker_group_id") or "")
    role = str(target.get("role") or "")
    physical_id = f"{group_id}:{role}"
    from .capture_geometry import driver_repeat_binding

    try:
        fixed_id, _fingerprint = driver_repeat_binding(
            speaker_group_id=group_id,
            role=role,
            target_fingerprint=str(target.get("target_fingerprint") or ""),
            capture_geometry="reference_axis",
        )
    except ValueError:
        return {physical_id}
    return {physical_id, fixed_id}


def _driver_record(status: Mapping[str, Any], target: Mapping[str, Any]) -> Mapping[str, Any]:
    latest = _mapping(_summary(status).get("latest_driver_measurements"))
    key = f"{target.get('speaker_group_id') or ''}:{target.get('role') or ''}"
    return _mapping(latest.get(key))


def _reference_axis_driver_record(
    status: Mapping[str, Any], target: Mapping[str, Any]
) -> Mapping[str, Any]:
    latest = _mapping(
        _summary(status).get("latest_reference_axis_driver_measurements")
    )
    key = f"{target.get('speaker_group_id') or ''}:{target.get('role') or ''}"
    return _mapping(latest.get(key))


def _driver_confirmed(status: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    """Whether the operator's by-ear driver confirmation is still intact.

    Reads the confirmation-only latest index (immune to sweep evidence -- see
    ``measurement._latest_current_driver_confirmations``) rather than the
    mixed ``latest_driver_measurements`` surface every other helper here
    uses: a missing driver whose confirmation was clobbered by a later sweep
    capture (JTS3 run 13 -> run 14) needs a different next action than a
    driver that has simply never been measured yet.

    Legacy/test callers that predate this key omit it entirely (same
    contract as ``driver_safety_profile_evaluation`` above); do not newly
    block them here. Every production caller goes through
    ``measurement.load_measurement_state`` / ``_summarise``, which always
    sets this key.
    """
    summary = _summary(status)
    if "latest_driver_confirmations" not in summary:
        return True
    latest = _mapping(summary.get("latest_driver_confirmations"))
    key = f"{target.get('speaker_group_id') or ''}:{target.get('role') or ''}"
    record = _mapping(latest.get(key))
    if not record:
        return False
    return (
        record.get("captured") is True
        and record.get("outcome") == "heard_correct_driver"
        and record.get("target_fingerprint") == target.get("target_fingerprint")
        and not any(
            isinstance(issue, Mapping) and issue.get("severity") == "blocker"
            for issue in record.get("issues") or []
        )
    )


def _usable_driver_acoustic(
    record: Mapping[str, Any],
    active_comparison_set: Mapping[str, Any],
    target: Mapping[str, Any],
) -> bool:
    from .crossover_eligibility import driver_acoustic_usable

    return driver_acoustic_usable(
        record,
        active_comparison_set,
        target,
        capture_geometry="near_field",
    )


def _usable_reference_axis_driver_acoustic(
    record: Mapping[str, Any],
    active_comparison_set: Mapping[str, Any],
    target: Mapping[str, Any],
) -> bool:
    """Require persisted, gated, repeat-aggregated fixed-axis evidence."""

    from .crossover_eligibility import driver_acoustic_usable

    return driver_acoustic_usable(
        record,
        active_comparison_set,
        target,
        capture_geometry="reference_axis",
    )


def _completed_insufficient_verdict(role: str, progress: Any) -> str:
    """Honest terminal copy for a fully-repeated but still-unusable driver.

    Reached only when the repeat set's own durable status is "completed"
    (every bounded repeat played, each individually accepted -- no
    outlier/clipping/transport rejection) yet the target is still in
    ``missing_drivers``/``missing_reference_axis_drivers``: the accepted
    evidence never cleared the per-band magnitude SNR floor (25 dB
    confident, 20 dB warn -- docs/active-crossover-information-design.md
    "Level control and SNR"; below 20 dB is "insufficient"). There is no
    fifth attempt to offer here -- repeat_admission.reserve() refuses a
    completed set -- so this names the shortfall with the same evidence
    style as an in-progress rejection ("17.4 dB SNR; 2.6 dB more needed" --
    the design doc's Language guide) and points at the one legitimate
    remedy: a fresh, louder level check.
    """
    from .crossover_eligibility import finite_float

    snr = finite_float(progress.last_result.get("estimated_snr_db"))
    shortfall = finite_float(progress.last_result.get("snr_shortfall_db"))
    band = str(progress.last_result.get("worst_band_id") or "").replace("_", " ")
    if snr is not None and shortfall is not None:
        band_clause = f" in the {band} band" if band else ""
        evidence = f" ({snr:.1f} dB SNR{band_clause}; {shortfall:.1f} dB more needed)"
    else:
        evidence = ""
    return (
        f"The {role} measurement finished, but there wasn't enough signal "
        f"above the room's noise to tune from{evidence}. Raise the "
        "measurement level or quiet the room, then restart the driver "
        "level check to measure again."
    )


def _level_state(status: Mapping[str, Any]) -> tuple[bool, str, bool]:
    level = _mapping(status.get("level_match"))
    last = _mapping(level.get("last"))
    ramp = _mapping(last.get("ramp"))
    state = str(ramp.get("state") or "")
    ready = level.get("ready") is True and level.get("valid") is not False
    return ready, state, level.get("running") is True


def _relay_active(status: Mapping[str, Any]) -> bool:
    relay = _mapping(status.get("relay"))
    return str(relay.get("status") or "") in {
        "starting",
        "awaiting_phone",
        "finishing",
        "committing",
        "stopping",
    }


def _relay_kind(status: Mapping[str, Any]) -> str:
    return str(_mapping(status.get("relay")).get("kind") or "")


def _level_run(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(status.get("level_match")).get("run"))


def _level_run_active(status: Mapping[str, Any]) -> bool:
    return str(_level_run(status).get("phase") or "") in {
        "awaiting_phone",
        "running",
    }


def _setup_ready(status: Mapping[str, Any]) -> bool:
    """Whether the "speaker_setup" step is satisfied.

    Normally requires ``setup.status == "ready"``. One narrow composition
    exception, shared with the level-match/sweep endpoint gates in
    correction_setup.py: a setup blocked only because the automatic capture
    sequence's own all-muted staged anchor is loaded, with the capture-entry
    stash pending, is "anchored mid-sequence by design", not "setup
    unproven" (JTS3 punch #24: after the woofer level lock, the tweeter
    lock was never offered; the only exit invalidated the locks already
    captured). The full composition story and the stash-as-discriminator
    argument live on
    :func:`jasper.active_speaker.setup_status.setup_blocked_only_by_in_sequence_anchor`.
    """

    from .setup_status import setup_blocked_only_by_in_sequence_anchor

    setup = _mapping(status.get("setup"))
    if setup.get("active") is not True:
        return False
    if setup.get("status") == "ready":
        return True
    return setup_blocked_only_by_in_sequence_anchor(status)


def _legacy_applied_profile_needs_reapply(status: Mapping[str, Any]) -> bool:
    applied = _mapping(status.get("applied_profile"))
    return bool(
        applied.get("status") == "applied"
        and not _mapping(applied.get("recomposition_snapshot"))
    )


_DRIVER_SAFETY_ROLE_LABELS = {
    "woofer": "Woofer",
    "mid": "Midrange",
    "tweeter": "Tweeter",
}


def _driver_safety_role_label(role: str) -> str:
    if role in _DRIVER_SAFETY_ROLE_LABELS:
        return _DRIVER_SAFETY_ROLE_LABELS[role]
    label = role.replace("_", " ").strip()
    return label.capitalize() if label else "Driver"


def _driver_safety_profile_nudges(
    evaluation: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Translate the safety profile's own verdict into plain-language copy.

    Reuses ``evaluate_driver_safety_profile``'s status/reasons rather than
    re-deriving completeness from field presence, and follows the Language
    guide in docs/active-crossover-information-design.md: no internal
    vocabulary (no "fingerprint", "candidate", "authority", or raw field
    names) in user-facing copy.
    """

    status = str(evaluation.get("status") or "missing")
    if status != "incomplete":
        text = {
            "missing": "Driver safety limits have not been set up yet.",
            "malformed": "Saved driver safety limits could not be read cleanly.",
            "stale": (
                "Saved driver safety limits no longer match the current "
                "speaker setup."
            ),
            "unconfirmed": (
                "Driver safety limits were saved but not yet confirmed."
            ),
        }.get(status, "Driver safety limits are not ready.")
        return [{
            "code": "crossover_driver_safety_profile_not_ready",
            "severity": "warn",
            "text": f"{text} Finish the driver details in speaker setup.",
        }]
    reasons = evaluation.get("reasons")
    reasons = reasons if isinstance(reasons, (list, tuple)) else ()
    missing_roles: list[str] = []
    review_roles: list[str] = []
    for reason in reasons:
        role, sep, detail = str(reason).partition(":")
        if not sep:
            continue
        label = _driver_safety_role_label(role)
        bucket = missing_roles if detail.endswith("_missing") else review_roles
        if label not in bucket:
            bucket.append(label)
    nudges: list[dict[str, str]] = []
    if missing_roles:
        nudges.append({
            "code": "crossover_driver_safety_profile_incomplete",
            "severity": "warn",
            "text": (
                f"{', '.join(missing_roles)} safety limits are missing "
                "— finish the driver details in speaker setup."
            ),
        })
    if review_roles:
        nudges.append({
            "code": "crossover_driver_safety_profile_needs_review",
            "severity": "warn",
            "text": (
                f"{', '.join(review_roles)} safety limits need review "
                "— check the driver details in speaker setup."
            ),
        })
    if not nudges:
        nudges.append({
            "code": "crossover_driver_safety_profile_incomplete",
            "severity": "warn",
            "text": (
                "Driver safety limits are incomplete. Finish the driver "
                "details in speaker setup."
            ),
        })
    return nudges


def _step_payload(done: set[str], active: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for step_id in _STEP_IDS:
        rows.append({
            "id": step_id,
            "label": _STEP_LABELS[step_id],
            "status": "done" if step_id in done else ("active" if step_id == active else "pending"),
        })
    return rows


def _progress(active: str) -> dict[str, int]:
    try:
        position = _STEP_IDS.index(active) + 1
    except ValueError:
        position = len(_STEP_IDS)
    return {"position": position, "total": len(_STEP_IDS)}


def build_crossover_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    active = bool(status.get("active"))
    if not active:
        return {
            "schema_version": CROSSOVER_ENVELOPE_SCHEMA_VERSION,
            "screen": "not_applicable",
            "active": False,
            "steps": [],
            "verdict_text": (
                "This speaker has no active crossover. Continue with room correction."
            ),
            "nudges": [],
            "relay": _mapping(status.get("relay")) or None,
            "next_action": {
                "id": "room",
                "label": "Correct the room",
                "href": "/correction/room/",
            },
            "progress": {"position": 0, "total": len(_STEP_IDS)},
        }

    unresolved_volume = _mapping(
        _mapping(status.get("level_match")).get("unresolved_volume_safety")
    )
    if unresolved_volume:
        return {
            "schema_version": CROSSOVER_ENVELOPE_SCHEMA_VERSION,
            "screen": "volume_recovery",
            "active": True,
            "steps": _step_payload(set(), "microphone"),
            "verdict_text": (
                "JTS could not confirm that the listening volume was restored. "
                "Recover the exact prior level or the safe emergency attenuation "
                "before continuing. No measurement or apply action is available "
                "until fresh readback confirms one of those levels."
            ),
            "nudges": [
                {
                    "code": "crossover_volume_safety_unresolved",
                    "severity": "warn",
                    "text": (
                        "Pause household playback before recovery. If it still fails, "
                        "stop playback and reapply the speaker profile before listening."
                    ),
                }
            ],
            "relay": _mapping(status.get("relay")) or None,
            "next_action": {
                "id": "recover_volume",
                "label": "Recover safe listening volume",
                "endpoint": "/correction/crossover/recover-volume",
                "body": {},
            },
            "alternate_actions": [],
            "progress": _progress("microphone"),
        }

    drivers = _targets(status, "drivers")
    measurements = _mapping(status.get("measurements"))
    active_comparison_set = _mapping(measurements.get("active_comparison_set"))
    missing_drivers = [
        target
        for target in drivers
        if not _usable_driver_acoustic(
            _driver_record(status, target),
            active_comparison_set,
            target,
        )
    ]
    missing_reference_axis_drivers = [
        target
        for target in drivers
        if not _usable_reference_axis_driver_acoustic(
            _reference_axis_driver_record(status, target),
            active_comparison_set,
            target,
        )
    ]
    level_ready, level_state, level_running = _level_state(status)
    durable_repeat = _mapping(
        _mapping(_mapping(status.get("level_match")).get("repeats")).get("durable")
    )
    repeat_targets = _mapping(durable_repeat.get("targets"))
    from .crossover_eligibility import driver_repeat_completed
    from .capture_geometry import comparison_set_valid

    comparison_set_ready = comparison_set_valid(active_comparison_set)
    level_last = _mapping(_mapping(status.get("level_match")).get("last"))
    level_ramp = _mapping(level_last.get("ramp"))
    level_lock_kind = str(level_ramp.get("lock_kind") or "")
    setup_ready = _setup_ready(status)
    if "driver_safety_profile_evaluation" in status:
        driver_safety_evaluation = _mapping(
            status.get("driver_safety_profile_evaluation")
        )
        driver_safety_authorized = (
            driver_safety_evaluation.get("confirmed_and_current") is True
        )
    else:
        # Legacy/test callers that predate this gate omit the key entirely;
        # do not newly block them here. Every production caller goes through
        # correction_crossover_backend.status_payload(), which always sets
        # this key (None when unreadable, so that path still fails closed).
        driver_safety_evaluation = {}
        driver_safety_authorized = True
    setup_contract = _mapping(status.get("setup"))
    protected_profile = _mapping(setup_contract.get("protected_profile"))
    commissioning_run = _mapping(status.get("commissioning_run"))
    protected_profile_context_id = str(
        protected_profile.get("candidate_fingerprint") or ""
    )
    retained_profile_context_id = str(
        commissioning_run.get("profile_context_id") or ""
    )
    profile_context_id = (
        retained_profile_context_id
        if commissioning_run.get("status") == "current"
        and retained_profile_context_id
        else protected_profile_context_id
    )
    comparison_set_ready = bool(
        comparison_set_ready
        and profile_context_id
        and active_comparison_set.get("profile_context_id") == profile_context_id
    )
    from .crossover_eligibility import automatic_measurement_eligibility

    automatic_measurements = automatic_measurement_eligibility(
        topology_id=str(_mapping(status.get("topology")).get("topology_id") or ""),
        profile_context_id=profile_context_id,
        driver_targets=drivers,
        measurements=measurements,
        repeat_state=durable_repeat,
    )
    # The comparison set durably carries the completed near-field locks and mic
    # identity. After a correction-web restart it is sufficient authority to
    # resume at the next fixed-axis re-level instead of replaying near-field.
    completed_near_field = bool(drivers) and all(
        driver_repeat_completed(
            target,
            repeat_targets,
            capture_geometry="near_field",
        )
        for target in drivers
    )
    level_ready = bool(
        (level_ready and comparison_set_ready)
        or (comparison_set_ready and completed_near_field)
    )
    applied_contract = _mapping(setup_contract.get("applied_crossover"))
    applied_ready = applied_contract.get("valid") is True
    applied_owner = str(applied_contract.get("owner") or "")
    automatic_applied = applied_ready and applied_owner == "automatic"
    automatic_candidate = _mapping(setup_contract.get("automatic_candidate"))
    automatic_candidate_ready = automatic_candidate.get("ready") is True
    manual_preservation = _mapping(setup_contract.get("manual_preservation"))
    manual_candidate_fingerprint = str(
        _mapping(setup_contract.get("baseline_profile")).get("candidate_fingerprint")
        or ""
    )
    automatic_candidate_fingerprint = str(
        automatic_candidate.get("candidate_fingerprint") or ""
    )
    isolated_evidence = _mapping(commissioning_run.get("isolated_evidence"))
    strict_isolated_complete = bool(
        commissioning_run.get("status") == "current"
        and isolated_evidence.get("status") == "complete"
    )
    region_commissioning = _mapping(status.get("region_commissioning"))
    legacy_reapply = _legacy_applied_profile_needs_reapply(status)
    active_comparison_set_id = str(
        active_comparison_set.get("comparison_set_id") or ""
    )
    applied_snapshot = _mapping(_mapping(status.get("applied_profile")).get(
        "recomposition_snapshot"
    ))
    applied_level_match = _mapping(applied_snapshot.get("level_match"))
    applied_comparison_set_id = str(
        applied_level_match.get("active_comparison_set_id") or ""
    )
    # A retune deliberately clears the old comparison set before the first
    # driver is measured.  Between per-driver relay sessions neither the relay
    # nor the ramp is running, so the lease's pending target is the only
    # authoritative signal that the sequential retune is still in progress.
    # Do not let the previously applied automatic profile hide that next step.
    level_sequence_pending = bool(
        _mapping(_mapping(status.get("level_match")).get("next_target"))
    )
    measurement_flow_active = (
        _relay_active(status)
        or _level_run_active(status)
        or level_running
        or level_sequence_pending
    )
    automatic_remeasure = automatic_applied and (
        measurement_flow_active
        or bool(
            active_comparison_set_id
            and active_comparison_set_id != applied_comparison_set_id
        )
    )

    done: set[str] = set()
    if setup_ready and driver_safety_authorized:
        done.add("speaker_setup")
    if level_ready:
        done.add("microphone")
    if (
        drivers
        and automatic_measurements.ready
    ):
        done.add("drivers")
    if strict_isolated_complete:
        done.add("drivers")
    if region_commissioning.get("status") in {"measured", "candidate_ready"}:
        done.add("alignment")
    if region_commissioning.get("status") == "verified":
        done.update(_STEP_IDS)
    if applied_ready and not automatic_remeasure and not strict_isolated_complete:
        done.add("apply")
    if automatic_applied and not automatic_remeasure:
        done.update({"microphone", "drivers"})

    nudges: list[dict[str, str]] = []
    level_run = _level_run(status)
    level_run_phase = str(level_run.get("phase") or "")
    level_run_unavailable = level_run.get("terminal_reason") == "state_unavailable"
    if level_run_unavailable:
        nudges.append({
            "code": "crossover_level_run_state_unavailable",
            "severity": "warn",
            "text": (
                "Level-check safety state is unavailable. JTS will not start "
                "another check until that state is repaired."
            ),
        })
    elif level_run.get("late_success") is True:
        nudges.append({
            "code": "crossover_level_run_late_success",
            "severity": "info",
            "text": (
                "The phone's measurement window ended first, but JTS correlated "
                "the later backend completion to the same exact run and saved it."
            ),
        })
    elif level_run_phase == "interrupted":
        nudges.append({
            "code": "crossover_level_run_interrupted",
            "severity": "warn",
            "text": (
                "The correction service restarted during the level check. The old "
                "run was closed and cannot complete a retry."
            ),
        })
    elif level_run_phase == "failed":
        nudges.append({
            "code": "crossover_level_run_failed",
            "severity": "warn",
            "text": (
                "The exact level-check run did not complete. Retry when the phone "
                "and protected speaker setup are ready."
            ),
        })
    elif level_run.get("phone_timeout") is True:
        nudges.append({
            "code": "crossover_level_run_phone_timeout",
            "severity": "warn",
            "text": (
                "The phone window ended, but JTS is still reconciling the same "
                "bounded backend run. Do not start another level check yet."
            ),
        })
    if level_state == "maxed_out":
        nudges.append({
            "code": "external_amplifier_too_low",
            "severity": "warn",
            "text": (
                "The microphone was still too quiet at the safe software limit. "
                "Raise the external amplifier a little, then retry."
            ),
        })
    elif level_ready and level_lock_kind == "bounded_low_level":
        shortfall = level_ramp.get("window_shortfall_db")
        shortfall_text = (
            f" ({float(shortfall):.1f} dB below the preferred window)"
            if isinstance(shortfall, (int, float))
            else ""
        )
        nudges.append({
            "code": "bounded_low_measurement_level",
            "severity": "warn",
            "text": (
                "The microphone level is stable and safe but lower than preferred"
                f"{shortfall_text}. JTS will verify each sweep before using it."
            ),
        })

    if durable_repeat.get("status") == "unavailable":
        level_ready = False
        done.discard("microphone")
        nudges.append({
            "code": "crossover_repeat_admission_unavailable",
            "severity": "warn",
            "text": (
                "Repeat safety state is unavailable. Run the microphone level "
                "check again before another sweep."
            ),
        })
    from .crossover_eligibility import driver_repeat_completed

    driver_target_ids = set().union(
        *(_driver_repeat_target_ids(target) for target in drivers)
    ) if drivers else set()
    exact_completed_controller_targets: set[str] = set()
    from .capture_geometry import driver_repeat_binding

    for target in drivers:
        for geometry in ("near_field", "reference_axis"):
            if not driver_repeat_completed(
                target,
                repeat_targets,
                capture_geometry=geometry,
            ):
                continue
            try:
                target_id, _fingerprint = driver_repeat_binding(
                    speaker_group_id=str(target.get("speaker_group_id") or ""),
                    role=str(target.get("role") or ""),
                    target_fingerprint=str(target.get("target_fingerprint") or ""),
                    capture_geometry=geometry,
                )
            except ValueError:
                continue
            exact_completed_controller_targets.add(target_id)
    blocked_controller_targets = []
    terminal_controller_targets = []
    interrupted_controller_targets = []
    for target_id, entry in repeat_targets.items():
        if not isinstance(entry, Mapping) or target_id not in driver_target_ids:
            continue
        orphaned_inflight = (
            entry.get("status") == "active"
            and bool(entry.get("inflight"))
            and not _relay_active(status)
        )
        # ready means the acoustic aggregate passed but the controller has not
        # durably observed final measurement completion. It blocks apply even
        # when that measurement write happened just before the controller write
        # failed; a fresh level-check context safely resets the gate.
        final_write_incomplete = entry.get("status") == "ready"
        terminal_set = entry.get("status") in {"aborted", "refused", "malformed"} or (
            entry.get("status") == "completed"
            and target_id not in exact_completed_controller_targets
        )
        if orphaned_inflight or final_write_incomplete or terminal_set:
            blocked_controller_targets.append(str(target_id))
        if terminal_set:
            terminal_controller_targets.append(str(target_id))
        elif orphaned_inflight or final_write_incomplete:
            interrupted_controller_targets.append(str(target_id))
    if blocked_controller_targets:
        level_ready = False
        done.discard("microphone")
    if interrupted_controller_targets:
        nudges.append({
            "code": "crossover_repeat_persistence_interrupted",
            "severity": "warn",
            "text": (
                "A repeat result could not be finished safely. Its attempt is "
                "preserved; run the microphone level check again before playback."
            ),
        })
    latest_rejection: Mapping[str, Any] = {}
    latest_status = ""
    from .crossover_eligibility import finite_float, mapping_sequence

    for entry in repeat_targets.values():
        if not isinstance(entry, Mapping):
            continue
        results = mapping_sequence(entry.get("results"))
        candidate = _mapping(results[-1]) if results else {}
        if candidate.get("accepted") is not True:
            latest_rejection = candidate
            latest_status = str(entry.get("status") or "")
    if latest_rejection or latest_status in {"refused", "aborted"}:
        reason = str(latest_rejection.get("reject_reason") or latest_status)
        # Infra failures (DSP-load convergence timeouts, gate/admission
        # refusals, other pre-tone transport errors — recorded with
        # `phase: "transport"` by `_finish_failed_repeat_attempt` in
        # correction_setup.py) never played a tone, so telling the operator
        # to "keep the room quiet" is actively misleading: there is nothing
        # in the room to fix. `repeat_admission.finish()` already defaults an
        # absent phase to "acoustic" for its own logging, so this reads the
        # same existing field rather than inventing a new classification.
        if latest_rejection.get("phase") == "transport":
            text = (
                "That attempt didn't finish on the speaker's side — nothing "
                "to fix in the room. Try again."
            )
        elif latest_rejection.get("clipping") is True:
            text = "The latest sweep clipped. Keep the microphone still and reduce the input gain."
        elif (shortfall := finite_float(
            latest_rejection.get("snr_shortfall_db")
        )) is not None:
            band = str(latest_rejection.get("worst_band_id") or "required")
            text = (
                f"The latest sweep needs {shortfall:.1f} dB "
                f"more SNR in the {band.replace('_', ' ')} band. Quiet the room and retry."
            )
        elif latest_rejection.get("above_validity_floor") is False:
            floor = finite_float(latest_rejection.get("validity_floor_hz"))
            suffix = f" ({floor:.0f} Hz floor)" if floor is not None else ""
            text = f"The latest sweep is below the reliable frequency floor{suffix}. Recheck microphone placement."
        else:
            text = f"The latest sweep was not usable ({reason.replace('_', ' ')}). Keep the room quiet and retry."
        nudges.append({
            "code": "crossover_repeat_rejected",
            "severity": "warn",
            "text": text,
        })

    alternate_actions: list[dict[str, Any]] = []
    if not setup_ready or not driver_safety_authorized:
        screen = "speaker_setup"
        if not setup_ready:
            verdict = (
                "Finish the protected speaker setup first. This proves the output map "
                "and tweeter protection before a microphone sweep can play."
            )
        else:
            # setup_ready is true but the driver safety profile itself is not
            # confirmed-and-current: do not offer any measurement action (level
            # match / sweeps) here, since the deep excitation admission would
            # only refuse it later, after locks and acceptance repeats.
            verdict = (
                "Confirm the driver safety details in speaker setup before "
                "measuring. JTS will not start a microphone sweep until each "
                "driver's safe playback limits are confirmed."
            )
            nudges.extend(_driver_safety_profile_nudges(driver_safety_evaluation))
        action: dict[str, Any] | None = {
            "id": "speaker_setup",
            "label": "Finish speaker setup",
            "href": "/sound/",
        }
        active_step = "speaker_setup"
    elif level_run_unavailable and not strict_isolated_complete:
        screen = "microphone"
        verdict = (
            "The level-check safety record cannot be read, so JTS is refusing "
            "another measurement run. Check the correction service diagnostics "
            "before retrying."
        )
        action = None
        active_step = "microphone"
    elif blocked_controller_targets and not strict_isolated_complete:
        screen = "microphone"
        verdict = (
            "The repeat sequence ended and cannot be resumed. Run the driver "
            "level check again before measuring or applying automatic trims."
            if terminal_controller_targets
            else (
                "The repeat result is safely held, but its final persistence did "
                "not complete. Run the driver level check again before measuring "
                "or applying automatic trims."
            )
        )
        action = {
            "id": "level_match",
            "label": "Restart driver level check",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"
    elif (
        not strict_isolated_complete
        and legacy_reapply
        and not (measurement_flow_active or level_ready)
    ):
        screen = "choose_tuning"
        if manual_preservation.get("ready") is True:
            verdict = (
                "Your current manual crossover is safe. Keep it as-is, or choose "
                "automatic driver level matching to measure and replace its trims."
            )
            action = {
                "id": "keep_manual",
                "label": "Keep current manual crossover",
                "endpoint": "/correction/crossover/apply",
                "body": {
                    "tuning_owner": "manual",
                    "expected_candidate_fingerprint": manual_candidate_fingerprint,
                },
            }
            alternate_actions = [{
                "id": "tune_automatic",
                "label": "Level-match drivers automatically",
                "endpoint": "/correction/crossover/level-match",
                "body": {},
            }, {
                "id": "edit_manual",
                "label": "Edit manual crossover",
                "href": "/sound/",
            }]
        else:
            verdict = str(
                manual_preservation.get("detail")
                or "The crossover inputs changed after the current manual profile was applied."
            )
            action = {
                "id": "edit_manual",
                "label": "Edit manual crossover",
                "href": "/sound/",
            }
            alternate_actions = [{
                "id": "tune_automatic",
                "label": "Level-match drivers automatically",
                "endpoint": "/correction/crossover/level-match",
                "body": {},
            }]
        active_step = "apply"
    elif (
        not strict_isolated_complete
        and applied_ready
        and applied_owner == "manual"
        and not (measurement_flow_active or level_ready)
    ):
        screen = "done_manual"
        verdict = "Your manual crossover is applied and ready for room correction."
        action = {
            "id": "room",
            "label": "Correct the room",
            "href": "/correction/room/",
        }
        alternate_actions = [
            {
                "id": "tune_automatic",
                "label": "Level-match drivers automatically",
                "endpoint": "/correction/crossover/level-match",
                "body": {},
            },
            {
                "id": "edit_manual",
                "label": "Edit manual crossover",
                "href": "/sound/",
            },
        ]
        active_step = "apply"
    elif (
        not strict_isolated_complete
        and automatic_applied
        and not automatic_remeasure
    ):
        screen = "done"
        verdict = (
            "The automatic driver trims are applied with your crossover frequency "
            "and slope, and the speaker is ready for room correction."
        )
        action = {
            "id": "room",
            "label": "Correct the room",
            "href": "/correction/room/",
        }
        active_step = "apply"
        alternate_actions = [
            {
                "id": "retune_automatic",
                "label": "Level-match drivers again",
                "endpoint": "/correction/crossover/level-match",
                "body": {},
            },
            {
                "id": "edit_manual",
                "label": "Set crossover manually",
                "href": "/sound/",
            },
        ]
    elif _relay_active(status) or _level_run_active(status) or level_running:
        screen = "waiting"
        relay_phase = str(_mapping(status.get("relay")).get("status") or "")
        verdict = (
            "JTS is stopping playback and restoring the speaker safely."
            if relay_phase == "stopping"
            else "The phone is finishing and uploading this measurement."
            if relay_phase == "finishing"
            else "JTS is saving the verified measurement."
            if relay_phase == "committing"
            else
            "JTS is finishing the same exact level-check run. This page will "
            "advance automatically when its terminal result is saved."
            if level_run.get("phone_timeout") is True
            else "Continue on the phone. This page will advance automatically when it finishes."
        )
        action = None
        active_step = "microphone" if (
            level_running or _relay_kind(status).startswith("level_ramp:")
        ) else (
            "alignment"
            if _relay_kind(status) == "crossover_sweep:summed"
            else "drivers"
        )
    elif not strict_isolated_complete and not level_ready and missing_drivers:
        level = _mapping(status.get("level_match"))
        next_target = _mapping(level.get("next_target"))
        if not next_target and missing_drivers:
            next_target = missing_drivers[0]
        next_role = str(next_target.get("role") or "driver")
        next_frequency = next_target.get("tone_frequency_hz")
        frequency_text = (
            f" at {float(next_frequency):g} Hz"
            if isinstance(next_frequency, (int, float))
            else ""
        )
        screen = "microphone"
        verdict = (
            f"Position the microphone for the {next_role}. JTS will play its "
            f"protected passband tone{frequency_text} and raise the level gradually "
            "from quiet. Choose a calibration file if you have one; continuing "
            "without one is supported."
        )
        action = {
            "id": "level_match",
            "label": (
                f"Retry {next_role} level check"
                if level_state in {"error", "maxed_out", "cancelled"}
                else f"Set {next_role} microphone level"
            ),
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"
    elif not strict_isolated_complete and missing_drivers:
        from .capture_geometry import driver_placement_instruction
        from .crossover_eligibility import repeat_progress, render_repeat_progress

        target = missing_drivers[0]
        role = str(target.get("role") or "driver")
        target_id = f"{target.get('speaker_group_id') or ''}:{role}"
        if not _driver_confirmed(status, target):
            # The pre-sweep gate (measurement.current_driver_floor_evidence)
            # will refuse this driver until it is re-confirmed by ear, no
            # matter how many repeat attempts are offered -- offering
            # "Measure {role}" here is a dead end that fails at the same
            # gate every time. Route to the actual remedy instead.
            screen = "driver"
            verdict = (
                f"The {role}'s driver confirmation needs to be redone -- "
                "there is nothing to fix in the room. Confirm the "
                f"{role} by ear, then measure it again."
            )
            action = {
                "id": "confirm_driver",
                "label": f"Confirm {role} by ear",
                "endpoint": "/correction/crossover/driver-test",
                "body": {
                    "speaker_group_id": str(target.get("speaker_group_id") or ""),
                    "role": role,
                },
            }
            active_step = "drivers"
        else:
            progress = repeat_progress(
                _mapping(status.get("level_match")).get("repeats"), target_id
            )
            repeat_failure = progress.failure
            if repeat_failure:
                nudges.append({
                    "code": "driver_repeat_capture_refused",
                    "severity": "warn",
                    "text": (
                        "Too few repeat sweeps cleared the per-band SNR and clipping "
                        "checks, or the service restarted mid-set. Quiet the room or "
                        "adjust the external amplifier, then run the driver level "
                        "check again before measuring."
                    ),
                })
            attempts = progress.attempts
            repeat_copy = render_repeat_progress(progress)
            if repeat_failure:
                screen = "microphone"
                verdict = (
                    f"The bounded repeat set for the {role} cannot continue. "
                    "Its attempts were preserved; run the driver level check to "
                    "start a fresh comparison-bound set."
                )
                action = {
                    "id": "level_match",
                    "label": f"Restart {role} driver level check",
                    "endpoint": "/correction/crossover/level-match",
                    "body": {},
                }
                active_step = "microphone"
            elif progress.completed:
                # The repeat set finished every bounded attempt and each was
                # individually accepted, but the target is still in
                # missing_drivers -- the accepted evidence itself is unusable
                # (e.g. per-band SNR "insufficient"). Offering "repeat N+1" here
                # is a dead end: repeat_admission.reserve() refuses a completed
                # set (JTS3 run 13 hit exactly this, looping on an action that
                # failed at reservation). Render the honest terminal instead.
                screen = "microphone"
                verdict = _completed_insufficient_verdict(role, progress)
                action = {
                    "id": "level_match",
                    "label": f"Restart {role} driver level check",
                    "endpoint": "/correction/crossover/level-match",
                    "body": {},
                }
                active_step = "microphone"
            else:
                screen = "driver"
                verdict = (
                    f"Measure the {role}. {driver_placement_instruction(role)} "
                    "JTS will use the safe protected path and saved level."
                    f"{repeat_copy}"
                )
                action = {
                    "id": "measure_driver",
                    "label": (
                        f"Measure {role} — repeat {attempts + 1}"
                        if attempts
                        else f"Position the mic, then measure {role}"
                    ),
                    "endpoint": "/correction/crossover/relay-capture",
                    "body": {
                        "kind": "driver",
                        "speaker_group_id": str(target.get("speaker_group_id") or ""),
                        "role": role,
                    },
                }
                active_step = "drivers"
    elif (
        not strict_isolated_complete
        and missing_reference_axis_drivers
        and not completed_near_field
    ):
        screen = "microphone"
        verdict = (
            "The near-field acoustics are saved, but their exact repeat ledger "
            "is not complete for the current driver topology. Run the complete "
            "driver level check again before moving the microphone to the "
            "fixed reference axis."
        )
        action = {
            "id": "level_match",
            "label": "Restart driver level check",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"
    elif not strict_isolated_complete and missing_reference_axis_drivers:
        from .capture_geometry import reference_axis_driver_placement_instruction
        from .crossover_eligibility import repeat_progress, render_repeat_progress

        target = missing_reference_axis_drivers[0]
        group_id = str(target.get("speaker_group_id") or "")
        role = str(target.get("role") or "driver")
        from .capture_geometry import driver_repeat_binding

        try:
            repeat_target_id, _repeat_fingerprint = driver_repeat_binding(
                speaker_group_id=group_id,
                role=role,
                target_fingerprint=str(target.get("target_fingerprint") or ""),
                capture_geometry="reference_axis",
            )
        except ValueError:
            repeat_target_id = ""
        progress = repeat_progress(
            _mapping(status.get("level_match")).get("repeats"), repeat_target_id
        )
        repeat_failure = progress.failure
        reference_axis_locks = _mapping(
            _mapping(status.get("level_match")).get(
                "reference_axis_driver_locks"
            )
        )
        physical_target_id = f"{group_id}:{role}"
        reference_axis_level_locked = physical_target_id in reference_axis_locks
        attempts = progress.attempts
        repeat_copy = render_repeat_progress(progress)
        if not reference_axis_level_locked:
            screen = "microphone"
            verdict = (
                f"Keep the microphone on the fixed reference axis for the {role}. "
                "JTS will set a new safe, non-clipping level for this distance "
                "before the repeat sweeps."
            )
            action = {
                "id": "level_match_reference_axis_driver",
                "label": f"Set fixed-axis {role} microphone level",
                "endpoint": "/correction/crossover/level-match",
                "body": {
                    "capture_geometry": "reference_axis",
                    "speaker_group_id": group_id,
                    "role": role,
                },
            }
            active_step = "microphone"
        elif repeat_failure:
            nudges.append({
                "code": "reference_axis_repeat_capture_refused",
                "severity": "warn",
                "text": (
                    "The fixed-axis repeat set did not produce enough valid "
                    "evidence. Keep the microphone position unchanged, quiet "
                    "the room, and restart the complete driver level check."
                ),
            })
            screen = "microphone"
            verdict = (
                f"The bounded fixed-axis repeat set for the {role} cannot "
                "continue. Its attempts were preserved; restart the driver "
                "level check to create a fresh comparison-bound set."
            )
            action = {
                "id": "level_match",
                "label": f"Restart {role} driver level check",
                "endpoint": "/correction/crossover/level-match",
                "body": {},
            }
            active_step = "microphone"
        elif progress.completed:
            # Same terminal shape as the near-field branch above: the fixed-
            # axis repeat set finished every bounded, individually-accepted
            # attempt, but the target is still unusable (insufficient SNR).
            # No fifth attempt exists to offer; render the honest terminal.
            screen = "microphone"
            verdict = _completed_insufficient_verdict(role, progress)
            action = {
                "id": "level_match",
                "label": f"Restart {role} driver level check",
                "endpoint": "/correction/crossover/level-match",
                "body": {},
            }
            active_step = "microphone"
        else:
            screen = "driver_reference_axis"
            verdict = (
                f"Measure the {role} from the fixed reference axis. "
                f"{reference_axis_driver_placement_instruction(role)} "
                "JTS reuses the protected level and gates the response at "
                f"the first room reflection.{repeat_copy}"
            )
            action = {
                "id": "measure_reference_axis_driver",
                "label": (
                    f"Measure fixed-axis {role} — repeat {attempts + 1}"
                    if attempts
                    else f"Fix the mic on-axis, then measure {role}"
                ),
                "endpoint": "/correction/crossover/relay-capture",
                "body": {
                    "kind": "driver",
                    "speaker_group_id": group_id,
                    "role": role,
                    "capture_geometry": "reference_axis",
                },
            }
            active_step = "drivers"
    elif not strict_isolated_complete and not automatic_measurements.ready:
        screen = "microphone"
        verdict = (
            "The current driver acoustics or their exact repeat ledger are not "
            "complete for this protected topology and profile. Restart the "
            "driver level check before applying automatic trims."
        )
        action = {
            "id": "level_match",
            "label": "Restart driver level check",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"
    elif commissioning_run.get("status") == "current" and not strict_isolated_complete:
        screen = "microphone"
        verdict = (
            "The fixed-axis driver captures did not complete the strict "
            "commissioning set. Keep the microphone fixed and restart the "
            "driver level check before combined alignment."
        )
        action = {
            "id": "level_match",
            "label": "Restart driver measurements",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"
    elif strict_isolated_complete and region_commissioning.get("status") == "needs_geometry":
        geometry_target = _mapping(region_commissioning.get("next_geometry"))
        lower_role = str(geometry_target.get("lower_role") or "lower driver")
        upper_role = str(geometry_target.get("upper_role") or "upper driver")
        fc_hz = geometry_target.get("fc_hz")
        fc_text = (
            f" at {float(fc_hz):g} Hz"
            if isinstance(fc_hz, (int, float))
            else ""
        )
        screen = "alignment_geometry"
        verdict = (
            f"Confirm the signed acoustic-path estimate for the {lower_role} "
            f"and {upper_role}{fc_text}. Enter {lower_role} path minus "
            f"{upper_role} path in millimetres; enter 0 only when you are "
            "explicitly attesting equal path length."
        )
        action = {
            "id": "attest_region_geometry",
            "label": "Confirm signed geometry",
            "endpoint": "/correction/crossover/region-geometry",
            "body": {
                "expected_target_fingerprint": str(
                    geometry_target.get("target_fingerprint") or ""
                ),
            },
            "fields": [
                {
                    "name": "signed_acoustic_path_difference_mm",
                    "label": (
                        f"{lower_role} path minus {upper_role} path (mm)"
                    ),
                    "type": "number",
                    "step": "0.1",
                    "required": True,
                }
            ],
        }
        active_step = "alignment"
    elif strict_isolated_complete and region_commissioning.get("status") == "collecting":
        stage_text = "next server-selected combined response"
        screen = "alignment"
        verdict = (
            f"Keep the microphone fixed. Measure the {stage_text} "
            "capture. JTS chooses everything else — position, polarity, "
            "and repeat count."
        )
        action = {
            "id": "measure_region_alignment",
            "label": f"Measure {stage_text}",
            "endpoint": "/correction/crossover/relay-capture",
            "body": {"kind": "summed"},
        }
        active_step = "alignment"
    elif strict_isolated_complete and region_commissioning.get("status") == "measured":
        screen = "review"
        verdict = (
            "Driver level, polarity, and bounded relative-delay evidence are "
            "complete. Candidate publication was interrupted; resume the exact "
            "stored evidence evaluation."
        )
        action = {
            "id": "prepare_measured_candidate",
            "label": "Prepare measured candidate",
            "endpoint": "/correction/crossover/candidate",
            "body": {},
        }
        active_step = "apply"
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "candidate_ready"
    ):
        screen = "review"
        verdict = (
            "Review the measured crossover candidate below. Frequency, filter "
            "family, and order stay as you set them; attenuation and delay come "
            "from the fixed-axis evidence, and normal-versus-reverse evidence "
            "retains the shown polarity."
        )
        candidate = _mapping(region_commissioning.get("candidate"))
        action = {
            "id": "apply_measured_candidate",
            "label": "Apply reviewed crossover",
            "endpoint": "/correction/crossover/apply",
            "body": {
                "tuning_owner": "automatic",
                "expected_candidate_fingerprint": str(
                    candidate.get("fingerprint") or ""
                ),
            },
        }
        active_step = "apply"
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "apply_finalization_required"
    ):
        candidate = _mapping(region_commissioning.get("candidate"))
        screen = "review"
        verdict = str(region_commissioning.get("detail") or "Finish applying.")
        action = {
            "id": "finish_measured_candidate_apply",
            "label": "Finish apply",
            "endpoint": "/correction/crossover/apply",
            "body": {
                "tuning_owner": "automatic",
                "expected_candidate_fingerprint": str(
                    candidate.get("fingerprint") or ""
                ),
            },
        }
        active_step = "apply"
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "apply_rolled_back"
    ):
        candidate = _mapping(region_commissioning.get("candidate"))
        screen = "review"
        verdict = str(region_commissioning.get("detail") or "Apply was restored.")
        action = {
            "id": "retry_measured_candidate_apply",
            "label": "Retry reviewed crossover",
            "endpoint": "/correction/crossover/apply",
            "body": {
                "tuning_owner": "automatic",
                "expected_candidate_fingerprint": str(
                    candidate.get("fingerprint") or ""
                ),
            },
        }
        active_step = "apply"
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "verification_failed"
    ):
        screen = "review"
        verdict = (
            "The applied crossover did not pass the three fixed-axis combined-response "
            "captures. Room correction remains locked. Return to speaker setup to "
            "edit the crossover or begin a fresh measurement sequence."
        )
        action = {
            "id": "edit_after_verification_failure",
            "label": "Back to speaker setup",
            "href": "/sound/",
        }
        active_step = "alignment"
        nudges.append({
            "code": "post_apply_verification_failed",
            "severity": "warn",
            "text": "The retained graph is known, but it has no verified Room authority.",
        })
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "applied_unverified"
    ):
        verification = _mapping(region_commissioning.get("verification"))
        next_target = _mapping(verification.get("next_target"))
        captured = int(next_target.get("captured_repeats") or 0)
        required = int(next_target.get("required_repeats") or 3)
        screen = "alignment"
        verdict = (
            "The reviewed crossover is applied and freshly read back. Keep the "
            "microphone at the same fixed axis for combined-response verification. "
            f"{captured} of {required} verification captures are saved."
        )
        action = {
            "id": "measure_post_apply_verification",
            "label": f"Verify combined response — capture {captured + 1}",
            "endpoint": "/correction/crossover/relay-capture",
            "body": {"kind": "verification"},
        }
        active_step = "alignment"
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "verified"
    ):
        verification = _mapping(region_commissioning.get("verification"))
        receipt = _mapping(verification.get("receipt"))
        screen = "done"
        verdict = (
            "The applied crossover passed all fixed-axis combined-response "
            "captures. Room correction is now available."
        )
        action = {
            "id": "room",
            "label": "Continue to Room correction",
            "href": "/correction/room/",
        }
        active_step = "complete"
        if receipt.get("fingerprint"):
            nudges.append({
                "code": "active_commissioning_verified",
                "severity": "ok",
                "text": "Verified receipt: " + str(receipt["fingerprint"])[:12],
            })
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "restore_finalization_required"
    ):
        screen = "apply"
        verdict = str(
            region_commissioning.get("detail")
            or "Finish the already-proved restore before continuing."
        )
        action = {
            "id": "finish_candidate_restore",
            "label": "Finish restore",
            "endpoint": "/correction/crossover/restore",
            "body": {},
        }
        active_step = "apply"
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "restore_required"
    ):
        screen = "apply"
        verdict = str(
            region_commissioning.get("detail")
            or "Restore the previous crossover before continuing."
        )
        action = {
            "id": "restore_candidate_predecessor",
            "label": "Restore previous crossover",
            "endpoint": "/correction/crossover/restore",
            "body": {},
        }
        active_step = "apply"
    elif (
        strict_isolated_complete
        and region_commissioning.get("status") == "candidate_refused"
    ):
        candidate_failure = _mapping(
            region_commissioning.get("candidate_failure")
        )
        nudges.append({
            "code": "measured_candidate_refused",
            "severity": "warn",
            "text": (
                "The saved measurements remain available for diagnosis, but "
                "they did not authorize an automatic crossover candidate."
            ),
        })
        screen = "microphone"
        verdict = str(
            region_commissioning.get("detail")
            or candidate_failure.get("detail")
            or "Restart the complete driver and alignment measurement sequence."
        )
        action = {
            "id": "level_match",
            "label": "Restart driver and alignment measurements",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"
    elif strict_isolated_complete and region_commissioning.get("status") == "unavailable":
        screen = "alignment"
        verdict = str(
            region_commissioning.get("detail")
            or "Combined crossover commissioning authority is unavailable."
        )
        action = None
        active_step = "alignment"
    elif automatic_candidate_ready:
        screen = "apply"
        replacing_manual = applied_ready and applied_owner == "manual"
        updating_automatic = applied_ready and applied_owner == "automatic"
        verdict = (
            "The automatic driver level measurements are complete. Review and "
            "explicitly replace your manual driver trims; crossover frequency and "
            "slope stay unchanged."
            if replacing_manual
            else (
                "The new driver level measurements are complete. Apply the updated "
                "driver trims."
                if updating_automatic
                else "The driver level measurements are complete. Apply the matched trims."
            )
        )
        action = {
            "id": "replace_manual" if replacing_manual else "apply_automatic",
            "label": (
                "Replace manual trims with automatic levels"
                if replacing_manual
                else (
                    "Apply updated driver levels"
                    if updating_automatic
                    else "Apply matched driver levels"
                )
            ),
            "endpoint": "/correction/crossover/apply",
            "body": {
                "tuning_owner": "automatic",
                "expected_candidate_fingerprint": automatic_candidate_fingerprint,
            },
        }
        active_step = "apply"
    else:
        screen = "microphone"
        verdict = str(
            automatic_candidate.get("detail")
            or "The automatic measurements are not usable yet. Repeat the guided level check."
        )
        action = {
            "id": "level_match",
            "label": "Repeat microphone and level check",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"

    return {
        "schema_version": CROSSOVER_ENVELOPE_SCHEMA_VERSION,
        "screen": screen,
        "active": True,
        "steps": _step_payload(done, active_step),
        "verdict_text": verdict,
        "nudges": nudges,
        "relay": _mapping(status.get("relay")) or None,
        "next_action": action,
        "alternate_actions": alternate_actions,
        "progress": _progress(active_step),
        "candidate_review": (
            _mapping(region_commissioning.get("candidate"))
            if region_commissioning.get("status") == "candidate_ready"
            else None
        ),
    }


def build_crossover_envelope_logged(status: Mapping[str, Any]) -> dict[str, Any]:
    envelope = build_crossover_envelope(status)
    log_event(
        logger,
        "correction.crossover_envelope_serve",
        screen=envelope["screen"],
        active=envelope["active"],
        step_count=len(envelope["steps"]),
        nudge_count=len(envelope["nudges"]),
        action=(envelope.get("next_action") or {}).get("id"),
        alternate_action_count=len(envelope.get("alternate_actions") or []),
    )
    return envelope
