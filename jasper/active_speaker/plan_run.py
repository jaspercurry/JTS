# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""One plan walk and retry owner, with evidence kept by RunManifest."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from itertools import groupby
from threading import Event
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from jasper.log_event import log_event
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from jasper.audio_measurement.wired_capture import WiredSplMonitor

from .angle_capture import (
    BASE_CANDIDATE,
    WALK_CEILING_ABOVE_STOP, WALK_COMMISSIONING_STOP_UNSET, WALK_NOTHING_PLAYABLE,
    WALK_SPL_CALIBRATION_REQUIRED, WALK_STIMULUS_NOT_ACCEPTED,
    AngleCaptureRequest, LateralWalkRefused, resolve_request, stop_specs,
)
from .angle_capture_spool import angle_request_document
from . import candidate_bank
from .commission_wiring import commissioning_spl_ceiling_db
from .crossover_v2.admission import (
    MAX_EXTRA_ATTEMPTS_PER_POSITION, SlotAttempts,
)
from .crossover_v2.capture_dispatch import assess
from .crossover_v2.capture_plan import PlanCapture, pose_batch_screens, position_screen_keys
from .crossover_v2.capture_source import CaptureBeginDeferred, CaptureBeginRefused, CaptureStopped
from .crossover_v2.measure_spec import MeasureSpec
from .crossover_v2.position_gate import POSITION_HOLD_POLL_S, PositionGate
from .crossover_v2.program_transaction import StimulusCaptureStopped
from .crossover_v2.refusal_copy import REASON_INTERNAL_ERROR, REASON_REGISTRY, TakeVerdict
from .crossover_v2.session import TuningSession
from .crossover_v2.spatial import analysis_curve_records
from .run_manifest import RunManifest

logger = logging.getLogger(__name__)
SPL_MONITOR_UNAVAILABLE = "unavailable_no_calibration"
_OWN_CODE = (CaptureBeginRefused, StimulusCaptureStopped)
Analyze = Callable[[Mapping[str, Any], str], ProgramAnalysis]


@dataclass
class RunSignals:
    """Thread-safe host inputs, consumed only by the executor."""

    retake: Event = field(default_factory=Event)
    complete: Event = field(default_factory=Event)
    stop: Event = field(default_factory=Event)


def take_spl_ceiling(
    stated_db_spl: float | None, *, commissioning_stop_db_spl: float,
) -> float:
    """The ceiling a run's monitor watches, in dB SPL at the microphone.

    A run that states none plays under the box's own commissioning stop, which
    is where the bound belongs: an operator asking for no ceiling is not asking
    to be unbounded. A run stating one ABOVE the stop is refused rather than
    silently clamped -- the number was typed, and clamping it would let an
    operator believe a louder measurement had been allowed.
    """
    stop = float(commissioning_stop_db_spl)
    if stated_db_spl is None:
        return stop
    stated = float(stated_db_spl)
    if stated > stop:
        raise LateralWalkRefused(
            WALK_CEILING_ABOVE_STOP,
            f"the run states an SPL ceiling of {stated:g} dB SPL, above this "
            f"box's commissioning stop of {stop:g} dB SPL",
        )
    return stated


def spl_monitor_note(ceiling_db_spl: float | None) -> str:
    """One run's SPL disclosure: the ceiling a monitor watched, or why none did."""
    if ceiling_db_spl is None:
        return SPL_MONITOR_UNAVAILABLE
    return f"ceiling_{float(ceiling_db_spl):g}_db_spl"


def spl_watch(
    stated_db_spl: float | None,
    *,
    topology: Any,
    preset: Any,
    sensitivity: Any | None,
    device: Any,
    resolved_ceiling_db_spl: float | None = None,
) -> tuple[WiredSplMonitor | None, str]:
    """Build the monitor from a preflight bound, or resolve it for a local caller.

    Without calibration, an unstated ceiling is disclosed as unmonitored;
    a stated ceiling must be enforceable. Preflight callers carry their proof.
    """
    ceiling = resolved_ceiling_db_spl
    if ceiling is None:
        try:
            stop = commissioning_spl_ceiling_db(topology, preset=preset)
        except ValueError as exc:
            raise LateralWalkRefused(WALK_COMMISSIONING_STOP_UNSET, str(exc)) from exc
        ceiling = take_spl_ceiling(stated_db_spl, commissioning_stop_db_spl=stop)
    if sensitivity is None:
        if stated_db_spl is None:
            return None, SPL_MONITOR_UNAVAILABLE
        raise LateralWalkRefused(
            WALK_SPL_CALIBRATION_REQUIRED,
            "an SPL ceiling requires a resolvable microphone sensitivity",
        )
    channel = int(SUPPORTED_MODELS[device.model_key].get("capture_channel", 0))
    return WiredSplMonitor(sensitivity, ceiling, channel), spl_monitor_note(ceiling)


def request_fingerprint(request: AngleCaptureRequest) -> str:
    """This walk's identity: sha256 over the document the spool banks it as.

    Asked of :func:`~.angle_capture_spool.angle_request_document` so a run's
    manifest names the same shape a staged walk has on disk, minus the clock --
    two runs of one walk fingerprint alike, and an edited stop does not.
    """
    return json_fingerprint(angle_request_document(request))


def resolve_candidate_scopes(candidate_ids: Iterable[str]) -> dict[str, str]:
    """Verify named candidates at run open; every trial uses its composed graph."""
    try:
        scopes = {}
        for candidate_id in sorted(set(candidate_ids) - {""}):
            candidate_bank.find_banked_candidate(candidate_id)
            scopes[candidate_id] = "candidate"
        return scopes
    except candidate_bank.CandidateBankRefusal as exc:
        raise LateralWalkRefused(exc.code, exc.detail) from exc


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


async def run_plan(
    request: AngleCaptureRequest, *, session: TuningSession, manifest: RunManifest,
    analyze: Analyze, gate: PositionGate | None = None,
    candidate_scopes: Mapping[str, str], aborts: Mapping[type[BaseException], str],
    signals: RunSignals | None = None, spl_monitor: str = SPL_MONITOR_UNAVAILABLE,
    clock: Callable[[], float] = time.monotonic,
    gain_ceiling_db: Mapping[str, float] | None = None,
    captures: Sequence[PlanCapture] | None = None,
    admit: Callable[[int, int, Any, SlotAttempts], None] | None = None,
    assessor: Callable[..., TakeVerdict] | None = None,
    measure: Callable[[MeasureSpec], Awaitable[Any]] | None = None,
) -> RunManifest:
    from .candidate_parts import baseline_candidate_ids  # lazy: baseline composition loads DSP analysis

    manifest.request_fingerprint = request_fingerprint(request)
    manifest.program = request.program
    manifest.spl_monitor = spl_monitor
    manifest.baseline_graph = request.baseline_graph_scope
    manifest.asked = {
        "poses": list({stop.place: _pose(stop) for stop in request.stops}.values()),
        "candidates": list(request.candidates or ("base",)), "ceiling": request.spl_ceiling_db_spl,
        "mover": request.mover, "level": asdict(request.level), "repeats": request.repeats,
        "retries_per_pose": request.retries_per_pose,
    }
    manifest.planned = [{"index": index * request.repeats + repeat, "repeat": repeat,
                         "pose": _pose(stop), "candidate_id": stop.candidate_id}
                        for index, stop in enumerate(request.stops) for repeat in range(1, request.repeats + 1)]
    try:
        resolved = resolve_request(request)
        try:
            if captures is None:
                specs = stop_specs(request, candidate_scopes=candidate_scopes,
                                   prompts=tuple(stop.prompt for stop in resolved),
                                   baseline_ids=baseline_candidate_ids(stop.purpose for stop in request.stops
                                                                       if stop.plays_summed and not stop.candidate_id))
            else:
                baselines = baseline_candidate_ids(capture.stop.purpose for capture in captures
                                                   if capture.spec.candidate_id == BASE_CANDIDATE)
                specs = tuple(replace(capture.spec, candidate_id=baselines[capture.stop.purpose or "speaker"])
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
        manifest.planned = [{"index": index, "repeat": capture.repeat,
                             "pose": _pose(capture.stop), "candidate_id": capture.stop.candidate_id}
                            for index, capture in enumerate(captures, 1)]
        places = [capture.stop.place for capture in captures]
    else:
        stops = [resolved[offset // request.repeats] for offset, _spec in playable]
        places = [request.stops[offset // request.repeats].place for offset, _spec in playable]
    screens = pose_batch_screens(list(range(1, len(stops) + 1)),
                                 [stop.prompt for stop in stops], [stop.candidate_id for stop in stops])
    work: list[_Work] = []
    for pose_index, (_place, batch) in enumerate(groupby(enumerate(playable), key=lambda row: places[row[0]])):
        rows = list(batch)
        for config, (index, (offset, spec)) in enumerate(rows, 1):
            entry = SimpleNamespace(screen={**stops[index].screen,
                                    "title": stops[index].prompt.headline, "body": stops[index].prompt.detail,
                                    **position_screen_keys(stops[index].prompt), **screens.get(index + 1, {})})
            work.append(_Work(spec, manifest.planned[offset], pose_index, config, len(rows), entry))
    return await _run(work, session=session, manifest=manifest, analyze=analyze, gate=gate,
                      aborts=aborts, signals=signals or RunSignals(), retries=request.retries_per_pose,
                      clock=clock, gain_ceiling_db=gain_ceiling_db, admit=admit, assessor=assessor, measure=measure)


async def run_specs(
    specs: Sequence[MeasureSpec], *, session: TuningSession, manifest: RunManifest,
    analyze: Analyze, aborts: Mapping[type[BaseException], str],
    signals: RunSignals | None = None, spl_monitor: str = SPL_MONITOR_UNAVAILABLE,
    clock: Callable[[], float] = time.monotonic,
    gain_ceiling_db: Mapping[str, float] | None = None,
) -> RunManifest:
    manifest.request_fingerprint = json_fingerprint({"specs": [s.to_dict() for s in specs]})
    manifest.spl_monitor = spl_monitor
    pose = _pose(SimpleNamespace(kind="bearing", angle_deg=(specs[0].positions or (0,))[0],
                                elevation_deg=specs[0].vertical_deg, distance_m=None,
                                place=None, seat_offset_m=None))
    manifest.asked = {"poses": [pose], "candidates": [s.candidate_id or "base" for s in specs],
                      "ceiling": specs[0].spl_ceiling_db_spl, "mover": "fixed",
                      "level": {"reference_volume_db": session.measurement_level_db}, "repeats": 1}
    manifest.planned = [{"index": i, "repeat": 1, "pose": pose, "candidate_id": spec.candidate_id}
                        for i, spec in enumerate(specs, 1)]
    work = [_Work(spec, stop, 0, i, len(specs), None)
            for i, (spec, stop) in enumerate(zip(specs, manifest.planned), 1)]
    return await _run(work, session=session, manifest=manifest, analyze=analyze, gate=None,
                      aborts=aborts, signals=signals or RunSignals(), retries=MAX_EXTRA_ATTEMPTS_PER_POSITION, clock=clock, gain_ceiling_db=gain_ceiling_db)


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
    work: Sequence[_Work], *, session: TuningSession, manifest: RunManifest, analyze: Analyze,
    gate: PositionGate | None, aborts: Mapping[type[BaseException], str], signals: RunSignals,
    retries: int, clock: Callable[[], float], gain_ceiling_db: Mapping[str, float] | None,
    admit: Callable[[int, int, Any, SlotAttempts], None] | None = None,
    assessor: Callable[..., TakeVerdict] | None = None,
    measure: Callable[[MeasureSpec], Awaitable[Any]] | None = None,
) -> RunManifest:
    manifest.specs = {item.stop["index"]: item.spec for item in work}
    aborting: tuple[type[BaseException], ...] = (*_OWN_CODE, *aborts, asyncio.CancelledError)
    started = clock()
    ledgers = {item.pose_index: SlotAttempts(retries_per_pose=retries) for item in work}
    attempts = [0] * len(work)
    offset, grant_epoch = 0, 0
    previous: int | None = None
    resume: int | None = None
    retry: TakeVerdict | None = None
    playing = [item.spec for item in work]
    moved: set[int] = set()
    verdict: TakeVerdict | None = None
    progress: dict[str, Any] = {}
    try:
        await manifest.persist()
        while offset < len(work):
            if signals.complete.is_set():
                manifest.reason = "complete_requested"
                break
            if signals.retake.is_set():
                signals.retake.clear()
                if previous is not None:
                    resume, offset = max(offset, previous + 1), previous
                    retry = TakeVerdict(True, next="fix_and_retake", charge="operator")
            item = work[offset]
            ledger = ledgers[item.pose_index]
            if retry is not None:
                if not ledger.can_retry(retry.charge):
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
            progress = {"pose": item.pose_index + 1, "poses": len(ledgers),
                        "config": item.config, "configs": item.size, "attempt": attempt,
                        "fault": retry.fault if retry else None, "next_action": retry.next if retry else None,
                        "budget": ledger.to_payload()}
            entry = item.entry
            if retry and retry.next == "fix_and_retake" and retry.fault and entry:
                entry = SimpleNamespace(screen={**entry.screen, "body": REASON_REGISTRY[retry.fault].message})
            take_started: float | None = None
            try:
                if signals.stop.is_set():
                    raise CaptureStopped("capture stopped")
                ledger.charge = retry.charge if retry is not None else "none"
                if gate:
                    gate.publish(progress)
                await _grant(gate, offset + 1, offset + 1 + grant_epoch, entry, signals,
                             (lambda: admit(offset + 1, attempt, entry, ledger)) if admit else None)
                if gate:
                    if item.pose_index not in moved:
                        manifest.mic_moves += 1
                        moved.add(item.pose_index)
                take_started = clock()
                if retry is not None:
                    if admit is None:
                        ledger.spend(retry.charge)
                    progress["budget"] = ledger.to_payload()
                    if gate:
                        gate.publish(progress)
                attempts[offset] = attempt
                if admit is None:
                    ledger.admitted += 1
                manifest.begin(item.stop, attempt=attempt, pose_index=item.pose_index)
                outcome = await (measure or session.measure)(spec)
                manifest.outcomes.append((outcome, str(session.graph_fingerprint)))
                verdict = None
                records = manifest.pending_records or [({}, "")]
                for ordinal, (record, record_id) in enumerate(records):
                    if record_id:
                        try:
                            analysis = await asyncio.to_thread(analyze, record, record_id)
                            program = ExcitationProgram.from_dict(record["program"]) if record.get("program") else None
                            assessed = (assessor or assess)(analysis, phase=program.phase if program else spec.program_phase or "verify",
                                              program=program, gain_ceiling_db=gain_ceiling_db)
                            if program is not None:
                                record = {**record, "curves": analysis_curve_records(analysis, program)}
                        except (ValueError, KeyError, OSError) as exc:
                            assessed = TakeVerdict(False, fault=REASON_INTERNAL_ERROR, next="stop",
                                                   evidence={"error_type": type(exc).__name__})
                    else:
                        incident = next((s.incident for s in outcome.stimuli if s.incident), "")
                        assessed = TakeVerdict(False, fault=incident if incident in REASON_REGISTRY else REASON_INTERNAL_ERROR,
                                               next="stop", evidence={"incident": incident})
                    if not outcome.complete:
                        incident = str(record.get("incident") or next((s.incident for s in outcome.stimuli if s.incident), ""))
                        assessed = replace(assessed, ok=False,
                                           fault=assessed.fault or (incident if incident in REASON_REGISTRY else REASON_INTERNAL_ERROR),
                                           next="stop" if assessed.next == "accept" else assessed.next,
                                           evidence={**assessed.evidence, "incident": incident})
                    await manifest.append(record, record_id, assessed, complete=outcome.complete,
                                          started_s=take_started - started, ended_s=clock() - started, ordinal=ordinal)
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
                    continue
                previous = offset
                retry = None
                if signals.retake.is_set():
                    continue
                offset = resume if resume is not None else offset + 1
                resume = None
            except _Control:
                continue
            except aborting as exc:
                manifest.reason = (str(exc.code) if isinstance(exc, _OWN_CODE) else
                                   next((code for cls, code in aborts.items() if isinstance(exc, cls)), "cancelled"))
                manifest.detail = str(exc) or type(exc).__name__
                manifest.cancelled = isinstance(exc, asyncio.CancelledError)
                manifest.stopped_at = {"pose_index": item.pose_index, "index": item.stop["index"]}
                if attempts[offset] != attempt:
                    manifest.begin(item.stop, attempt=attempt, pose_index=item.pose_index)
                fault = manifest.reason if manifest.reason in REASON_REGISTRY else REASON_INTERNAL_ERROR
                already_banked = {take["artifacts"]["record_id"] for take in manifest.takes}
                ended = clock()
                for record, record_id in manifest.pending_records or [({}, "")]:
                    if record_id and record_id in already_banked:
                        continue
                    await manifest.append(record, record_id, TakeVerdict(False, fault=fault, next="stop",
                                          evidence={"incident": manifest.reason}), complete=False,
                                          started_s=(take_started if take_started is not None else ended) - started,
                                          ended_s=ended - started)
                break
            finally:
                if take_started is not None:
                    while len(manifest.wall_s) <= item.pose_index:
                        manifest.wall_s.append(0.0)
                    manifest.wall_s[item.pose_index] += clock() - take_started
    except BaseException:  # noqa: BLE001 - finalize failure evidence, then propagate unchanged
        manifest.reason = manifest.reason or REASON_INTERNAL_ERROR
        raise
    finally:
        manifest.finalized = True
        if gate:
            gate.abandon_hold()
            gate.publish({**progress, "status": manifest.status, "manifest": manifest.path,
                          "takes": manifest.takes_measured, "not_measured": manifest.takes_skipped,
                          "fault": manifest.reason or (verdict.fault if verdict else None),
                          "next_action": "accept" if manifest.status == "complete" else "stop"})
        log_event(logger, "active_speaker.plan_run", status=manifest.status, reason=manifest.reason,
                  takes=manifest.takes_measured, skipped=manifest.takes_skipped, mic_moves=manifest.mic_moves)
        await manifest.persist()
    return manifest
