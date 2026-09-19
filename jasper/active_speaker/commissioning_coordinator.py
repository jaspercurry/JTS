# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The declaration, protected experiment, and apply steps for a new speaker."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from jasper.identity.reader import SPEAKER_SETUP_PAGE_PATH
from jasper.json_fields import finite_float, parse_utc_iso
from jasper.output_topology import OutputTopology, cardioid_cabinet_channels, topology_is_subless_passive_mains
from .driver_safety import driver_floor_issues
from .applied_identity import applied_identity
from .capture_status import SESSION_ENDED_STATUSES
from .measurement_programs import PURPOSE_REAR, PURPOSE_ROOM, PURPOSE_SPEAKER, RUNNABLE_PROGRAMS, available_programs, program
from .wizard_client import APPLY_PATH, CAPTURE_CANCEL_PATH
from .round_copy import round_lines, packet_lines, round_verdict

COORDINATOR_KIND = "jts_active_speaker_commissioning_view"
VIEW_STATUS_NOT_REQUIRED = "not_required"
COMMISSIONING_STEP_PAGE_TITLES = {
    "layout": "Choose speaker layout",
    "research": "Driver values",
    "experiment": "First speaker experiment",
    "profile": "Apply speaker profile",
}
_MEASURE_LABELS = {name: f"Measure {name}" for name in RUNNABLE_PROGRAMS}
_MEASURE_LABELS.update(speaker="Measure the baseline", room="Measure the room", rear="Measure the rear woofer")


def round_status(capture: Mapping[str, Any]) -> list[str]:
    facts = capture.get("run") or {}
    if facts.get("round_dir"):
        lines = packet_lines(facts["round_dir"])
        if lines:
            return lines
        facts = {**facts, "packet_error": "packet_unreadable"}
    return round_lines(facts, pending=bool(capture.get("position_pending") or capture.get("join")))


def round_capture(capture: Mapping[str, Any], verdict: str, *, advertise_capture: bool = True) -> dict[str, Any]:
    from .crossover_v2.position_gate import retake_action  # lazy: gate imports measurement

    facts = capture.get("run") or {}
    result = {"capture": dict(capture) if advertise_capture else None, "round_lines": round_status(capture),
              "verdict_text": round_verdict(facts, verdict)}
    if not facts.get("pose_details"):
        return result
    held = capture.get("join") or capture.get("position_pending") or {}
    live = capture.get("status") not in SESSION_ENDED_STATUSES
    actions = [a for a in held.get("actions", ()) if a["id"] != "retake"] + [
        retake_action(), {"id": "reset_round", "label": "Reset the round", "endpoint": CAPTURE_CANCEL_PATH, "body": {}},
    ] if live and facts.get("mover") == "human" else []
    return {**result, "capture": None, "pending": {"actions": actions} if live else None, "busy": live}


def round_choices(status: Mapping[str, Any], selected_id: str = "") -> list[dict[str, Any]]:
    from .angle_capture import REGIME_BRANCHES, request_for_program  # lazy: measurement planning
    from .crossover_v2.conductor_context import resolve_conductor_context  # lazy: measurement planning
    from .crossover_v2.refusal_copy import (  # lazy: measurement planning
        CrossoverV2Refused, REASON_MEASUREMENT_CANDIDATE_REQUIRED, refusal_copy_for,
    )
    from .plan_run import prepare_plan_captures, preview_schedule  # lazy: measurement planning

    default = program(load_commissioning_view()["next_action"].get("program") or "speaker")
    default_id = f"{default.program_id}/{default.size}"
    choices = []
    for name, size in available_programs():
        plan = program(name, size)
        choice: dict[str, Any] = {"id": f"{name}/{size}", "label": f"{name}/{size}",
                                  "default": f"{name}/{size}" == default_id,
                                  "poses": plan.mic_move_count, "captures": plan.capture_count}
        if choice["id"] == (selected_id or default_id):
            if plan.regime == REGIME_BRANCHES:
                # A branches round measures one saved candidate's two branches
                # and this picker names no candidate, so the row discloses the
                # refusal ``request_for_program`` raises rather than let it
                # escape the route as a 500 (#5321).
                copy, _ = refusal_copy_for(REASON_MEASUREMENT_CANDIDATE_REQUIRED)
                choice.update(code=REASON_MEASUREMENT_CANDIDATE_REQUIRED, lines=[copy])
            else:
                try:
                    context = resolve_conductor_context(status, require_banked_level=False)
                except CrossoverV2Refused as exc:
                    # Disclosed the way jasper.web._common.refusal_envelope
                    # renders one: ``str(exc)`` is the household sentence its
                    # raisers pass; some carry no code.
                    choice.update(code=exc.code or None, lines=[str(exc)])
                else:
                    request = request_for_program(plan, mover=plan.mover or "human")
                    captures = prepare_plan_captures(request, roles_bands=context.roles_bands)
                    facts = preview_schedule(request, captures, context)
                    choice.update(lines=round_lines(facts), action={"id": "run_program", "label": "Start the round",
                                  "endpoint": "/sound/speaker/crossover/v2/session", "body": {"plan": request.to_dict()}})
        choices.append(choice)
    return choices


def _programs_for_topology(topology: OutputTopology) -> tuple[str, ...]:
    passive = topology_is_subless_passive_mains(topology)
    rear = cardioid_cabinet_channels(
        (channel.role, channel.output_variant, channel.physical_output_index)
        for group in topology.speaker_groups for channel in group.channels
        if channel.physical_output_index is not None
    )
    return tuple(name for name in RUNNABLE_PROGRAMS
                 if not (name == PURPOSE_SPEAKER and passive or name == PURPOSE_REAR and rear is None))


def _next_program_action(
    profile: Mapping[str, Any] | None,
    identity: Mapping[str, Any],
    recent_rounds: Mapping[str, Mapping[str, Any]],
    *,
    programs: tuple[str, ...],
) -> dict[str, Any]:
    """Choose from the latest banked round per program for this applied identity."""
    from .baseline_profile import applied_layers  # lazy: baseline imports measurement

    baseline = {"id": "run_program", "enabled": True,
                "program": programs[0], "label": _MEASURE_LABELS[programs[0]]}
    # Plan #5073 §2 rule (a): no round for this identity means measure the baseline first.
    if not recent_rounds:
        return baseline
    layers = applied_layers(profile)
    room_at = finite_float((recent_rounds.get(PURPOSE_ROOM) or {}).get("started_at"))
    room_stale = room_at is not None and any(
        (finite_float((recent_rounds.get(name) or {}).get("started_at")) or 0) > room_at
        for name in programs if name != PURPOSE_ROOM
    )
    program = next((name for name in programs if not layers[name]
                    or name == PURPOSE_ROOM and room_stale), programs[0])
    round_ = recent_rounds.get(program) or {}
    applied_at = parse_utc_iso(str(identity.get("applied_at") or "")) or 0
    if (not layers[program] and not (program == PURPOSE_ROOM and room_stale)
            and (finite_float(round_.get("started_at")) or 0) > applied_at):
        return {"id": "copy_prompt", "label": f"Copy the {program} prompt", "enabled": True,
                "program": program, "round_dir": round_["round_dir"]}
    return {**baseline, "program": program, "label": _MEASURE_LABELS[program]}


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
    applied_profile: Mapping[str, Any] | None = None,
    applied_profile_verdict: str = "",
    first_experiment: Mapping[str, Any] | None = None,
    recent_rounds: Mapping[str, Mapping[str, Any]] | None = None,
    programs: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    from .baseline_profile import APPLIED_PROFILE_DISPLACED, reviewed_candidate_refusal  # lazy: baseline imports measurement

    draft, preview, review = design_draft or {}, crossover_preview or {}, baseline_profile or {}
    summary = (measurements or {}).get("summary") or {}
    programs = _programs_for_topology(topology) if programs is None else programs
    passive = PURPOSE_SPEAKER not in programs
    has_layout = bool(topology.speaker_groups)
    design_ready = passive or draft.get("status") == "ready_for_review"
    preview_ready = passive or preview.get("status") == "ready_for_protected_staging"
    safety_ready = passive or bool(draft.get("driver_safety_profile")) and not driver_floor_issues(draft["driver_safety_profile"])
    values_ready = design_ready and preview_ready and safety_ready
    profile_applied = applied_profile is not None and applied_profile_verdict != APPLIED_PROFILE_DISPLACED
    applied = applied_identity(applied_profile) or {}
    experiment = dict(first_experiment or {})
    experiment_complete = bool(experiment.get("candidate_fingerprint")) or profile_applied
    review_ready = bool((review.get("permissions") or {}).get("may_compile")
                        or (review.get("permissions") or {}).get("may_apply"))
    disclosures = []
    if applied_profile is not None:
        refusal = reviewed_candidate_refusal(review, str(applied_profile.get("candidate_fingerprint") or ""))
        if refusal:
            disclosures = [{**refusal["issues"][-1], "severity": "warning", "status": "disclosed_stale"}]
    messages = {
        "layout": "Declare the speaker layout and assign each driver to its output.",
        "research": "Save the driver values and crossover settings.",
        "experiment": "Place the microphone at the design mark and run the speaker program.",
        "profile": "Apply the candidate named in the experiment packet to finish commissioning.",
    }
    steps = []
    active = False
    for step_id, done, not_required in (
        ("layout", has_layout, False), ("research", values_ready, passive),
        ("experiment", experiment_complete, passive), ("profile", profile_applied, passive),
    ):
        status = "not_required" if not_required else "done" if done else "todo" if active else "active"
        active = active or status == "active"
        steps.append({"id": step_id, "label": COMMISSIONING_STEP_PAGE_TITLES[step_id],
                      "status": status, "message": messages[step_id]})
    current = next((step["id"] for step in steps if step["status"] == "active"),
                   "layout" if passive else "profile")
    if profile_applied or (has_layout and passive):
        status = "applied" if profile_applied else VIEW_STATUS_NOT_REQUIRED
        action = _next_program_action(applied_profile, applied, recent_rounds or {},
                                      programs=programs)
    elif not has_layout:
        status = "needs_layout"
        action = {"id": "declare_speaker", "label": "Declare the speaker", "enabled": True,
                  "endpoint": SPEAKER_SETUP_PAGE_PATH, "method": "GET", "body": {}}
    elif not values_ready:
        status = "needs_driver_safety_profile" if design_ready and preview_ready else "needs_driver_values"
        action = {"id": "save_driver_values", "label": "Save values", "enabled": True,
                  "endpoint": "./active-speaker/design-draft", "method": "POST", "body": {}}
    elif not experiment_complete:
        status = "needs_first_experiment"
        action = {"id": "run_speaker_program", "label": "Run speaker experiment", "enabled": True,
                  "endpoint": "/sound/speaker/crossover/", "method": "GET", "body": {}, "program": PURPOSE_SPEAKER}
    else:
        status = "ready_to_save_profile" if review_ready else "blocked"
        action = {"id": "apply_candidate", "label": "Apply speaker profile", "enabled": review_ready,
                  "endpoint": APPLY_PATH, "method": "POST",
                  "body": {"expected_candidate_fingerprint": experiment["candidate_fingerprint"]}}
    checks_complete = bool(summary.get("driver_checks_complete") or summary.get("driver_measurements_complete"))
    checks = {"complete": checks_complete, "source": "measurements" if checks_complete else "missing",
              "captured": int(summary.get("captured_driver_check_count") or summary.get("captured_driver_count") or 0),
              "required": int(summary.get("required_driver_check_count") or summary.get("required_driver_count") or 0)}
    return {
        "artifact_schema_version": 1, "kind": COORDINATOR_KIND, "status": status,
        "steps": steps, "current_step": current, "next_action": action,
        "first_experiment": {**experiment, "complete": experiment_complete}, "combined_groups": [],
        "applied_profile": {
            "stands": profile_applied, "verdict": applied_profile_verdict if profile_applied else "",
            "exists": applied_profile is not None,
            "candidate_fingerprint": applied.get("candidate"), "record": applied.get("record"),
            "applied_at": applied.get("applied_at"),
            "config_path": applied.get("config_path"), "disclosures": disclosures,
        },
        "review": {"ready": review_ready, "may_apply": review_ready,
                   "status": review.get("status"), "issues": list(review.get("issues") or [])},
        "driver_values": {"complete": values_ready, "design_ready": design_ready,
                          "preview_ready": preview_ready, "driver_floors_declared": safety_ready},
        "driver_spacing_mm": (draft.get("manual_settings") or {}).get("driver_spacing_mm"),
        "driver_checks": checks,
        "test_level": dict((calibration_level or {}).get("test_signal") or {}),
        "runtime": {"commission": dict(commission or {}), "startup_load": dict(startup_load or {})},
    }


def load_commissioning_view(
    topology: OutputTopology | None = None,
    *,
    commission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Share commissioning inputs between /sound/ and the crossover envelope.

    A caller that omits ``commission`` silently degrades the view; ``None``
    composes identical steps.
    """
    from jasper.active_speaker.baseline_profile import (
        compile_commissioning_profile, load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commissioning_experiment import commissioning_candidate, commissioning_experiment_summary  # lazy: candidate imports baseline
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds  # lazy: reader imports baseline
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.active_speaker.startup_load import load_startup_load_state
    from jasper.output_topology import load_output_topology

    if topology is None:
        topology = load_output_topology()
    design_draft = load_design_draft(topology=topology)
    preview = build_crossover_preview(design_draft)
    measurements = load_measurement_state(topology)
    calibration_level = load_calibration_level_state()
    _, baseline = compile_commissioning_profile(
        topology=topology, design_draft=design_draft, crossover_preview=preview,
    )
    applied = load_applied_baseline_profile_state()
    experiment = {}
    if applied is None:
        try:
            experiment = commissioning_experiment_summary(commissioning_candidate(topology, design_draft))
        except (OSError, ValueError, LookupError):
            pass
    programs = _programs_for_topology(topology)
    return build_commissioning_view(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
        measurements=measurements,
        commission=commission,
        startup_load={"state": load_startup_load_state()},
        baseline_profile=baseline,
        calibration_level=calibration_level,
        applied_profile=applied,
        recent_rounds=latest_banked_rounds(applied_identity(applied) or {}, programs=programs),
        programs=programs,
        first_experiment=experiment,
        applied_profile_verdict=read_applied_profile_verdict(applied),
    )


def read_applied_profile_verdict(applied: Mapping[str, Any] | None) -> str:
    """Ask the speaker whether it is still playing the applied profile.

    ``""`` when it is. Otherwise one of
    :func:`baseline_profile.applied_profile_displacement`'s verdicts, or
    :data:`~jasper.active_speaker.baseline_profile.APPLIED_PROFILE_CONFIG_MISSING`
    — the record's own ``config.exists`` is frozen at apply time, so only a
    fresh stat sees the file go missing under it. Two reads at wizard cadence;
    nothing polled reaches this.
    """

    from .baseline_profile import (
        APPLIED_PROFILE_CONFIG_MISSING,
        applied_profile_displacement,
    )

    if applied is None:
        return ""
    verdict = applied_profile_displacement(applied)
    if verdict:
        return verdict
    config = applied.get("config") if isinstance(applied.get("config"), Mapping) else {}
    recorded = str(config.get("path") or "")
    return "" if Path(recorded).exists() else APPLIED_PROFILE_CONFIG_MISSING
