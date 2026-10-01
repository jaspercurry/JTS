# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Project run status into mover screens and shared measurement status fields."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .driver_safety import driver_floor_issues
from jasper.identity.reader import SPEAKER_SETUP_PAGE_PATH
from jasper.platform.json_fields import as_mapping
from jasper.platform.log_event import log_event
from .measurement_programs import PURPOSE_SPEAKER, preset
from .measurement_view import round_capture, run_door
from .round_copy import CHOOSE_PROGRAM, RUN_ENDED
from .crossover_v2.coordinator import series_position_from_state
from .crossover_v2.position_gate import RETAKE_ENDPOINT
from .capture_status import CAPTURE_COMPLETE, CAPTURE_FAILED, SESSION_ENDED_STATUSES
from .crossover_v2.journey import (
    CAPTURE_PHASES,
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_REVIEW,
    PHASE_TIMING,
    PHASE_VERIFY,
    PRE_CLOUD_CAPTURE_PHASES,
    pending_capture_phase,
)
from .crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    ReasonSpec,
    TIMING_RESET_NOTE,
    NON_RETRIABLE_CODES,
    TEMPLATE_FIX_AND_RETRY,
    TEMPLATE_HARD_STOP,
    TEMPLATE_SESSION_RESTART,
    TEMPLATE_SILENT_AUTO_RETRY,
    TEMPLATE_VERIFY_FAIL,
    reason_message,
)
from .crossover_v2.refusal_copy import REASON_VOLUME_UNRESOLVED

logger = logging.getLogger(__name__)

CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION = 19

_STEP_IDS = (
    "speaker_setup",
    "microphone_check",
    "measure",
    "verify",
)
_STEP_LABELS = {
    "speaker_setup": "Protected speaker setup",
    "microphone_check": "Microphone check",
    "measure": "Measure",
    "verify": "Verify",
}

# Exhaustive: an unmapped journey phase raises instead of moving the stepper backwards.
_PHASE_STEP = {
    PHASE_CHECK: "microphone_check",
    PHASE_MEASURE: "measure",
    PHASE_CLOUD_MEASURE: "measure",
    PHASE_LATERAL: "measure",
    PHASE_TIMING: "measure",
    PHASE_APPLYING: "measure",
    PHASE_REVIEW: "measure",
    PHASE_VERIFY: "verify",
    PHASE_CLOUD_VERIFY: "verify",
    PHASE_DONE: "verify",
}


def _retake_action() -> dict[str, Any]:
    return {"id": "crossover_v2_retake", "label": "Record the last spot again",
            "endpoint": RETAKE_ENDPOINT, "body": {}, "show_during_capture": True}


def _v2(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return as_mapping(status.get("crossover_v2"))


def _step_payload(active_step: str, done_steps: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for step_id in _STEP_IDS:
        rows.append({
            "id": step_id,
            "label": _STEP_LABELS[step_id],
            "status": (
                "done" if step_id in done_steps
                else "active" if step_id == active_step
                else "pending"
            ),
        })
    return rows


def _progress(active_step: str) -> dict[str, int]:
    try:
        position = _STEP_IDS.index(active_step) + 1
    except ValueError:
        position = len(_STEP_IDS)
    return {"position": position, "total": len(_STEP_IDS)}


def _done_before(active_step: str) -> set[str]:
    """Every step strictly before the active one is done (monotonic journey)."""
    try:
        frontier = _STEP_IDS.index(active_step)
    except ValueError:
        frontier = len(_STEP_IDS)
    return set(_STEP_IDS[:frontier])


def _applied_chip(status: Mapping[str, Any]) -> dict[str, str]:
    """Durable applied-crossover chip — reuse the legacy contract shape."""
    contract = as_mapping(as_mapping(status.get("setup")).get("applied_crossover"))
    if contract.get("valid") is not True:
        return {"state": "none", "label": "No speaker profile applied"}
    owner = str(contract.get("owner") or "")
    if owner == "automatic":
        return {"state": "automatic", "label": "Speaker configuration active"}
    if owner == "manual":
        return {"state": "manual", "label": "Speaker configuration active"}
    return {"state": "applied", "label": "Speaker profile applied"}


def _timing_door(action_id: str) -> dict[str, Any]:
    """Where a timing action leads (#5925): a speaker round measures timing; a reset
    or an apply is the assistant's step, from the speaker page's tuning prompt."""
    if action_id in ("measure_timing", "remeasure_timing"):
        return run_door(preset(PURPOSE_SPEAKER))
    return {"href": SPEAKER_SETUP_PAGE_PATH}


def _setup_ready(status: Mapping[str, Any]) -> bool:
    setup = as_mapping(status.get("setup"))
    safety = as_mapping(status.get("driver_safety_profile"))
    if safety:
        return not driver_floor_issues(safety)
    return setup.get("active") is True and setup.get("status") == "ready"


def _envelope(
    *,
    screen: str,
    active_step: str,
    verdict: str,
    nudges: list[dict[str, str]] | None = None,
    next_action: dict[str, Any] | None = None,
    alternate_actions: list[dict[str, Any]] | None = None,
    status: Mapping[str, Any],
    advertise_capture: bool = True,
    busy: bool = False,
    terminal_status: str | None = None,
    round_ordinal: int | None = None,
) -> dict[str, Any]:
    # The speaker round's packet is the one timing verdict; a live candidate is not judged twice (#5632).
    timing_action = dict(as_mapping(as_mapping(status.get("timing")).get("next_action"))) or None
    if timing_action:
        timing_action.update(_timing_door(str(timing_action.get("id"))))
    if timing_action and timing_action.get("id") == "reset_timing":
        alternate_actions = [*([next_action] if next_action and next_action != timing_action else []), *(alternate_actions or [])]
        next_action = timing_action
    next_action = next_action or timing_action
    actions = [next_action, *(alternate_actions or [])]
    return {
        "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
        "flow": "v2",
        "screen": screen,
        "terminal_status": terminal_status,
        "round_ordinal": round_ordinal,
        "phase": _v2(status).get("phase"),
        "active": True,
        "steps": _step_payload(active_step, _done_before(active_step)),
        "nudges": nudges or [],
        "round_choices": status.get("round_choices", []),
        "next_action": next_action,
        "alternate_actions": alternate_actions or [],
        "action_note": TIMING_RESET_NOTE if any(
            action and action.get("id") == "reset_timing" for action in actions
        ) else None,
        "timing": dict(as_mapping(status.get("timing"))),
        "busy": bool(busy),
        **round_capture(as_mapping(status.get("capture")), verdict, advertise_capture=advertise_capture),
        "progress": _progress(active_step),
        "applied": _applied_chip(status),
    }


def _awaiting_plan_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    return _envelope(
        screen="awaiting_plan", active_step="microphone_check",
        verdict=CHOOSE_PROGRAM,
        status=status, advertise_capture=False,
    )


def _failure_failed_roles(status: Mapping[str, Any]) -> tuple[str, ...]:
    """The drivers the failed capture names; empty for a record written before they were kept."""
    roles = as_mapping(_v2(status).get("failure")).get("failed_roles")
    return tuple(str(role) for role in roles) if isinstance(roles, list) else ()


def _reason_message(
    code: str, spec: ReasonSpec, status: Mapping[str, Any],
) -> str:
    """Use the registry's copy with recorded evidence (issue #1922)."""
    return reason_message(code, spec, failed_roles=_failure_failed_roles(status))


def _reset_action() -> dict[str, Any]:
    return {"id": "reset", "label": "Start over",
            "endpoint": "/sound/speaker/crossover/reset", "body": {}}


def _failure_envelope(code: str, status: Mapping[str, Any]) -> dict[str, Any]:
    spec = REASON_REGISTRY.get(code)
    capture = as_mapping(status.get("capture"))
    live = bool(capture) and capture.get("status") not in SESSION_ENDED_STATUSES
    action: dict[str, Any] | None = _reset_action()
    if spec:
        if spec.template == TEMPLATE_SILENT_AUTO_RETRY:
            action = None
        elif spec.template == TEMPLATE_HARD_STOP:
            action = dict(spec.own_action) if spec.own_action else {
                "id": "speaker_setup", "label": "Back to speaker setup", "href": "/sound/speaker/",
            }
        elif spec.template == TEMPLATE_SESSION_RESTART:
            action = {**_reset_action(), "id": "restart_session"}
        elif spec.template in {TEMPLATE_FIX_AND_RETRY, TEMPLATE_VERIFY_FAIL} and code not in NON_RETRIABLE_CODES:
            action = _retake_action() if live else None
    if live and action:
        action = {**action, "show_during_capture": True}
    return _envelope(
        screen="finished", active_step="verify", terminal_status=None if live else CAPTURE_FAILED,
        verdict=_reason_message(code, spec, status) if spec else "Measurement failed.",
        nudges=[] if live else [{"code": "run_ended", "severity": "info", "text":
            RUN_ENDED}],
        next_action=action, status=status, advertise_capture=live,
    )


def build_crossover_envelope_v2(status: Mapping[str, Any]) -> dict[str, Any]:
    """The v2 session envelope for the served status.

    Stamped with :data:`CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION` rather than a
    number written down here.
    """
    if not bool(status.get("active")):
        return {
            "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
            "flow": "v2",
            "screen": "not_applicable",
            "active": False,
            "steps": [],
            "verdict_text": "This speaker has no active crossover.",
            "nudges": [],
            "capture": as_mapping(status.get("capture")) or None,
            "next_action": None,
            "alternate_actions": [],
            "action_note": None,
            "timing": dict(as_mapping(status.get("timing"))),
            "progress": {"position": 0, "total": len(_STEP_IDS)},
            "applied": _applied_chip(status),
        }

    v2 = _v2(status)
    phase = str(v2.get("phase") or PHASE_CHECK)

    # Keys on needs_recovery, NOT unresolved_volume_safety alone: a
    # crash-hydrated active plan surfaces no unresolved payload but still
    # needs draining.
    if bool(v2.get("needs_recovery")):
        spec = REASON_REGISTRY[REASON_VOLUME_UNRESOLVED]
        return _envelope(
            screen="volume_recovery", active_step="microphone_check",
            verdict=spec.message,
            nudges=[{
                "code": "crossover_v2_volume_unresolved",
                "severity": "warn",
                "text": spec.message,
            }],
            next_action={
                "id": "recover_volume",
                "label": "Recover safe listening volume",
                "endpoint": "/sound/speaker/crossover/recover-volume",
                "body": {},
            },
            status=status,
        )

    if not _setup_ready(status):
        return _envelope(
            screen="speaker_setup", active_step="speaker_setup",
            verdict="Declare the speaker layout and add the missing driver limits before measuring.",
            next_action={"id": "speaker_setup", "label": "Finish speaker setup", "href": "/sound/speaker/"},
            status=status,
        )

    capture = as_mapping(status.get("capture"))
    terminal = capture.get("status")
    run = as_mapping(capture.get("run"))
    # A staged capture is a new session; the durable failure is the previous session's.
    durable_failure = {} if terminal == "awaiting_join" else as_mapping(v2.get("failure"))
    failure_code = str(run.get("fault") or durable_failure.get("code") or "")
    durable_complete = not capture and phase in {PHASE_REVIEW, PHASE_APPLYING, PHASE_DONE}
    if terminal in SESSION_ENDED_STATUSES or durable_complete:
        if terminal == CAPTURE_FAILED or (durable_complete and failure_code):
            return _failure_envelope(failure_code, status)
        ordinal = None
        if durable_complete:
            terminal = CAPTURE_COMPLETE
            if phase != PHASE_DONE:
                ordinal = series_position_from_state(v2).ordinal
        verdict = RUN_ENDED if terminal == CAPTURE_COMPLETE else "Measurement stopped."
        return _envelope(
            screen="finished", active_step="verify", terminal_status=str(terminal), round_ordinal=ordinal,
            verdict=verdict, next_action=_reset_action(), status=status, advertise_capture=False,
        )
    if failure_code:
        return _failure_envelope(failure_code, status)
    if not capture:
        return _awaiting_plan_envelope(status)
    if capture.get("join") or (phase == PHASE_CHECK and capture):
        phase = PHASE_MEASURE
    active_step = _PHASE_STEP[phase]
    if phase == PHASE_CHECK:
        env = _awaiting_plan_envelope(status)
    elif phase == PHASE_MEASURE:
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "Keep the microphone still — JTS is measuring both drivers. Follow "
                "the measurement page; it continues automatically."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_CLOUD_MEASURE:
        # Same wizard screen as MEASURE; verdict copy changes since the
        # point of this phase is moving the microphone, not holding still.
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "JTS is measuring from a few different spots — follow the "
                "step below. Moving the microphone between spots is what lets "
                "JTS tell the speaker apart from the room."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_LATERAL:
        # R16's walk (§4.4). Bespoke copy: must state the return to the mark.
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "JTS is measuring from a few spots either side of the mark, "
                "and then back on it — follow the step below. Moving the "
                "microphone is what shows how the speaker's drivers hand over "
                "to each other away from the middle."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_TIMING:
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "Keep the microphone still on the mark — JTS is measuring the "
                "drivers together to read their timing. Follow the measurement "
                "page; it continues automatically."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_VERIFY:
        env = _envelope(
            screen="verify", active_step=active_step,
            verdict=(
                "The crossover is applied. Put the microphone back where it "
                "started and follow the measurement page to confirm the result."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_CLOUD_VERIFY:
        env = _envelope(
            screen="verify", active_step=active_step,
            verdict=(
                "Checking the result from the same few spots — follow the "
                "prompts on the measurement page."
            ),
            next_action=None,
            status=status,
        )
    else:
        env = _awaiting_plan_envelope(status)

    log_event(
        logger, "correction.crossover_v2_envelope_serve",
        screen=env["screen"], phase=phase, failure="",
    )
    return env


def crossover_v2_phase(
    state: Mapping[str, Any] | None, *, review_declined: bool,
) -> str:
    """Project the durable journey phase, including old recorded declines."""
    accepted = set(
        state.get("accepted_phases") or () if isinstance(state, Mapping) else ()
    )
    applied = bool(state and state.get("applied"))
    recorded = state.get("session_phases") if isinstance(state, Mapping) else None
    known = (
        tuple(str(p) for p in recorded if str(p) in CAPTURE_PHASES)
        if isinstance(recorded, (list, tuple))
        else ()
    )
    phases = known or PRE_CLOUD_CAPTURE_PHASES
    pending = pending_capture_phase(phases, accepted, applied=applied)
    if pending is not None:
        return pending
    if PHASE_VERIFY not in phases:
        if applied:
            return PHASE_DONE
        if review_declined:
            return PHASE_CHECK
        return PHASE_REVIEW
    return PHASE_DONE
