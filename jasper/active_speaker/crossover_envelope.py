# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure screen envelope for sequential Layer-A acoustic commissioning.

``/sound/`` owns topology, driver protection, output identity, and the safe
starting profile.  This envelope owns the distinct microphone journey:

    mic/calibration + per-driver level -> driver sweeps -> atomic apply

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

CROSSOVER_ENVELOPE_SCHEMA_VERSION = 2

_STEP_IDS = ("speaker_setup", "microphone", "drivers", "apply")
_STEP_LABELS = {
    "speaker_setup": "Protected speaker setup",
    "microphone": "Microphone and level",
    "drivers": "Measure each driver",
    "apply": "Apply speaker profile",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _targets(status: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [
        item for item in _list(_mapping(status.get("targets")).get(kind))
        if isinstance(item, Mapping)
    ]


def _summary(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(status.get("measurements")).get("summary"))


def _driver_record(status: Mapping[str, Any], target: Mapping[str, Any]) -> Mapping[str, Any]:
    latest = _mapping(_summary(status).get("latest_driver_measurements"))
    key = f"{target.get('speaker_group_id') or ''}:{target.get('role') or ''}"
    return _mapping(latest.get(key))


def _usable_driver_acoustic(
    record: Mapping[str, Any],
    active_comparison_set: Mapping[str, Any],
) -> bool:
    from .capture_geometry import (
        DRIVER_PLACEMENT_POLICY_ID,
        capture_proof_valid,
    )

    acoustic = _mapping(record.get("acoustic"))
    return bool(
        acoustic.get("verdict") == "present"
        and record.get("mic_clipping") is not True
        and acoustic.get("mic_clipping") is not True
        and capture_proof_valid(
            record,
            active_comparison_set,
            policy_id=DRIVER_PLACEMENT_POLICY_ID,
            role=str(record.get("role") or ""),
            speaker_group_id=str(record.get("speaker_group_id") or ""),
        )
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
    return str(relay.get("status") or "") in {"starting", "awaiting_phone"}


def _relay_kind(status: Mapping[str, Any]) -> str:
    return str(_mapping(status.get("relay")).get("kind") or "")


def _setup_ready(status: Mapping[str, Any]) -> bool:
    setup = _mapping(status.get("setup"))
    return setup.get("active") is True and setup.get("status") == "ready"


def _legacy_applied_profile_needs_reapply(status: Mapping[str, Any]) -> bool:
    applied = _mapping(status.get("applied_profile"))
    return bool(
        applied.get("status") == "applied"
        and not _mapping(applied.get("recomposition_snapshot"))
    )


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

    drivers = _targets(status, "drivers")
    measurements = _mapping(status.get("measurements"))
    active_comparison_set = _mapping(measurements.get("active_comparison_set"))
    missing_drivers = [
        target
        for target in drivers
        if not _usable_driver_acoustic(
            _driver_record(status, target),
            active_comparison_set,
        )
    ]
    level_ready, level_state, level_running = _level_state(status)
    from .capture_geometry import comparison_set_valid

    level_ready = level_ready and comparison_set_valid(active_comparison_set)
    level_last = _mapping(_mapping(status.get("level_match")).get("last"))
    level_ramp = _mapping(level_last.get("ramp"))
    level_lock_kind = str(level_ramp.get("lock_kind") or "")
    setup_ready = _setup_ready(status)
    setup_contract = _mapping(status.get("setup"))
    applied_contract = _mapping(setup_contract.get("applied_crossover"))
    applied_ready = applied_contract.get("valid") is True
    applied_owner = str(applied_contract.get("owner") or "")
    automatic_applied = applied_ready and applied_owner == "automatic"
    automatic_candidate = _mapping(setup_contract.get("automatic_candidate"))
    automatic_candidate_ready = automatic_candidate.get("ready") is True
    manual_preservation = _mapping(setup_contract.get("manual_preservation"))
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
        _relay_active(status) or level_running or level_sequence_pending
    )
    automatic_remeasure = automatic_applied and (
        measurement_flow_active
        or bool(
            active_comparison_set_id
            and active_comparison_set_id != applied_comparison_set_id
        )
    )

    done: set[str] = set()
    if setup_ready:
        done.add("speaker_setup")
    if level_ready:
        done.add("microphone")
    if drivers and not missing_drivers:
        done.add("drivers")
    if applied_ready:
        done.add("apply")
    if automatic_applied and not automatic_remeasure:
        done.update({"microphone", "drivers"})

    nudges: list[dict[str, str]] = []
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

    durable_repeat = _mapping(
        _mapping(_mapping(status.get("level_match")).get("repeats")).get("durable")
    )
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
    repeat_targets = _mapping(durable_repeat.get("targets"))
    driver_target_ids = {
        f"{target.get('speaker_group_id') or ''}:{target.get('role') or ''}"
        for target in drivers
    }
    blocked_controller_targets = []
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
        if orphaned_inflight or final_write_incomplete:
            blocked_controller_targets.append(str(target_id))
    if blocked_controller_targets:
        level_ready = False
        done.discard("microphone")
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
    for entry in repeat_targets.values():
        if not isinstance(entry, Mapping):
            continue
        results = _list(entry.get("results"))
        candidate = _mapping(results[-1]) if results else {}
        if candidate.get("accepted") is not True or entry.get("status") in {
            "refused", "aborted"
        }:
            latest_rejection = candidate
            latest_status = str(entry.get("status") or "")
    if latest_rejection or latest_status in {"refused", "aborted"}:
        reason = str(latest_rejection.get("reject_reason") or latest_status)
        if latest_rejection.get("clipping") is True:
            text = "The latest sweep clipped. Keep the microphone still and reduce the input gain."
        elif isinstance(latest_rejection.get("snr_shortfall_db"), (int, float)):
            band = str(latest_rejection.get("worst_band_id") or "required")
            text = (
                f"The latest sweep needs {float(latest_rejection['snr_shortfall_db']):.1f} dB "
                f"more SNR in the {band.replace('_', ' ')} band. Quiet the room and retry."
            )
        elif latest_rejection.get("above_validity_floor") is False:
            floor = latest_rejection.get("validity_floor_hz")
            suffix = f" ({float(floor):.0f} Hz floor)" if isinstance(floor, (int, float)) else ""
            text = f"The latest sweep is below the reliable frequency floor{suffix}. Recheck microphone placement."
        else:
            text = f"The latest sweep was not usable ({reason.replace('_', ' ')}). Keep the room quiet and retry."
        nudges.append({
            "code": "crossover_repeat_rejected",
            "severity": "warn",
            "text": text,
        })

    alternate_actions: list[dict[str, Any]] = []
    if not setup_ready:
        screen = "speaker_setup"
        verdict = (
            "Finish the protected speaker setup first. This proves the output map "
            "and tweeter protection before a microphone sweep can play."
        )
        action: dict[str, Any] | None = {
            "id": "speaker_setup",
            "label": "Finish speaker setup",
            "href": "/sound/",
        }
        active_step = "speaker_setup"
    elif blocked_controller_targets:
        screen = "microphone"
        verdict = (
            "The repeat result is safely held, but its final persistence did "
            "not complete. Run the driver level check again before measuring "
            "or applying automatic trims."
        )
        action = {
            "id": "level_match",
            "label": "Restart driver level check",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        }
        active_step = "microphone"
    elif legacy_reapply and not (measurement_flow_active or level_ready):
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
                "body": {"tuning_owner": "manual"},
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
    elif applied_ready and applied_owner == "manual" and not (
        measurement_flow_active or level_ready
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
    elif automatic_applied and not automatic_remeasure:
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
    elif _relay_active(status) or level_running:
        screen = "waiting"
        verdict = "Continue on the phone. This page will advance automatically when it finishes."
        action = None
        active_step = "microphone" if (
            level_running or _relay_kind(status).startswith("level_ramp:")
        ) else (
            "drivers"
        )
    elif not level_ready and missing_drivers:
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
    elif missing_drivers:
        from .capture_geometry import driver_placement_instruction

        target = missing_drivers[0]
        role = str(target.get("role") or "driver")
        target_id = f"{target.get('speaker_group_id') or ''}:{role}"
        repeat_state = _mapping(
            _mapping(_mapping(status.get("level_match")).get("repeats")).get(
                "targets"
            )
        )
        repeat = _mapping(repeat_state.get(target_id))
        repeat_failures = _mapping(
            _mapping(_mapping(status.get("level_match")).get("repeats")).get(
                "failures"
            )
        )
        repeat_failure = _mapping(repeat_failures.get(target_id))
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
        attempts = int(repeat.get("attempts") or 0)
        accepted = int(repeat.get("accepted") or 0)
        repeat_target = int(repeat.get("target") or 3)
        repeat_copy = (
            f" Repeat {attempts + 1}; {accepted} of {repeat_target} accepted so far."
            if attempts
            else f" JTS takes {repeat_target} stationary repeats."
        )
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
            "body": {"tuning_owner": "automatic"},
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
