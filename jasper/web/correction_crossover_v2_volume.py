# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session volume and recovery."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused


import concurrent.futures
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.active_speaker.session_volume_plan import VolumeDoor

logger = logging.getLogger(__name__)

_volume_plan_lock = threading.Lock()
_volume_plan: Any = None


# --------------------------------------------------------------------------- #
# session volume plan singleton (§5.5)
# --------------------------------------------------------------------------- #


def session_volume_plan() -> Any:
    """The one durable-state-backed SessionVolumePlan this process owns."""
    global _volume_plan
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_SESSION_VOLUME_STATE_PATH,
        SessionVolumePlan,
    )

    with _volume_plan_lock:
        if _volume_plan is None:
            _volume_plan = SessionVolumePlan(
                state_path=DEFAULT_SESSION_VOLUME_STATE_PATH
            )
        return _volume_plan


def set_volume_plan_for_tests(plan: Any) -> None:
    global _volume_plan
    with _volume_plan_lock:
        _volume_plan = plan


# CamillaDSP set-and-confirm takes a few RPCs; bound each recovery drain.
_SESSION_VOLUME_DRAIN_TIMEOUT_S = 15.0


def _session_volume_read(camilla_factory: Any) -> Callable[[], Any]:
    """The main-volume READER, fail-closed on CamillaUnavailable.

    **The write half is gone, and its absence is the point of W5-c1.** This
    factory returned a ``(set, get)`` pair, and that ``_set`` was the one named
    exception to ``VolumeOwner`` owning this fader — it called
    ``CamillaController.set_volume_db`` directly, with no coordinator and no
    arbitration. Every writer that consumed it now goes through the owner, so
    there is nothing left for it to serve and it is deleted rather than left
    for the next caller to find.

    Reads never were the exception, and both survivors are reads: the
    capture-time hold, and :func:`_volume_door`'s physical snapshot.
    """
    from jasper.camilla import CamillaUnavailable

    async def _get() -> float | None:
        try:
            return await camilla_factory().get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    return _get


def _refuse_without_a_volume_owner(where: str) -> "CrossoverV2Refused":
    """The one refusal for a process with no fader owner, household copy and all.

    ``jasper.web.__main__`` installs an owner before serving, so a process
    without one is a REGISTRATION defect rather than a shape this module
    supports — there is no second authority to fall back to, and minting one
    would be the arbitration failure the owner exists to delete.

    Raised rather than answered with a door that quietly fails every verb: a
    door like that is a guard against a hypothetical, and the paths that would
    reach it already treat a raise as a failed drain. The household gets
    registry copy from here, because the wizard's 500 arm renders an
    unmapped exception's own string and internals are not household copy.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_INTERNAL_ERROR,
        REASON_REGISTRY,
    )

    log_event(
        logger,
        "correction.crossover_v2_volume_owner_absent",
        level=logging.CRITICAL,
        where=where,
    )
    return CrossoverV2Refused(
        REASON_REGISTRY[REASON_INTERNAL_ERROR].message,
        code=REASON_INTERNAL_ERROR,
    )


def _volume_door(
    camilla_factory: Any, *, claim: Any = None, reason: str = "drain",
) -> "VolumeDoor":
    """The plan's one door for every path in this module that drains or opens.

    ONE builder, which is the point: this module's fader authority is
    :class:`~jasper.volume_owner.VolumeOwner`, and every caller asks for its
    door here rather than assembling one, so the binding is a single fact.

    ``claim`` is the session's :class:`~jasper.active_speaker.crossover_v2.
    volume_claim.MeasurementVolumeClaim` when a session is opening, and
    ``None`` for the two OUT-OF-RUNNER drains — ceiling enforcement and
    unresolved recovery — which run when no session exists and therefore have
    no claim to establish through. They need the restore leg only, and a door
    that cannot establish says so rather than pretending.

    **The capture-time hold keeps a RAW getter and must not be moved onto this
    door.** Not because it happens not to write: because
    ``hold_measurement_volume`` is the #2925 tripwire, and a tripwire has to
    read the PHYSICAL fader. This door's own read is physical for exactly that
    reason — see ``OwnerVolumeDoor.read_household_level_db`` — but routing the
    hold through the plan's door would still be wrong, because the hold asks
    its question per stimulus against the level the PLAN declares, not against
    a household level.
    """
    from jasper.active_speaker.crossover_v2.volume_claim import OwnerVolumeDoor
    from jasper.volume_owner import volume_owner

    owner = volume_owner()
    if owner is None:
        raise _refuse_without_a_volume_owner(reason)
    return OwnerVolumeDoor(
        owner, read_fader=_session_volume_read(camilla_factory), claim=claim,
    )


def enforce_session_volume_ceiling_if_stale(
    run_async: Any, camilla_factory: Any
) -> bool:
    """Lazy wall-clock-ceiling enforcement (W6.1 — ``enforce_ceiling`` had zero
    callers, so the 1800 s ceiling never existed at runtime).

    Invoked on the crossover status and envelope reads. Cheap on the happy
    path: ``stale_active`` is an in-memory check, so a healthy session pays
    nothing; only a session that has outlived
    ``DEFAULT_WALL_CLOCK_CEILING_S`` is force-drained here, restoring the
    household volume. v2-only. Returns True iff a stale session was drained.

    **A LIVE session's claim outranks this drain, and that is not a failure.**
    This runs on the request thread while a ``TuningSession`` may still hold
    the fader — the slow-but-alive positioner this exists for. The owner then
    RECORDS the household level behind that claim and lands it on release, so
    the drain answers ``DEFERRED``: nothing is latched and no recovery is
    offered. The caller's gate still hears that the ceiling expired.
    """
    plan = session_volume_plan()
    try:
        if not plan.stale_active():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    result: Any = None
    try:
        result = run_async(
            plan.enforce_ceiling(_volume_door(camilla_factory)),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_ceiling_enforce_failed",
            level=logging.ERROR,
        )
    if result is SessionVolumeRestoreResult.DEFERRED:
        log_event(
            logger,
            "correction.crossover_v2_ceiling_enforce_deferred",
            level=logging.INFO,
        )
    return True


def v2_volume_recovery_active() -> bool:
    """True when the v2 session-volume plan holds a state the recover-volume
    endpoint must drain (unresolved, or a crash-hydrated active plan). The
    legacy-lease path 409s these because they live on the v2 plan, not the
    lease — the observed ``crossover_volume_recovery_not_required`` bug."""
    try:
        return bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        return True  # fail-closed: an unreadable state still offers recovery


RECOVERY_DEFERRED = SessionVolumeRestoreResult.DEFERRED.value


def recover_session_volume(
    run_async: Any, camilla_factory: Any
) -> tuple[bool, str]:
    """Drain the v2 plan's unresolved / stale-active state (the volume_recovery
    screen's ``recover_volume`` action). Returns ``(succeeded, result_value)``.

    Routes to ``SessionVolumePlan.recover_unresolved`` — the v2 owner of the
    unresolved state — instead of the legacy lease.

    **A deferral is not a recovery.** A live session's claim outranks this
    drain, so the household level is recorded rather than restored; telling
    the household "recovered" would name an event that has not happened yet.
    ``DEFERRED`` therefore reports failure here, and the caller's copy says
    what is actually true — the restore lands when that session finishes.
    """
    plan = session_volume_plan()
    try:
        result = run_async(
            plan.recover_unresolved(_volume_door(camilla_factory)),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except concurrent.futures.TimeoutError:
        log_event(
            logger,
            "correction.crossover_v2_volume_recovery_timeout",
            level=logging.ERROR,
        )
        result = SessionVolumeRestoreResult.FAILED
    succeeded = result not in (
        SessionVolumeRestoreResult.FAILED,
        SessionVolumeRestoreResult.DEFERRED,
    )
    return succeeded, getattr(result, "value", str(result))
