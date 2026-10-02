# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Project run status into mover screens."""

from __future__ import annotations

from typing import Any, Mapping

from .driver_safety import driver_floor_issues
from jasper.identity.reader import SPEAKER_SETUP_PAGE_PATH
from jasper.platform.json_fields import as_mapping
from .measurement_programs import PROGRAM_ROWS, PURPOSE_SPEAKER, preset, run_purpose
from .measurement_view import round_capture, run_door
from .round_copy import CHOOSE_PROGRAM, RUN_ENDED, RUN_UNDER_WAY
from .crossover_v2.position_gate import RETAKE_ENDPOINT
from .capture_status import CAPTURE_COMPLETE, CAPTURE_FAILED, SESSION_ENDED_STATUSES
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

CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION = 20

# The stepper is per run: the setup state and the live capture place it (#5925).
# Whether a tune is applied is the separate ``applied`` chip.
_STEP_IDS = (
    "speaker_setup",
    "microphone_check",
    "measure",
    "done",
)
_STEP_LABELS = {
    "speaker_setup": "Protected speaker setup",
    "microphone_check": "Microphone check",
    "measure": "Measure",
    "done": "Done",
}


_RUN_HEADLINES = {row.purpose: row.run_headline for row in PROGRAM_ROWS}


def _retake_action() -> dict[str, Any]:
    return {"id": "crossover_v2_retake", "label": "Record the last spot again",
            "endpoint": RETAKE_ENDPOINT, "body": {}, "show_during_capture": True}


def _v2(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return as_mapping(status.get("crossover_v2"))


def _steps(active_step: str) -> list[dict[str, str]]:
    """Every step before the active one is done; every step after it is pending."""
    frontier = _STEP_IDS.index(active_step)
    return [
        {"id": step_id, "label": _STEP_LABELS[step_id],
         "status": "done" if index < frontier else "active" if index == frontier else "pending"}
        for index, step_id in enumerate(_STEP_IDS)
    ]


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
    terminal_status: str | None = None,
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
        "active": True,
        "steps": _steps(active_step),
        "nudges": nudges or [],
        "round_choices": status.get("round_choices", []),
        "next_action": next_action,
        "alternate_actions": alternate_actions or [],
        "action_note": TIMING_RESET_NOTE if any(
            action and action.get("id") == "reset_timing" for action in actions
        ) else None,
        "timing": dict(as_mapping(status.get("timing"))),
        "busy": False,
        **round_capture(as_mapping(status.get("capture")), verdict, advertise_capture=advertise_capture),
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
        screen="finished", active_step="done", terminal_status=None if live else CAPTURE_FAILED,
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
            "applied": _applied_chip(status),
        }

    v2 = _v2(status)

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
    if terminal in SESSION_ENDED_STATUSES:
        if terminal == CAPTURE_FAILED:
            return _failure_envelope(failure_code, status)
        verdict = RUN_ENDED if terminal == CAPTURE_COMPLETE else "Measurement stopped."
        return _envelope(
            screen="finished", active_step="done", terminal_status=str(terminal),
            verdict=verdict, next_action=_reset_action(), status=status, advertise_capture=False,
        )
    if failure_code:
        return _failure_envelope(failure_code, status)
    if not capture:
        return _awaiting_plan_envelope(status)
    return _envelope(
        screen="measure", active_step="measure",
        verdict=_RUN_HEADLINES.get(run_purpose(run.get("program")), RUN_UNDER_WAY),
        next_action=None,
        status=status,
    )
