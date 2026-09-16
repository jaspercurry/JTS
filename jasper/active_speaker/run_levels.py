# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Plan and execute levels, finishing each pose before moving."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from itertools import groupby
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence, cast

from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from jasper.json_fields import finite_float

from .angle_capture import AngleCaptureRequest, LateralWalkRefused, resolve_request
from .crossover_v2.capture_plan import position_screen_keys
from .crossover_v2.door import IsolationHold
from .crossover_v2.journey import PHASE_ENTRY_BASELINE
from .crossover_v2.measurement_context import capture_basis
from .crossover_v2.position_gate import PositionGate
from .plan_run import Analyze, PlanCapture, RunDoor, RunSignals, _Control, _grant, prepare_plan_captures, run_plan
from .preflight import PreflightFacts, PreflightIssue, PreflightReport, preflight
from .run_manifest import RunManifest

LEVEL_OFFSETS_DB = (0.0, -5.0, -10.0, -15.0)


@dataclass(frozen=True)
class LevelLadder:
    levels: tuple[PreflightReport, ...]
    facts: PreflightFacts
    admissions: list[dict[str, Any]] = field(default_factory=list)

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
            "anchor_stimulus": self.facts.anchor.record.get("stimulus"),
            "requested_stimulus": {"program": self.plan.program, "template": self.plan.template.to_dict(),
                                   "stops": [{"regime": stop.regime, "stimulus": stop.stimulus}
                                             for stop in self.plan.stops]},
            "admissions": self.admissions,
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
    return _ladder(tuple(replace(plan, level=replace(plan.level,
                        level_db=anchor.reference_volume_db + offset if anchor else None))
                         for offset in sorted(LEVEL_OFFSETS_DB)), facts)


def _ladder(plans: Sequence[AngleCaptureRequest], facts: PreflightFacts) -> LevelLadder:
    reports: list[PreflightReport] = []
    for plan in plans:
        reports.append(preflight(plan, facts, defer_rung=any(not report.blocking for report in reports)))
    return LevelLadder(tuple(reports), facts)


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
    return _ladder(tuple(replace(plan, levels=None, level=replace(plan.level, level_db=value))
                         for value in plan.levels), facts)


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
    """Finish admissible levels at each pose under one mic hold."""
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
                previous: list[dict[str, Any]] | None = None
                for level_index, report in enumerate(admitted):
                    request = replace(report.plan, stops=stops)
                    report = preflight(request, ladder.facts, previous_rung=previous)
                    if report.blocking:
                        issue = next(issue for issue in report.issues if issue.blocking)
                        raise LateralWalkRefused(issue.code, issue.detail)
                    request = report.plan
                    observations: list[dict[str, Any]] = []
                    admission = {"pose_index": pose_index, "level_index": level_index + 1,
                                 "requested_db_spl": request.level.predicted_db_spl,
                                 "admitted_db_spl": request.level.predicted_db_spl,
                                 "level_db": request.level.volume_db, **report.rung_admission,
                                 "observations": observations}
                    ladder.admissions.append(admission)
                    gate.publish({"status": "running", "pose": pose_index,
                                  "level": {"session": request.level.resolved.session() if request.level.resolved else None,
                                            "run": {"level_db": request.level.volume_db}},
                                  "level_index": level_index + 1, "levels": len(admitted)})
                    bound = prepare(request)
                    def analyze(record: Mapping[str, Any], record_id: str) -> ProgramAnalysis:
                        basis = capture_basis(record)
                        spl = (record.get("capture_integrity") or {}).get("spl") or {}
                        measured = finite_float(spl.get("loudest_half_second_db_spl"))
                        predicted = request.level.predicted_db_spl
                        anchor_stimulus = ladder.facts.anchor.record.get("stimulus") or {}
                        observations.append({"record_id": record_id, "level_db": basis["level_db"], "spl": spl,
                            "run_stimulus": {"program_id": basis["program_id"],
                                             "wav_sha256": basis["stimulus_wav_sha256"],
                                             "peak_dbfs": basis["stimulus_peak_dbfs"]},
                            "stimulus_mismatch": (anchor_stimulus["program_id"] != basis["program_id"]
                                if anchor_stimulus.get("program_id") and basis["program_id"] else None),
                            "measured_offset_db": measured - predicted
                                if measured is not None and predicted is not None else None})
                        return bound.analyze(record, record_id)

                    bound.door.hold = nullcontext(held)
                    if level_index == 0:
                        bound.manifest.mic_moves = 1
                    result = await run_plan(
                        request, manifest=bound.manifest, door=bound.door,
                        analyze=analyze, assessor=bound.assessor, captures=bound.captures,
                        aborts=aborts, signals=signals,
                    )
                    results.append(result)
                    previous = observations
                    if result.cancelled or result.stopped_at or any(take.get("next") == "stop" for take in result.takes):
                        signals.request_stop(result.reason or "take_stopped")
                    if signals.complete.is_set() or signals.stop.is_set():
                        return tuple(results)
        return tuple(results)
    finally:
        gate.abandon_hold()
