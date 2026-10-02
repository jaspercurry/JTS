# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""A dry run's takes: each distinct take graph built, composed and admitted as its run would (#6113)."""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Awaitable, Iterator, Sequence, cast

from jasper.audio_measurement.program import BASE_STIMULUS_PEAK_DBFS, RoleBand
from jasper.platform.paths import CANONICAL_CAMILLA_CONFIG_DIR

from .angle_capture import AngleCaptureRequest
from .branch_chain import confirmed_protection_sections
from .candidate_parts import baseline_candidate_id
from .capture_schedule import PlanCapture, prepare_plan_captures
from .crossover_v2.composition import bind_program_composer, compose_plan_program
from .crossover_v2.door import bind_measurement_graph
from .crossover_v2.measure_spec import (
    MeasureSpec, branch_channels_for, branch_probes, inverted_roles_for, level_trims_for, measurement_delays_for,
)
from .crossover_v2.program_transaction import ProgramForStimulus, admission_incident
from .crossover_v2.programs import excitation_from_context, probe_fader_db
from .crossover_v2.refusal_copy import REASON_REGISTRY
from .measurement_emit import MeasurementGraphProfile, measurement_graph_evidence
from .measurement_programs import BASE_CANDIDATE, candidate_identity
from .preflight import PreflightIssue, PreflightReport
from .program_admission import ProgramAdmission, ProgramAdmissionRefusal
from .program_playback import ProgramPlaybackRefused
from .run_levels import LevelLadder

if TYPE_CHECKING:
    from .crossover_v2.conductor_context import V2ConductorContext

#: The refusals no fader changes: the take's graph and target map, its declared
#: inputs, and the roles its channels carry. These block a dry run; any other
#: refusal reads the fader the run's probe finds or the dry run's own render, so
#: it is listed and does not block (#6113).
BLOCKING_REFUSALS = frozenset({
    ProgramAdmissionRefusal.GRAPH_NOT_PROVEN, ProgramAdmissionRefusal.TARGET_NOT_MAPPED,
    ProgramAdmissionRefusal.MEASUREMENT_INPUTS_INVALID, ProgramAdmissionRefusal.CHANNEL_ROLE_INCONSISTENT,
})


def _played(plan: AngleCaptureRequest, roles_bands: Sequence[RoleBand]) -> Iterator[tuple[int, PlanCapture, MeasureSpec]]:
    """Each take the run plays, in its order, with its base named as ``plan_run.run_plan`` names
    it: a branch take that finds its level plays each branch's probe first (ADR-0403 §3)."""
    captures = prepare_plan_captures(plan, roles_bands=roles_bands)
    base = baseline_candidate_id() if any(capture.spec.candidate_id == BASE_CANDIDATE for capture in captures) else ""
    for index, capture in enumerate(captures, 1):
        spec = (replace(capture.spec, candidate_id=base) if capture.spec.candidate_id == BASE_CANDIDATE
                else capture.spec)
        for played in (*branch_probes(spec), spec):
            yield index, capture, played


def _issue(index: int, capture: PlanCapture, spec: MeasureSpec, admission: ProgramAdmission,
           level_db: float) -> PreflightIssue:
    code = admission_incident(ProgramPlaybackRefused(admission))
    issue = PreflightIssue.from_code(code, REASON_REGISTRY[code].message,
                                     blocking=not BLOCKING_REFUSALS.isdisjoint(admission.refusals))
    take = {"index": index, "candidate_id": candidate_identity(capture.stop.candidate_id),
            "regime": capture.stop.regime, "place": capture.stop.pose.place, "graph_scope": spec.graph_scope}
    refused = {key: value for key, value in admission.to_dict().items() if key in ("refusals", "muted_output")}
    return replace(issue, evidence={"take": take, "level_db": level_db, **refused})


async def _take_issues(plan: AngleCaptureRequest, context: V2ConductorContext, level_db: float,
                       bundle_dir: Path) -> tuple[list[PreflightIssue], int]:
    # A dry run installs no graph and plays nothing, so it opens no DSP.
    graph = bind_measurement_graph(
        MeasurementGraphProfile(context.preset, context.topology, context.role_channels, context.playback_device,
                                protection_sections_by_role=confirmed_protection_sections(
                                    context.safety_profile, context.role_targets)),
        camilla_factory=lambda: None, config_dir=CANONICAL_CAMILLA_CONFIG_DIR)
    # The run's conductor at the fader it opens at; until CHECK solves MEASURE's gains,
    # the run's preview stands in for them (predictive_program_for_spec).
    conductor = SimpleNamespace(
        excitation=excitation_from_context(context, level_db), set_program=lambda *_: None,
        gain_plan_db=dict.fromkeys((band.role for band in context.roles_bands), BASE_STIMULUS_PEAK_DBFS))
    text = ""
    written: list[str] = []
    compose = bind_program_composer(
        program_for_spec=lambda spec, stimulus_dbfs: compose_plan_program(
            conductor, spec, stimulus_dbfs, context=context),
        store=SimpleNamespace(bundle_dir=bundle_dir, identify_artifact=written.append),
        capture_session_id="dry-run", cam_factory=lambda: None, config_dir=str(CANONICAL_CAMILLA_CONFIG_DIR),
        topology=context.topology, safety_profile=context.safety_profile, role_targets=context.role_targets,
        graph_yaml=lambda: text, level_reference_yaml=graph.level_reference_yaml, roles=context.roles_bands,
        graph_evidence_for_spec=lambda spec: measurement_graph_evidence(
            scope=spec.graph_scope, candidate_id=spec.candidate_id, cleared_layers=spec.cleared_layers),
    )
    seen: set[str] = set()
    issues: list[PreflightIssue] = []
    for index, capture, spec in _played(plan, context.roles_bands):
        graph.select_scope(spec.graph_scope, spec.candidate_id, branch_channels_for(spec), spec.cleared_layers)
        # A drivers graph carries no level match here; admission never reads a drivers take's graph.
        text = graph.graph_yaml(inverted_roles_for(spec), measurement_delays_for(spec), level_trims_for(spec, None))
        if text in seen:
            continue
        seen.add(text)
        played = await cast(Awaitable[ProgramForStimulus], compose(spec=spec, level_db=level_db))
        admission = await played.seams["readmit"]()
        (bundle_dir / written.pop()).unlink()
        if not admission.allowed:
            issues.append(_issue(index, capture, spec, admission, level_db))
    return issues, len(seen)


def admit_dry_run_takes(report: PreflightReport | LevelLadder,
                        context: V2ConductorContext) -> PreflightReport | LevelLadder:
    """``report`` with an issue for each distinct take graph its run's admission refuses,
    composed at the fader the run opens at: the probe fader, or the plan's level when
    that is lower (ADR-0403 §4)."""
    plan = report.plan
    level_db = probe_fader_db(context.driver_caps_dbfs)
    if plan.level.level_db is not None:
        level_db = min(level_db, plan.level.level_db)
    with tempfile.TemporaryDirectory(prefix="jasper-dry-run-") as bundle_dir:
        issues, graphs = asyncio.run(_take_issues(plan, context, level_db, Path(bundle_dir)))
    checked = {"graphs": graphs, "level_db": level_db}
    if isinstance(report, LevelLadder):
        return replace(report, levels=tuple(replace(rung, issues=(*rung.issues, *issues), take_admission=checked)
                                            for rung in report.levels))
    return replace(report, issues=(*report.issues, *issues), take_admission=checked)
