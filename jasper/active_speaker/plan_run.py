# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run a stated plan against one held session: poses outer, takes inner.

The executor half of :mod:`.angle_capture`, which turns an operator's intent
into resolved stops and deliberately stops there. One loop serves both callers:
a WALK (:func:`run_plan`, a staged :class:`~.angle_capture.AngleCaptureRequest`)
and a spec LIST at one pose (:func:`run_specs`, the measurement CLI's batch) --
so "what ends a run", "what a take reports" and "what the microphone was asked
to do" have one answer apiece rather than one per door.

The microphone moves once per POSE. Every take of one pose runs under a single
placement grant, which is exactly what
:meth:`~.crossover_v2.position_gate.PositionGate.gate` already carries across a
declared batch -- the same gate the capture page and the lab arm satisfy.

Dependency direction: a sibling of :mod:`.crossover_v2_flow` that imports FROM
it through :mod:`.angle_capture`, never a module under ``crossover_v2/`` (whose
modules may not reach the flow).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from itertools import groupby
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from jasper.log_event import log_event

from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS
from jasper.audio_measurement.wired_capture import WiredSplMonitor

from .angle_capture import (
    WALK_CEILING_ABOVE_STOP,
    WALK_SPL_CALIBRATION_REQUIRED,
    WALK_STIMULUS_NOT_ACCEPTED,
    AngleCaptureRequest,
    LateralWalkRefused,
    refuse_unplayable_walk_policy,
    resolve_request,
    stop_specs,
)
from .angle_capture_spool import angle_request_document
from .crossover_v2.capture_plan import pose_batch_screens, position_screen_keys
from .crossover_v2.capture_source import CaptureBeginDeferred, CaptureBeginRefused
from .crossover_v2.measure_spec import MeasureSpec
from .crossover_v2.position_gate import POSITION_HOLD_POLL_S, PositionGate
from .crossover_v2.program_transaction import StimulusCaptureStopped
from .crossover_v2.session import MeasureOutcome, TuningSession

logger = logging.getLogger(__name__)

__all__ = [
    "PLAN_RESULT_KIND",
    "PLAN_RESULT_SCHEMA_VERSION",
    "RUN_INTERRUPTED",
    "RUN_MEASURED",
    "RUN_REFUSED",
    "SPL_MONITOR_UNAVAILABLE",
    "TAKE_INCOMPLETE",
    "TAKE_MEASURED",
    "PlanResult",
    "TakeResult",
    "request_fingerprint",
    "run_plan",
    "run_specs",
    "spl_monitor_note",
    "spl_watch",
    "take_spl_ceiling",
]

#: Every take this run asked for banked cleanly.
RUN_MEASURED = "measured"
#: The run stopped part-way and ``stopped_at`` names where. Every take already
#: banked stays banked.
RUN_INTERRUPTED = "interrupted"
#: Nothing played: the plan was refused before the first take.
RUN_REFUSED = "refused"

TAKE_MEASURED = "measured"
#: The take played and did not bank everything it asked for; ``reason`` is the
#: engine's own incident.
TAKE_INCOMPLETE = "incomplete"

#: What :attr:`PlanResult.spl_monitor` says when no monitor watched the takes
#: because this box cannot turn a recording into dB SPL. A DISCLOSURE, not a
#: gate: the commissioning stop still bounds the level a session may claim, and
#: nothing here relaxes it.
SPL_MONITOR_UNAVAILABLE = "unavailable_no_calibration"

PLAN_RESULT_KIND = "jts_plan_result"
PLAN_RESULT_SCHEMA_VERSION = 1

#: The two failures a RUN names for ITSELF, reported under their own ``code``
#: rather than a caller's word: a placement grant that will not come (the gate's
#: per-hold and session budgets) and a capture the kernel stopped -- which is
#: how ``spl_ceiling_exceeded`` reaches an operator by name.
_OWN_CODE = (CaptureBeginRefused, StimulusCaptureStopped)


# --------------------------------------------------------------------------- #
# the level bound every take plays under
# --------------------------------------------------------------------------- #


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
    commissioning_stop_db_spl: float,
    sensitivity: Any | None,
    device: Any,
) -> tuple[WiredSplMonitor | None, str]:
    """The monitor one session's takes record under, and its SPL disclosure.

    ONE owner for both doors that play a stated walk -- ``jasper-measure`` and
    the wizard's session open -- so the same walk is bounded the same way
    whichever took it. Every run is bounded, not only one that typed a ceiling:
    :func:`take_spl_ceiling` resolves the number, refusing one above the box's
    own commissioning stop.

    ``sensitivity`` is ``None`` on a box that cannot turn a recording into dB
    SPL. That DISCLOSES when the run stated no ceiling of its own, and REFUSES
    when it stated one: a bound nothing can enforce is something an operator
    must be able to act on rather than a number quietly ignored.
    """
    ceiling = take_spl_ceiling(
        stated_db_spl, commissioning_stop_db_spl=commissioning_stop_db_spl,
    )
    if sensitivity is None:
        if stated_db_spl is None:
            return None, SPL_MONITOR_UNAVAILABLE
        raise LateralWalkRefused(
            WALK_SPL_CALIBRATION_REQUIRED,
            "an SPL ceiling requires a resolvable microphone sensitivity",
        )
    channel = int(SUPPORTED_MODELS[device.model_key].get("capture_channel", 0))
    return WiredSplMonitor(sensitivity, ceiling, channel), spl_monitor_note(ceiling)


# --------------------------------------------------------------------------- #
# the package a run answers with
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TakeResult:
    """One take: where in the plan it sat, what played, and what banked.

    ``attempt`` is the begin this take was admitted under; nothing here retries,
    so it tracks :attr:`stop_index` and the gate's batch carry reads the pair.
    ``level_db`` and ``stimulus_dbfs`` are the FIRST stimulus's -- a ladder walk
    plays its remaining rungs at the same claimed level, and every rung it banked
    is in ``record_ids``. ``started_s``/``ended_s`` are seconds from the run's
    own start, never a wall clock: the package is about durations.
    """

    pose_index: int
    stop_index: int
    attempt: int
    candidate_id: str
    graph_fingerprint: str
    level_db: float | None
    stimulus_dbfs: float | None
    record_ids: tuple[str, ...]
    status: str
    reason: str
    started_s: float
    ended_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "pose_index": self.pose_index,
            "stop_index": self.stop_index,
            "attempt": self.attempt,
            "candidate_id": self.candidate_id,
            "graph_fingerprint": self.graph_fingerprint,
            "level_db": self.level_db,
            "stimulus_dbfs": self.stimulus_dbfs,
            "record_ids": list(self.record_ids),
            "status": self.status,
            "reason": self.reason,
            "started_s": round(self.started_s, 3),
            "ended_s": round(self.ended_s, 3),
        }


@dataclass(frozen=True)
class PlanResult:
    """What one run of a plan produced, compact enough to read whole.

    Counts first, then one row per take. ``stopped_at`` is present only on an
    interrupted run and names the pose and stop in flight, which the banked ids
    alone cannot locate. ``decisions_needed`` is what the run wants a caller to
    decide before the next one -- empty until something fills it.

    ``specs`` (one per stop this run PLAYS, in play order -- a skipped stop
    names none) and ``outcomes`` (the engine's own answers, in take order) are
    excluded from
    :meth:`to_dict`: a host that reports per spec — the CLI's per-spec incidents
    and playback, and WHICH spec an interrupted run stopped on — reads them
    rather than building the plan a second time to find out.
    """

    request_fingerprint: str
    status: str
    poses: int
    stops_planned: int
    takes_measured: int
    takes_skipped: int
    mic_moves: int
    #: Begins this run admitted, INCLUDING the one an interruption stopped --
    #: which is what makes ``attempts - takes_measured`` the honest count of
    #: what was started and not banked.
    attempts: int
    wall_s: tuple[float, ...]
    spl_monitor: str
    takes: tuple[TakeResult, ...] = ()
    reason: str = ""
    detail: str = ""
    stopped_at: Mapping[str, int] | None = None
    decisions_needed: tuple[Any, ...] = ()
    specs: tuple[MeasureSpec, ...] = field(default=(), repr=False)
    outcomes: tuple[tuple[MeasureOutcome, str], ...] = field(
        default=(), repr=False,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PLAN_RESULT_KIND,
            "schema_version": PLAN_RESULT_SCHEMA_VERSION,
            "request_fingerprint": self.request_fingerprint,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "poses": self.poses,
            "stops_planned": self.stops_planned,
            "takes_measured": self.takes_measured,
            "takes_skipped": self.takes_skipped,
            "mic_moves": self.mic_moves,
            "attempts": self.attempts,
            "wall_s": [round(seconds, 3) for seconds in self.wall_s],
            "spl_monitor": self.spl_monitor,
            "stopped_at": dict(self.stopped_at) if self.stopped_at else None,
            "takes": [take.to_dict() for take in self.takes],
            "decisions_needed": list(self.decisions_needed),
        }


def request_fingerprint(request: AngleCaptureRequest) -> str:
    """This walk's identity: sha256 over the document the spool banks it as.

    Asked of :func:`~.angle_capture_spool.angle_request_document` so a run's
    receipt names the same shape a staged walk has on disk, minus the clock --
    two runs of one walk fingerprint alike, and an edited stop does not.
    """
    return _digest(angle_request_document(request))


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# the two doors
# --------------------------------------------------------------------------- #


async def run_plan(
    request: AngleCaptureRequest,
    *,
    session: TuningSession,
    gate: PositionGate | None = None,
    candidate_scopes: Mapping[str, str],
    aborts: Mapping[type[BaseException], str],
    spl_monitor: str = SPL_MONITOR_UNAVAILABLE,
    clock: Callable[[], float] = time.monotonic,
) -> PlanResult:
    """Walk one stated request under an open session, poses outer.

    ``gate`` holds the first begin of every pose batch until something reports
    the microphone in place; ``None`` says the microphone is ALREADY where the
    plan asks (a single-pose run, a test), and nothing waits.

    ``candidate_scopes`` maps a stop's candidate fingerprint to the scope that
    compiles its complete graph, resolved by the caller that can read the bank.
    ``aborts`` is the caller's own table of which failures end the run and what
    each is called -- stated, never reached for, because the vocabulary a run
    refuses in belongs to the host that answers in it.

    A per-driver stop plays the phase's own composed program rather than a spec
    and is SKIPPED here, counted in ``takes_skipped``: composing a phase program
    is the session host's, not this loop's.
    """
    fingerprint = request_fingerprint(request)
    try:
        refuse_unplayable_walk_policy(request)
        resolved = resolve_request(request)
        prompts = tuple(stop.prompt for stop in resolved)
        try:
            specs = stop_specs(
                request, candidate_scopes=candidate_scopes, prompts=prompts,
            )
        except ValueError as exc:
            # Only the stop's own pose is new on that construction; the spec's
            # own sentence names the field it refused.
            raise LateralWalkRefused(WALK_STIMULUS_NOT_ACCEPTED, str(exc)) from exc
    except LateralWalkRefused as exc:
        return _refused(fingerprint, exc, spl_monitor=spl_monitor)

    # The playable subset, resolved ONCE and numbered over itself. The gate
    # carries a pose's grant on the pair ``(index - 1, attempt - 1)``, so a
    # skipped stop counted in the numbering would ask a second placement grant
    # at the pose the microphone is already standing at.
    playable = [offset for offset, spec in enumerate(specs) if spec is not None]
    batches = [
        [place for place, _offset in group]
        for _place, group in groupby(
            enumerate(playable), key=lambda row: request.stops[row[1]].place,
        )
    ]
    indexes = [place + 1 for place in range(len(playable))]
    screens = pose_batch_screens(
        indexes, [prompts[offset] for offset in playable],
        [resolved[offset].candidate_id for offset in playable],
    )
    entries = {
        index: SimpleNamespace(screen={
            **resolved[offset].screen,
            **position_screen_keys(resolved[offset].prompt),
            **screens.get(index, {}),
        })
        for index, offset in zip(indexes, playable)
    }
    return await _run(
        batches, [specs[offset] for offset in playable], entries=entries,
        skipped=len(specs) - len(playable), fingerprint=fingerprint,
        session=session, gate=gate, aborts=aborts, spl_monitor=spl_monitor,
        clock=clock,
    )


async def run_specs(
    specs: Sequence[MeasureSpec],
    *,
    session: TuningSession,
    aborts: Mapping[type[BaseException], str],
    spl_monitor: str = SPL_MONITOR_UNAVAILABLE,
    clock: Callable[[], float] = time.monotonic,
) -> PlanResult:
    """Measure a spec list against ONE microphone placement, through the same loop.

    A batch of configs at one pose IS a one-pose plan: nothing moves the
    microphone between them, so there is no gate and one placement is what the
    whole run costs.
    """
    return await _run(
        [list(range(len(specs)))], tuple(specs), entries={}, skipped=0,
        fingerprint=_digest([spec.to_dict() for spec in specs]),
        session=session, gate=None, aborts=aborts, spl_monitor=spl_monitor,
        clock=clock,
    )


# --------------------------------------------------------------------------- #
# the one loop
# --------------------------------------------------------------------------- #


async def _run(
    batches: Sequence[Sequence[int]],
    specs: Sequence[MeasureSpec],
    *,
    entries: Mapping[int, Any],
    skipped: int,
    fingerprint: str,
    session: TuningSession,
    gate: PositionGate | None,
    aborts: Mapping[type[BaseException], str],
    spl_monitor: str,
    clock: Callable[[], float],
) -> PlanResult:
    """Poses outer, takes inner, under one grant per pose.

    An interruption -- a cancel, an operator's own ``KeyboardInterrupt``, or one
    of the caller's ``aborts`` -- KEEPS every take already banked and reports
    where it stopped. The session's close puts the speaker back exactly once
    either way, which is the engine's own guarantee and not re-taken here.
    """
    aborting: tuple[type[BaseException], ...] = (*_OWN_CODE, *aborts)
    started = clock()
    takes: list[TakeResult] = []
    outcomes: list[tuple[MeasureOutcome, str]] = []
    wall_s: list[float] = []
    mic_moves = 0
    attempts = 0
    stopped: tuple[str, str, Mapping[str, int]] | None = None
    for pose_index, batch in enumerate(batches):
        pose_started = clock()
        granted = False
        for offset in batch:
            spec = specs[offset]
            # ``attempt`` tracks the stop, so the gate's batch carry sees the
            # (index - 1, attempt - 1) pair it grants the rest of a pose on.
            index = attempt = offset + 1
            attempts += 1
            try:
                if gate is not None and index in entries:
                    await _grant(gate, index, attempt, entries[index])
                    if not granted:
                        mic_moves += 1
                        granted = True
                take_started = clock()
                outcome = await session.measure(spec)
            except aborting as exc:
                stopped = (
                    _abort_reason(exc, aborts), str(exc) or type(exc).__name__,
                    {"pose_index": pose_index, "stop_index": offset},
                )
                break
            # Read before the next take swaps the install: the session re-proves
            # the graph per stimulus, so this fingerprint names the variant graph
            # THIS take measured through.
            outcomes.append((outcome, str(session.graph_fingerprint)))
            takes.append(_take(
                outcome, pose_index=pose_index, stop_index=offset, attempt=attempt,
                graph_fingerprint=str(session.graph_fingerprint),
                started_s=take_started - started, ended_s=clock() - started,
            ))
        wall_s.append(clock() - pose_started)
        if stopped is not None:
            break
    result = PlanResult(
        request_fingerprint=fingerprint,
        status=RUN_MEASURED if stopped is None else RUN_INTERRUPTED,
        poses=len(batches),
        stops_planned=len(specs) + skipped,
        takes_measured=len(takes),
        takes_skipped=skipped,
        mic_moves=mic_moves,
        attempts=attempts,
        wall_s=tuple(wall_s),
        spl_monitor=spl_monitor,
        takes=tuple(takes),
        reason="" if stopped is None else stopped[0],
        detail="" if stopped is None else stopped[1],
        stopped_at=None if stopped is None else stopped[2],
        specs=tuple(specs),
        outcomes=tuple(outcomes),
    )
    log_event(
        logger, "active_speaker.plan_run",
        level=logging.WARNING if stopped is not None else logging.INFO,
        status=result.status, reason=result.reason,
        poses=result.poses, takes=result.takes_measured,
        skipped=result.takes_skipped, mic_moves=result.mic_moves,
        spl_monitor=result.spl_monitor, request=fingerprint[:12],
    )
    return result


async def _grant(
    gate: PositionGate, index: int, attempt: int, entry: Any,
) -> None:
    """Hold this begin until the microphone is reported in place.

    The gate's own budgets bound the wait -- a per-hold ceiling and the session
    ceiling, both raising ``CaptureBeginRefused`` -- so nothing here carries a
    second clock. The cadence is the one every other runner keeps
    (:data:`~.crossover_v2.position_gate.POSITION_HOLD_POLL_S`), so gate logging
    and driver pacing see one rhythm.
    """
    while True:
        try:
            gate.gate(index, attempt, entry)
            return
        except CaptureBeginDeferred:
            await asyncio.sleep(POSITION_HOLD_POLL_S)


def _abort_reason(
    exc: BaseException, aborts: Mapping[type[BaseException], str],
) -> str:
    """What this failure is CALLED: :data:`_OWN_CODE`'s own word, else the
    caller's word for its type.

    ``isinstance`` and not ``aborts[type(exc)]``: a subclass is still that
    failure, and a ``KeyError`` would replace the answer.
    """
    if isinstance(exc, _OWN_CODE):
        return str(exc.code)
    return next(word for cls, word in aborts.items() if isinstance(exc, cls))


def _take(
    outcome: MeasureOutcome,
    *,
    pose_index: int,
    stop_index: int,
    attempt: int,
    graph_fingerprint: str,
    started_s: float,
    ended_s: float,
) -> TakeResult:
    """One engine answer as the package's own row."""
    first = outcome.stimuli[0] if outcome.stimuli else None
    incidents = [s.incident for s in outcome.stimuli if s.incident]
    complete = bool(outcome.stimuli) and not incidents and all(
        s.banked for s in outcome.stimuli
    )
    return TakeResult(
        pose_index=pose_index,
        stop_index=stop_index,
        attempt=attempt,
        candidate_id=outcome.spec.candidate_id,
        graph_fingerprint=graph_fingerprint,
        level_db=None if first is None else first.level_db,
        stimulus_dbfs=None if first is None else first.stimulus_dbfs,
        record_ids=outcome.record_ids,
        status=TAKE_MEASURED if complete else TAKE_INCOMPLETE,
        reason=incidents[0] if incidents else "",
        started_s=started_s,
        ended_s=ended_s,
    )


def _refused(
    fingerprint: str, exc: LateralWalkRefused, *, spl_monitor: str,
) -> PlanResult:
    """A plan refused before anything played, in the same package shape."""
    log_event(
        logger, "active_speaker.plan_run", level=logging.WARNING,
        status=RUN_REFUSED, reason=exc.reason, detail=exc.detail,
        request=fingerprint[:12],
    )
    return PlanResult(
        request_fingerprint=fingerprint,
        status=RUN_REFUSED,
        poses=0,
        stops_planned=0,
        takes_measured=0,
        takes_skipped=0,
        mic_moves=0,
        attempts=0,
        wall_s=(),
        spl_monitor=spl_monitor,
        reason=exc.reason,
        detail=exc.detail,
    )
