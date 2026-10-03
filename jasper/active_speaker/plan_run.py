# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Execute planned captures with one retry owner and one evidence manifest (ADR-0296)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from itertools import accumulate, groupby
from collections import Counter
from functools import partial
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from jasper.platform.json_fields import finite_float
from jasper.platform.log_event import log_event
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS
from jasper.audio_measurement.program import ExcitationProgram, KIND_PILOT, KIND_SUMMED_SWEEP, KIND_SWEEP, is_level_probe
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from jasper.audio_measurement.wired_capture import WIRED_POST_ROLL_S, WiredSplMonitor
from jasper.runtime.measurement_window import MeasurementWindowError

from .angle_capture import (
    WALK_COMMISSIONING_STOP_UNSET, WALK_SPL_CALIBRATION_REQUIRED, WALK_STIMULUS_NOT_ACCEPTED, WALK_LEVEL_POLICY_INVALID,
    AngleCaptureRequest, LateralWalkRefused,
    level_sets, resolve_request, take_level,
)
from .capture_schedule import PlanCapture, prepare_plan_captures as prepare_plan_captures, run_probe_index
from .crossover_v2.admission import SlotAttempts
from .crossover_v2.capture_dispatch import assess, level_drift_verdict
from .crossover_v2.capture_plan import announce_run, pose_batch_screens, position_geometry, position_screen_keys
from .crossover_v2.capture_source import CaptureBeginDeferred, CaptureBeginRefused, CaptureStopped
from .crossover_v2.door import IsolationHold, OpenMeasurementDoor, MeasurementDoorRefused, level_window
from .crossover_v2.journey import PHASE_CHECK
from .crossover_v2.measure_spec import CANDIDATE_SCOPES, MeasureSpec, branch_probes, solo_target
from .crossover_v2.position_gate import POSITION_HOLD_POLL_S, PositionGate
from .crossover_v2.program_transaction import StimulusCaptureStopped, playback_observer
from .crossover_v2.refusal_copy import (
    CAPTURE_QUALITY_REFUSAL_CODES, REASON_INTERNAL_ERROR, REASON_LEVEL_UNSOLVED, REASON_REGISTRY, REASON_RETRIES_SPENT,
    REASON_USER_STOPPED, TakeVerdict,
    channel_map_failed_roles, exception_detail,
)
from .crossover_v2.session import TuningSession
from .program_failure import classify_program_failure
from .restore_wait import resilient_restore
from .measurement_programs import BASE_CANDIDATE, POSE_KIND_SEAT, PoseLevel, run_level
from .crossover_v2.programs import predictive_program_for_spec, probe_backoff_db, probe_fader_db, run_fader_db
from .run_manifest import RunManifest, driver_level_mismatches
from .round_copy import PLACE_MICROPHONE, take_counts

logger = logging.getLogger(__name__)
_OWN_CODE = (CaptureBeginRefused, StimulusCaptureStopped)
Analyze = Callable[[Mapping[str, Any]], ProgramAnalysis]
#: What a take's assessment may raise and still answer with a stop; any other ends the run.
_ASSESSMENT_FAILURES = (ValueError, KeyError, OSError)
#: The verdicts that retake at the level they name.
_LEVEL_RETAKES = frozenset({"retake_louder", "retake_quieter"})
#: dB a branch take's sum plays under the lower of its branches' levels: two
#: branches in phase read at most 6 dB over the louder one alone (ADR-0403 §3).
BRANCH_SUM_MARGIN_DB = 6.0
#: A graded take's host effects (a rearm, an acceptance), held while its capture
#: plays so that no later rung is composed from them (ADR-0383).
_held_effects: ContextVar[list[Callable[[], None]] | None] = ContextVar("held_effects", default=None)


def after_grading(effect: Callable[[], None]) -> None:
    """Run ``effect`` now, or, inside a capture, before its next take is graded or once it has played."""
    held = _held_effects.get()
    if held is None:
        effect()
    else:
        held.append(effect)


def _release(held: list[Callable[[], None]]) -> None:
    while held:
        held.pop(0)()


@dataclass
class RunSignals:
    """Thread-safe host inputs, consumed only by the executor."""

    retake: Event = field(default_factory=Event)
    complete: Event = field(default_factory=Event)
    stop: Event = field(default_factory=Event)
    stop_reason: str = field(init=False, default=REASON_USER_STOPPED)
    _stop_lock: Any = field(init=False, default_factory=Lock, repr=False)

    def request_stop(self, reason: str = REASON_USER_STOPPED) -> None:
        with self._stop_lock:
            if not self.stop.is_set():
                self.stop_reason = reason
                self.stop.set()


def spl_monitor_note(ceiling_db_spl: float) -> str:
    """The commissioning stop watched for the run."""
    return f"ceiling_{float(ceiling_db_spl):g}_db_spl"


def spl_watch(*, ceiling_db_spl: float, sensitivity: Any | None, device: Any) -> tuple[WiredSplMonitor, str]:
    """Require a calibrated watch at the commissioning stop."""
    if sensitivity is None:
        raise LateralWalkRefused(
            WALK_SPL_CALIBRATION_REQUIRED,
            "an SPL ceiling requires a resolvable microphone sensitivity",
        )
    channel = int(SUPPORTED_MODELS[device.model_key].get("capture_channel", 0))
    return WiredSplMonitor(sensitivity, ceiling_db_spl, channel), spl_monitor_note(ceiling_db_spl)


def request_fingerprint(request: AngleCaptureRequest) -> str:
    """This walk's identity: sha256 over ``AngleCaptureRequest.to_dict()``.

    The inline plan and manifest use the same document without a clock, so
    two runs of one walk fingerprint alike, and an edited stop does not.
    """
    return json_fingerprint(request.to_dict())


@dataclass
class RunDoor:
    hold: AbstractAsyncContextManager[IsolationHold]
    build_session: Callable[[OpenMeasurementDoor, Callable[[], str]], TuningSession]
    sensitivity: Any
    device: Any
    ceiling_db_spl: float | None
    current: TuningSession | None = None
    isolation: IsolationHold | None = None
    program_for_spec: Callable[..., ExcitationProgram] | None = None
    #: The session's driver caps; a door that states them finds its run's fader (ADR-0403 §4).
    caps_dbfs: Mapping[str, float] | None = None
    #: dB a take of the run may play over the take it probed: the largest bass lift
    #: and rear-woofer sum against the probe's graph (ADR-0403 §4).
    margin_db: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.current is not None and self.current.is_open


@dataclass(frozen=True)
class _Work:
    spec: MeasureSpec
    stop: Mapping[str, Any]
    pose_index: int
    config: int
    size: int
    entry: Any
    #: The rule this take is levelled and graded by; ``None`` plays at the fader or carries its set's level.
    pose_level: PoseLevel | None
    #: The take whose landed level this take's set shares (``angle_capture.level_sets``).
    level_set: int | None


def _first_play(spec: MeasureSpec) -> MeasureSpec:
    """What a take plays first: a branch take that finds its level plays its
    first branch's probe (ADR-0403 §3)."""
    return next(iter(branch_probes(spec)), spec)


def _relevelled(spec: MeasureSpec, played: ExcitationProgram, probes: Sequence[float], peak_db: float) -> MeasureSpec:
    """A branch take's level retake to the new peak its assessment names, from what
    each segment played (ADR-0407). A cut lowers every segment by what the peak
    moves, even one its ceiling held. A raise lifts each by that much, never over its
    own probe's level (the sum's: 6 dB under the lower), and lifts nothing when the
    peak played under what it asked, held at its ceiling."""
    alone = {segment.role: segment.gain_db for segment in played.stimulus_segments() if segment.kind == KIND_SWEEP}
    plays = [*(alone[target] for target in spec.branch_target_ids), played.segment("sweep_verify").gain_db]
    asked = [*spec.branch_levels_dbfs, spec.level_ladder_dbfs[0]]
    loudest = max(range(len(plays)), key=plays.__getitem__)
    shift = peak_db - plays[loudest]
    if shift > 0.0:
        held = plays[loudest] < asked[loudest] - 1e-6
        tops = [*probes, min(probes) - BRANCH_SUM_MARGIN_DB]
        moved = [play if held else min(play + shift, top) for play, top in zip(plays, tops)]
    else:
        moved = [play + shift for play in plays]
    return replace(spec, level_ladder_dbfs=(moved[-1],), branch_levels_dbfs=tuple(moved[:-1]))


def _landed_db_spl(verdict: TakeVerdict, rule: PoseLevel, probe: ExcitationProgram) -> float:
    """Where the take a probe levelled lands: 1 dB under its target, or less where its
    raise or its ceiling held it (ADR-0365)."""
    target, heard = (finite_float(verdict.evidence.get(key)) for key in ("level_target_db_spl", "level_db_spl"))
    aim = (rule.target_db_spl if target is None else target) - rule.tolerance_db / 2
    ceiling = max(segment.gain_db for segment in probe.stimulus_segments())
    return ((aim if heard is None else min(aim, heard + rule.max_raise_db))
            - max(0.0, (verdict.next_gain_db or 0.0) - ceiling))


def _pose(stop: Any) -> dict[str, Any]:
    pose = stop.pose
    return {"kind": pose.kind, "azimuth_deg": pose.azimuth_deg, "elevation_deg": pose.elevation_deg,
            "distance_m": pose.distance_m, "place": pose.place, "seat_offset_m": pose.seat_offset_m,
            **({"driver": pose.driver} if pose.driver else {})}


def _planned_row(index: int, repeat: int, stop: Any) -> dict[str, Any]:
    return {"index": index, "repeat": repeat, "pose": _pose(stop),
            "candidate_id": stop.candidate_id, "purpose": stop.purpose, "purposes": list(stop.purposes)}


# Allow a person to move the stand and confirm placement between pose batches.
HUMAN_MOVE_ALLOWANCE_S = 30


def schedule_facts(captures: Sequence[tuple[Mapping[str, Any], MeasureSpec]], program_for_spec: Callable[..., ExcitationProgram],
                   *, mover: str, program: str = "") -> dict[str, Any]:
    poses, pose_sweeps, work_sweeps, measurements_per_pose = [], [], [], []
    played_s = 0.0
    for _, batch in groupby(captures, key=lambda capture: capture[0]["place"]):
        details: list[dict[str, Any]] = []
        keys = []
        for measurement, (pose, spec) in enumerate(batch, 1):
            if not details:
                poses.append(dict(pose))
            first = program_for_spec(spec)
            # A take that finds its level or its run's fader plays its probe first (ADR-0365,
            # ADR-0403 §4), and a branch set's first take one probe of each branch alone (§3).
            probe = first if is_level_probe(first) else None
            excitation = first if probe is None else program_for_spec(spec, stimulus_dbfs=0.0)
            for play in (*([] if probe is None else [probe]), *map(program_for_spec, branch_probes(spec)), excitation):
                played_s += play.total_samples / play.sample_rate_hz + WIRED_POST_ROLL_S
            segments = excitation.stimulus_segments()
            work_sweeps.append(len(segments))
            for segment in segments:
                keys.append((spec.graph_scope, spec.candidate_id, spec.program_phase, segment.role, segment.kind))
                details.append({"role": segment.role or "summed", "kind": segment.kind, "phase": spec.program_phase,
                                "scope": spec.graph_scope})
        totals = Counter(keys)
        seen: Counter[tuple[str, str | None, str | None, str | None, str]] = Counter()
        for key, row in zip(keys, details):
            seen[key] += 1
            row.update(repeat=seen[key], repeats=totals[key])
        measurements_per_pose.append(measurement)
        pose_sweeps.append(details)
    rows = [row for pose_rows in pose_sweeps for row in pose_rows]
    counts = [len(pose_rows) for pose_rows in pose_sweeps]
    return {"program": program, "mover": mover, "poses": len(poses), "pose_details": poses,
            "measurements": len(captures), "measurements_per_pose": measurements_per_pose,
            "sweeps_per_pose": counts, "sweeps": len(rows), "work_sweeps": work_sweeps,
            "timing_sweeps": sum(row["scope"] == "timing" and row["kind"] == KIND_SUMMED_SWEEP for row in rows),
            "preparation_sweeps": sum(row["kind"] == KIND_PILOT for row in rows),
            "estimated_seconds": played_s + (len(poses) * HUMAN_MOVE_ALLOWANCE_S if mover == "human" else 0),
            "pose_sweeps": pose_sweeps}


def preview_schedule(request: AngleCaptureRequest, captures: Sequence[PlanCapture], context: Any) -> dict[str, Any]:
    specs = list(announce_run([capture.spec for capture in captures]))
    shared = level_sets([capture.stop for capture in captures], [capture.spec.graph_scope for capture in captures])
    probed = run_probe_index([(capture.spec.graph_scope, start is not None) for capture, start in zip(captures, shared)])
    if probed is not None:
        specs[probed] = replace(specs[probed], level_probe=True)
    return schedule_facts([(_pose(c.stop), spec) for c, spec in zip(captures, specs)], predictive_program_for_spec(context),
                          mover=request.mover, program=request.program or "")


async def publish_sweeps(progress: dict[str, Any], gate: PositionGate, before: int,
                         program: ExcitationProgram, play: Callable[[], Awaitable[Any]]) -> Any:
    handles = []
    def publish(index: int) -> None:
        try:
            row = progress["pose_sweeps"][progress["pose"] - 1][before + index - 1]
        except (KeyError, IndexError):
            return
        progress.update(sweep=before + index, role=row["role"], sweep_kind=row["kind"],
                        repeat=row["repeat"], repeats=row["repeats"])
        gate.publish(progress)
    try:
        for index, segment in enumerate(program.stimulus_segments(), 1):
            handles.append(asyncio.get_running_loop().call_later(segment.start_sample / program.sample_rate_hz,
                                                                 publish, index))
        return await play()
    finally:
        for handle in handles:
            handle.cancel()


async def run_plan(
    request: AngleCaptureRequest, *, session: TuningSession | None = None, manifest: RunManifest,
    door: RunDoor | None = None,
    analyze: Analyze, gate: PositionGate | None = None,
    aborts: Mapping[type[BaseException], str],
    signals: RunSignals | None = None, spl_monitor: str = "",
    clock: Callable[[], float] = time.monotonic,
    gain_ceiling_db: Mapping[str, float] | None = None,
    captures: Sequence[PlanCapture],
    admit: Callable[[int, int, Any, SlotAttempts], None] | None = None,
    assessor: Callable[..., TakeVerdict] | None = None,
    measure: Callable[[TuningSession, MeasureSpec], Awaitable[Any]] | None = None,
    announce: bool = True,
) -> RunManifest:
    from .candidate_parts import baseline_candidate_id  # lazy: baseline composition loads DSP analysis

    manifest.request_fingerprint = request_fingerprint(request)
    manifest.preset, manifest.layout = request.program, request.layout
    manifest.spl_monitor = spl_monitor
    manifest.asked = {
        "poses": list({stop.pose.place: _pose(stop) for stop in request.stops}.values()),
        "candidates": list(request.candidates or ("base",)),
        "mover": request.mover, "level": asdict(request.level), "repeats": request.repeats,
        "retries_per_pose": request.retries_per_pose,
    }
    manifest.planned = [_planned_row(index * request.repeats + repeat, repeat, stop)
                        for index, stop in enumerate(request.stops) for repeat in range(1, request.repeats + 1)]
    try:
        resolve_request(request)
        try:
            baseline_id = (baseline_candidate_id()
                           if any(capture.spec.candidate_id == BASE_CANDIDATE for capture in captures) else "")
        except ValueError as exc:
            raise LateralWalkRefused(getattr(exc, "code", WALK_STIMULUS_NOT_ACCEPTED), str(exc)) from exc
    except LateralWalkRefused as exc:
        manifest.reason, manifest.detail, manifest.finalized = exc.reason, exc.detail, True
        await manifest.persist()
        return manifest

    # Every take is a prepared capture, so each take that levels itself is marked to probe (ADR-0405).
    specs = tuple(replace(capture.spec, candidate_id=baseline_id)
                  if capture.spec.candidate_id == BASE_CANDIDATE else capture.spec for capture in captures)
    stops = [capture.resolved(request) for capture in captures]
    manifest.planned = [_planned_row(index, capture.repeat, capture.stop) for index, capture in enumerate(captures, 1)]
    angle_stops = [capture.stop for capture in captures]
    places = [stop.pose.place for stop in angle_stops]
    scopes = [spec.graph_scope for spec in specs]
    level_starts = level_sets(angle_stops, scopes)
    level = request.level.level_db
    if level is None and session is not None:
        level = session.measurement_level_db
    finds = door is not None and bool(door.caps_dbfs)
    if (level is None and not finds) or (door is None and (session is None or level != session.measurement_level_db)):
        raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "The plan needs a level or a probe to find one")
    if announce:
        specs = announce_run(specs)
    manifest.level = {"run": {"level_db": level, "level_source": request.level_source}}
    expanded = []
    planned: list[dict[str, Any]] = []
    for pose_index, (_place, batch) in enumerate(groupby(enumerate(specs), key=lambda row: places[row[0]])):
        for offset, spec in batch:
            stop = {**manifest.planned[offset], "index": len(planned) + 1,
                    "pose": {**manifest.planned[offset]["pose"],
                             "distance_m": position_geometry(stops[offset].prompt).mark_distance_m},
                    "capture_index": manifest.planned[offset]["index"]}
            planned.append(stop)
            if spec is not None:
                expanded.append((spec, stop, pose_index, stops[offset],
                                 take_level(angle_stops[offset], scope=spec.graph_scope, over_timing="timing" in scopes)
                                 if spec.level_probe else None, level_starts[offset]))
    manifest.planned = planned
    screens = pose_batch_screens(list(range(1, len(expanded) + 1)),
                                 [row[3].prompt for row in expanded], [row[3].candidate_id for row in expanded])
    work: list[_Work] = []
    for _pose_index, expanded_batch in groupby(enumerate(expanded), key=lambda row: row[1][2]):
        expanded_rows = list(expanded_batch)
        for config, (index, (played_spec, stop, pose_index, resolved_stop, rule, level_set)) in enumerate(
                expanded_rows, 1):
            entry = SimpleNamespace(screen={**resolved_stop.screen,
                                    "title": resolved_stop.prompt.headline, "body": resolved_stop.prompt.detail,
                                    **position_screen_keys(resolved_stop.prompt), **screens.get(index + 1, {})})
            work.append(_Work(played_spec, stop, pose_index, config, len(expanded_rows), entry, rule, level_set))
    return await _run(work, session=session, door=door, level=level,
                      manifest=manifest, analyze=analyze, gate=gate,
                      aborts=aborts, signals=signals or RunSignals(), retries=request.retries_per_pose,
                      clock=clock, gain_ceiling_db=gain_ceiling_db, admit=admit, assessor=assessor, measure=measure)


class _Control(Exception):
    pass


def _is_replay(attempt: int, ledger: SlotAttempts, spent: int) -> bool:
    """A take after the first that spent no retry, such as the play after a level probe (#5722)."""
    return attempt > 1 and ledger.by_household + ledger.by_speaker == spent


async def _grant(gate: PositionGate | None, index: int, attempt: int, entry: Any, signals: RunSignals,
                 admit: Callable[[], None] | None = None) -> None:
    while True:
        if signals.stop.is_set():
            raise CaptureStopped("capture stopped")
        if signals.complete.is_set() or signals.retake.is_set():
            raise _Control
        try:
            if gate:
                gate.gate(index, attempt, entry)
            if admit:
                admit()
            return
        except CaptureBeginDeferred:
            await asyncio.sleep(POSITION_HOLD_POLL_S)


async def _run(
    work: Sequence[_Work], *, session: TuningSession | None, manifest: RunManifest, analyze: Analyze,
    door: RunDoor | None = None, level: float | None = None,
    gate: PositionGate | None, aborts: Mapping[type[BaseException], str], signals: RunSignals,
    retries: int, clock: Callable[[], float], gain_ceiling_db: Mapping[str, float] | None,
    admit: Callable[[int, int, Any, SlotAttempts], None] | None = None,
    assessor: Callable[..., TakeVerdict] | None = None,
    measure: Callable[[TuningSession, MeasureSpec], Awaitable[Any]] | None = None,
) -> RunManifest:
    def default_admit(index: int, attempt: int, entry: Any, ledger: SlotAttempts) -> None:
        ledger.admit()

    def failure_reason(exc: BaseException) -> str:
        classified = classify_program_failure(exc)
        code = classified[0] if classified else getattr(exc, "code", None) or getattr(exc, "reason", None)
        return code if isinstance(code, str) and code in REASON_REGISTRY else REASON_INTERNAL_ERROR

    def attempt_records() -> list[tuple[Mapping[str, Any], str]]:
        return manifest.pending_records or [({"take_id": manifest.allocate_take_id()}, "")]

    level_observations: dict[str, TakeVerdict] = {}

    def observe_level(item: _Work, record: Mapping[str, Any]) -> TakeVerdict:
        take_id = str(record["take_id"])
        if take_id not in level_observations:
            observation = manifest.level_observation(record)
            if item.pose_level is not None:
                # A take at a pose that levels itself answers to its target, never its repeats (ADR-0361).
                observation["level_reference_db_spl"] = None
            level_observations[take_id] = level_drift_verdict(**observation)
        return level_observations[take_id]

    run_task = asyncio.current_task()
    failures: dict[str, Exception] = {}

    def grade(item: _Work, spec: MeasureSpec, record: Mapping[str, Any], level_verdict: TakeVerdict) -> TakeVerdict:
        try:
            _release(_held_effects.get() or [])
            analysis = analyze(record)
            program = ExcitationProgram.from_dict(record["program"]) if record.get("program") else None
            assessed = (assessor or assess)(analysis, phase=program.phase if program else spec.program_phase or "verify",
                                            spl=(record.get("capture_integrity") or {}).get("spl"),
                                            program=program, gain_ceiling_db=gain_ceiling_db, level_verdict=level_verdict,
                                            # A branch take is levelled by its branches' probes, never by
                                            # its own reading (ADR-0403 §3).
                                            pose_level=None if spec.graph_scope == "candidate_branches"
                                            else item.pose_level,
                                            level_asked_dbfs=next(iter(spec.level_ladder_dbfs), None))
            if program is not None and is_level_probe(program):
                log_event(logger, "active_speaker.level_probe", fields={
                    "pose": item.pose_index + 1, "driver": solo_target(spec) or None,
                    "distance_m": item.stop["pose"].get("distance_m"), "fault": assessed.fault,
                    "next_gain_db": assessed.next_gain_db,
                    **{key: value for key, value in assessed.evidence.items()
                       if key.startswith("level_")}})
            return assessed
        except Exception as exc:  # noqa: BLE001 - the take banks a stop; the loop raises an unexpected error after measure
            failures[str(record["take_id"])] = exc
            log_event(logger, "active_speaker.take_assessment_failed", level=logging.WARNING,
                      take_id=record["take_id"], error_type=type(exc).__name__)
            return TakeVerdict(False, fault=REASON_INTERNAL_ERROR, next="stop", evidence={"error_type": type(exc).__name__})

    async def judge(item: _Work, spec: MeasureSpec, record: Mapping[str, Any]) -> tuple[TakeVerdict, Mapping[str, Any]]:
        level_verdict = observe_level(item, record)
        # Grading a take banked as its run is cancelled would hold the cancel, and one
        # after its capture's assessment raised would run host effects for a capture
        # the run abandons (ADR-0383).
        if ((run_task is not None and run_task.cancelling())
                or any(not isinstance(failure, _ASSESSMENT_FAILURES) for failure in failures.values())):
            unassessed = TakeVerdict(False, fault=REASON_INTERNAL_ERROR, next="stop", evidence={"assessed": False})
            return unassessed, level_verdict.evidence
        return await asyncio.to_thread(grade, item, spec, record, level_verdict), level_verdict.evidence

    admit = admit or default_admit
    manifest.specs = {item.stop["index"]: item.spec for item in work}
    aborting: tuple[type[BaseException], ...] = (*_OWN_CODE, *aborts, CaptureStopped, asyncio.CancelledError)
    ledgers = {item.pose_index: SlotAttempts(retries_per_pose=retries) for item in work}
    attempts = [0] * len(work)
    offset, grant_epoch = 0, 0
    retry: TakeVerdict | None = None
    retry_was_measured = False
    playing = [_first_play(item.spec) for item in work]
    branch_levels: dict[int, list[float]] = {}
    #: Each branch set's probe levels, and a branch take's next levels after its own level retake.
    set_probes: dict[int | None, tuple[float, ...]] = {}
    relevelled: dict[int, MeasureSpec] = {}
    unlevelled: set[int] = set()
    # A run finds its fader with a probe of its first summed take that plays at the run's
    # fader, before the first take of that take's placement, banked as that take's attempts.
    # Until then only takes that level themselves play, at the probe fader: preflight refuses a
    # run where a take at its fader would play first. The fader held is the probe's, turned down by
    # what its margins pass the stop by, never above the level the plan states (ADR-0403 §4).
    caps, cap = (door.caps_dbfs if door is not None else None), level
    probe_at = probe_start = None
    other_graphs = False

    def hold_fader(found: float, source: str) -> float:
        held = found if cap is None else min(found, cap)
        manifest.level["run"].update(level_db=held,
                                     source=source if held == found else manifest.level["run"]["level_source"])
        return held

    if caps:
        probe_at = run_probe_index([(item.spec.graph_scope, item.level_set is not None) for item in work])
        level = hold_fader(probe_fader_db(caps), "probe_fader")
        manifest.level["run"]["probe_fader_db"] = level
        if probe_at is not None:
            probe_start = next(index for index, item in enumerate(work) if item.pose_index == work[probe_at].pose_index)
            manifest.level["run"]["level_db"] = None
            playing[probe_at] = replace(work[probe_at].spec, level_probe=True)
            probed = (work[probe_at].spec.graph_scope, work[probe_at].spec.candidate_id)
            other_graphs = any((item.spec.graph_scope, item.spec.candidate_id) != probed for item in work
                               if item.spec.graph_scope in CANDIDATE_SCOPES and item.level_set is None)

    def solved_level(offset: int, pending: TakeVerdict) -> float | None:
        """The last level solved for a take that levels itself, never above the last it
        played; a branch take's needs both its branches' levels (ADR-0403 §3)."""
        probes, found = branch_probes(work[offset].spec), branch_levels.get(offset, [])
        if len(found) < len(probes):
            return None
        solved = [*playing[offset].level_ladder_dbfs, *((min(found) - BRANCH_SUM_MARGIN_DB,) if probes else ()),
                  *((pending.next_gain_db,) if pending.next in _LEVEL_RETAKES and pending.next_gain_db is not None
                    else ())]
        return min(solved, default=None)

    moved: set[int] = set()
    verdict: TakeVerdict | None = None
    schedule: dict[str, Any] = schedule_facts([(item.stop["pose"], playing[index] if index == probe_at else item.spec)
                                               for index, item in enumerate(work)], door.program_for_spec,
                              mover=manifest.asked["mover"], program=manifest.preset or "") if door and door.program_for_spec else {"poses": len(ledgers)}
    sweep_offsets = list(accumulate(schedule.get("work_sweeps", [0] * len(work)), initial=0))
    progress: dict[str, Any] = {}
    stack, window = AsyncExitStack(), AsyncExitStack()
    hold: IsolationHold | None = None
    try:
        await manifest.persist()
        if door is not None:
            if door.ceiling_db_spl is None:
                raise LateralWalkRefused(WALK_COMMISSIONING_STOP_UNSET, "Preflight supplied no SPL ceiling")
            hold = door.isolation = await stack.enter_async_context(door.hold)
            await stack.enter_async_context(window)
        while offset < len(work):
            if signals.complete.is_set():
                manifest.reason = "complete_requested"
                break
            if signals.retake.is_set():
                signals.retake.clear()
                pose = work[offset].pose_index
                offset = next(i for i, row in enumerate(work) if row.pose_index == pose)
                if offset == probe_start and probe_at is not None:
                    offset = probe_at
                retry = TakeVerdict(True, next="fix_and_retake", charge="operator")
                retry_was_measured = False
                manifest.discard_pose(pose)
                if not attempts[offset]:
                    # Before any take played, a redo only asks for the placement again.
                    retry = None
                    grant_epoch += 1
                    if gate:
                        gate.abandon_hold()
                elif work[offset].pose_level is not None:
                    # A pose that levels itself starts over with its retries, so its redo is free (ADR-0361).
                    ledgers[pose] = SlotAttempts(retries_per_pose=retries)
            if offset == probe_start and probe_at is not None:
                offset = probe_at
            if offset in unlevelled:
                # A take of a set that found no level plays at none, never at the run's fader (ADR-0361 §3).
                manifest.mark_not_measured(work[offset].stop["index"], REASON_LEVEL_UNSOLVED)
                retry = None
                retry_was_measured = False
                offset += 1
                continue
            item = work[offset]
            if offset == probe_at:
                item = replace(item, pose_level=run_level(item.stop["pose"]["kind"]))
            ledger = ledgers[item.pose_index]
            if retry is not None:
                if not ledger.can_admit(retry.charge):
                    if (retry_was_measured and retry.fault in CAPTURE_QUALITY_REFUSAL_CODES
                            and item.spec.program_phase != PHASE_CHECK and offset != probe_at):
                        manifest.mark_not_measured(item.stop["index"], retry.fault)
                        if item.pose_level is not None:
                            # The rest of a set that levels itself plays only at a level solved for it,
                            # the last one, or not at all (ADR-0361 §3, ADR-0403).
                            found = solved_level(offset, retry)
                            for index in range(offset + 1, len(work)):
                                if work[index].level_set == item.level_set:
                                    if found is None:
                                        unlevelled.add(index)
                                    else:
                                        # Each branch alone carries the last level solved for it too (ADR-0407).
                                        pending = relevelled.get(offset, playing[offset]).branch_levels_dbfs
                                        playing[index] = replace(
                                            work[index].spec, level_ladder_dbfs=(found,), branch_levels_dbfs=tuple(
                                                map(min, playing[offset].branch_levels_dbfs, pending)))
                        relevelled.pop(offset, None)
                        retry = None
                        retry_was_measured = False
                        offset += 1
                        continue
                    manifest.reason = retry.fault or REASON_RETRIES_SPENT
                    break
                # A run with no gate is a ladder's rung: its ladder holds the placement, so the
                # take plays again where it is (#6113).
                if retry.next == "fix_and_retake":
                    if probe_at is not None and session is not None:
                        # The fader sits at the probe fader only while the probe plays (ADR-0403 §4).
                        await window.aclose()
                        session = None
                    grant_epoch += 1
                    if gate:
                        gate.abandon_hold()
                    # A new placement starts each take there that levels itself at its probe again (ADR-0365).
                    for index, row in enumerate(work):
                        if row.pose_index == item.pose_index and row.pose_level is not None:
                            playing[index] = _first_play(row.spec)
                            branch_levels.pop(index, None)
                            relevelled.pop(index, None)
                if retry.next in _LEVEL_RETAKES:
                    if retry.next_gain_db is None:
                        manifest.reason = retry.fault or "retry_gain_missing"
                        break
                    playing[offset] = (relevelled.pop(offset) if offset in relevelled
                                       else replace(playing[offset], level_ladder_dbfs=(retry.next_gain_db,)))
                if retry.charge == "replay":
                    # A level probe's next play is the take, not a retake (ADR-0365).
                    retry = None
            spec = playing[offset]
            attempt = attempts[offset] + 1
            before = sweep_offsets[offset] - sweep_offsets[offset - item.config + 1]
            notices = {}
            if retry:
                reason = retry.fault or ("operator" if retry.next == "fix_and_retake" and retry.charge == "operator" else None)
                notices = dict(retake_measurement=offset + 1, retake_sweep=before + 1, retake_pose=item.pose_index + 1, retake_action=retry.next,
                               retake_sweep_end=before + sweep_offsets[offset + 1] - sweep_offsets[offset],
                               **({"retake_reason": reason} if reason else {}),
                               **({"level_raise_dbfs": retry.next_gain_db} if retry.next == "retake_louder" else {}))
            if item.pose_level is not None:
                notices["level_step"] = "levelled" if spec.level_ladder_dbfs else "probe"
            progress = {**schedule, **notices, "pose": item.pose_index + 1,
                        "level": manifest.level, "config": item.config, "configs": item.size, "attempt": attempt,
                        "fault": retry.fault if retry else None, "next_action": retry.next if retry else None,
                        "budget": ledger.to_payload(), "sweep": before + 1, "measurement": offset + 1,
                        "level_mismatches": driver_level_mismatches(manifest.joined())}
            placed = probe_start if offset == probe_at and probe_start is not None else offset
            entry = work[placed].entry
            if retry and retry.next == "fix_and_retake" and retry.fault and entry:
                entry = SimpleNamespace(screen={**entry.screen, "body": f"{REASON_REGISTRY[retry.fault].message} {PLACE_MICROPHONE}"})
            take_started: float | None = None
            spent = ledger.by_household + ledger.by_speaker
            try:
                if signals.stop.is_set():
                    raise CaptureStopped("capture stopped")
                # A take spends a retry only when it retries a pose that already admitted one (#5722).
                ledger.charge = retry.charge if retry is not None and ledger.admitted else "replay"
                if gate:
                    gate.publish(progress)
                await _grant(gate, placed + 1, placed + 1 + grant_epoch, entry, signals,
                             lambda: admit(placed + 1, attempt, entry, ledger))
                if gate:
                    if item.pose_index not in moved:
                        manifest.mic_moves += 1
                        moved.add(item.pose_index)
                if session is None:
                    assert door is not None and door.ceiling_db_spl is not None
                    assert hold is not None and level is not None
                    monitor, manifest.spl_monitor = spl_watch(
                        ceiling_db_spl=door.ceiling_db_spl, sensitivity=door.sensitivity, device=door.device)
                    opened = await window.enter_async_context(level_window(level, hold=hold, spl_monitor=monitor))
                    session = door.build_session(opened, manifest.allocate_take_id)
                    door.current = session
                    await window.enter_async_context(session)
                assert session is not None
                take_started = clock()
                progress["budget"] = ledger.to_payload()
                if gate:
                    gate.publish(progress)
                attempts[offset] = attempt
                manifest.begin(item.stop, attempt=attempt, pose_index=item.pose_index,
                               replay=_is_replay(attempt, ledger, spent))
                # The bank judges each take before it writes the record (ADR-0383).
                manifest.judge = partial(judge, item, spec)
                held: list[Callable[[], None]] = []
                token = playback_observer.set(partial(publish_sweeps, progress, gate, before) if gate else None)
                holding = _held_effects.set(held)
                try:
                    outcome = await measure(session, spec) if measure else await session.measure(spec)
                finally:
                    playback_observer.reset(token)
                    _held_effects.reset(holding)
                    manifest.judge = None
                if held:
                    await asyncio.to_thread(_release, held)
                manifest.detail = next((s.detail for s in outcome.stimuli if s.detail), "")
                verdict = None
                records = attempt_records()
                for ordinal, (record, record_id) in enumerate(records):
                    level_verdict = observe_level(item, record)
                    if record_id:
                        if (failure := failures.pop(str(record["take_id"]), None)) is not None:
                            if not isinstance(failure, _ASSESSMENT_FAILURES):
                                raise failure
                            manifest.detail = exception_detail(failure)
                        assessed = TakeVerdict(**record["verdict"])
                    else:
                        incident = next((s.incident for s in outcome.stimuli if s.incident), "")
                        assessed = TakeVerdict(False, fault=incident if incident in REASON_REGISTRY else REASON_INTERNAL_ERROR,
                                               next="stop", evidence={"incident": incident,
                                                                       **next((s.evidence for s in outcome.stimuli if s.evidence), {})})
                    if not outcome.complete:
                        incident = str(record.get("incident") or next((s.incident for s in outcome.stimuli if s.incident), ""))
                        assessed = replace(assessed, ok=False,
                                           fault=assessed.fault or (incident if incident in REASON_REGISTRY else REASON_INTERNAL_ERROR),
                                           next="stop" if assessed.next == "accept" else assessed.next,
                                           evidence={**assessed.evidence, "incident": incident})
                    await manifest.append(record, record_id, assessed, complete=outcome.complete,
                                          level_observation=level_verdict.evidence, ordinal=ordinal)
                    if verdict is None or (verdict.next != "stop" and assessed.next != "accept"):
                        verdict = assessed
                assert verdict is not None
                if gate:
                    gate.publish({**progress, "fault": verdict.fault, "next_action": verdict.next})
                if verdict.next == "stop":
                    manifest.reason = verdict.fault or "take_stopped"
                    manifest.failed_roles = channel_map_failed_roles(verdict.evidence)
                    break
                if signals.complete.is_set():
                    if offset + 1 < len(work):
                        manifest.reason = "complete_requested"
                    break
                if verdict.next != "accept":
                    retry = verdict
                    retry_was_measured = outcome.complete and any(record_id for _, record_id in records)
                    if offset == probe_at and verdict.next in _LEVEL_RETAKES and verdict.next_gain_db is not None:
                        # The run holds the fader its probe found and plays its placement from its
                        # first take (ADR-0403 §4).
                        assert caps is not None and door is not None and door.ceiling_db_spl is not None
                        assert item.pose_level is not None and probe_start is not None
                        probe = ExcitationProgram.from_dict(next(
                            record["program"] for record, _ in records if record.get("program")))
                        rise = door.margin_db + (probe_backoff_db(probe, caps) if other_graphs else 0.0)
                        bound = door.ceiling_db_spl
                        if item.stop["pose"]["kind"] == POSE_KIND_SEAT:
                            # The first seat spot reads at most 76 dB, 9 dB under the stop (ADR-0403 §4).
                            bound = min(bound, item.pose_level.target_db_spl + item.pose_level.tolerance_db)
                        solved = run_fader_db(probe, verdict.next_gain_db, caps, cut_db=max(0.0, _landed_db_spl(
                            verdict, item.pose_level, probe) + item.pose_level.tolerance_db + rise - bound))
                        level = hold_fader(solved, "probe")
                        manifest.level["run"]["probe_level_db"] = solved
                        await window.aclose()
                        session, playing[offset], probe_at = None, work[offset].spec, None
                        offset, retry, retry_was_measured = probe_start, None, False
                        continue
                    probes = branch_probes(item.spec)
                    if spec in probes and verdict.next in _LEVEL_RETAKES and verdict.next_gain_db is not None:
                        # A branch take probes each branch alone, then plays each alone at its own
                        # level and their sum under the lower one by what two in phase add (ADR-0407).
                        levels = branch_levels.setdefault(offset, [])
                        levels.append(verdict.next_gain_db)
                        if len(levels) < len(probes):
                            playing[offset] = probes[len(levels)]
                            retry = replace(verdict, next="retake_same", next_gain_db=None)
                        else:
                            set_probes[item.level_set] = tuple(levels)
                            playing[offset] = replace(item.spec, branch_levels_dbfs=tuple(levels))
                            relevelled.pop(offset, None)
                            retry = replace(verdict, next_gain_db=min(levels) - BRANCH_SUM_MARGIN_DB)
                    elif spec.branch_levels_dbfs and verdict.next in _LEVEL_RETAKES and verdict.next_gain_db is not None:
                        # Its own retake names the take's new peak (ADR-0407).
                        played = ExcitationProgram.from_dict(next(
                            record["program"] for record, _ in records if record.get("program")))
                        relevelled[offset] = _relevelled(spec, played, set_probes[item.level_set], verdict.next_gain_db)
                        retry = replace(verdict, next_gain_db=relevelled[offset].level_ladder_dbfs[0])
                    continue
                retry = None
                retry_was_measured = False
                if offset == probe_at:
                    # A probe is never kept (ADR-0365): the run found no fader, so nothing else plays.
                    manifest.reason = REASON_LEVEL_UNSOLVED
                    break
                if item.pose_level is not None:
                    # The rest of this take's set plays at the level it landed (ADR-0361, ADR-0403).
                    for index in range(offset + 1, len(work)):
                        if work[index].level_set == item.level_set:
                            playing[index] = replace(work[index].spec, level_ladder_dbfs=spec.level_ladder_dbfs,
                                                     branch_levels_dbfs=spec.branch_levels_dbfs)
                            unlevelled.discard(index)
                if signals.retake.is_set():
                    continue
                offset += 1
            except _Control:
                continue
            except aborting as exc:
                manifest.reason = (signals.stop_reason if isinstance(exc, CaptureStopped) or
                                   (isinstance(exc, asyncio.CancelledError) and signals.stop.is_set()) else
                                   str(exc.code) if isinstance(exc, _OWN_CODE) else
                                   next((code for cls, code in aborts.items() if isinstance(exc, cls)), "cancelled"))
                manifest.detail = exception_detail(exc)
                manifest.cancelled = isinstance(exc, asyncio.CancelledError)
                manifest.stopped_at = {"pose_index": item.pose_index, "index": item.stop["index"]}
                if attempts[offset] != attempt:
                    manifest.begin(item.stop, attempt=attempt, pose_index=item.pose_index,
                                   replay=_is_replay(attempt, ledger, spent))
                fault = manifest.reason if manifest.reason in REASON_REGISTRY else REASON_INTERNAL_ERROR
                already_banked = {take["record_id"] for take in manifest.takes}
                for record, record_id in attempt_records():
                    if record_id and record_id in already_banked:
                        continue
                    await manifest.append(record, record_id, TakeVerdict(False, fault=fault, next="stop",
                                          evidence={"incident": manifest.reason}), complete=False,
                                          level_observation=observe_level(item, record).evidence)
                break
            finally:
                failures.clear()
                if take_started is not None:
                    while len(manifest.wall_s) <= item.pose_index:
                        manifest.wall_s.append(0.0)
                    manifest.wall_s[item.pose_index] += clock() - take_started
    except (MeasurementDoorRefused, LateralWalkRefused) as exc:
        if not manifest.reason:
            manifest.reason, manifest.detail = exc.reason, exc.detail
    except BaseException as exc:  # noqa: BLE001 - finalize failure evidence, then propagate unchanged
        manifest.reason = manifest.reason or failure_reason(exc)
        manifest.detail = manifest.detail or exception_detail(exc)
        raise
    finally:
        try:
            try:
                await resilient_restore(stack.__aexit__(*sys.exc_info()))
            except MeasurementDoorRefused as exc:
                if not manifest.reason:
                    manifest.reason, manifest.detail = exc.reason, exc.detail
            except BaseException as exc:  # noqa: BLE001 - preserve cleanup failures after finalizing
                # A window that cancelled the run names why, as its page does.
                code = failure_reason(exc)
                manifest.reason = (code if isinstance(exc, MeasurementWindowError) and manifest.cancelled
                                   and code != REASON_INTERNAL_ERROR else manifest.reason or code)
                manifest.detail = manifest.detail or exception_detail(exc)
                raise
        finally:
            manifest.finalized = True
            document = manifest.to_dict()
            mismatches = driver_level_mismatches(manifest.joined())
            if gate:
                gate.abandon_hold()
                gate.publish({**progress, "status": manifest.status, "manifest": manifest.path,
                              "level": manifest.level, **take_counts(document), "not_measured": manifest.takes_skipped,
                              "level_mismatches": mismatches,
                              "fault": manifest.reason or (verdict.fault if verdict else None),
                              "next_action": "accept" if manifest.status == "complete" else "stop"})
            for finding in mismatches:
                log_event(logger, "active_speaker.driver_level_mismatch", level=logging.WARNING, fields={
                    "role": finding["role"], **finding["pose"], "spread_db": finding["spread_db"],
                    "unit_drive_db_spl": finding["unit_drive_db_spl"]})
            log_event(logger, "active_speaker.plan_run", status=manifest.status, reason=manifest.reason,
                      takes=manifest.takes_measured, skipped=manifest.takes_skipped, mic_moves=manifest.mic_moves)
            await manifest.persist()
    return manifest
