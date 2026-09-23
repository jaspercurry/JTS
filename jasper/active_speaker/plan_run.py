# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Execute planned captures with one retry owner and one evidence manifest (ADR-0296)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import asdict, dataclass, field, replace
from itertools import accumulate, groupby
from collections import Counter
from functools import partial
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from jasper.log_event import log_event
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS
from jasper.audio_measurement.program import ExcitationProgram, RoleBand, KIND_PILOT, KIND_SUMMED_SWEEP
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from jasper.audio_measurement.wired_capture import WiredSplMonitor

from .angle_capture import (
    BASE_CANDIDATE, REGIME_PER_DRIVER, REGIME_SUMMED,
    WALK_COMMISSIONING_STOP_UNSET, WALK_NOTHING_PLAYABLE,
    WALK_SPL_CALIBRATION_REQUIRED, WALK_STIMULUS_NOT_ACCEPTED, WALK_LEVEL_POLICY_INVALID,
    AngleCaptureRequest, AngleStop, ResolvedStop, LateralWalkRefused,
    candidate_identity, design_axis_spec, resolve_request, stop_specs,
)
from .commission_wiring import commissioning_spl_ceiling_db
from .crossover_v2.admission import (
    MAX_EXTRA_ATTEMPTS_PER_POSITION, SlotAttempts,
)
from .crossover_v2.capture_dispatch import assess, level_drift_verdict
from .crossover_v2.capture_plan import pose_batch_screens, position_geometry, position_screen_keys
from .crossover_v2.capture_source import CaptureBeginDeferred, CaptureBeginRefused, CaptureStopped
from .crossover_v2.door import IsolationHold, OpenMeasurementDoor, MeasurementDoorRefused, level_window
from .crossover_v2.journey import PHASE_CHECK, PHASE_ENTRY_BASELINE, PHASE_LATERAL, PHASE_MEASURE
from .crossover_v2.measure_spec import MeasureSpec
from .crossover_v2.position_gate import POSITION_HOLD_POLL_S, PositionGate
from .crossover_v2.program_transaction import StimulusCaptureStopped, playback_observer
from .crossover_v2.refusal_copy import (
    CAPTURE_QUALITY_REFUSAL_CODES, REASON_INTERNAL_ERROR, REASON_REGISTRY, REASON_USER_STOPPED, TakeVerdict, exception_detail,
)
from .crossover_v2.session import TuningSession
from .crossover_v2.spatial import analysis_curve_records
from .crossover_v2.planning import analysis_json
from .program_failure import classify_program_failure
from .restore_wait import resilient_restore
from .measurement_programs import BRANCH_PAIR_DRIVERS, POSE_KIND_BEARING, PURPOSE_SPEAKER
from .crossover_v2.programs import predictive_program_for_spec
from .run_manifest import RunManifest
from .round_copy import PLACE_MICROPHONE, take_counts

from jasper.audio_measurement.calibration import resolve_mic_sensitivity
from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from .seat_level_reference import AnchorFacts, LevelUnresolved, ResolvedLevel, load_seat_level_reference, resolve_anchor_level

logger = logging.getLogger(__name__)
_OWN_CODE = (CaptureBeginRefused, StimulusCaptureStopped)
Analyze = Callable[[Mapping[str, Any], str], ProgramAnalysis]


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


def spl_watch(
    *,
    topology: Any,
    preset: Any,
    sensitivity: Any | None,
    device: Any,
    resolved_ceiling_db_spl: float | None = None,
) -> tuple[WiredSplMonitor, str]:
    """Require a calibrated watch at the commissioning stop."""
    ceiling = resolved_ceiling_db_spl
    if ceiling is None:
        try:
            stop = commissioning_spl_ceiling_db(topology, preset=preset)
        except ValueError as exc:
            raise LateralWalkRefused(WALK_COMMISSIONING_STOP_UNSET, str(exc)) from exc
        ceiling = stop
    if sensitivity is None:
        raise LateralWalkRefused(
            WALK_SPL_CALIBRATION_REQUIRED,
            "an SPL ceiling requires a resolvable microphone sensitivity",
        )
    channel = int(SUPPORTED_MODELS[device.model_key].get("capture_channel", 0))
    return WiredSplMonitor(sensitivity, ceiling, channel), spl_monitor_note(ceiling)


def measurement_spl_watch(
    *, topology: Any, preset: Any, device: Any,
    mic_serial: str | None = None,
) -> tuple[WiredSplMonitor, str, ResolvedLevel]:
    sensitivity = (resolve_mic_sensitivity(mic_serial=mic_serial) if mic_serial
                   else resolved_household_sensitivity(device))
    monitor, note = spl_watch(topology=topology, preset=preset, sensitivity=sensitivity, device=device)
    try:
        level, _rebase = resolve_anchor_level(facts=AnchorFacts(load_seat_level_reference() or {}, sensitivity))
    except LevelUnresolved as exc:
        raise LateralWalkRefused(exc.reason, exc.detail) from exc
    return monitor, note, level


def request_fingerprint(request: AngleCaptureRequest) -> str:
    """This walk's identity: sha256 over ``AngleCaptureRequest.to_dict()``.

    The inline plan and manifest use the same document without a clock, so
    two runs of one walk fingerprint alike, and an edited stop does not.
    """
    return json_fingerprint(request.to_dict())


@dataclass(frozen=True)
class PlanCapture:
    stop: AngleStop
    spec: MeasureSpec
    repeat: int = 1

    def resolved(self, request: AngleCaptureRequest) -> ResolvedStop:
        return resolve_request(replace(request, stops=(self.stop,),
            candidates=(self.stop.candidate_id or BASE_CANDIDATE,), repeats=1))[0]


def prepare_plan_captures(
    request: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = (),
) -> tuple[PlanCapture, ...]:
    """Derive preparation and requested captures together (ADR-0297)."""
    resolved = resolve_request(request)
    placed = stop_specs(request,
                        prompts=tuple(stop.prompt for stop in resolved), baseline_id=BASE_CANDIDATE,
                        roles_bands=roles_bands)
    captures: list[PlanCapture] = []
    if any(stop.regime == REGIME_PER_DRIVER for stop in request.stops):
        captures.append(PlanCapture(
            AngleStop(0, REGIME_PER_DRIVER),
            replace(design_axis_spec(request), program_phase=PHASE_CHECK),
        ))
    base_stop = next((stop for stop in request.stops if candidate_identity(stop.candidate_id) == BASE_CANDIDATE and stop.purpose == PURPOSE_SPEAKER), None)
    # The speaker flow needs an entry baseline; other rounds use their first take as the level reference.
    if base_stop is not None:
        base_request = replace(request, stops=(replace(base_stop, angle_deg=0, elevation_deg=0,
            kind=POSE_KIND_BEARING, distance_m=None, seat_offset_m=None,
            headline="", detail="", regime=REGIME_SUMMED, branch_pair=BRANCH_PAIR_DRIVERS),),
                               candidates=(), repeats=1)
        base_spec, = stop_specs(base_request,
                                prompts=(resolve_request(base_request)[0].prompt,), baseline_id=BASE_CANDIDATE,
                                roles_bands=roles_bands)
        assert base_spec is not None
        captures.extend(PlanCapture(base_request.stops[0], replace(
            base_spec, graph_scope="timing", program_phase=PHASE_ENTRY_BASELINE,
        ), repeat) for repeat in range(1, request.repeats + 1))
    for offset, spec in enumerate(placed):
        stop = request.stops[offset // request.repeats]
        if spec is None:
            spec = replace(design_axis_spec(request), positions=(stop.angle_deg,),
                           vertical_deg=stop.elevation_deg,
                           pose_prompts=(resolved[offset // request.repeats].prompt.text,))
        captures.append(PlanCapture(stop, replace(spec, program_phase=(
            PHASE_MEASURE if stop.regime == REGIME_PER_DRIVER else PHASE_LATERAL
        )), offset % request.repeats + 1))
    return tuple(captures)


@dataclass
class RunDoor:
    hold: AbstractAsyncContextManager[IsolationHold]
    build_session: Callable[[OpenMeasurementDoor, Callable[[], str]], TuningSession]
    sensitivity: Any
    device: Any
    ceiling_db_spl: float | None
    current: TuningSession | None = None
    opened: OpenMeasurementDoor | None = None
    program_for_spec: Callable[[MeasureSpec], ExcitationProgram] | None = None

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


def _pose(stop: Any) -> dict[str, Any]:
    return {"kind": stop.kind, "deg": stop.angle_deg, "elevation_deg": stop.elevation_deg,
            "distance_m": stop.distance_m, "place": stop.place, "seat_offset_m": stop.seat_offset_m}


def _planned_row(index: int, repeat: int, stop: Any) -> dict[str, Any]:
    return {"index": index, "repeat": repeat, "pose": _pose(stop),
            "candidate_id": stop.candidate_id, "purpose": stop.purpose}


# Allow a person to move the stand and confirm placement between pose batches.
HUMAN_MOVE_ALLOWANCE_S = 30


def schedule_facts(captures: Sequence[tuple[Mapping[str, Any], MeasureSpec]], program_for_spec: Callable[[MeasureSpec], ExcitationProgram],
                   *, mover: str, program: str = "") -> dict[str, Any]:
    poses, pose_sweeps, work_sweeps, measurements_per_pose = [], [], [], []
    for _, batch in groupby(captures, key=lambda capture: capture[0]["place"]):
        details: list[dict[str, Any]] = []
        keys = []
        for measurement, (pose, spec) in enumerate(batch, 1):
            if not details:
                poses.append(dict(pose))
            excitation = program_for_spec(spec)
            segments = excitation.stimulus_segments()
            work_sweeps.append(len(segments))
            for segment in segments:
                keys.append((spec.graph_scope, spec.candidate_id, spec.program_phase, segment.role, segment.kind))
                details.append({"role": segment.role or "summed", "kind": segment.kind, "phase": spec.program_phase,
                                "scope": spec.graph_scope, "seconds": segment.n_samples / excitation.sample_rate_hz})
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
            "estimated_seconds": sum(row["seconds"] for row in rows) +
                                 (len(poses) * HUMAN_MOVE_ALLOWANCE_S if mover == "human" else 0),
            "pose_sweeps": pose_sweeps}


def preview_schedule(request: AngleCaptureRequest, captures: Sequence[PlanCapture], context: Any) -> dict[str, Any]:
    return schedule_facts([(_pose(c.stop), c.spec) for c in captures], predictive_program_for_spec(context),
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
    captures: Sequence[PlanCapture] | None = None,
    admit: Callable[[int, int, Any, SlotAttempts], None] | None = None,
    assessor: Callable[..., TakeVerdict] | None = None,
    measure: Callable[[TuningSession, MeasureSpec], Awaitable[Any]] | None = None,
) -> RunManifest:
    from .candidate_parts import baseline_candidate_id  # lazy: baseline composition loads DSP analysis

    manifest.request_fingerprint = request_fingerprint(request)
    manifest.program = request.program
    manifest.spl_monitor = spl_monitor
    manifest.asked = {
        "poses": list({stop.place: _pose(stop) for stop in request.stops}.values()),
        "candidates": list(request.candidates or ("base",)),
        "mover": request.mover, "level": asdict(request.level), "repeats": request.repeats,
        "retries_per_pose": request.retries_per_pose,
    }
    manifest.planned = [_planned_row(index * request.repeats + repeat, repeat, stop)
                        for index, stop in enumerate(request.stops) for repeat in range(1, request.repeats + 1)]
    try:
        resolved = resolve_request(request)
        try:
            needs_base = (any(stop.plays_summed and not stop.candidate_id for stop in request.stops)
                          if captures is None else any(capture.spec.candidate_id == BASE_CANDIDATE for capture in captures))
            baseline_id = baseline_candidate_id() if needs_base else ""
            if captures is None:
                specs = stop_specs(request, prompts=tuple(stop.prompt for stop in resolved), baseline_id=baseline_id)
            else:
                specs = tuple(replace(capture.spec, candidate_id=baseline_id)
                              if capture.spec.candidate_id == BASE_CANDIDATE else capture.spec for capture in captures)
        except ValueError as exc:
            raise LateralWalkRefused(getattr(exc, "code", WALK_STIMULUS_NOT_ACCEPTED), str(exc)) from exc
        playable = [(offset, spec) for offset, spec in enumerate(specs) if spec is not None]
        for offset, spec in enumerate(specs):
            if spec is None:
                manifest.planned[offset]["reason"] = WALK_NOTHING_PLAYABLE
        if not playable and captures is None:
            raise LateralWalkRefused(WALK_NOTHING_PLAYABLE, "No composed per-driver spec was supplied")
    except LateralWalkRefused as exc:
        manifest.reason, manifest.detail, manifest.finalized = exc.reason, exc.detail, True
        await manifest.persist()
        return manifest

    if captures is not None:
        stops = [capture.resolved(request) for capture in captures]
        manifest.planned = [_planned_row(index, capture.repeat, capture.stop)
                            for index, capture in enumerate(captures, 1)]
        places = [capture.stop.place for capture in captures]
    else:
        stops = [resolved[offset // request.repeats] for offset in range(len(specs))]
        places = [request.stops[offset // request.repeats].place for offset in range(len(specs))]
    anchor = request.level.resolved
    level = request.level.volume_db
    if level is None and session is not None:
        level = session.measurement_level_db
    if level is None or (door is None and (session is None or level != session.measurement_level_db)):
        raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "The plan needs a resolved session level")
    manifest.level = {
        **({"session": anchor.session()} if anchor is not None else {}),
        "run": {"level_db": level,
                "offset_db": request.level.offset_db if anchor is not None else None,
                "level_source": request.level_source},
    }
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
                expanded.append((spec, stop, pose_index, stops[offset]))
    manifest.planned = planned
    screens = pose_batch_screens(list(range(1, len(expanded) + 1)),
                                 [row[3].prompt for row in expanded], [row[3].candidate_id for row in expanded])
    work: list[_Work] = []
    for _pose_index, expanded_batch in groupby(enumerate(expanded), key=lambda row: row[1][2]):
        expanded_rows = list(expanded_batch)
        for config, (index, (played_spec, stop, pose_index, resolved_stop)) in enumerate(expanded_rows, 1):
            entry = SimpleNamespace(screen={**resolved_stop.screen,
                                    "title": resolved_stop.prompt.headline, "body": resolved_stop.prompt.detail,
                                    **position_screen_keys(resolved_stop.prompt), **screens.get(index + 1, {})})
            work.append(_Work(played_spec, stop, pose_index, config, len(expanded_rows), entry))
    return await _run(work, session=session, door=door, level=level,
                      manifest=manifest, analyze=analyze, gate=gate,
                      aborts=aborts, signals=signals or RunSignals(), retries=request.retries_per_pose,
                      clock=clock, gain_ceiling_db=gain_ceiling_db, admit=admit, assessor=assessor, measure=measure)


async def run_specs(
    specs: Sequence[MeasureSpec], *, session: TuningSession, manifest: RunManifest,
    analyze: Analyze, aborts: Mapping[type[BaseException], str],
    signals: RunSignals | None = None, spl_monitor: str = "",
    clock: Callable[[], float] = time.monotonic,
    gain_ceiling_db: Mapping[str, float] | None = None,
    assessor: Callable[..., TakeVerdict] | None = None,
) -> RunManifest:
    manifest.request_fingerprint = json_fingerprint({"specs": [s.to_dict() for s in specs]})
    manifest.spl_monitor = spl_monitor
    stop = SimpleNamespace(kind="bearing", angle_deg=(specs[0].positions or (0,))[0],
                           elevation_deg=specs[0].vertical_deg, distance_m=None,
                           place=None, seat_offset_m=None, candidate_id="", purpose=PURPOSE_SPEAKER)
    pose = _pose(stop)
    manifest.asked = {"poses": [pose], "candidates": [s.candidate_id or "base" for s in specs],
                      "mover": "fixed",
                      "level": {"reference_volume_db": session.measurement_level_db}, "repeats": 1}
    manifest.planned = [_planned_row(i, 1, SimpleNamespace(**{**vars(stop), "candidate_id": spec.candidate_id}))
                        for i, spec in enumerate(specs, 1)]
    work = [_Work(spec, stop, 0, i, len(specs), None)
            for i, (spec, stop) in enumerate(zip(specs, manifest.planned), 1)]
    return await _run(work, session=session, manifest=manifest, analyze=analyze, gate=None,
                      aborts=aborts, signals=signals or RunSignals(), retries=MAX_EXTRA_ATTEMPTS_PER_POSITION,
                      clock=clock, gain_ceiling_db=gain_ceiling_db, assessor=assessor)


class _Control(Exception):
    pass


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
        if ledger.charge != "none":
            ledger.spend(ledger.charge)
        ledger.admitted += 1

    def failure_reason(exc: BaseException) -> str:
        classified = classify_program_failure(exc)
        code = classified[0] if classified else getattr(exc, "code", None)
        return code if isinstance(code, str) and code in REASON_REGISTRY else REASON_INTERNAL_ERROR

    def attempt_records() -> list[tuple[Mapping[str, Any], str]]:
        return manifest.pending_records or [({"take_id": manifest.allocate_take_id()}, "")]

    level_observations: dict[str, TakeVerdict] = {}

    def observe_level(record: Mapping[str, Any]) -> TakeVerdict:
        take_id = str(record["take_id"])
        if take_id not in level_observations:
            level_observations[take_id] = level_drift_verdict(**manifest.level_observation(record))
        return level_observations[take_id]

    admit = admit or default_admit
    manifest.specs = {item.stop["index"]: item.spec for item in work}
    aborting: tuple[type[BaseException], ...] = (*_OWN_CODE, *aborts, CaptureStopped, asyncio.CancelledError)
    started = clock()
    ledgers = {item.pose_index: SlotAttempts(retries_per_pose=retries) for item in work}
    attempts = [0] * len(work)
    offset, grant_epoch = 0, 0
    retry: TakeVerdict | None = None
    retry_was_measured = False
    playing = [item.spec for item in work]
    moved: set[int] = set()
    verdict: TakeVerdict | None = None
    schedule: dict[str, Any] = schedule_facts([(item.stop["pose"], item.spec) for item in work], door.program_for_spec,
                              mover=manifest.asked["mover"], program=manifest.program or "") if door and door.program_for_spec else {"poses": len(ledgers)}
    sweep_offsets = list(accumulate(schedule.get("work_sweeps", [0] * len(work)), initial=0))
    progress: dict[str, Any] = {}
    stack = AsyncExitStack()
    hold: IsolationHold | None = None
    try:
        await manifest.persist()
        if door is not None:
            if door.ceiling_db_spl is None:
                raise LateralWalkRefused(WALK_COMMISSIONING_STOP_UNSET, "Preflight supplied no SPL ceiling")
            hold = await stack.enter_async_context(door.hold)
        while offset < len(work):
            if signals.complete.is_set():
                manifest.reason = "complete_requested"
                break
            if signals.retake.is_set():
                signals.retake.clear()
                pose = work[offset].pose_index
                offset = next(i for i, row in enumerate(work) if row.pose_index == pose)
                retry = TakeVerdict(True, next="fix_and_retake", charge="operator")
                retry_was_measured = False
            item = work[offset]
            ledger = ledgers[item.pose_index]
            if retry is not None:
                if not ledger.can_retry(retry.charge):
                    if (retry_was_measured and retry.fault in CAPTURE_QUALITY_REFUSAL_CODES
                            and item.spec.program_phase != PHASE_CHECK):
                        manifest.mark_not_measured(item.stop["index"], retry.fault)
                        retry = None
                        retry_was_measured = False
                        offset += 1
                        continue
                    manifest.reason = retry.fault or "retries_spent"
                    break
                if retry.next == "fix_and_retake":
                    if gate is None:
                        manifest.reason = retry.fault or "placement_required"
                        break
                    grant_epoch += 1
                    if gate:
                        gate.abandon_hold()
                if retry.next in {"retake_louder", "retake_quieter"}:
                    if retry.next_gain_db is None:
                        manifest.reason = retry.fault or "retry_gain_missing"
                        break
                    playing[offset] = replace(playing[offset], level_ladder_dbfs=(retry.next_gain_db,))
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
            progress = {**schedule, **notices, "pose": item.pose_index + 1,
                        "level": manifest.level, "config": item.config, "configs": item.size, "attempt": attempt,
                        "fault": retry.fault if retry else None, "next_action": retry.next if retry else None,
                        "budget": ledger.to_payload(), "sweep": before + 1, "measurement": offset + 1}
            entry = item.entry
            if retry and retry.next == "fix_and_retake" and retry.fault and entry:
                entry = SimpleNamespace(screen={**entry.screen, "body": f"{REASON_REGISTRY[retry.fault].message} {PLACE_MICROPHONE}"})
            take_started: float | None = None
            try:
                if signals.stop.is_set():
                    raise CaptureStopped("capture stopped")
                ledger.charge = retry.charge if retry is not None else "none"
                if gate:
                    gate.publish(progress)
                await _grant(gate, offset + 1, offset + 1 + grant_epoch, entry, signals,
                             lambda: admit(offset + 1, attempt, entry, ledger))
                if gate:
                    if item.pose_index not in moved:
                        manifest.mic_moves += 1
                        moved.add(item.pose_index)
                if session is None:
                    assert door is not None and hold is not None and level is not None
                    monitor, manifest.spl_monitor = spl_watch(
                        topology=None, preset=None, sensitivity=door.sensitivity, device=door.device,
                        resolved_ceiling_db_spl=door.ceiling_db_spl,
                    )
                    door.opened = await stack.enter_async_context(level_window(level, hold=hold, spl_monitor=monitor))
                    session = door.build_session(door.opened, manifest.allocate_take_id)
                    door.current = session
                    await stack.enter_async_context(session)
                assert session is not None
                take_started = clock()
                if retry is not None:
                    progress["budget"] = ledger.to_payload()
                    if gate:
                        gate.publish(progress)
                attempts[offset] = attempt
                manifest.begin(item.stop, attempt=attempt, pose_index=item.pose_index)
                token = playback_observer.set(partial(publish_sweeps, progress, gate, before) if gate else None)
                try:
                    outcome = await measure(session, spec) if measure else await session.measure(spec)
                finally:
                    playback_observer.reset(token)
                manifest.detail = next((s.detail for s in outcome.stimuli if s.detail), "")
                manifest.outcomes.append((outcome, str(session.graph_fingerprint)))
                verdict = None
                records = attempt_records()
                for ordinal, (record, record_id) in enumerate(records):
                    level_verdict = observe_level(record)
                    if record_id:
                        try:
                            analysis = await asyncio.to_thread(analyze, record, record_id)
                            program = ExcitationProgram.from_dict(record["program"]) if record.get("program") else None
                            assessed = await asyncio.to_thread(assessor or assess, analysis, phase=program.phase if program else spec.program_phase or "verify",
                                              spl=(record.get("capture_integrity") or {}).get("spl"),
                                              program=program, gain_ceiling_db=gain_ceiling_db, level_verdict=level_verdict)
                            if program is not None:
                                record = {**record, "curves": analysis_curve_records(analysis, program),
                                          "analysis": analysis_json(analysis)}
                        except (ValueError, KeyError, OSError) as exc:
                            manifest.detail = exception_detail(exc)
                            assessed = TakeVerdict(False, fault=REASON_INTERNAL_ERROR, next="stop",
                                                   evidence={"error_type": type(exc).__name__})
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
                                          started_s=take_started - started, ended_s=clock() - started,
                                          level_observation=level_verdict.evidence, ordinal=ordinal)
                    if verdict is None or (verdict.next != "stop" and assessed.next != "accept"):
                        verdict = assessed
                assert verdict is not None
                if gate:
                    gate.publish({**progress, "fault": verdict.fault, "next_action": verdict.next})
                if verdict.next == "stop":
                    manifest.reason = verdict.fault or "take_stopped"
                    break
                if signals.complete.is_set():
                    if offset + 1 < len(work):
                        manifest.reason = "complete_requested"
                    break
                if verdict.next != "accept":
                    retry = verdict
                    retry_was_measured = outcome.complete and any(record_id for _, record_id in records)
                    continue
                retry = None
                retry_was_measured = False
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
                    manifest.begin(item.stop, attempt=attempt, pose_index=item.pose_index)
                fault = manifest.reason if manifest.reason in REASON_REGISTRY else REASON_INTERNAL_ERROR
                already_banked = {take["artifacts"]["record_id"] for take in manifest.takes}
                ended = clock()
                for record, record_id in attempt_records():
                    if record_id and record_id in already_banked:
                        continue
                    await manifest.append(record, record_id, TakeVerdict(False, fault=fault, next="stop",
                                          evidence={"incident": manifest.reason}), complete=False,
                                          started_s=(take_started if take_started is not None else ended) - started,
                                          ended_s=ended - started, level_observation=observe_level(record).evidence)
                break
            finally:
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
                manifest.reason = manifest.reason or failure_reason(exc)
                manifest.detail = manifest.detail or exception_detail(exc)
                raise
        finally:
            manifest.finalized = True
            if gate:
                gate.abandon_hold()
                gate.publish({**progress, "status": manifest.status, "manifest": manifest.path,
                              "level": manifest.level, **take_counts(manifest.to_dict()), "not_measured": manifest.takes_skipped,
                              "fault": manifest.reason or (verdict.fault if verdict else None),
                              "next_action": "accept" if manifest.status == "complete" else "stop"})
            log_event(logger, "active_speaker.plan_run", status=manifest.status, reason=manifest.reason,
                      takes=manifest.takes_measured, skipped=manifest.takes_skipped, mic_moves=manifest.mic_moves)
            await manifest.persist()
    return manifest
