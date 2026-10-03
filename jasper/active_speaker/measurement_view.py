# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Measurement screen facts and plan choices, separate from speaker setup."""

from dataclasses import replace
from typing import Any, Mapping

from .capture_status import SESSION_ENDED_STATUSES
from .measurement_programs import Preset, available_presets, first_plan, offered_here, plan_poses, preset, run_preset
from .movers import MOVER_ARM
from .round_copy import round_lines, round_verdict, status_lines
from .wizard_client import CAPTURE_CANCEL_PATH, SESSION_PATH


def round_status(capture: Mapping[str, Any]) -> list[str]:
    return status_lines(capture.get("run") or {}, pending=capture.get("position_pending") or capture.get("join") or {})


def round_capture(capture: Mapping[str, Any], verdict: str, *, advertise_capture: bool = True) -> dict[str, Any]:
    from .crossover_v2.position_gate import retake_action  # lazy: gate imports measurement

    facts = capture.get("run") or {}
    result = {"capture": dict(capture) if advertise_capture else None, "round_lines": round_status(capture),
              "verdict_text": round_verdict(facts, verdict)}
    if capture.get("join") or not facts.get("pose_details"):
        return result
    held = capture.get("position_pending") or {}
    live = capture.get("status") not in SESSION_ENDED_STATUSES
    actions = [a for a in held.get("actions", ()) if a["id"] != "retake"] + [
        retake_action(), {"id": "reset_round", "label": "Reset the round", "endpoint": CAPTURE_CANCEL_PATH, "body": {}},
    ] if live and facts.get("mover") == "human" else []
    return {**result, "capture": None, "pending": {**held, "actions": actions} if live else None, "busy": live}


#: What an arm plan asks before it starts; the post then carries ``attest`` as true.
RIG_CLEAR_CONFIRM = {
    "title": "Is the arm's path clear?",
    "message": "The arm turns the microphone through its full sweep. Check that nothing is in its path.",
    "confirm_label": "The path is clear",
    "attest": "attest_rig_clear",
}


def run_door(plan: Preset) -> dict[str, Any]:
    """What a page action posts to run ``plan`` at its layout; an arm plan asks first."""
    return {"endpoint": SESSION_PATH, "body": {"request": {"program": plan.preset, "layout": plan.layout}},
            **({"confirm": dict(RIG_CLEAR_CONFIRM)} if plan.mover == MOVER_ARM else {})}


def _choice_id(row: Preset, layout: str) -> str:
    """A page choice: the preset id at its default layout, ``preset@layout`` at another."""
    return row.preset if layout == row.layout else f"{row.preset}@{layout}"


def round_choices(status: Mapping[str, Any], selected_id: str = "") -> list[dict[str, Any]]:
    from .angle_capture import LateralWalkRefused  # lazy: measurement planning
    from .crossover_v2.conductor_context import resolve_conductor_context  # lazy: measurement planning
    from .crossover_v2.refusal_copy import (  # lazy: measurement planning
        CrossoverV2Refused, REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, refusal_copy_for,
    )
    from .plan_run import prepare_plan_captures, preview_schedule  # lazy: measurement planning
    from .arm_walk import ARM_DISCOVERY_REUSE_S, mover_present  # lazy: measurement planning
    from .preflight import mover_unavailable_issue  # lazy: measurement planning
    from .run_request import RunRequest, resolve_plan  # lazy: measurement planning

    from .commissioning_coordinator import load_commissioning_view  # lazy: setup is read only when choosing a default

    view = load_commissioning_view()
    programs = view["programs"]
    targets = view["near_field_drivers"]
    rows = [preset(name) for name in available_presets()]
    plans = {_choice_id(row, layout): run_preset(row.preset, layout) for row in rows for layout in row.layouts}
    plans = {key: plan for key, plan in plans.items() if offered_here(plan, programs=programs, targets=targets)}
    first = first_plan(view["next_action"].get("program") or programs[0])
    refused = bool(selected_id) and selected_id not in plans
    default_id = selected_id or _choice_id(preset(first.preset), first.layout)
    choices = []
    for plan_id, plan in plans.items():
        walked = replace(plan, poses=plan_poses(plan, targets))
        choice: dict[str, Any] = {"id": plan_id, "label": plan_id, "default": plan_id == default_id,
                                  "poses": walked.mic_move_count, "captures": walked.capture_count}
        if choice["id"] == default_id:
            # Posted as the request, which the door resolves (#5737).
            door = run_door(plan)
            try:
                request = resolve_plan(RunRequest.from_mapping(door["body"]["request"]), targets=lambda: targets)
                context = resolve_conductor_context(status)
            except LateralWalkRefused as exc:
                choice.update(code=exc.reason, lines=[refusal_copy_for(exc.reason)[0]])
            except CrossoverV2Refused as exc:
                # Disclosed the way jasper.web.correction_runtime.refusal_envelope
                # renders one: ``str(exc)`` is the household sentence its
                # raisers pass; some carry no code.
                choice.update(code=exc.code or None, lines=[str(exc)])
            else:
                if mover_present(request.mover, reuse_s=ARM_DISCOVERY_REUSE_S):
                    captures = prepare_plan_captures(request, roles_bands=context.roles_bands)
                    facts = preview_schedule(request, captures, context)
                    choice.update(lines=round_lines(facts), action={"id": "run_program", "label": "Start measurement", **door})
                else:
                    issue = mover_unavailable_issue(request.program)
                    choice.update(code=issue.code, lines=[issue.detail], next_action=issue.next_action,
                                  evidence=dict(issue.evidence))
        choices.append(choice)
    if refused:
        copy, _ = refusal_copy_for(REASON_MEASUREMENT_PROGRAM_NOT_OFFERED)
        choices.append({"id": selected_id, "label": selected_id, "default": True,
                        "code": REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, "lines": [copy]})
    return choices
