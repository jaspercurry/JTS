# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Plan and execute the bass level ladder, finishing each pose before moving."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, replace
from itertools import groupby
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from jasper.audio_measurement.program import RoleBand

from .angle_capture import AngleCaptureRequest, LateralWalkRefused, resolve_request
from .crossover_v2.capture_plan import position_screen_keys
from .crossover_v2.door import IsolationHold
from .crossover_v2.journey import PHASE_LATERAL
from .crossover_v2.position_gate import PositionGate
from .measurement_programs import PURPOSE_BASS
from .plan_run import Analyze, PlanCapture, RunDoor, RunSignals, _Control, _grant, prepare_plan_captures, run_plan
from .preflight import PreflightFacts, PreflightIssue, PreflightReport, preflight
from .run_manifest import RunManifest

LEVEL_OFFSETS_DB = (0.0, -5.0, -10.0, -15.0)


@dataclass(frozen=True)
class BassLevelLadder:
    levels: tuple[PreflightReport, ...]

    @property
    def plan(self) -> AngleCaptureRequest:
        return self.levels[0].plan

    @property
    def admissible(self) -> tuple[PreflightReport, ...]:
        return tuple(report for report in self.levels if not report.blocking)

    @property
    def blocking(self) -> bool:
        return not self.admissible

    @property
    def issues(self) -> tuple[PreflightIssue, ...]:
        return self.levels[0].issues if self.blocking else ()

    @property
    def spl_ceiling_db_spl(self) -> float | None:
        return self.levels[0].spl_ceiling_db_spl

    def to_dict(self) -> dict[str, Any]:
        return {
            "levels": [{"offset_db": report.plan.level.offset_db, "level_db": report.plan.level.volume_db,
                        "admissible": not report.blocking, **report.to_dict()}
                       for report in self.levels],
            "admissible_levels_db": [report.plan.level.volume_db for report in self.admissible],
        }


def bass_level_ladder(plan: AngleCaptureRequest, facts: PreflightFacts) -> BassLevelLadder:
    if any(stop.purpose != PURPOSE_BASS for stop in plan.stops):
        raise ValueError("the bass level ladder requires bass captures")
    anchor_report = preflight(replace(plan, level=replace(plan.level, level_db=None)), facts)
    anchor = anchor_report.plan.level.resolved
    return BassLevelLadder((anchor_report, *(
        preflight(replace(plan, level=replace(plan.level,
                  level_db=anchor.reference_volume_db + offset if anchor else None)), facts)
        for offset in LEVEL_OFFSETS_DB[1:]
    )))


def preflight_levels(plan: AngleCaptureRequest, facts: PreflightFacts,
                     levels: str | None = None) -> PreflightReport | BassLevelLadder:
    if levels is None:
        return preflight(plan, facts)
    if not isinstance(levels, str) or plan.level.level_db is not None or any(stop.purpose != PURPOSE_BASS for stop in plan.stops):
        raise ValueError("levels require a bass plan without level-db")
    if levels == "auto":
        return bass_level_ladder(plan, facts)
    values = tuple(float(value) for value in levels.split(","))
    if len(set(values)) != len(values):
        raise ValueError("levels must be distinct")
    return BassLevelLadder(tuple(preflight(replace(plan, level=replace(plan.level, level_db=value)), facts)
                                 for value in values))


def prepare_bass_captures(plan: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = ()) -> tuple[PlanCapture, ...]:
    return tuple(capture for capture in prepare_plan_captures(plan, roles_bands=roles_bands)
                 if capture.spec.program_phase == PHASE_LATERAL)


@dataclass(frozen=True)
class BassLevelRun:
    manifest: RunManifest
    door: RunDoor
    analyze: Analyze
    assessor: Callable[..., Any] | None = None
    captures: tuple[PlanCapture, ...] | None = None


async def run_bass_levels(
    ladder: BassLevelLadder, *, hold: AbstractAsyncContextManager[IsolationHold],
    prepare: Callable[[AngleCaptureRequest], BassLevelRun], gate: PositionGate,
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
