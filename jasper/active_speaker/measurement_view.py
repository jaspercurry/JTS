# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Measurement screen facts and plan choices, separate from speaker setup."""

from typing import Any, Mapping

from .capture_status import SESSION_ENDED_STATUSES
from .measurement_programs import BRANCH_PAIR_FRONT_REAR, PURPOSE_REAR, RUNNABLE_PROGRAMS, available_programs, program
from .round_copy import round_lines, packet_lines, round_verdict
from .wizard_client import CAPTURE_CANCEL_PATH


def round_status(capture: Mapping[str, Any]) -> list[str]:
    facts = capture.get("run") or {}
    if facts.get("round_dir"):
        lines = packet_lines(facts["round_dir"])
        if lines:
            return lines
        facts = {**facts, "packet_error": "packet_unreadable"}
    return round_lines(facts, pending=capture.get("position_pending") or capture.get("join") or {})


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


#: Registry ids whose layout, purpose, and regime duplicate another id's
#: (reviewer finding R4-D9): ``seat/cloud`` mirrors ``room/cloud`` and
#: ``seat/express`` mirrors ``room/seat``. Hidden from the picker only -- the
#: ids stay registered and keep resolving through :func:`program` (ADR-0277).
_ALIAS_PLAN_IDS = frozenset({"seat/cloud", "seat/express"})


def round_choices(status: Mapping[str, Any], selected_id: str = "") -> list[dict[str, Any]]:
    from .angle_capture import REGIME_BRANCHES, request_for_program  # lazy: measurement planning
    from .crossover_v2.conductor_context import resolve_conductor_context  # lazy: measurement planning
    from .crossover_v2.refusal_copy import (  # lazy: measurement planning
        CrossoverV2Refused, REASON_MEASUREMENT_CANDIDATE_REQUIRED, REASON_MEASUREMENT_PROGRAM_NOT_OFFERED,
        refusal_copy_for,
    )
    from .plan_run import prepare_plan_captures, preview_schedule  # lazy: measurement planning

    from .commissioning_coordinator import load_commissioning_view  # lazy: setup is read only when choosing a default

    view = load_commissioning_view()
    programs = view["programs"]
    plans = {f"{name}/{size}": program(name, size) for name, size in available_programs()
             if f"{name}/{size}" not in _ALIAS_PLAN_IDS}
    plans = {key: plan for key, plan in plans.items()
             if not ((plan.purpose in RUNNABLE_PROGRAMS and plan.purpose not in programs)
                     or (plan.branch_pair == BRANCH_PAIR_FRONT_REAR and PURPOSE_REAR not in programs)
                     or not {pose.driver for pose in plan.poses if pose.driver} <= set(view["near_field_drivers"]))}
    default = program(view["next_action"].get("program") or programs[0])
    refused = bool(selected_id) and selected_id not in plans
    default_id = selected_id or f"{default.program_id}/{default.size}"
    choices = []
    for plan_id, plan in plans.items():
        choice: dict[str, Any] = {"id": plan_id, "label": plan_id, "default": plan_id == default_id,
                                  "poses": plan.mic_move_count, "captures": plan.capture_count}
        if choice["id"] == default_id:
            if plan.regime == REGIME_BRANCHES:
                # See issue #5321.
                copy, _ = refusal_copy_for(REASON_MEASUREMENT_CANDIDATE_REQUIRED)
                choice.update(code=REASON_MEASUREMENT_CANDIDATE_REQUIRED, lines=[copy])
            else:
                try:
                    context = resolve_conductor_context(status, require_banked_level=False)
                except CrossoverV2Refused as exc:
                    # Disclosed the way jasper.web.correction_runtime.refusal_envelope
                    # renders one: ``str(exc)`` is the household sentence its
                    # raisers pass; some carry no code.
                    choice.update(code=exc.code or None, lines=[str(exc)])
                else:
                    request = request_for_program(plan, mover=plan.mover or "human")
                    captures = prepare_plan_captures(request, roles_bands=context.roles_bands)
                    facts = preview_schedule(request, captures, context)
                    choice.update(lines=round_lines(facts), action={"id": "run_program", "label": "Start measurement",
                                  "endpoint": "/sound/speaker/crossover/v2/session", "body": {"plan": request.to_dict()}})
        choices.append(choice)
    if refused:
        copy, _ = refusal_copy_for(REASON_MEASUREMENT_PROGRAM_NOT_OFFERED)
        choices.append({"id": selected_id, "label": selected_id, "default": True,
                        "code": REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, "lines": [copy]})
    return choices
