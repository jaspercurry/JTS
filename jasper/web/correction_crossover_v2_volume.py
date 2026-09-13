# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session volume, measurement pause, and recovery."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused


import asyncio
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


# Hold the exclusive measurement window between plays so voice reconciliation
# cannot change the admitted volume. Register each play as the abort target.
_session_pause_cm: Any = None
_session_abort_target: Any = None


async def acquire_session_measurement_pause() -> None:
    """Enter (once) the coordinator measurement window for the whole session.

    Idempotent: if the session already holds it, this is a no-op so a spurious
    second acquire cannot open a second exclusive window. Raises
    ``MeasurementWindowError`` if the window cannot be opened (e.g. a live voice
    session) — the caller surfaces that as a session-open failure.
    """
    global _session_pause_cm, _session_abort_target
    if _session_pause_cm is not None:
        return
    from jasper.measurement_window import (
        MeasurementAbortTarget,
        measurement_window,
    )

    target = MeasurementAbortTarget()
    cm = measurement_window(abort_target=target)
    await cm.__aenter__()
    _session_pause_cm = cm
    _session_abort_target = target
    log_event(logger, "correction.crossover_v2_measurement_pause", action="acquire")


async def release_session_measurement_pause() -> None:
    """Exit the held session measurement window (idempotent).

    Every drain path (close / abandon / ceiling / unresolved-recover) calls
    this; a drain that runs when nothing is held (already released, or a
    crash-fresh process that never entered it) is a safe no-op — never a
    double-release.
    """
    global _session_pause_cm, _session_abort_target
    cm = _session_pause_cm
    if cm is None:
        return
    _session_pause_cm = None
    _session_abort_target = None
    await cm.__aexit__(None, None, None)
    log_event(logger, "correction.crossover_v2_measurement_pause", action="release")


def session_measurement_pause_held() -> bool:
    """True while the session holds the one measurement window (per-play skip)."""
    return _session_pause_cm is not None


def reset_session_measurement_pause_for_tests() -> None:
    """Test seam: drop the held-window reference without an ``__aexit__``."""
    global _session_pause_cm, _session_abort_target
    _session_pause_cm = None
    _session_abort_target = None


async def _play_under_session_pause(play_body: Callable[[], Any]) -> None:
    """Run one play under the session-held window, abort-target registered.

    Before Finding C the per-play window's ENTERING task was the play task, so
    the coordinator's isolation-loss abort (cancel the entering task) stopped
    the sweep. The held window's entering task is the session runner, whose
    cancel would not stop an in-flight play — so the play task registers
    itself as the abort target while playing. A latched abort (gate-lease
    renew failure between plays) refuses the next play with a NAMED error, and
    a cancel that lands mid-play surfaces as the same named error, so the
    runner's cleanup arm persists an honest failure either way.
    """
    from jasper.measurement_window import MeasurementWindowError

    target = _session_abort_target
    if target is not None and target.failed:
        raise MeasurementWindowError(
            "measurement isolation was lost (the music-isolation gate lease "
            "could not be renewed); restart the measurement session"
        )
    task = asyncio.current_task()
    if target is not None and task is not None:
        target.register(task)
    try:
        await play_body()
    except asyncio.CancelledError:
        if target is not None and target.failed:
            # The coordinator aborted THIS play on isolation loss — surface a
            # named terminal error (not a bare cancellation) so the session
            # runner's cleanup arm persists it and tells the phone.
            raise MeasurementWindowError(
                "measurement isolation was lost mid-play; playback was "
                "stopped before household music could re-enter the mix"
            ) from None
        raise
    finally:
        if target is not None:
            target.clear()


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
    ``None`` for the three OUT-OF-RUNNER drains — ceiling enforcement,
    unresolved recovery and the new-session reconcile — which run when no
    session exists and therefore have no claim to establish through. They need
    the restore leg only, and a door that cannot establish says so rather than
    pretending.

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


def _session_measurement_claim_held() -> bool:
    """Does a measurement session still own the fader, in this process?

    The ``VolumeOwner`` is process-global and already knows, so nothing here
    needs a handle, a session object, or any knowledge of the graph. No owner
    means no claim and therefore no session: releasing is correct, not a
    fail-open, because a measurement session cannot have run without one.
    """
    from jasper.volume_owner import ClaimKind, volume_owner

    owner = volume_owner()
    if owner is None:
        return False
    return owner.holds_kind(ClaimKind.SESSION_MEASUREMENT)


def _release_pause_best_effort(run_async: Any) -> None:
    """Release the measurement pause for a drain that runs OUTSIDE the runner
    (recover / ceiling / new-session reconcile).

    **THE contract for all three drains, stated once, here.** Voice/mux
    isolation is freed only when no ``SESSION_MEASUREMENT`` claim is held —
    the owner's knowledge, never a restore OUTCOME. An outcome cannot tell
    "the session is finished with its isolation" from "deferred", "the drain
    raised", or "landed by coincidence": a household level that happens to
    equal the measurement level answers ``LANDED`` under a live claim (both
    default to ``MEASUREMENT_REFERENCE_VOLUME_DB`` on a box that never ran
    seat-SPL), and a raising drain answers nothing at all. Gating on the claim
    makes every one of those hold the pause.

    Why the claim and not the graph: the graph is the session's, and the web
    layer having its own handle on it is the process global this wave deleted.
    The claim is the owner's, the owner is already process-wide, and a live
    claim is the honest proxy for "a session is still measuring".

    Gating on a claim cannot strand the pause: ``TuningSession._give_back_held``
    releases the claim in the ``finally`` around the graph restore, so even a
    graph that will not come back still gives the claim up, and the next drain
    frees the isolation.

    Idempotent: a drain that runs when nothing is held — a session that never
    opened, a crash-fresh process, a second drain — is a safe no-op.
    """
    if _session_measurement_claim_held():
        log_event(
            logger,
            "correction.crossover_v2_pause_release_withheld",
            level=logging.INFO,
        )
        return
    try:
        run_async(
            release_session_measurement_pause(),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        logger.warning("v2 session measurement-pause release failed", exc_info=True)


def enforce_session_volume_ceiling_if_stale(
    run_async: Any, camilla_factory: Any
) -> bool:
    """Lazy wall-clock-ceiling enforcement (W6.1 — ``enforce_ceiling`` had zero
    callers, so the 1800 s ceiling never existed at runtime).

    Invoked on envelope build (on read) and at session open. Cheap on the happy
    path: ``stale_active`` is an in-memory check, so a healthy session pays
    nothing; only a session that has outlived
    ``DEFAULT_WALL_CLOCK_CEILING_S`` is force-drained here, restoring the
    household volume and releasing any held measurement pause. v2-only. Returns
    True iff a stale session was drained.

    **A LIVE session's claim outranks this drain, and that is not a failure.**
    This runs on the request thread while a ``TuningSession`` may still hold
    the fader — the slow-but-alive positioner this exists for. The owner then
    RECORDS the household level behind that claim and lands it on release, so
    the drain answers ``DEFERRED``: nothing is latched and no recovery is
    offered. The caller's gate still hears that the ceiling expired.

    The measurement pause is NOT this arm's to reason about — a raising drain
    reaches the release with no outcome at all. :func:`_release_pause_best_effort`
    owns that contract for all three drains.
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
    _release_pause_best_effort(run_async)
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
    unresolved state — instead of the legacy lease, and releases any held
    measurement pause on success.

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
    if succeeded:
        _release_pause_best_effort(run_async)
    return succeeded, getattr(result, "value", str(result))


def reconcile_session_volume_for_new_session(
    run_async: Any, camilla_factory: Any
) -> None:
    """Drain any residual session volume before a fresh session opens (W6.1 E1).

    A stale-active is force-drained by the ceiling; a residual owned-active
    leftover from a prior failed session in THIS process (``open`` refuses over
    any non-``None`` state) is drained too, so ``plan.open`` starts clean rather
    than raising ``SessionVolumePlanError`` into the silent
    200→adapter_failed loop observed live (run 2's retry). A latched
    ``unresolved`` / crash-hydrated ``needs_recovery`` state is NOT drained here
    — the caller's ``needs_recovery`` gate refuses it toward the recover-volume
    screen.

    **A live session's claim defers this drain rather than failing it.** The
    household level is recorded behind that claim and lands on release, so
    nothing latches and the caller's ``needs_recovery`` gate does not send a
    household that simply opened a second session toward the recovery screen.
    The pause is :func:`_release_pause_best_effort`'s call, not this one's:
    this arm reaches it with no outcome whenever ``abandon`` raises.
    """
    plan = session_volume_plan()
    enforce_session_volume_ceiling_if_stale(run_async, camilla_factory)
    if plan.measurement_volume_db is None or plan.needs_recovery:
        return
    try:
        reconciled = run_async(
            plan.abandon(
                _volume_door(camilla_factory), reason="stale_session_reset",
            ),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
        if reconciled is SessionVolumeRestoreResult.DEFERRED:
            # A live session still holds the fader. Its level is recorded and
            # lands on release; nothing is latched, so the caller's
            # ``needs_recovery`` gate does not send a household that simply
            # opened a second session toward the recovery screen.
            log_event(
                logger, "correction.crossover_v2_stale_session_reset_deferred",
            )
            return
        log_event(logger, "correction.crossover_v2_stale_session_reset")
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_stale_session_reset_failed",
            level=logging.ERROR,
        )
    _release_pause_best_effort(run_async)
