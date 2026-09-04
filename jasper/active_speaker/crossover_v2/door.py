# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The kernel path onto a held speaker: consult, claim, install, yield, restore.

One linear ``async with``, spelled once so two operator doors cannot disagree
about the give-back. ``measurement_window()`` wraps the WHOLE session:
without it jasper-voice's idle reconciler reverts the opened measurement volume
toward the household level within ~200 ms (W6.1 hardware run 2), and cap
enforcement then silently understates. The install is ``set_active_config_raw``,
so a restart or ``kill -9`` restores the applied graph by doing nothing
(ADR-0193).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from jasper.log_event import log_event

from ..measurement_emit import MeasurementGraphProfile, emit_measurement_graph
from ..restore_wait import resilient_restore

logger = logging.getLogger(__name__)

__all__ = [
    "REFUSE_NO_VOLUME_OWNER",
    "REFUSE_SESSION_LIVE",
    "REFUSE_VOLUME_NOT_OPEN",
    "MeasurementDoorRefused",
    "OpenMeasurementDoor",
    "measurement_door",
]

#: Another measurement holds the speaker, or a previous one left its volume
#: unresolved. The interlock's own sentence rides in ``detail``.
REFUSE_SESSION_LIVE = "measurement_door_session_live"
#: This process registered no :class:`~jasper.volume_owner.VolumeOwner`. A
#: wiring defect in the door's ``main``, not a state the speaker can be in.
REFUSE_NO_VOLUME_OWNER = "measurement_door_no_volume_owner"
#: The plan could not establish the declared measurement volume. The speaker was
#: put back by the plan's own drain before this refusal was raised.
REFUSE_VOLUME_NOT_OPEN = "measurement_door_volume_not_open"


class MeasurementDoorRefused(RuntimeError):
    """The door did not open. Carries a code and the sentence behind it."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class OpenMeasurementDoor:
    """What a held speaker hands the body of the ``async with``.

    Re-taking the claim or the install through :class:`~.session.TuningSession`
    is not double work: the claim is contracted idempotent for the SAME level,
    and the install is contracted idempotent and costs one liveness read when
    nothing moved.
    """

    graph: Any
    claim: Any
    plan: Any
    measurement_volume_db: float
    graph_fingerprint: str
    #: The tuning-scope hash of the graph this door opened ON — the round's
    #: comparability anchor, banked at entry (#3489). Distinct from
    #: ``graph_fingerprint``, which names the MEASUREMENT graph the door then
    #: installed. ``""`` when the entry graph could not be named; the live
    #: verdict is ``graph.comparability_boundary``.
    entry_scope_fingerprint: str = ""


@asynccontextmanager
async def measurement_door(
    *,
    profile: MeasurementGraphProfile,
    measurement_volume_db: float,
    camilla_factory: Callable[[], Any],
    action: str,
    config_dir: str | Path | None = None,
    volume_state_path: str | Path | None = None,
    wall_clock_ceiling_s: float | None = None,
    gate_owner: str | None = None,
) -> AsyncIterator[OpenMeasurementDoor]:
    """Hold this speaker for one measurement session, and give it back.

    ``action`` closes the interlock's refusal sentence (*"…before <action>"*).

    ``config_dir`` defaults to the SAME
    :data:`~..staging.DEFAULT_CAMILLA_CONFIG_DIR` every sibling DSP writer locks
    against. A different directory is a different lock identity and would not
    serialize against them at all.

    ``gate_owner`` is this door's identity on the mux diagnostic gate.
    ``mux.FANIN_TEST_OWNERS`` is a CLOSED allowlist, so an owner missing from it
    is refused the gate, the correction lane never carries, and the door
    measures silence with every daemon healthy. ``None`` keeps
    ``MEASUREMENT_GATE_OWNER``, the wizard's.

    Raises :class:`MeasurementDoorRefused` before yielding when the door cannot
    open, and leaves the speaker as it found it on every such path.
    """
    from jasper.measurement_window import (
        MEASUREMENT_GATE_OWNER,
        measurement_window,
    )

    from ..session_volume_plan import (
        DEFAULT_SESSION_VOLUME_STATE_PATH,
        SessionVolumeOpenResult,
        SessionVolumePlan,
        SessionVolumePlanError,
        live_measurement_session,
    )
    from ..staging import DEFAULT_CAMILLA_CONFIG_DIR

    state_path = (
        DEFAULT_SESSION_VOLUME_STATE_PATH
        if volume_state_path is None
        else Path(volume_state_path)
    )
    busy = live_measurement_session(state_path=state_path, action=action)
    if busy is not None:
        raise MeasurementDoorRefused(REFUSE_SESSION_LIVE, busy)

    owner, claim = _measurement_claim()
    volume_door = _volume_door(owner, camilla_factory, claim=claim)
    plan = SessionVolumePlan(state_path=state_path)
    if wall_clock_ceiling_s is not None:
        plan.set_wall_clock_ceiling_s(wall_clock_ceiling_s)
    graph = _session_graph(
        profile,
        camilla_factory=camilla_factory,
        config_dir=(
            DEFAULT_CAMILLA_CONFIG_DIR if config_dir is None else config_dir
        ),
    )

    # The window wraps the open, because the latch's first write is a fader
    # write like any other.
    async with measurement_window(
        gate_owner=MEASUREMENT_GATE_OWNER if gate_owner is None else gate_owner
    ):
        # A leftover ACTIVE record past its own wall-clock ceiling is a crashed
        # run, not a live one: `live_measurement_session` deliberately lets it
        # through, and `plan.open` then refuses over it. A no-op when nothing
        # is stale.
        await plan.enforce_ceiling(volume_door)
        body_error: BaseException | None = None
        volume_open = False
        try:
            try:
                opened = await plan.open(measurement_volume_db, volume_door)
            except SessionVolumePlanError as exc:
                raise MeasurementDoorRefused(
                    REFUSE_VOLUME_NOT_OPEN, str(exc)
                ) from exc
            if opened is not SessionVolumeOpenResult.OPENED:
                raise MeasurementDoorRefused(
                    REFUSE_VOLUME_NOT_OPEN,
                    f"the measurement volume did not confirm at "
                    f"{measurement_volume_db:.2f} dB ({opened.value}); the "
                    "speaker was put back",
                )
            volume_open = True
            fingerprint = await graph.install()
            log_event(
                logger,
                "active_speaker.measurement_door",
                action="open",
                fingerprint=fingerprint,
                measurement_volume_db=f"{measurement_volume_db:.2f}",
            )
            yield OpenMeasurementDoor(
                graph=graph,
                claim=claim,
                plan=plan,
                measurement_volume_db=measurement_volume_db,
                graph_fingerprint=fingerprint,
                entry_scope_fingerprint=graph.entry_scope_fingerprint,
            )
        except BaseException as raised:  # noqa: BLE001 - CancelledError is the point
            # Held so the give-back can ATTACH its own failures to it rather
            # than raise over it. ``BaseException`` and not ``Exception``: a
            # ``CancelledError`` is the commonest arrival here and is not an
            # ``Exception``.
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
            await resilient_restore(
                _give_back(
                    graph, claim, plan, volume_door,
                    reason=(
                        "measurement_door_closed" if volume_open
                        else "measurement_door_open_failed"
                    ),
                    body_error=body_error,
                )
            )


async def _give_back(
    graph: Any,
    claim: Any,
    plan: Any,
    volume_door: Any,
    *,
    reason: str,
    body_error: BaseException | None = None,
) -> None:
    """Graph, then claim, then the plan's snapshot — reverse order of taking.

    Every step runs even when an earlier one raises: a graph that will not come
    back must not strand the fader at measurement level. All three are
    idempotent and safe against nothing-held.

    ``body_error`` is the exception already in flight, if any. A cleanup failure
    is ATTACHED to it rather than raised over it, because an ``__aexit__`` that
    raised would demote the real cause to ``__context__`` and report the
    symptom. With nothing in flight a give-back failure IS the failure and
    propagates.
    """
    first: BaseException | None = None
    for step in (
        graph.restore,
        claim.release,
        lambda: plan.close(volume_door, reason=reason),
    ):
        try:
            await step()
        except BaseException as failure:  # noqa: BLE001 - see the docstring
            # EVERY step still runs: a graph that will not come back must not
            # stop the fader coming down. The FIRST failure is the one kept, as
            # it is the one nearest the cause.
            if first is None:
                first = failure
    if first is None:
        return
    if body_error is None:
        raise first
    if body_error.__context__ is None:
        body_error.__context__ = first


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


def _session_graph(
    profile: MeasurementGraphProfile,
    *,
    camilla_factory: Callable[[], Any],
    config_dir: str | Path,
) -> Any:
    """The session's one measurement graph, over the shared emit."""
    from jasper.dsp_apply import dsp_writer_lock

    from .composition import confirm_graph_is_live
    from .session_graph import MeasurementSessionGraph

    return MeasurementSessionGraph(
        emit=partial(emit_measurement_graph, profile),
        cam_factory=camilla_factory,
        writer_lock=lambda: dsp_writer_lock(
            str(config_dir), source="crossover_v2_session_graph"
        ),
        confirm_live=confirm_graph_is_live,
    )
