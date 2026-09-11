# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement daemon's capture and microphone state.

The layer :mod:`jasper.web.correction_handlers` route bodies and
:mod:`jasper.web.correction_setup`'s request handler both call: the capture
slot and its stop/position/retake signals, and the household microphone
readers. The loop bridge it schedules onto lives one layer down,
in :mod:`jasper.web.correction_runtime`.

Split out of ``correction_setup``; it imports nothing from its two callers,
which is what keeps the modules acyclic.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from contextlib import (
    AbstractContextManager,
    ExitStack,
)
from dataclasses import dataclass
from typing import Any


from ..audio_measurement import household_mic
from ..active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from ..log_event import log_event

from ._common import refusal_envelope
from . import correction_runtime
from .correction_runtime import logger


# Guards the capture slot below. Lazy work only.
# ORDERING: never hold this lock across a correction_runtime bridge call
# (ensure_loop / run_async) — loop work takes this lock too, so the bridge
# would stall behind the holder.
_session_lock = threading.Lock()

# The measurement capture in flight, surfaced in /status, or None. Claimed by
# the route that opens a session and updated by its background runner. Guarded
# by _session_lock (same single-session scope).
_capture_slot: dict[str, Any] | None = None
_pending_capture: tuple[CaptureKind, Callable[[str], AbstractContextManager[Any]]] | None = None
_capture_stop_request: Callable[[], None] | None = None
# The active session's position gate, or None — set for a GATED round (the
# remote commission tier, and a hand-walked round).
# Same lifecycle as ``_capture_stop_request``: set when the slot is claimed,
# dropped the moment the slot leaves an in-flight status — which is what stops a
# finished session from still advertising a position it is waiting for, and
# stops a late driver POST from releasing a gate nobody is holding.
_capture_position_gate: Any | None = None
# The active session's all-spots-measured signal, or None — set by the
# session's driver/wizard POST. Same claimed-with-the-slot,
# dropped-when-not-in-flight lifecycle as the two above.
_capture_complete_request: Callable[[], None] | None = None
# The active session's per-take RETAKE signal, or None. Same
# claimed-with-the-slot, dropped-when-not-in-flight lifecycle as the three
# above, which is what stops a POST arriving after the walk from re-opening a
# slot nothing is holding.
_capture_retake_request: Callable[[], None] | None = None
_CAPTURE_STOPPABLE_STATUSES = frozenset({"starting", "awaiting_capture"})
_CAPTURE_IN_FLIGHT_STATUSES = _CAPTURE_STOPPABLE_STATUSES | {"stopping"}


def _set_capture_slot(value: dict[str, Any] | None) -> None:
    global _capture_slot, _capture_stop_request, _capture_position_gate
    global _capture_complete_request, _capture_retake_request
    with _session_lock:
        if value is not None and _capture_position_gate is not None:
            value = {**value, "run": _capture_position_gate.published().get("run")}
        _capture_slot = value
        if value is None or value.get("status") not in _CAPTURE_IN_FLIGHT_STATUSES:
            _capture_stop_request = None
            _capture_position_gate = None
            _capture_complete_request = None
            _capture_retake_request = None


def _get_capture_slot() -> dict[str, Any] | None:
    with _session_lock:
        return dict(_capture_slot) if _capture_slot else None


def _get_capture_slot_for(kind_prefix: str) -> dict[str, Any] | None:
    """Return capture state only to the flow that owns it.

    The process has one hardware-safe capture slot; a page must never render
    another flow's waiting state.
    """
    capture = _get_capture_slot()
    if capture is None:
        with _session_lock:
            pending = _pending_capture
        if pending is not None and pending[0].label.startswith(kind_prefix):
            return _pending_payload(pending[0])
        return None
    if not str(capture.get("kind") or "").startswith(kind_prefix):
        return None
    # A gated session's live position hold AND the entry its last grant is
    # executing, merged in here rather than pushed into the slot by the gate:
    # the gate owns both facts and hands them over under one lock, so there is
    # one writer and no window in which the slot advertises a hold the gate has
    # already released or pairs it with an entry already executing.
    #
    # THREE guards keep a hold from outliving its session, and none of them is
    # the envelope: ``_set_capture_slot`` drops ``_capture_position_gate`` as
    # soon as the slot leaves an in-flight status; the in-flight test below
    # re-checks that on every read; and the gate clears its own ``_pending`` on
    # both exits from a hold. A finished session therefore reports no hold even
    # if its gate object is still referenced somewhere.
    with _session_lock:
        gate = _capture_position_gate
    if gate is not None and capture.get("status") in _CAPTURE_IN_FLIGHT_STATUSES:
        try:
            published = gate.published()
        except (OSError, RuntimeError, ValueError):
            logger.warning("could not read the position gate", exc_info=True)
            published = {}
        if published.get("run"):
            capture["run"] = published["run"]
        for key in ("pending", "current"):
            if published.get(key):
                capture[f"position_{key}"] = published[key]
    return capture


def _enforce_session_volume_ceiling(v2host: Any) -> None:
    """Lazy wall-clock-ceiling enforcement, and the one place a live position
    gate learns the walk outlived its ceiling (issue #2506).

    The enforcement itself is unchanged and cheap on the happy path: an
    in-memory ``stale_active`` check, then a force-drain of a session volume
    that outlived the ceiling its stage armed. What is added is telling the
    session's :class:`~.correction_crossover_v2.PositionGate`, when there is
    one, so a hold blocking on a slow-but-alive positioner ends by NAME
    (``session_ceiling_expired``) rather than as an anonymous timeout.

    It has to be told rather than sample the plan itself: this call drains what
    it finds, so the plan stops reporting ``stale_active`` immediately after,
    and a gate sampling on its own 1.5 s re-post cadence would race that drain.
    Detection therefore has ONE owner, which is this call.
    """
    if not v2host.enforce_session_volume_ceiling_if_stale(
        correction_runtime.run_async, correction_runtime.camilla_controller
    ):
        return
    with _session_lock:
        gate = _capture_position_gate
    if gate is None:
        return
    try:
        gate.note_session_ceiling_expired()
    except (OSError, RuntimeError, ValueError):
        logger.warning("could not mark the position gate's ceiling", exc_info=True)


def _begin_capture_slot(
    kind_label: str,
    *,
    request_stop: Callable[[], None] | None = None,
    position_gate: Any | None = None,
    request_complete: Callable[[], None] | None = None,
    request_retake: Callable[[], None] | None = None,
) -> bool:
    """Atomically claim the single capture slot. Returns False if one is
    already in flight (so a double-tap can't spawn two sessions + a file
    race for one position — mirrors /autolevel's "already in progress" guard).
    The slot is released by `_set_capture_slot(None)` on a failed open, or by the
    background runner setting `complete`/`failed`."""
    global _capture_slot, _capture_stop_request, _capture_position_gate
    global _capture_complete_request, _capture_retake_request
    with _session_lock:
        if (
            _capture_slot
            and _capture_slot.get("status") in _CAPTURE_IN_FLIGHT_STATUSES
        ):
            return False
        _capture_slot = {"status": "starting", "kind": kind_label}
        _capture_stop_request = request_stop
        _capture_position_gate = position_gate
        _capture_complete_request = request_complete
        _capture_retake_request = request_retake
        return True


def _publish_capture_waiting(kind_label: str) -> dict[str, Any]:
    """Open the capture window without overwriting a concurrent Stop."""

    global _capture_slot
    with _session_lock:
        capture = _capture_slot
        if (
            capture is None
            or capture.get("kind") != kind_label
            or capture.get("status") not in {"starting", "stopping"}
        ):
            raise RuntimeError("capture ownership changed while the session opened")
        status = "awaiting_capture" if capture.get("status") == "starting" else "stopping"
        _capture_slot = {**capture, "status": status}
        return dict(_capture_slot)


def _request_capture_stop(kind_prefix: str) -> dict[str, Any]:
    """Signal the active matching capture owner and expose Stop as in progress.

    The owner publishes ``stopped`` only after its capture worker, audio
    player, and rollback have all drained. Keeping ``stopping`` in the global
    slot prevents a second run from entering during cleanup.
    """

    global _capture_slot, _pending_capture
    with _session_lock:
        if _pending_capture is not None and _pending_capture[0].label.startswith(kind_prefix):
            kind, _ = _pending_capture
            _pending_capture = None
            stopped = {"status": "stopped", "kind": kind.label}
            if not _capture_slot or _capture_slot.get("status") not in _CAPTURE_IN_FLIGHT_STATUSES:
                _capture_slot = stopped
            return stopped
        capture = _capture_slot
        if capture is None or capture.get("status") not in _CAPTURE_STOPPABLE_STATUSES:
            raise ValueError("no matching capture is running")
        if not str(capture.get("kind") or "").startswith(kind_prefix):
            raise ValueError("no matching capture is running")
        callback = _capture_stop_request
        if callback is None:
            raise RuntimeError("this capture cannot be stopped safely")
        try:
            # Request callbacks are deliberately non-blocking signals. Fire
            # one under the same lock as the public state so another tab can
            # never observe ``stopping`` before the owner is actually signaled.
            callback()
        except (OSError, RuntimeError, ValueError) as exc:
            _capture_slot = {
                **capture,
                "status": "failed",
                "error": "the measurement stop signal failed",
            }
            raise RuntimeError("the measurement stop signal failed") from exc
        _capture_slot = {**capture, "status": "stopping"}
        return dict(_capture_slot)


@dataclass(frozen=True)
class CaptureKind:
    """Per-flow plug for the generic capture orchestrator (`_run_capture`).

    Each measurement flow injects only what is flow-specific — how to mint its
    capture session, and how to run it + consume the recorded WAV (play its
    stimulus, then analyze). The orchestrator owns everything common: the
    single-slot re-entrancy guard, the `/status.capture` holder, and the
    background-task lifecycle. Adding a kind is a descriptor, not a second copy
    of the handler.

    ``open()`` mints the kind's session; ``run_and_consume(pi_session)`` walks
    it and feeds each recorded WAV to the kind's analysis seam.
    """

    label: str
    open: Callable[[], Any]
    run_and_consume: Callable[[Any], Awaitable[None]]
    request_stop: Callable[[], None] | None = None
    #: A gated session's position gate, or None — the remote tier's, or a
    #: hand-walked round's (#2879). Only the crossover v2 kinds ever set it;
    #: every other flow leaves it unset and is untouched.
    position_gate: Any | None = None
    #: The session's all-spots-measured signal, or None. Routed to
    #: POST /crossover/v2/complete via the slot, with the same lifecycle
    #: as ``request_stop``.
    request_complete: Callable[[], None] | None = None
    #: The session's per-take retake signal, or None. Routed to
    #: POST /crossover/v2/retake via the slot, same lifecycle again.
    request_retake: Callable[[], None] | None = None
    session_id: str = ""
    join_entry: Any = None


def _pending_payload(kind: CaptureKind) -> dict[str, Any]:
    return {"status": "awaiting_join", "kind": kind.label, "session_id": kind.session_id,
            "url": "/sound/speaker/crossover/", "first_prompt": dict(kind.join_entry.screen),
            "join": kind.position_gate.invitation(kind.join_entry) if kind.position_gate else None,
            "index": 1, "attempt": 1}


def _stage_capture(kind: CaptureKind, *, idle_hold: Callable[[str], AbstractContextManager[Any]]) -> dict[str, Any]:
    global _pending_capture, _capture_slot
    with _session_lock:
        _pending_capture = (kind, idle_hold)
        if _capture_slot and _capture_slot.get("status") not in _CAPTURE_IN_FLIGHT_STATUSES:
            _capture_slot = None
    return _pending_payload(kind)


def _join_capture(index: int, attempt: int) -> dict[str, Any] | None:
    global _pending_capture
    with _session_lock:
        pending = _pending_capture
        if pending is None:
            return None
        if (index, attempt) != (1, 1):
            raise ValueError("The first placement must name index 1 and attempt 1")
        _pending_capture = None
    kind, idle_hold = pending
    try:
        return _run_capture(kind, idle_hold=idle_hold)
    except Exception as exc:  # noqa: BLE001 - publish admission faults, then propagate
        if kind.position_gate is not None:
            envelope = refusal_envelope(exc)
            if not envelope["code"]:
                envelope = refusal_envelope(code="internal_error")
            kind.position_gate.abandon_hold()
            kind.position_gate.publish({"status": "failed", "fault": envelope["code"],
                                        "next_action": envelope["next_action"]})
            capture = _get_capture_slot()
            if capture and capture.get("status") == "failed" and capture.get("kind") == kind.label:
                _set_capture_slot({**capture, **envelope, "run": kind.position_gate.published()["run"]})
        raise


def _run_capture(
    kind: CaptureKind,
    *,
    idle_hold: Callable[[str], AbstractContextManager[Any]],
) -> dict[str, Any]:
    """Own the common capture lifecycle for any kind. The caller has already run
    the kind's own state/calibration prechecks; this claims the slot, mints the
    session, and spawns the background runner.

    ``idle_hold`` — REQUIRED, no default. This function's job is spawning work
    that outlives its caller's HTTP request, and the socket-activated process
    `os._exit(0)`s after ~600 s with nothing inbound. On 2026-07-29 (JTS3,
    issue #1854) that killed a crossover-v2 session mid-verify, because the
    wizard saw no inbound traffic for the whole measurement. Whether this
    kind's runner needs the process kept alive is a decision each call site
    owns and states:

    * pass the process's real hold (``systemd.IdleShutdownTracker.hold``, from
      ``main`` through the handler cfg) when the runner must survive an idle
      window — long walks, anything whose only traffic is outbound;
    * pass ``systemd.no_hold`` when it must not, or need not.

    A real hold is taken here, on the request thread BEFORE the runner is
    scheduled, and released in the runner's own ``finally``, so no window
    exists in either direction."""
    if not _begin_capture_slot(
        kind.label,
        request_stop=kind.request_stop,
        position_gate=kind.position_gate,
        request_complete=kind.request_complete,
        request_retake=kind.request_retake,
    ):
        # Name the ACTUAL holder when it is still readable. A race between
        # this read and the failed claim above can only widen to the generic
        # wording, never misreport which measurement is in the way.
        holder = _get_capture_slot()
        held_by = str(holder.get("kind") or "") if holder else ""
        raise CrossoverV2Refused(
            (f"a capture ({held_by})" if held_by else "another capture")
            + " already holds the measurement slot; finish or cancel it"
            " before starting another", code="capture_slot_busy",
        )
    spawned = False
    session_hold = ExitStack()
    try:
        if kind.join_entry is not None:
            assert kind.position_gate is not None
            kind.position_gate.join(kind.join_entry)
        rc = kind.open()

        async def _run() -> None:
            from jasper.active_speaker.crossover_v2.capture_source import (
                CaptureStopped,
            )

            try:
                await kind.run_and_consume(rc.pi_session)
                capture = _get_capture_slot()
                if (
                    capture is not None
                    and capture.get("kind") == kind.label
                    and capture.get("status") == "stopping"
                ):
                    raise CaptureStopped("capture stopped")
                _set_capture_slot({"status": "complete", "kind": kind.label})
            except (asyncio.CancelledError, CaptureStopped):
                _set_capture_slot({
                    "status": "stopped",
                    "kind": kind.label,
                    "error": "Measurement stopped safely.",
                })
                log_event(
                    logger,
                    "correction.capture_stopped",
                    kind=kind.label,
                )
            except Exception as exc:  # noqa: BLE001 — surface loudly; never crash the loop
                log_event(
                    logger,
                    "correction.capture_failed",
                    level=logging.WARNING,
                    exc_info=True,
                    kind=kind.label,
                    reason=type(exc).__name__,
                )
                _set_capture_slot({
                    "status": "failed",
                    "kind": kind.label,
                    **refusal_envelope(exc),
                })
            finally:
                # Every terminal path — complete, stopped, failed, and any
                # raise out of the arms above — releases the idle-exit hold
                # here, so the wizard can idle out again the moment the
                # session is genuinely over.
                session_hold.close()

        waiting = _publish_capture_waiting(kind.label)
        session_hold.enter_context(idle_hold(f"capture:{kind.label}"))
        asyncio.run_coroutine_threadsafe(_run(), correction_runtime.ensure_loop())
        spawned = True
        return {"status": waiting["status"]}
    finally:
        if not spawned:
            session_hold.close()  # nothing will run to release it
            if kind.join_entry is None:
                _set_capture_slot(None)
            else:
                _set_capture_slot({"status": "failed", "kind": kind.label})


def _crossover_blocking_phase() -> str | None:
    """Return another active measurement phase that should block crossover."""

    from .active_speaker_flow import blocking_measurement_phase

    return blocking_measurement_phase()


def _default_setup_calibration_for_spec() -> Any | None:
    """Build the capture spec's OPTIONAL ``default_setup.calibration`` hint
    from the household's remembered mic.

    Never binding. The measurement source reads the hint and mints the
    capture's own ``setup.calibration`` reference from it when it is marked
    ``resolvable: true``. Any resolution miss yields no hint
    rather than blocking the capture.

    ``resolvable`` is a SECOND, freshly-taken resolver call — not inferred
    from ``found`` succeeding above — so the flag always reflects a
    just-checked fact rather than "resolved a moment ago, presumed still
    good." `resolve_household_mic_calibration` is itself documented
    fail-soft (returns `None`, never raises), so this stays a plain call: a
    miss here simply leaves `resolvable` at its `False` default, which
    `DefaultSetupCalibration.to_dict()` omits from the wire payload.
    """
    from jasper.active_speaker.crossover_v2.sweep_spec import (
        DefaultSetupCalibration,
    )
    from jasper.audio_measurement.calibration import (  # lazy: numpy
        configured_calibration_root,
    )

    found = household_mic.resolved_household_mic()
    if found is None:
        return None
    household, resolved = found
    mode = "upload" if household.provider == "manual_upload" else "serial"
    resolvable = (
        household_mic.resolve_household_mic_calibration(
            household, root=configured_calibration_root()
        )
        is not None
    )
    return DefaultSetupCalibration(
        mode=mode,
        model=household.model_key,
        serial_display=household.serial_display or "",
        calibration_id=resolved.calibration_id,
        resolvable=resolvable,
    )
