# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Plan and execute levels, finishing each pose before moving."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import asdict, dataclass, replace
from itertools import groupby
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence, cast

from jasper.audio_measurement.program import RoleBand

from .angle_capture import AngleCaptureRequest, LateralWalkRefused, resolve_request
from .crossover_v2.capture_plan import position_screen_keys
from .crossover_v2.door import IsolationHold
from .crossover_v2.journey import PHASE_ENTRY_BASELINE
from .crossover_v2.position_gate import PositionGate
from .plan_run import Analyze, PlanCapture, RunDoor, RunSignals, _Control, _grant, prepare_plan_captures, run_plan
from .preflight import PreflightFacts, PreflightIssue, PreflightReport, preflight
from .run_manifest import RunManifest

LEVEL_OFFSETS_DB = (0.0, -5.0, -10.0, -15.0)


@dataclass(frozen=True)
class LevelLadder:
    levels: tuple[PreflightReport, ...]

    @property
    def plan(self) -> AngleCaptureRequest:
        admitted = self.admissible
        if not admitted:
            return self.levels[0].plan
        return replace(admitted[0].plan, level=replace(admitted[0].plan.level, level_db=None),
                       levels=tuple(cast(float, report.plan.level.volume_db) for report in admitted))

    @property
    def admissible(self) -> tuple[PreflightReport, ...]:
        return tuple(report for report in self.levels if not report.blocking)

    @property
    def blocking(self) -> bool:
        return not self.admissible

    @property
    def issues(self) -> tuple[PreflightIssue, ...]:
        if self.blocking:
            return self.levels[0].issues
        return tuple(replace(issue, blocking=False,
                             detail=f"Dropped rung {report.plan.level.predicted_db_spl} dB SPL: {issue.code}",
                             evidence={**issue.evidence, "level_db": report.plan.level.volume_db,
                                       "predicted_db_spl": report.plan.level.predicted_db_spl, "dropped": True})
                     for report in self.levels if report.blocking
                     for issue in (next(issue for issue in report.issues if issue.blocking),))

    @property
    def spl_ceiling_db_spl(self) -> float | None:
        return self.levels[0].spl_ceiling_db_spl

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [asdict(issue) for issue in self.issues],
            "levels": [{"offset_db": report.plan.level.offset_db if report.plan.level.resolved else None,
                        "level_db": report.plan.level.volume_db,
                        "predicted_db_spl": report.plan.level.predicted_db_spl,
                        "admissible": not report.blocking, **report.to_dict()}
                       for report in self.levels],
            "admissible_levels_db": [report.plan.level.volume_db for report in self.admissible],
        }


def level_ladder(plan: AngleCaptureRequest, facts: PreflightFacts) -> LevelLadder:
    plan = replace(plan, levels=None)
    anchor_report = preflight(replace(plan, level=replace(plan.level, level_db=None)), facts)
    anchor = anchor_report.plan.level.resolved
    return LevelLadder((anchor_report, *(
        preflight(replace(plan, level=replace(plan.level,
                  level_db=anchor.reference_volume_db + offset if anchor else None)), facts)
        for offset in LEVEL_OFFSETS_DB[1:]
    )))


def _comma_floats(text: str) -> tuple[float, ...]:
    return tuple(float(value) for value in text.split(","))


def preflight_levels(plan: AngleCaptureRequest, facts: PreflightFacts,
                     levels: str | None = None, *, spl: str | None = None) -> PreflightReport | LevelLadder:
    if spl is not None:
        if levels is not None or plan.levels is not None or plan.level.level_db is not None:
            raise ValueError("spl requires a plan without levels or level-db")
        requested = _comma_floats(spl)
        report = preflight(plan, facts)
        anchor = report.plan.level.resolved
        if anchor is None:
            return report
        plan = replace(report.plan, levels=tuple(anchor.fader_db_for(value) for value in requested))
    if levels is not None:
        if not isinstance(levels, str) or plan.level.level_db is not None:
            raise ValueError("levels require a plan without level-db")
        if levels == "auto":
            return level_ladder(plan, facts)
        plan = replace(plan, levels=_comma_floats(levels))
    if plan.levels is None:
        return preflight(plan, facts)
    return LevelLadder(tuple(preflight(replace(plan, levels=None, level=replace(plan.level, level_db=value)), facts)
                             for value in plan.levels))


def prepare_level_captures(plan: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = ()) -> tuple[PlanCapture, ...]:
    return tuple(capture for capture in prepare_plan_captures(plan, roles_bands=roles_bands)
                 if capture.spec.program_phase != PHASE_ENTRY_BASELINE)


@dataclass(frozen=True)
class LevelRun:
    manifest: RunManifest
    door: RunDoor
    analyze: Analyze
    assessor: Callable[..., Any] | None = None
    captures: tuple[PlanCapture, ...] | None = None


async def run_levels(
    ladder: LevelLadder, *, hold: AbstractAsyncContextManager[IsolationHold],
    prepare: Callable[[AngleCaptureRequest], LevelRun], gate: PositionGate,
    aborts: Mapping[type[BaseException], str], signals: RunSignals | None = None,
) -> tuple[RunManifest, ...]:
    """The host supplies each round's record/session bindings under one mic hold.

    One placement starts all levels at that pose. A take needing human recovery
    ends the sequence with its partial manifest; it cannot start a new placement.
    """
    admitted = ladder.admissible
    if not admitted:
        issue = next(issue for issue in ladder.levels[0].issues if issue.blocking)
        raise LateralWalkRefused(issue.code, issue.detail)
    signals = signals or RunSignals()
    results: list[RunManifest] = []
    try:
        async with hold as held:
            for pose_index, (_, group) in enumerate(groupby(ladder.plan.stops, key=lambda stop: stop.place), 1):
                stops = tuple(group)
                prompt = resolve_request(replace(ladder.plan, stops=stops))[0].prompt
                entry = SimpleNamespace(screen={"title": prompt.headline, "body": prompt.detail,
                                               **position_screen_keys(prompt)})
                try:
                    await _grant(gate, pose_index, pose_index, entry, signals)
                except _Control:
                    return tuple(results)
                for level_index, report in enumerate(admitted):
                    request = replace(report.plan, stops=stops)
                    gate.publish({"status": "running", "pose": pose_index,
                                  "level": {"session": request.level.resolved.session() if request.level.resolved else None,
                                            "run": {"level_db": request.level.volume_db}},
                                  "level_index": level_index + 1, "levels": len(admitted)})
                    bound = prepare(request)
                    bound.door.hold = nullcontext(held)
                    if level_index == 0:
                        bound.manifest.mic_moves = 1
                    result = await run_plan(
                        request, manifest=bound.manifest, door=bound.door,
                        analyze=bound.analyze, assessor=bound.assessor, captures=bound.captures,
                        aborts=aborts, signals=signals,
                    )
                    results.append(result)
                    if result.status != "complete" or signals.complete.is_set() or signals.stop.is_set():
                        return tuple(results)
        return tuple(results)
    finally:
        gate.abandon_hold()
