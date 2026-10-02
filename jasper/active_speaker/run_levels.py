# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Plan and execute levels, finishing each pose before moving."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from itertools import groupby
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from jasper.platform.json_fields import finite_float

from .angle_capture import AngleCaptureRequest, LateralWalkRefused, resolve_request
from .crossover_v2.capture_plan import pose_batch_screens, position_screen_keys
from .crossover_v2.door import IsolationHold
from .crossover_v2.measurement_context import capture_basis
from .crossover_v2.position_gate import PositionGate
from .crossover_v2.refusal_copy import REASON_LEVEL_UNSOLVED
from .plan_run import Analyze, PlanCapture, RunDoor, RunSignals, _Control, _grant, prepare_plan_captures, run_plan
from .preflight import PreflightFacts, PreflightIssue, PreflightReport, preflight
from .run_manifest import RunManifest

#: The ladder's rungs, each this many dB under the level its first rung finds (ADR-0403 §4).
LEVEL_OFFSETS_DB = (0.0, -5.0, -10.0, -15.0)


@dataclass(frozen=True)
class LevelLadder:
    """A run at several levels, loudest first. Each rung's plan states its step
    under the first rung, whose run finds the level with its probe (ADR-0403 §4)."""

    levels: tuple[PreflightReport, ...]
    facts: PreflightFacts
    admissions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def plan(self) -> AngleCaptureRequest:
        rungs = self.admissible or self.levels
        return replace(rungs[0].plan, level=replace(rungs[0].plan.level, level_db=None),
                       levels=tuple(report.plan.level.level_db or 0.0 for report in rungs))

    @property
    def rung_admission(self) -> Mapping[str, Any]:
        return self.levels[0].rung_admission

    @property
    def admissible(self) -> tuple[PreflightReport, ...]:
        return tuple(report for report in self.levels if not report.blocking)

    @property
    def blocking(self) -> bool:
        return not self.admissible

    @property
    def blocking_issue(self) -> PreflightIssue:
        return self.levels[0].blocking_issue

    @property
    def issues(self) -> tuple[PreflightIssue, ...]:
        return self.levels[0].issues

    @property
    def spl_ceiling_db_spl(self) -> float | None:
        return self.levels[0].spl_ceiling_db_spl

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_stimulus": {"program": self.plan.program, "template": self.plan.template.to_dict(),
                                   "stops": [{"regime": stop.regime, "stimulus": stop.stimulus}
                                             for stop in self.plan.stops]},
            "admissions": deepcopy(self.admissions),
            "issues": [asdict(issue) for issue in self.issues],
            "levels": [{"step_db": report.plan.level.level_db, "admissible": not report.blocking, **report.to_dict()}
                       for report in self.levels],
        }


def level_ladder(plan: AngleCaptureRequest, facts: PreflightFacts) -> LevelLadder:
    return _ladder(tuple(replace(plan, levels=None, level=replace(plan.level, level_db=offset))
                         for offset in LEVEL_OFFSETS_DB), facts)


def _ladder(plans: Sequence[AngleCaptureRequest], facts: PreflightFacts) -> LevelLadder:
    """A stated ladder keeps its steps under its loudest rung, which the run's probe finds (ADR-0403 §4).
    Only its first rung at its first pose probes, so that rung's preflight checks the probe's order."""
    top = max(float(plan.level.level_db or 0.0) for plan in plans)
    return LevelLadder(tuple(preflight(replace(plan, level=replace(plan.level, level_db=float(plan.level.level_db or 0.0) - top)),
                                       facts, finds_fader=False)
                             for plan in sorted(plans, key=lambda plan: -(plan.level.level_db or 0.0))), facts)


def preflight_levels(plan: AngleCaptureRequest, facts: PreflightFacts,
                     levels: str | None = None) -> PreflightReport | LevelLadder:
    if levels == "auto":
        if plan.level.level_db is not None:
            raise ValueError("levels require a plan without level-db")
        return level_ladder(plan, facts)
    if plan.levels is None:
        return preflight(plan, facts)
    return _ladder(tuple(replace(plan, levels=None, level=replace(plan.level, level_db=value))
                         for value in plan.levels), facts)


def prepare_level_captures(plan: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = ()) -> tuple[PlanCapture, ...]:
    return prepare_plan_captures(plan, roles_bands=roles_bands)


def ladder_captures(
    plan: AngleCaptureRequest, levels: str | None, captures: Sequence[PlanCapture],
) -> tuple[PlanCapture, ...]:
    """The captures a run of ``plan`` plays: a ladder plays each placement's captures at every
    rung before the microphone moves (:func:`run_levels`). ``levels`` is what
    ``run_request.resolve_plan`` answers with the plan: ``"auto"`` for the preset's whole ladder."""
    rungs = len(plan.levels) if plan.levels is not None else len(LEVEL_OFFSETS_DB) if levels == "auto" else 1
    return tuple(capture for _, group in groupby(captures, key=lambda capture: capture.stop.pose.place)
                 for capture in (*group,) * rungs)


@dataclass(frozen=True)
class LevelRun:
    manifest: RunManifest
    door: RunDoor
    analyze: Analyze
    assessor: Callable[..., Any] | None
    captures: tuple[PlanCapture, ...]


async def run_levels(
    ladder: LevelLadder, *, hold: AbstractAsyncContextManager[IsolationHold],
    prepare: Callable[[AngleCaptureRequest], LevelRun], gate: PositionGate,
    aborts: Mapping[type[BaseException], str], signals: RunSignals | None = None,
    save_ladder: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
) -> tuple[RunManifest, ...]:
    """Finish admissible levels at each pose under one mic hold. The first rung at
    the first pose finds the run's level with its probe, and every rung plays its
    step under that level; a first rung that finds none ends the ladder (ADR-0403 §4)."""
    admitted = ladder.admissible
    if not admitted:
        issue = ladder.levels[0].blocking_issue
        raise LateralWalkRefused(issue.code, issue.detail)
    signals = signals or RunSignals()
    # Each hold counts what the ladder plays at its position by the run's schedule, which the
    # host publishes to the gate before the run (#6206).
    counts = (gate.published().get("run") or {}).get("measurements_per_pose") or ()
    results: list[RunManifest] = []
    found: float | None = None
    try:
        async with hold as held:
            for pose_index, (_, group) in enumerate(groupby(ladder.plan.stops, key=lambda stop: stop.pose.place), 1):
                stops = tuple(group)
                first = resolve_request(replace(ladder.plan, stops=stops))[0]
                prompt = first.prompt
                entry = SimpleNamespace(screen={"title": prompt.headline, "body": prompt.detail,
                                               **position_screen_keys(prompt),
                                               **pose_batch_screens([pose_index], [prompt], [first.candidate_id],
                                                                    counts[pose_index - 1:pose_index] or None)[pose_index]})
                try:
                    await _grant(gate, pose_index, pose_index, entry, signals)
                except _Control:
                    return tuple(results)
                for level_index, planned in enumerate(admitted):
                    step = planned.plan.level.level_db or 0.0
                    request = replace(planned.plan, stops=stops, level=replace(
                        planned.plan.level, level_db=None if found is None else found + step))
                    report = preflight(request, ladder.facts, finds_fader=found is None)
                    request = report.plan
                    observations: list[dict[str, Any]] = []
                    admission = {"pose_index": pose_index, "level_index": level_index + 1, "step_db": step,
                                 "level_db": request.level.level_db, **report.rung_admission,
                                 "observations": observations}
                    ladder.admissions.append(admission)
                    if save_ladder:
                        await save_ladder(ladder.to_dict())
                    if report.blocking:
                        issue = report.blocking_issue
                        raise LateralWalkRefused(issue.code, issue.detail)
                    gate.publish({"status": "running", "program": request.program, "pose": pose_index,
                                  "level": {"run": {"level_db": request.level.level_db}},
                                  "level_index": level_index + 1, "levels": len(admitted)})
                    bound = prepare(request)
                    def analyze(record: Mapping[str, Any]) -> ProgramAnalysis:
                        basis = capture_basis(record)
                        raw_spl = (record.get("capture_integrity") or {}).get("spl") or {}
                        observations.append({"take_id": record["take_id"], "level_db": finite_float(basis["level_db"]),
                            "spl": {key: finite_float(raw_spl.get(key)) for key in (
                                "loudest_half_second_db_spl", "max_window_db_spl", "ceiling_db_spl")},
                            "run_stimulus": {"stimulus_id": basis["stimulus_id"],
                                             "wav_sha256": basis["stimulus_wav_sha256"],
                                             "peak_dbfs": basis["stimulus_peak_dbfs"]}})
                        return bound.analyze(record)

                    bound.door.hold = nullcontext(held)
                    if level_index == 0:
                        bound.manifest.mic_moves = 1
                    result = await run_plan(
                        request, manifest=bound.manifest, door=bound.door,
                        analyze=analyze, assessor=bound.assessor, captures=bound.captures,
                        aborts=aborts, signals=signals,
                    )
                    results.append(result)
                    if found is None:
                        found = admission["level_db"] = result.level.get("run", {}).get("probe_level_db")
                    if save_ladder:
                        await save_ladder(ladder.to_dict())
                    if found is None or result.cancelled or result.stopped_at or any(
                            take.get("next") == "stop" for take in result.takes):
                        signals.request_stop(result.reason or REASON_LEVEL_UNSOLVED)
                    if signals.complete.is_set() or signals.stop.is_set():
                        return tuple(results)
        return tuple(results)
    finally:
        gate.abandon_hold()
