# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""One run isolation hold, with sequential fixed-level windows (ADR-0305)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, cast

from jasper.log_event import log_event
from jasper.audio_measurement.wired_capture import WiredSplMonitor

from ..candidate_bank import CandidateBankRefusal, find_banked_candidate
from ..design_draft import load_design_draft
from ..measured_crossover_candidate import MeasuredCrossoverCandidate
from ..measurement_emit import (
    MeasurementGraphProfile, TuningGraphScope, compile_tuning_graph,
    emit_measurement_graph,
)
from ..restore_wait import resilient_restore
from ..session_volume_plan import SessionVolumeRestoreResult
from .measure_spec import CANDIDATE_SCOPES
from .refusal_copy import REASON_MEASURE_SPL_CALIBRATION_REQUIRED, REASON_VOLUME_RESTORE_DEFERRED

logger = logging.getLogger(__name__)
REFUSE_SESSION_LIVE = "measurement_door_session_live"
REFUSE_NO_VOLUME_OWNER = "measurement_door_no_volume_owner"
REFUSE_VOLUME_NOT_OPEN = "measurement_door_volume_not_open"


class MeasurementDoorRefused(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}")


@dataclass
class _HeldGraph:
    """Sessions borrow the graph; only the isolation hold restores it (ADR-0305)."""

    inner: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def restore(self) -> None:
        pass


@dataclass
class IsolationHold:
    graph: Any
    claim: Any
    plan: Any
    volume_door: Any
    window_open: bool = False


@dataclass
class OpenMeasurementDoor:
    graph: Any
    claim: Any
    plan: Any
    measurement_volume_db: float
    graph_fingerprint: str
    spl_monitor: WiredSplMonitor
    entry_scope_fingerprint: str = ""
    restore_result: SessionVolumeRestoreResult | None = None


@asynccontextmanager
async def isolation_hold(
    *, graph: Any, camilla_factory: Callable[[], Any], action: str,
    volume_state_path: str | Path | None = None,
    wall_clock_ceiling_s: float | None = None, gate_owner: str | None = None,
    plan: Any = None,
) -> AsyncIterator[IsolationHold]:
    from jasper.measurement_window import MEASUREMENT_GATE_OWNER, measurement_window  # lazy: coordinator boundary
    from ..session_volume_plan import (  # lazy: live plan binding
        DEFAULT_SESSION_VOLUME_STATE_PATH, SessionVolumePlan, live_measurement_session,
    )

    state_path = DEFAULT_SESSION_VOLUME_STATE_PATH if volume_state_path is None else Path(volume_state_path)
    busy = live_measurement_session(state_path=state_path, action=action)
    if busy is not None:
        raise MeasurementDoorRefused(REFUSE_SESSION_LIVE, busy)
    owner, claim = _measurement_claim()
    volume_door = _volume_door(owner, camilla_factory, claim=claim)
    plan = plan if plan is not None else SessionVolumePlan(state_path=state_path)
    if wall_clock_ceiling_s is not None:
        plan.set_wall_clock_ceiling_s(wall_clock_ceiling_s)
    # The coordinator renews all three leases across windows and poses (ADR-0305).
    # The window wraps the open, because the latch's first write is a fader
    # write like any other.
    async with measurement_window(gate_owner=MEASUREMENT_GATE_OWNER if gate_owner is None else gate_owner):
        await plan.enforce_ceiling(volume_door)
        body_error: BaseException | None = None
        try:
            yield IsolationHold(_HeldGraph(graph), claim, plan, volume_door)
        except BaseException as exc:  # noqa: BLE001 - preserve cancellation through cleanup
            body_error = exc
            raise
        finally:
            try:
                await resilient_restore(graph.restore())
            except BaseException as exc:  # noqa: BLE001 - keep the original failure
                if body_error is None:
                    raise
                if body_error.__context__ is None:
                    body_error.__context__ = exc


@asynccontextmanager
async def level_window(
    level_db: float, *, hold: IsolationHold, spl_monitor: WiredSplMonitor | None,
) -> AsyncIterator[OpenMeasurementDoor]:
    from ..session_volume_plan import SessionVolumeOpenResult, SessionVolumePlanError  # lazy: live plan binding

    if spl_monitor is None:
        raise MeasurementDoorRefused(REASON_MEASURE_SPL_CALIBRATION_REQUIRED, "A level window needs an SPL watch")
    if hold.window_open:
        raise MeasurementDoorRefused(REFUSE_SESSION_LIVE, "A level window is already open")
    hold.window_open = True
    graph, claim, plan = hold.graph, hold.claim, hold.plan
    body_error: BaseException | None = None
    volume_open = False
    opened_door: OpenMeasurementDoor | None = None
    try:
        # The plan persists its intent before it reads the fader back; with no household
        # level to restore, a failed open would latch the emergency floor.
        if await hold.volume_door.read_household_level_db() is None:
            raise MeasurementDoorRefused(REFUSE_VOLUME_NOT_OPEN, "the household volume is unreadable")
        try:
            opened = await plan.open(level_db, hold.volume_door)
        except SessionVolumePlanError as exc:
            raise MeasurementDoorRefused(REFUSE_VOLUME_NOT_OPEN, str(exc)) from exc
        if opened is not SessionVolumeOpenResult.OPENED:
            raise MeasurementDoorRefused(REFUSE_VOLUME_NOT_OPEN, opened.value)
        volume_open = True
        fingerprint = await graph.install()
        opened_door = OpenMeasurementDoor(graph, claim, plan, level_db, fingerprint, spl_monitor,
                                          graph.entry_scope_fingerprint)
        log_event(logger, "active_speaker.measurement_door", action="open",
                  fingerprint=fingerprint, measurement_volume_db=level_db)
        yield opened_door
    except BaseException as raised:  # noqa: BLE001 - preserve cancellation through cleanup
        body_error = raised
        raise
    finally:
        # ``plan.open`` is INSIDE this guard: it persists its durable
        # ``active`` intent before the first volume mutation, so a
        # cancellation landing in that gap would otherwise leave a record no
        # later process drains, and every operator door would read a live
        # measurement for the whole wall-clock ceiling. A ``finally`` on a
        # flag rather than an ``except``, because a ``CancelledError`` is not
        # an ``Exception``. SHIELDED: a cancel inside the give-back would
        # strand the fader at measurement level with nothing latched.
        try:
            result = await resilient_restore(_give_back(
                claim, plan, hold.volume_door,
                reason="measurement_door_closed" if volume_open else "measurement_door_open_failed",
                body_error=body_error,
            ))
            if opened_door is not None:
                opened_door.restore_result = result
            if body_error is None:
                if result is SessionVolumeRestoreResult.DEFERRED:
                    raise MeasurementDoorRefused(REASON_VOLUME_RESTORE_DEFERRED, "Household volume restore is pending")
                if result is SessionVolumeRestoreResult.FAILED:
                    raise SessionVolumePlanError("Household volume restore failed")
        finally:
            hold.window_open = False


async def _give_back(
    claim: Any, plan: Any, volume_door: Any, *, reason: str, body_error: BaseException | None = None,
) -> SessionVolumeRestoreResult | None:
    """Release the Main claim and close the volume plan inside the graph hold.

    Every step runs even when an earlier one raises, and the first failure is
    kept. With a body error, that failure becomes its context unless it already
    has one, so the original cause stays the one raised.
    """
    first: BaseException | None = None
    result: SessionVolumeRestoreResult | None = None

    async def close_plan() -> None:
        nonlocal result
        result = await plan.close(volume_door, reason=reason)

    # Restore every slot even after a failure; keep the original failure (ADR-0179).
    for step in (claim.release, close_plan):
        try:
            await step()
        except BaseException as failure:  # noqa: BLE001 - cleanup must survive cancellation
            if first is None:
                first = failure
    if first is not None:
        if body_error is None:
            raise first
        if body_error.__context__ is None:
            body_error.__context__ = first
    return result


def _measurement_claim() -> tuple[Any, Any]:
    """The process's owner and this session's ONE claim at ``SESSION_MEASUREMENT``.

    Minted once and injected into both things that hold it — the plan's door and
    the engine's volume seam — because they are one claim, not two.
    """
    from jasper.volume_owner import volume_owner

    from .volume_claim import MeasurementVolumeClaim

    owner = volume_owner()
    if owner is None:
        raise MeasurementDoorRefused(
            REFUSE_NO_VOLUME_OWNER,
            "this process registered no fader owner; a door that minted its "
            "own would be the second authority the owner exists to delete",
        )
    return owner, MeasurementVolumeClaim(owner)


def _volume_door(
    owner: Any, camilla_factory: Callable[[], Any], *, claim: Any,
) -> Any:
    """The plan's door onto the same owner the claim is taken through.

    The read is the PHYSICAL fader, which is what makes the snapshot every drain
    restores toward a state rather than an intent.
    """
    from jasper.camilla import CamillaUnavailable

    from .volume_claim import OwnerVolumeDoor

    async def _read_fader() -> float | None:
        try:
            return await camilla_factory().get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    return OwnerVolumeDoor(owner, read_fader=_read_fader, claim=claim)


def bind_measurement_graph(
    profile: MeasurementGraphProfile,
    *,
    camilla_factory: Callable[[], Any],
    config_dir: str | Path,
    candidate: MeasuredCrossoverCandidate | None = None,
) -> Any:
    """Bind neutral driver and complete tuning graphs to one session owner."""
    from jasper.dsp_apply import dsp_writer_lock

    from ..baseline_profile import load_applied_baseline_profile_state  # lazy: baseline compilation imports the door
    from ..candidate_parts import candidate_from_design_draft  # lazy: candidate parts imports baseline compilation

    from .composition import confirm_graph_is_live
    from .session_graph import MeasurementSessionGraph

    def emit_scoped(scope: str, candidate_id: str, branch_channels: Mapping[str, int]) -> str:
        selected = None
        if scope in CANDIDATE_SCOPES:
            selected = (reference if reference is not None and candidate_id == reference.fingerprint else
                        candidate if candidate is not None else find_banked_candidate(candidate_id).candidate)
        return compile_tuning_graph(
            profile,
            scope=cast(TuningGraphScope, scope),
            candidate=selected,
            branch_channels=branch_channels,
        )

    try:
        if candidate is None:
            try:
                applied = load_applied_baseline_profile_state() or {}
                fingerprint = (applied.get("source") or {}).get("measured_candidate_fingerprint", "")
                reference = find_banked_candidate(fingerprint).candidate
            except (CandidateBankRefusal, OSError, ValueError):
                reference = candidate_from_design_draft(profile.topology, load_design_draft(topology=profile.topology))
        else:
            reference = candidate
        reference_yaml = emit_scoped("candidate", reference.fingerprint, {})
    except (CandidateBankRefusal, OSError, ValueError):
        if candidate is not None:
            raise
        reference = reference_yaml = None
        log_event(logger, "active_speaker.level_reference", result="unavailable")

    graph = MeasurementSessionGraph(
        level_reference_yaml=reference_yaml,
        emit=partial(emit_measurement_graph, profile),
        emit_scoped=emit_scoped,
        cam_factory=camilla_factory,
        writer_lock=lambda: dsp_writer_lock(
            str(config_dir), source="crossover_v2_session_graph"
        ),
        confirm_live=confirm_graph_is_live,
    )
    if candidate is not None:
        graph.select_scope("candidate", candidate.fingerprint)
    return graph
