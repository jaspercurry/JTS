# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The declaration, protected experiment, and apply steps for a new speaker."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from jasper.identity.reader import SPEAKER_SETUP_PAGE_PATH
from jasper.json_fields import finite_float, parse_utc_iso
from .driver_safety import driver_floor_issues
from .applied_identity import applied_identity
from jasper.output_topology import OutputTopology
from jasper.output_topology_store import load_output_topology
from .measurement_programs import PURPOSE_ROOM, PURPOSE_SPEAKER, PROGRAM_ROWS, programs_for_topology

COORDINATOR_KIND = "jts_active_speaker_commissioning_view"
VIEW_STATUS_NOT_REQUIRED = "not_required"
COMMISSIONING_STEP_PAGE_TITLES = {
    "layout": "Choose speaker layout",
    "research": "Driver details",
    "profile": "Save to speaker",
}
_MEASURE_LABELS = {row.purpose: row.measure_label for row in PROGRAM_ROWS}


def next_program_action(
    profile: Mapping[str, Any] | None,
    identity: Mapping[str, Any],
    recent_rounds: Mapping[str, Mapping[str, Any]],
    *,
    programs: tuple[str, ...],
) -> dict[str, Any]:
    """Choose from applied layers and the latest rounds for this identity."""
    from .baseline_profile import applied_layers  # lazy: baseline imports measurement

    # Decision d18 / ADR-0301: the trial verifies an apply; a new baseline round is not required.
    layers = applied_layers(profile)
    room_at = finite_float((recent_rounds.get(PURPOSE_ROOM) or {}).get("started_at"))
    room_stale = room_at is not None and any(
        (finite_float((recent_rounds.get(name) or {}).get("started_at")) or 0) > room_at
        for name in programs if name != PURPOSE_ROOM
    )
    program = next((name for name in programs if not layers[name]
                    or name == PURPOSE_ROOM and room_stale), None)
    if program is None:
        return {"id": None, "enabled": False, "program": None, "label": "Tuning complete", "reason_code": "complete"}
    round_ = recent_rounds.get(program) or {}
    applied_at = parse_utc_iso(str(identity.get("applied_at") or "")) or 0
    if (not layers[program] and not (program == PURPOSE_ROOM and room_stale)
            and (finite_float(round_.get("started_at")) or 0) > applied_at):
        return {"id": "copy_prompt", "label": f"Copy the {program} prompt", "enabled": True,
                "program": program, "round_dir": round_["round_dir"], "reason_code": "round_available"}
    reason = ("upstream_changed" if program == PURPOSE_ROOM and room_stale else
              "layer_not_applied" if profile is not None else "never_measured")
    return {"id": "run_program", "enabled": True, "program": program, "label": _MEASURE_LABELS[program], "reason_code": reason}


def build_commissioning_view(
    topology: OutputTopology,
    *,
    design_draft: Mapping[str, Any] | None = None,
    crossover_preview: Mapping[str, Any] | None = None,
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
    from .applied_tune import reviewed_candidate_refusal  # lazy: the review compiles graphs
    from .baseline_profile import APPLIED_PROFILE_DISPLACED  # lazy: baseline imports measurement

    draft, preview, review = design_draft or {}, crossover_preview or {}, baseline_profile or {}
    programs = programs_for_topology(topology) if programs is None else programs
    passive = PURPOSE_SPEAKER not in programs
    has_layout = bool(topology.speaker_groups)
    design_ready = passive or draft.get("status") == "ready_for_review"
    preview_ready = passive or preview.get("status") == "ready_for_protected_staging"
    safety_ready = passive or bool(draft.get("driver_safety_profile")) and not driver_floor_issues(draft["driver_safety_profile"])
    values_ready = design_ready and preview_ready and safety_ready
    profile_applied = applied_profile is not None and applied_profile_verdict != APPLIED_PROFILE_DISPLACED
    applied = applied_identity(applied_profile) or {}
    experiment = dict(first_experiment or {})
    experiment_complete = bool(experiment.get("candidate_fingerprint"))
    review_ready = bool((review.get("permissions") or {}).get("may_compile"))
    disclosures = []
    if applied_profile is not None:
        refusal = reviewed_candidate_refusal(review, str(applied_profile.get("candidate_fingerprint") or ""))
        if refusal:
            disclosures = [{**refusal["issues"][-1], "severity": "warning", "status": "disclosed_stale"}]
    messages = {
        "layout": "Declare the speaker layout and assign each driver to its output.",
        "research": "Save the driver values and crossover settings.",
        "profile": "Save the starting crossover and trims to the speaker.",
    }
    steps = []
    active = False
    for step_id, done, not_required in (
        ("layout", has_layout, False), ("research", values_ready, passive),
        ("profile", profile_applied, passive),
    ):
        status = "not_required" if not_required else "done" if done else "todo" if active else "active"
        active = active or status == "active"
        steps.append({"id": step_id, "label": COMMISSIONING_STEP_PAGE_TITLES[step_id],
                      "status": status, "message": messages[step_id]})
    current = next((step["id"] for step in steps if step["status"] == "active"),
                   "layout" if passive else "profile")
    if profile_applied or (has_layout and passive):
        status = "applied" if profile_applied else VIEW_STATUS_NOT_REQUIRED
        action = next_program_action(applied_profile, applied, recent_rounds or {},
                                      programs=programs)
    elif not has_layout:
        status = "needs_layout"
        action = {"id": "declare_speaker", "label": "Declare the speaker", "enabled": True,
                  "endpoint": SPEAKER_SETUP_PAGE_PATH, "method": "GET", "body": {}}
    elif not values_ready:
        status = "needs_driver_safety_profile" if design_ready and preview_ready else "needs_driver_values"
        action = {"id": "save_driver_values", "label": "Save values", "enabled": True,
                  "endpoint": "./active-speaker/design-draft", "method": "POST", "body": {}}
    else:
        status = "ready_to_save_profile" if review_ready else "blocked"
        action = {"id": "save_baseline_profile", "label": "Save to speaker", "enabled": review_ready,
                  "endpoint": "./active-speaker/baseline-profile/save-and-apply", "method": "POST", "body": {}}
    return {
        "artifact_schema_version": 1, "kind": COORDINATOR_KIND, "status": status,
        "steps": steps, "current_step": current, "next_action": action, "programs": programs,
        "first_experiment": {**experiment, "complete": experiment_complete}, "combined_groups": [],
        "applied_profile": {
            "stands": profile_applied, "verdict": applied_profile_verdict if profile_applied else "",
            "exists": applied_profile is not None,
            "candidate_fingerprint": applied.get("candidate"), "record": applied.get("record"),
            "applied_at": applied.get("applied_at"),
            "config_path": applied.get("config_path"), "disclosures": disclosures,
        },
        "review": {"ready": review_ready, "status": review.get("status"), "issues": list(review.get("issues") or [])},
        "driver_values": {"complete": values_ready, "design_ready": design_ready,
                          "preview_ready": preview_ready, "driver_floors_declared": safety_ready},
        "driver_spacing_mm": (draft.get("manual_settings") or {}).get("driver_spacing_mm"),
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
    from jasper.active_speaker.applied_tune import compile_commissioning_profile  # lazy: import cost (graph compilation)
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state  # lazy: import cost
    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commissioning_experiment import commissioning_candidate, commissioning_experiment_summary  # lazy: candidate imports baseline
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds  # lazy: reader imports baseline
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.startup_load import load_startup_load_state

    if topology is None:
        topology = load_output_topology()
    design_draft = load_design_draft(topology=topology)
    preview = build_crossover_preview(design_draft)
    calibration_level = load_calibration_level_state()
    applied = load_applied_baseline_profile_state()
    baseline = compile_commissioning_profile(
        applied_profile=applied, topology=topology, design_draft=design_draft, crossover_preview=preview,
    )
    experiment = {}
    if applied is None:
        try:
            experiment = commissioning_experiment_summary(commissioning_candidate(topology, design_draft))
        except (OSError, ValueError, LookupError):
            pass
    programs = programs_for_topology(topology)
    return build_commissioning_view(
        topology,
        design_draft=design_draft,
        crossover_preview=preview,
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
