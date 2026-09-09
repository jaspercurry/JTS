# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-correction wizard: session, capture and microphone state.

The layer :mod:`jasper.web.correction_handlers` route bodies and
:mod:`jasper.web.correction_setup`'s request handler both call: the
measurement session and its lock, the single asyncio loop thread and the
`_run_async` bridge onto it, the capture slot and its stop/position/retake
signals, the volume and autolevel claims, and the household microphone /
calibration / readiness readers.

Split out of ``correction_setup`` unchanged; it imports nothing from its two
callers, which is what keeps the three modules acyclic.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import math
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import (
    AbstractContextManager,
    ExitStack,
)
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


from ..audio_measurement import household_mic
from ..log_event import log_event
from ..transition_log import TransitionLog

from ._common import (
    JsonBodyError,
    read_json_object,
)


#: One logger for this wizard's three modules (correction_setup,
#: correction_handlers, correction_capture) so every event= line keeps
#: the journal name operators already grep.
logger = logging.getLogger("jasper.web.correction_setup")

# When the writer boundary proceeds on an UNREADABLE receipt whose binding did
# not match (a disclosed fail-open), surface it once per transition rather than
# on every retried accept. Keyed by the banked-under binding. See ADR-0196.
_AUTHORITY_UNCONFIRMED_DISCLOSURE = TransitionLog(reminder_sec=3600.0)


# 48 kHz, EC=NS=AGC=false — pinned by the iOS verify step. The Phase 1
# sweep math assumes the captured signal is at this rate; on mismatch
# we refuse the upload rather than silently resampling (silent
# resampling would produce a working but wrong correction).
REQUIRED_SAMPLE_RATE = 48000
MAX_JSON_BODY_BYTES = 64 * 1024
MAX_CALIBRATION_UPLOAD_JSON_BYTES = 1024 * 1024
# Browser captures are mono 16-bit PCM at 48 kHz. A normal 10 s sweep
# upload is ~1 MB; 32 MB leaves generous room for measurement-window
# setup latency while still avoiding unbounded reads in the Pi web
# process.
MAX_WAV_BODY_BYTES = 32 * 1024 * 1024
MAX_SYNC_WAV_BODY_BYTES = 2 * 1024 * 1024
MAX_DEVICE_FIELD_CHARS = 160
_FOLLOWER_DELEGATED_PAGE_PATHS = frozenset({"/", "/sync"})


class BadRequest(ValueError):
    """Client supplied an invalid request body."""


class RequestConflict(RuntimeError):
    """Client request conflicts with the current correction session state."""


# Module-level session + bridge to the async loop. Lazy-init on
# first use so importing this module is cheap (lets `python -m
# jasper.web.correction_setup --help` work without spinning up a
# loop).
_session_lock = threading.Lock()
_session = None  # type: ignore[var-annotated]
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None

# The measurement capture in flight, surfaced in /status, or None. Claimed by
# the route that opens a session and updated by its background runner. Guarded
# by _session_lock (same single-session scope).
_capture_slot: dict[str, Any] | None = None
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
# Exact set/readback plus the emergency set/readback each use Camilla's bounded
# reconnect contract. Keep the HTTP owner alive for the complete sequence.
_CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S = 45.0
_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S = _CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S


#: The level-match session-measurement claim: taken by the ramp's first write,
#: moved by every write after it, re-asserted before each sweep, and released
#: by the restore that gives the household its level back.
#:
#: It OUTLIVES the request that took it for the same reason ``_AUTOLEVEL_CLAIM``
#: does — the domain forces the lifetime. The ramp locks a measurement level,
#: later sweeps play at it across separate requests, and only the restore ends
#: it. Module-scoped follows this file's own idiom (``_LEVEL_LEASE``,
#: ``session_volume_plan()``), and a process exit mid-journey strands the claim
#: exactly as it already stranded the fader — #3038's pre-existing class made
#: legible in the owner's ledger, not a new failure mode.
_LEVEL_MATCH_CLAIM: Any = None


async def _assert_level_match_level(db: float) -> bool:
    """Take the level-match claim, or MOVE it — never a second one.

    The ramp's first write acquires; every write after it, and every
    before-sweep re-assertion, relevels the held claim. Release-then-reacquire
    would settle to the household level between steps, which is the loud
    direction with a tone playing.

    Returns whether the level is established, because that is what
    ``ensure_level_match_volume`` and the ramp both already branch on. A
    refusal — including a ``VolumeClaimConflict`` from a measurement claim
    another journey still holds — is disclosed and answered ``False`` rather
    than raised, so the existing "could not establish" paths carry it.
    """

    global _LEVEL_MATCH_CLAIM
    from jasper.volume_owner import (
        ClaimKind,
        VolumeClaimRefused,
        volume_owner,
    )

    owner = volume_owner()
    if owner is None:
        log_event(
            logger,
            "correction.level_match_owner_absent",
            level=logging.CRITICAL,
        )
        return False
    try:
        if _LEVEL_MATCH_CLAIM is None:
            _LEVEL_MATCH_CLAIM = await owner.acquire_level(
                ClaimKind.SESSION_MEASUREMENT, float(db)
            )
        else:
            _LEVEL_MATCH_CLAIM = await owner.relevel(
                _LEVEL_MATCH_CLAIM, float(db)
            )
    except VolumeClaimRefused as exc:
        log_event(
            logger,
            "correction.level_match_level_refused",
            level=logging.ERROR,
            to_db=f"{float(db):.1f}",
            reason=type(exc).__name__,
        )
        return False
    return True


def _household_level_door() -> Any:
    """The owner's household-level door, for the level-match restores.

    Every level-match restore answers one question — *give the household its
    level back* — so they share one door rather than each binding a raw
    ``cam.set_volume_db`` with its own ``best_effort`` choice. The owner's
    doors carry that contract once (bound ``best_effort=True`` at
    registration), which is the actual win: the flag stops being a per-site
    decision that can drift.

    It is also where the level-match measurement claim ENDS. When the ramp's
    claim is still held — the ordinary case, since the level it locked is what
    the sweeps played at — the release and the re-declaration are ONE call, so
    the fader lands on the household level in a single write instead of
    stepping through whatever was declared before it.

    A missing owner is a registration defect — ``web/__main__.main`` installs
    one before serving — so this discloses at CRITICAL and hands back a door
    that reports "not in effect" rather than minting a second owner. Every
    caller already treats that answer as a failed restore, so the existing
    disclosure path carries it.
    """

    from jasper.volume_owner import volume_owner

    owner = volume_owner()
    if owner is not None:

        async def _return_household(db: float) -> bool:
            global _LEVEL_MATCH_CLAIM
            claim, _LEVEL_MATCH_CLAIM = _LEVEL_MATCH_CLAIM, None
            if claim is not None:
                await owner.release(claim, household_level_db=float(db))
                return True
            return await owner.declare_household_level_db(float(db))

        return _return_household

    async def _no_owner(_db: float) -> bool:
        log_event(
            logger,
            "correction.level_match_restore_owner_absent",
            level=logging.CRITICAL,
        )
        return False

    return _no_owner
















def _crossover_volume_safety_refusal() -> dict[str, str]:
    return {
        "status": "refused",
        "reason": "crossover_volume_safety_unresolved",
        "next_step": (
            "Use Recover safe listening volume before another crossover action."
        ),
    }


def _set_capture_slot(value: dict[str, Any] | None) -> None:
    global _capture_slot, _capture_stop_request, _capture_position_gate
    global _capture_complete_request, _capture_retake_request
    with _session_lock:
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
        return None
    if not str(capture.get("kind") or "").startswith(kind_prefix):
        return None
    # A gated session's live position hold, merged in here rather than pushed
    # into the slot by the gate: the gate owns the fact and this is a read, so
    # there is one writer and no window in which the slot advertises a hold the
    # gate has already released.
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
            pending = gate.pending()
        except (OSError, RuntimeError, ValueError):
            logger.warning("could not read the position gate", exc_info=True)
            pending = None
        if pending:
            capture["position_pending"] = pending
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
    if not v2host.enforce_session_volume_ceiling_if_stale(_run_async, _camilla):
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

    global _capture_slot
    with _session_lock:
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








def _capture_failure_message(exc: BaseException) -> str:
    """The household-facing text for a capture-lifecycle failure.

    ``CrossoverV2LocalSeamError`` (W6 hardware run 3 finding G) wraps a bare
    ``OSError`` raised by the v2 crossover's play/DSP seam -- e.g. the DSP
    writer lock's ``os.open`` hitting a read-only ``config_dir`` (finding F),
    which surfaced the raw
    ``"[Errno 30] Read-only file system: '/etc/camilladsp/.dsp_apply.lock'"``
    string on the wizard's status line via the generic ``str(exc)`` fallback
    below. Its household copy comes from the SAME
    ``REASON_REGISTRY[REASON_INTERNAL_ERROR]`` text the v2 envelope itself
    renders for an internal error, so the two surfaces never say different
    things about the same failure.

    The PROGRAM family -- ``ProgramPlaybackError`` (incl.
    ``ProgramPlaybackRefused``), ``ProgramAdmissionError``,
    ``CrossoverV2FlowError`` -- is the leak issue #1820 filed:
    ``ProgramPlaybackRefused``'s ``str(exc)``, built at its raise site by
    joining raw enum values
    (``"program re-admission refused: program_profile_not_confirmed"``),
    reached the wizard's status line verbatim -- violating
    ``crossover_v2_flow``'s own written contract that a bare reason code never
    reaches the household. It routes through
    ``jasper.web.correction_crossover_v2.classify_program_failure``, the SAME
    classifier the v2 session runner's cleanup arm uses to pick the failure
    screen, so both surfaces name the same refusal with the same sentence.

    The raw exception string still reaches the journal unchanged --
    ``event=correction.capture_failed`` logs with ``exc_info=True`` regardless
    of the mapped message. Every other exception falls back to ``str(exc)``.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_INTERNAL_ERROR,
        REASON_REGISTRY,
    )
    from jasper.web.correction_crossover_v2 import (
        CrossoverV2LocalSeamError,
        classify_program_failure,
    )

    if isinstance(exc, CrossoverV2LocalSeamError):
        return REASON_REGISTRY[REASON_INTERNAL_ERROR].message
    classified = classify_program_failure(exc)
    if classified is not None:
        return REASON_REGISTRY[classified[0]].message
    return str(exc)


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
        raise ValueError(
            (f"a capture ({held_by})" if held_by else "another capture")
            + " already holds the measurement slot; finish or cancel it"
            " before starting another"
        )
    spawned = False
    session_hold = ExitStack()
    try:
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
                # This outer net flips /status.capture to failed and carries the
                # household-facing reason (see _capture_failure_message) so the
                # status page can show why.
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
                    "error": _capture_failure_message(exc),
                })
            finally:
                # Every terminal path — complete, stopped, failed, and any
                # raise out of the arms above — releases the idle-exit hold
                # here, so the wizard can idle out again the moment the
                # session is genuinely over.
                session_hold.close()

        waiting = _publish_capture_waiting(kind.label)
        session_hold.enter_context(idle_hold(f"capture:{kind.label}"))
        asyncio.run_coroutine_threadsafe(_run(), _ensure_loop())
        spawned = True
        return {"status": waiting["status"]}
    finally:
        if not spawned:
            session_hold.close()  # nothing will run to release it
            _set_capture_slot(None)  # release the slot on any early failure




_start_in_progress = False

_ACTIVE_SESSION_STATES = frozenset({
    "needs_noise_capture",
    "preparing",
    "sweeping",
    "awaiting_capture",
    "needs_repeat_capture",
    "awaiting_repeat_capture",
    "needs_next_position",
    "analyzing",
    "verifying",
    "awaiting_verify_capture",
})
_BUNDLE_DELETE_BLOCKED_STATES = _ACTIVE_SESSION_STATES | {"ready"}


def _active_state_for_session(sess: Any | None) -> str | None:
    if sess is None:
        return None
    state = getattr(getattr(sess, "state", None), "value", None)
    return state if state in _ACTIVE_SESSION_STATES else None


def _correction_start_blocker() -> str | None:
    """Return the room-correction phase that blocks another measurement."""
    with _session_lock:
        if _start_in_progress:
            return "starting"
        return _active_state_for_session(_session)


def active_correction_phase() -> str | None:
    """Read-only: the active room-correction session state, or None.

    The counterpart to sync's ``active_phase()`` so another measurement
    flow (active-speaker commissioning) can exclude correction without the side
    effect of ``_reserve_start_slot`` (which reserves /start)."""
    with _session_lock:
        return _active_state_for_session(_session)


def _crossover_blocking_phase() -> str | None:
    """Return another active measurement phase that should block crossover."""

    from .active_speaker_flow import blocking_measurement_phase

    return blocking_measurement_phase()


def _reserve_start_slot() -> str | None:
    """Atomically reserve /start or return the state blocking it.

    The session state only becomes active once the background sweep task
    starts. This small reservation closes the gap between accepting
    `/start` and the new session visibly leaving IDLE.
    """
    global _start_in_progress
    # The pair-sync flow shares this process precisely so the measurement
    # surfaces can exclude each other here (both open measurement_window;
    # concurrent windows would interleave the renderer stop/start).
    # Active-speaker commissioning excludes the same way (it plays sweeps
    # through the production graph) but participates cooperatively rather
    # than holding a window — see active_speaker_flow.
    # Lazy imports: these modules never import this module back at import time.
    from .active_speaker_flow import active_phase as _active_speaker_phase
    from .sync_flow import active_phase as _sync_phase
    sync_active = _sync_phase()
    if sync_active is not None:
        return f"sync:{sync_active}"
    commissioning = _active_speaker_phase()
    if commissioning is not None:
        return f"active_speaker:{commissioning}"
    with _session_lock:
        if _start_in_progress:
            return "starting"
        active_state = _active_state_for_session(_session)
        if active_state is not None:
            return active_state
        _start_in_progress = True
        return None


def _clear_start_slot() -> None:
    global _start_in_progress
    with _session_lock:
        _start_in_progress = False


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start (or reuse) a single background asyncio loop. The HTTP
    handlers schedule coroutines onto it via
    `run_coroutine_threadsafe`."""
    global _loop, _loop_thread
    with _session_lock:
        if _loop is None or not _loop.is_running():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_loop.run_forever,
                name="jasper-correction-loop",
                daemon=True,
            )
            _loop_thread.start()
    return _loop


def _run_async(coro, *, timeout: float | None = 60.0):
    """Run a coroutine on the background loop and return its result.

    Long timeout default (60 s) covers sweep playback (10 s) + setup
    margin. Endpoints that should be fast (status / apply / reset)
    pass shorter timeouts.
    """
    drained = threading.Event()

    async def _tracked():
        try:
            return await coro
        finally:
            drained.set()

    fut = asyncio.run_coroutine_threadsafe(_tracked(), _ensure_loop())
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # A timed-out HTTP/poll thread no longer owns a useful result. Cancel
        # the loop task so delayed measurement audio cannot start after the
        # caller has already reported failure. Owning coroutines retain their
        # bounded/shielded rollback in ``finally`` blocks.
        fut.cancel()
        if not drained.wait(_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S):
            log_event(
                logger,
                "correction.async_cancel_drain_timeout",
                level=logging.CRITICAL,
                timeout_s=_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S,
            )
            # A terminal response must never release measurement ownership
            # while its graph/volume finalizer can still mutate the speaker.
            # The threshold above is an observability alarm, not permission to
            # abandon cleanup; fail closed until the owner actually drains.
            drained.wait()
        raise


def _run_graph_mutation(coro):
    """Wait for one Room-owned graph mutation to reach a terminal result.

    CamillaController bounds and drains each transport attempt. Shared writer-
    lock admission is currently blocking and remains a Shared-owned bounded-
    admission gap. Once admitted, adding a second outer deadline here could
    cancel between graph load and rollback/state persistence, so Room waits for
    the transaction's terminal result.
    """

    return _run_async(coro, timeout=None)


def _get_or_create_session():
    """Single global session. Reset by /reset (which transitions
    APPLIED → IDLE) or by an explicit /start (which creates a fresh
    one regardless of prior state)."""
    from jasper.correction.session import MeasurementSession
    global _session
    with _session_lock:
        if _session is None:
            _session = MeasurementSession()
        return _session


def _replace_session(
    *,
    total_positions: int,
    target_choice: str,
    strategy_choice: str,
    mic_calibration=None,
    input_device: dict[str, Any] | None = None,
    repeat_main_position: bool,
):
    """Replace the global session with a fresh one. Called by /start
    so the user can re-run measurements without restarting the
    daemon. Phase 2 takes total_positions + target_choice from the
    body so the new session is configured before its first sweep."""
    from jasper.correction.session import MeasurementSession
    global _session
    with _session_lock:
        _session = MeasurementSession(
            total_positions=total_positions,
            target_choice=target_choice,
            strategy_choice=strategy_choice,
            mic_calibration=mic_calibration,
            input_device=input_device,
            repeat_main_position=repeat_main_position,
        )
        return _session


def _read_json_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int = MAX_JSON_BODY_BYTES,
) -> dict[str, Any]:
    """Parse JSON body. Empty body → {}."""
    try:
        return read_json_object(handler, max_bytes=max_bytes)
    except JsonBodyError as exc:
        if exc.code == "invalid_content_length":
            raise BadRequest("invalid Content-Length") from exc
        raise BadRequest(str(exc)) from exc


def _camilla() -> "Any":
    """Construct a CamillaController against the configured host/port.
    Factored so tests can monkeypatch a single seam — and so the
    /start reset path doesn't drift from the /apply + /reset paths.
    """
    from jasper.camilla import primary_controller
    return primary_controller()


def _save_household_mic(record: Any, *, serial: str | None = None) -> None:
    """Persist a just-established calibration as the household's default
    measurement mic (``jasper.audio_measurement.household_mic``).

    Called from the two points a calibration is NEWLY established —
    ``_handle_calibration_fetch`` and ``_handle_calibration_upload`` below.
    Handlers that merely load an already-established ``calibration_id``
    WITHOUT the household saying so (``_handle_start``,
    ``_handle_local_capture_setup``) do not call this, and neither does a
    capture resolving the reference minted from this record: the household
    record only moves on a new success.

    Fail-soft: a write failure must never block the calibration that
    triggered it. A different mic than the currently-remembered one is
    never refused — the new success simply replaces the record (the
    cross-session staleness guard, item 6): logged as
    ``correction.household_mic_replaced`` rather than blocked.
    """
    path = household_mic.household_mic_path()
    try:
        new_record = household_mic.household_mic_from_calibration(record, serial=serial)
        previous = household_mic.read_household_mic(path=path)
        household_mic.write_household_mic(new_record, path=path)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "failed to persist household mic record: %r", exc, exc_info=True,
        )
        return
    # A replace is any change of mic IDENTITY: the model, or — within the
    # same model — a different physical unit (serial_hash). The hashes
    # themselves stay out of the log line (they are stable per-unit
    # identifiers; the event only needs to say WHAT kind of change
    # happened), so `changed=` is the minimal discriminator.
    changed: list[str] = []
    if previous is not None:
        if previous.model_key != new_record.model_key:
            changed.append("model")
        if previous.serial_hash != new_record.serial_hash:
            changed.append("serial")
    if previous is not None and changed:
        log_event(
            logger,
            "correction.household_mic_replaced",
            old_model=previous.model_key,
            new_model=new_record.model_key,
            changed="+".join(changed),
        )
    else:
        log_event(
            logger,
            "correction.household_mic_saved",
            model=new_record.model_key,
        )


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


def _household_mic_prefill_payload() -> dict[str, Any] | None:
    """Server-rendered prefill for the room wizard's local mic/calibration
    UI. ``None`` when there is no
    household default, or its calibration is no longer resolvable — the page
    then renders exactly as it did before this feature. Reuses
    ``_calibration_payload``'s shape (``{"calibration": ..., "preview":
    ...}``) so the page's existing `showCalibrationLoaded` renderer can
    consume it unmodified; `model_key` additionally selects the right
    `<option>` in the model picker.

    The crossover flow has no equivalent local UI — it reads the spec
    `default_setup` hint above instead.
    """
    found = household_mic.resolved_household_mic()
    if found is None:
        return None
    household, resolved = found
    return {"model_key": household.model_key, **_calibration_payload(resolved)}


def _short_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:MAX_DEVICE_FIELD_CHARS]


def _device_id_hash(value: Any) -> str | None:
    text = _short_text(value)
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _optional_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _runtime_integrity_summary(sess: Any) -> dict[str, Any] | None:
    report = getattr(sess, "runtime_integrity", None)
    if report is None or not hasattr(report, "summary"):
        return None
    try:
        return report.summary()
    except Exception:  # noqa: BLE001
        logger.debug("runtime_integrity summary unavailable", exc_info=True)
        return None


async def _run_session_background_audio(
    sess: Any,
    operation: Callable[[], Awaitable[None]],
) -> None:
    """Use the session-owned cancellable slot when the session provides it."""
    runner = getattr(sess, "run_background_audio_operation", None)
    if callable(runner):
        await runner(operation)
    else:
        await operation()


def _schedule_measurement_sweep(sess: Any, cam: Any, *, from_state: Any) -> None:
    """Start the next normal measurement sweep and wait for visible progress."""
    from jasper.correction import playback
    from jasper.measurement_window import measurement_window

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with measurement_window():
                await sess.prepare_and_play_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("measurement sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(
        _run_session_background_audio(sess, _run_sweep),
        _ensure_loop(),
    )
    _run_async(sess.state_changed_from(from_state), timeout=6.0)




def _schedule_repeat_sweep(sess: Any, cam: Any, *, from_state: Any) -> None:
    """Start the optional main-seat repeat sweep."""
    from jasper.correction import playback
    from jasper.measurement_window import measurement_window

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with measurement_window():
                await sess.prepare_and_play_repeat_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("repeat sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(
        _run_session_background_audio(sess, _run_sweep),
        _ensure_loop(),
    )
    _run_async(sess.state_changed_from(from_state), timeout=6.0)


def _sanitize_input_device(raw: Any) -> dict[str, Any] | None:
    """Normalize browser-reported input-device metadata before bundles.

    Browser `deviceId` values can be stable identifiers, so persist
    hashes rather than raw IDs. Labels are user-visible in the browser
    picker and useful for debugging, but still capped.
    """
    if not isinstance(raw, dict):
        return None
    source_channel_count = _optional_float(raw.get("source_channel_count"))
    captured_channel_count = _optional_float(
        raw.get("captured_channel_count")
    )
    sanitized = {
        "device_id_hash": _device_id_hash(raw.get("device_id")),
        "requested_device_id_hash": _device_id_hash(
            raw.get("requested_device_id"),
        ),
        "actual_device_id_hash": _device_id_hash(raw.get("actual_device_id")),
        "label": _short_text(raw.get("label")),
        "browser_label": _short_text(raw.get("browser_label")),
        "sample_rate": _optional_float(raw.get("sample_rate")),
        # `channel_count` remains the normalized artifact-width contract used
        # by browser-audio quality checks. Preserve the wider raw USB source
        # width separately for diagnostics (for example UMIK-2 source=2,
        # captured=1).
        "channel_count": (
            captured_channel_count
            if captured_channel_count is not None
            else _optional_float(raw.get("channel_count"))
        ),
        "source_channel_count": source_channel_count,
        "captured_channel_count": captured_channel_count,
        "echo_cancellation": _optional_bool(raw.get("echo_cancellation")),
        "noise_suppression": _optional_bool(raw.get("noise_suppression")),
        "auto_gain_control": _optional_bool(raw.get("auto_gain_control")),
    }
    return {k: v for k, v in sanitized.items() if v is not None} or None


# UX-side mirror lives in deploy/assets/correction/js/main.js
# (looksLikeBuiltInMic); keep the two patterns in sync. This server gate is
# the one that actually blocks a wrong-mic measurement.
_BUILTIN_MIC_LABEL_RE = re.compile(
    r"iphone|ipad|ipod|macbook|built[- ]?in|^\s*default", re.IGNORECASE
)


def _calibration_device_mismatch(
    mic_calibration: Any, input_device: dict[str, Any] | None
) -> str | None:
    """Detect applying a vendor measurement-mic calibration curve to audio
    captured from the phone's built-in mic — a silent, measurement-
    invalidating mismatch. The browser blocks this too, but this is the
    reliable backstop a stale/bypassed client cannot evade.
    """
    if mic_calibration is None or not input_device:
        return None
    # Every entry in the calibration registry is an external USB measurement
    # mic that can never be the phone's own built-in mic. Derive the provider
    # set from the registry so a new vendor only has to be added in one place.
    # mic_calibration is non-None here, so calibration (numpy) is already
    # imported — this lazy import keeps the idle module import numpy-free.
    from jasper.audio_measurement.calibration import SUPPORTED_MODELS
    external_providers = {
        spec["provider"] for spec in SUPPORTED_MODELS.values()
    }
    provider = str(getattr(mic_calibration, "provider", "") or "")
    if provider not in external_providers:
        return None
    label = str(input_device.get("browser_label") or input_device.get("label") or "")
    if label and _BUILTIN_MIC_LABEL_RE.search(label):
        return (
            f'captured device "{label}" looks like a built-in mic, but '
            f"a {provider} measurement-mic calibration is loaded; select the USB "
            "measurement mic before measuring"
        )
    return None




async def _read_room_correction_readiness_with_graph(
    cam: Any,
) -> tuple[dict[str, Any], Any]:
    """Read Active's decision and retain its canonical live graph proof."""
    from jasper.active_speaker.setup_status import read_active_speaker_setup_status
    from jasper.camilla import CamillaUnavailable

    try:
        graph = await _classify_live_bass_extension_graph(cam)
        running_raw = await cam.get_active_config_raw(best_effort=False)
    except CamillaUnavailable as exc:
        raise RuntimeError("the running CamillaDSP graph is unavailable") from exc
    if not isinstance(running_raw, str) or not running_raw.strip():
        raise RuntimeError("the running CamillaDSP graph is unavailable")
    return read_active_speaker_setup_status(active_config_text=running_raw), graph


async def _read_room_correction_readiness(cam: Any) -> dict[str, Any]:
    """Read Active's decision against CamillaDSP's fresh running graph."""

    readiness, _graph = await _read_room_correction_readiness_with_graph(cam)
    return readiness


async def _classify_live_bass_extension_graph(cam: Any):
    """Prove the live graph and every bass authority in one canonical read."""

    from jasper.active_speaker.state_paths import baseline_profile_state_path
    from jasper.active_speaker.environment import DEFAULT_CAMILLA_STATEFILE
    from jasper.active_speaker.runtime_contract import (
        classify_active_bass_extension_graph,
    )
    from jasper.active_speaker.staging import staged_metadata_path
    from jasper.bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH
    from jasper.bass_extension.profile import DEFAULT_PROFILE_PATH
    from jasper.output_topology import load_output_topology_strict

    graph = await classify_active_bass_extension_graph(
        load_output_topology_strict(),
        statefile_path=Path(DEFAULT_CAMILLA_STATEFILE),
        read_active_graph_text=lambda: cam.get_active_config_raw(best_effort=False),
        canonicalize_graph_text=lambda raw: cam.normalize_config_raw(
            raw, best_effort=False
        ),
        applied_baseline_path=baseline_profile_state_path(),
        profile_path=DEFAULT_PROFILE_PATH,
        intent_path=BASS_EXTENSION_APPLY_INTENT_PATH,
        staged_metadata_path=staged_metadata_path(),
    )
    summary = graph.details.get("bass_extension_profile_summary")
    if not graph.allowed or not isinstance(summary, Mapping):
        issue = graph.issues[0] if graph.issues else {}
        code = issue.get("code") or graph.classification
        detail = issue.get("message") or ""
        raise RuntimeError(
            f"the running CamillaDSP graph authority is unavailable ({code})"
            + (f": {detail}" if detail else "")
        )
    return graph


def _room_correction_readiness() -> dict[str, Any]:
    """Synchronous web-handler bridge for Active's fresh decision."""

    return _run_async(
        _read_room_correction_readiness(_camilla()),
        timeout=2.0,
    )


@dataclass(frozen=True)
class _RoomReadiness:
    allowed: bool
    blocker: dict[str, Any] | None
    reason: str
    detail: str
    active: bool | None = None
    authority: str | None = None
    layer_a_identity: str | None = None

    @property
    def authority_binding(self) -> tuple[bool | None, str | None, str | None]:
        """Opaque Active decision that Room may carry and compare only.

        Total, including the denied answer. Active publishes no authority and
        no Layer A identity when it cannot vouch, and ``_normalize_room_readiness``
        carries no ``active`` on that path either — so the denied binding is
        ``(None, None, None)``, a real binding meaning "unproven" rather than
        an absent one. Under ruling S10 that is a state Room runs in rather
        than refuses. The writer boundary still compares it: an authority that
        APPEARS or changes mid run is drift either way, and a run that started
        unproven and is still unproven has not moved.
        """

        return (self.active, self.authority, self.layer_a_identity)


def _normalize_room_readiness(raw: Any) -> _RoomReadiness:
    """Normalize one Active-owned decision without reading its evidence.

    Room does not inspect measurement artifacts or reconstruct crossover
    authority. It validates the versioned Active-owned decision and consumes
    that one result. Manual applied-profile authority and automatic
    receipt-backed authority are deliberately distinct; an older unversioned
    active result remains rejected. Only Active's safe local recovery href
    crosses this adapter.
    """
    from jasper.correction import failures
    from jasper.active_speaker._common import (
        ROOM_AUTHORITY_RECEIPT_ABSENT,
        ROOM_AUTHORITY_RECEIPT_MALFORMED,
        ROOM_AUTHORITY_RECEIPT_STALE,
        ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
        ROOM_AUTHORITY_RECEIPT_UNREADABLE,
    )
    from jasper.active_speaker.setup_status import (
        ROOM_AUTHORITY_AUTOMATIC_COMMISSIONING_RECEIPT,
        ROOM_AUTHORITY_MANUAL_APPLIED_PROFILE,
        ROOM_AUTHORITY_PASSIVE_NOT_REQUIRED,
        ROOM_ELIGIBILITY_SCHEMA_VERSION,
    )

    # The closed set of Active-owned commissioning denials, whose `detail` is
    # bounded copy from setup_status._RECEIPT_DETAIL. Only these carry detail
    # through to the block; a non-receipt reason's detail may be arbitrary and
    # must not reach a household surface.
    receipt_denials = {
        ROOM_AUTHORITY_RECEIPT_ABSENT,
        ROOM_AUTHORITY_RECEIPT_STALE,
        ROOM_AUTHORITY_RECEIPT_MALFORMED,
        ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
        ROOM_AUTHORITY_RECEIPT_UNREADABLE,
    }

    setup = raw if isinstance(raw, Mapping) else {}
    acoustic_raw = setup.get("acoustic_commissioning")
    acoustic = acoustic_raw if isinstance(acoustic_raw, Mapping) else {}
    active = setup.get("active")
    allowed = setup.get("room_correction_allowed")
    acoustic_allowed = acoustic.get("allowed")
    acoustic_status = acoustic.get("status")
    decision_schema_version = acoustic.get("decision_schema_version")
    authority = acoustic.get("authority")
    layer_a_identity = acoustic.get("layer_a_identity")
    well_formed = (
        isinstance(active, bool)
        and isinstance(allowed, bool)
        and isinstance(acoustic_raw, Mapping)
        and isinstance(acoustic_allowed, bool)
        and acoustic_allowed is allowed
        and type(decision_schema_version) is int
        and decision_schema_version == ROOM_ELIGIBILITY_SCHEMA_VERSION
        and (
            (
                active is False
                and allowed is True
                and acoustic_status == "not_required"
                and authority == ROOM_AUTHORITY_PASSIVE_NOT_REQUIRED
                and layer_a_identity is None
            )
            or (
                active is True
                and allowed is True
                and acoustic_status == "ready"
                and authority in {
                    ROOM_AUTHORITY_MANUAL_APPLIED_PROFILE,
                    ROOM_AUTHORITY_AUTOMATIC_COMMISSIONING_RECEIPT,
                }
                and isinstance(layer_a_identity, str)
                and bool(layer_a_identity)
            )
            or (
                allowed is False
                and acoustic_status in {"incomplete", "unknown"}
                and authority is None
                and layer_a_identity is None
            )
        )
    )
    href = acoustic.get("setup_href")
    action = None
    if (
        well_formed
        and (allowed is False or (active is True and allowed is True))
        and
        isinstance(href, str)
        and href.startswith("/")
        and not href.startswith("//")
        and "\\" not in href
        and not any(ord(char) < 0x20 for char in href)
        and not urlparse(href).scheme
        and not urlparse(href).netloc
    ):
        action = {"label": "Open speaker setup", "href": href}

    if well_formed and allowed is True:
        return _RoomReadiness(
            allowed=True,
            blocker=None,
            reason="speaker_readiness_allowed",
            detail="speaker readiness allows room correction",
            active=active,
            authority=authority,
            layer_a_identity=(
                layer_a_identity if isinstance(layer_a_identity, str) else None
            ),
        )

    reason = str(
        acoustic.get("reason")
        or setup.get("reason")
        or (
            "speaker_readiness_malformed"
            if not well_formed
            else "speaker_room_correction_not_ready"
        )
    )
    detail = str(
        acoustic.get("detail")
        or setup.get("detail")
        or "speaker setup is not ready for room correction"
    )
    cause = str(acoustic.get("cause") or "")
    unavailable = not well_formed or acoustic_status == "unknown"
    if reason == ROOM_AUTHORITY_RECEIPT_UNREADABLE:
        # A receipt JTS could not OPEN is a machine fault, not an unconfigured
        # speaker and not a step to retry: Active's own detail for this denial
        # says re-running commissioning is unlikely to clear it. So it is
        # neither "finish speaker setup first" (wrong wizard) nor the "Check
        # again" retry loop -- a non-retryable device fault, ADR-0196.
        public_code = failures.SPEAKER_READINESS_FAULT
        recovery_action = None
    elif unavailable:
        public_code = failures.SPEAKER_READINESS_UNAVAILABLE
        recovery_action = action or failures.ROOM_RETRY_ACTION
    else:
        public_code = failures.SPEAKER_SETUP_INCOMPLETE
        recovery_action = action or failures.ROOM_RETRY_ACTION
    # A receipt denial's bounded detail (and errno+path) ride the block so the
    # ABSENT/STALE/MALFORMED/SUPERSEDED/UNREADABLE distinction survives past
    # this line for the doctor, `/state`, and logs. The browser still renders
    # from `code`, and a non-receipt reason's detail (possibly arbitrary) is
    # never carried.
    is_receipt_denial = reason in receipt_denials
    blocker = failures.public_failure(
        public_code,
        recovery_action=recovery_action,
        detail=detail if is_receipt_denial else None,
        cause=(cause or None) if is_receipt_denial else None,
    )
    return _RoomReadiness(
        allowed=False,
        blocker=blocker,
        reason=reason,
        detail=detail,
    )


def _room_readiness() -> _RoomReadiness:
    """Read and normalize Active's one decision for envelope and `/start`."""

    from jasper.correction import failures

    try:
        return _normalize_room_readiness(_room_correction_readiness())
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        log_event(
            logger,
            "correction.readiness_unavailable",
            error_type=type(exc).__name__,
            level=logging.WARNING,
        )
        return _RoomReadiness(
            allowed=False,
            blocker=failures.public_failure(
                failures.SPEAKER_READINESS_UNAVAILABLE,
                recovery_action=failures.ROOM_RETRY_ACTION,
            ),
            reason="speaker_readiness_unavailable",
            detail="speaker readiness could not be read",
        )


async def _assert_room_authority_current(
    cam: Any,
    expected: tuple[bool | None, str | None, str | None] | None,
) -> Mapping[str, Any]:
    """Revalidate the accepted Active identity at a DSP-writer boundary."""

    from jasper.active_speaker._common import ROOM_AUTHORITY_RECEIPT_UNREADABLE

    if expected is None:
        raise RuntimeError("room correction authority binding is missing")
    raw_readiness, graph = await _read_room_correction_readiness_with_graph(cam)
    current = _normalize_room_readiness(
        raw_readiness,
    )
    # An UNREADABLE receipt at this DSP-writer boundary is a machine fault, not
    # evidence the crossover authority moved. A denial collapses the binding to
    # (None, None, None), so without this a transient read fault between /start
    # and accept would read as "authority changed" and DISCARD a completed
    # six-position measurement. The binding is preserved; a genuine APPEARS or
    # CHANGES is still refused. See ADR-0196.
    unreadable = current.reason == ROOM_AUTHORITY_RECEIPT_UNREADABLE
    binding_matches = current.authority_binding == expected
    if binding_matches or unreadable:
        if unreadable and not binding_matches:
            # Fail-OPEN, disclosed: we proceed rather than discard a completed
            # run (blocker 2), but the binding did NOT match, so if the
            # authority genuinely changed we are banking under the prior one.
            # The receipt is unreadable, so we cannot tell drift from a
            # transient fault -- surface it (once per transition via the shared
            # gate) so the fail-open is visible, never silent. ADR-0196.
            if _AUTHORITY_UNCONFIRMED_DISCLOSURE.should_log(
                str(expected), "unreadable_at_writer_boundary"
            ):
                log_event(
                    logger,
                    "correction.layer_a_authority_unconfirmed",
                    level=logging.WARNING,
                    expected_active=expected[0],
                    expected_authority=expected[1],
                    reason=current.reason,
                )
        summary = graph.details.get("bass_extension_profile_summary")
        if isinstance(summary, Mapping):
            return summary
        raise RuntimeError("room correction bass authority evidence is invalid")
    log_event(
        logger,
        "correction.layer_a_authority_changed",
        level=logging.WARNING,
        expected_active=expected[0],
        current_active=current.active,
        expected_authority=expected[1],
        current_authority=current.authority,
    )
    raise RuntimeError(
        "speaker crossover authority changed during this Room run; "
        "reset or start a new measurement"
    )


def _calibration_payload(record) -> dict[str, Any]:
    from jasper.audio_measurement import calibration
    return {
        "calibration": record.public_metadata(),
        "preview": calibration.preview_curve(record.curve),
    }
