# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS correction measurement hub at /correction/.

The user opens the hub on a phone and chooses the measurement job:
room correction, active-crossover acoustic checks, or bass tuning. Room
correction captures pre-sweep room noise plus one or more measurement
positions, reviews confidence/visualization evidence, and optionally
applies a bounded room-correction profile through the shared CamillaDSP
apply path.

Architecture:
  - stdlib `ThreadingHTTPServer` — same pattern as voice_setup,
    spotify_setup, bluetooth_setup. No FastAPI / ASGI dependency.
  - Single in-memory `MeasurementSession` (jasper.correction.session)
    drives the multi-step state machine.
  - Browser polls GET /status every 500 ms while work is active, the
    presentation envelope every 900 ms on active screens, and lightweight
    entry facts every 10 s while idle — simpler than SSE in stdlib and bounded
    for state transitions that take seconds.
  - Background asyncio loop in a daemon thread bridges the sync HTTP
    handlers to the async session methods.
  - HTTP routes (after nginx strips the /correction/ prefix): this
    module now serves far more routes than fit a comment table.

Why a separate service from jasper-web (Spotify + voice settings):
the correction flow eventually imports numpy/scipy through
`jasper.correction.*` while handling measurements. Keeping this
socket-activated service separate from lightweight setup pages keeps
the idle management UI cheap on a 1 GB Pi.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import inspect
import logging
import math
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import (
    AbstractContextManager,
    ExitStack,
)
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from jasper.audio_measurement import room_boundary

from ..log_event import log_event
from ..transition_log import TransitionLog
from . import correction_room_flow, correction_tuning
from ._systemd import no_hold

from ._common import (
    JsonBodyError,
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    guard_mutating_request,
    guard_read_request,
    read_json_object,
    reject_csrf,
    send_html_response,
    send_json_response,
)

logger = logging.getLogger(__name__)

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
_FOLLOWER_DELEGATED_PAGE_PATHS = frozenset({"/", "/room", "/balance", "/sync"})


class BadRequest(ValueError):
    """Client supplied an invalid request body."""


class RequestConflict(RuntimeError):
    """Client request conflicts with the current correction session state."""


class TuningSetupUnavailable(RequestConflict):
    """The optional tuning assistant has no configured model credential."""


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

#: The session-measurement claim the autolevel ramp holds, from its first
#: quiet write until the workflow settles at ``/apply`` or ``/reset``.
#:
#: It OUTLIVES the request that took it because the level does: the ramp locks
#: a measurement level, the sweeps play at it, and only apply/reset returns the
#: household its own. Module-scoped for the same reason ``_LEVEL_LEASE`` and
#: ``session_volume_plan()`` are — this process serves one measurement session.
#:
#: **The failure mode is unchanged by routing, and is #3038's.** A process exit
#: mid-session strands the claim exactly as it already stranded the fader; the
#: claim makes that state legible in the owner's ledger rather than invisible.
_AUTOLEVEL_CLAIM: Any = None


def _take_autolevel_claim() -> Any:
    """Hand back the held autolevel claim, clearing it. ``None`` when none."""

    global _AUTOLEVEL_CLAIM
    claim, _AUTOLEVEL_CLAIM = _AUTOLEVEL_CLAIM, None
    return claim


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


# Mutating routes this handler accepts. Module-scoped so route membership is
# pinnable by a test (deleting a line would otherwise 404 a route silently).
_POST_ROUTES = frozenset({
    "/start",
    "/next-position",
    "/repeat-position",
    "/verify",
    "/test-tone",
    "/autolevel/start",
    "/autolevel/lock",
    "/autolevel/cancel",
    "/upload-noise",
    "/upload-capture",
    "/local-capture/setup",
    "/calibration/fetch",
    "/calibration/upload",
    "/apply",
    "/reset",
    "/session/delete",
    "/interpret",
    "/propose",
    "/propose/apply",
    "/crossover/capture-cancel",
    "/crossover/reset",
    "/crossover/recover-volume",
    # v2 session flow — the only crossover-measurement flow. There is no
    # per-driver flow and no JASPER_CROSSOVER_FLOW selector to branch on.
    "/crossover/v2/session",
    "/crossover/v2/verify",
    "/crossover/v2/apply",
    # Make a PREVIOUSLY-MINTED, banked candidate the live published one again,
    # so the apply door above can reach it by fingerprint. The apply slot is
    # single-valued and every measure session overwrites it; this is the lookup
    # it never had.
    "/crossover/v2/republish",
    # The review screen's "Keep current sound", which #2641 found inert.
    "/crossover/v2/decline",
    # A GATED session's position release — the report that the microphone has
    # reached the angle the envelope named, from an EXTERNAL driver on the
    # remote tier or from the person holding the tape on a hand-walked wired
    # round (#2879).
    "/crossover/v2/position-ready",
    # The WIRED session's all-spots-measured confirmation (#2662 W2b) — the
    # local stand-in for the phone's authenticated completion event.
    "/crossover/v2/complete",
    # The WIRED session's per-take retake — the local stand-in for the phone's
    # ``begin_capture {retake: true}``, re-opening the slot that just
    # completed while the walk is still waiting on a person.
    "/crossover/v2/retake",
    "/balance/start",
    "/balance/ramp",
    "/balance/meter",
    "/balance/lock",
    "/balance/stop",
    "/balance/apply",
    "/balance/reset",
    "/sync/start",
    "/sync/play",
    "/sync/analyze",
    "/sync/apply",
    "/sync/stop",
    "/sync/reset",
})


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

    * pass the process's real hold (``_systemd.IdleShutdownTracker.hold``, from
      ``main`` through the handler cfg) when the runner must survive an idle
      window — long walks, anything whose only traffic is outbound;
    * pass ``_systemd.no_hold`` when it must not, or need not.

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

    The counterpart to balance/sync ``active_phase()`` so another measurement
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
    # The pair-balance and pair-sync flows share this process precisely so the
    # measurement surfaces can exclude each other here (both open
    # measurement_window; concurrent windows would interleave the
    # renderer stop/start). Active-speaker commissioning excludes the same way
    # (it plays sweeps through the production graph) but participates
    # cooperatively rather than holding a window — see active_speaker_flow.
    # Lazy imports: these modules never import this module back at import time.
    from .active_speaker_flow import active_phase as _active_speaker_phase
    from .balance_flow import active_phase as _balance_phase
    from .sync_flow import active_phase as _sync_phase
    balance_active = _balance_phase()
    if balance_active is not None:
        return f"balance:{balance_active}"
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


_PAGE_BODY = correction_room_flow._PAGE_BODY


def _render_follower_page(hostname: str, csrf_token: str = "") -> bytes:
    return correction_room_flow.render_follower_page(
        hostname,
        csrf_token,
        leader_url=bonded_follower_leader_web_url("/sound/room/"),
    )


def _render_page(hostname: str, csrf_token: str = "", flash: str = "") -> bytes:
    if bonded_follower_active():
        return _render_follower_page(hostname, csrf_token)
    return correction_room_flow.render_page(
        hostname,
        csrf_token,
        required_sample_rate=REQUIRED_SAMPLE_RATE,
        household_mic_prefill_payload=_household_mic_prefill_payload(),
    )


# ----------------------------------------------------------------------
# HTTP route handlers — sync wrappers around async session methods.
# ----------------------------------------------------------------------


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


def _calibration_root() -> Path:
    return Path(
        os.environ.get(
            "JASPER_CORRECTION_CALIBRATION_DIR",
            "/var/lib/jasper/correction/calibration_mics",
        )
    )


def _household_mic_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH",
            "/var/lib/jasper/correction/household_mic.json",
        )
    )


def _save_household_mic(record: Any, *, serial: str | None = None) -> None:
    """Persist a just-established calibration as the household's default
    measurement mic (``jasper.correction.household_mic``).

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
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        read_household_mic,
        write_household_mic,
    )

    path = _household_mic_path()
    try:
        new_record = household_mic_from_calibration(record, serial=serial)
        previous = read_household_mic(path=path)
        write_household_mic(new_record, path=path)
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


def _resolved_household_mic() -> tuple[Any, Any] | None:
    """Read + resolve the household mic record in one fail-soft step.

    Returns ``(HouseholdMicRecord, CalibrationRecord)`` when a household
    default exists AND its calibration is still resolvable on disk, else
    ``None``. Shared by the spec prefill hint and the room wizard's
    server-rendered banner so both degrade identically when the record is
    absent or its calibration has been removed from under it.
    """
    from jasper.correction.household_mic import (
        read_household_mic,
        resolve_household_mic_calibration,
    )

    household = read_household_mic(path=_household_mic_path())
    if household is None:
        return None
    resolved = resolve_household_mic_calibration(household, root=_calibration_root())
    if resolved is None:
        return None
    return household, resolved


def _default_setup_calibration_for_spec() -> Any | None:
    """Build the capture spec's OPTIONAL ``default_setup.calibration`` hint
    from the household's remembered mic.

    Never binding. The measurement source reads the hint and mints the
    capture's own ``setup.calibration`` reference from it when it is marked
    ``resolvable: true`` (``correction_crossover_v2_wired
    ._wired_setup_reference``). Fail-soft: any resolution miss yields no hint
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
    from jasper.correction.household_mic import resolve_household_mic_calibration

    found = _resolved_household_mic()
    if found is None:
        return None
    household, resolved = found
    mode = "upload" if household.provider == "manual_upload" else "serial"
    resolvable = (
        resolve_household_mic_calibration(household, root=_calibration_root())
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
    found = _resolved_household_mic()
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
    from jasper.correction import coordinator, playback

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with coordinator.measurement_window():
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
    from jasper.correction import coordinator, playback

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with coordinator.measurement_window():
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


def _handle_start(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /start: snapshot the current DSP graph, load a measurement
    baseline with room/preference layers stripped, replace the session, and
    ask the browser for pre-sweep room-noise capture. The sweep starts only
    after `POST /upload-noise` lands.

    Body fields:
      - total_positions: supported household count; defaults to the
        session-owned six-position policy.
      - target_choice:   one registered Room target; defaults to flat.
      - strategy_choice: 'safe' | 'balanced' on the household surface.
      - noise_floor_db:  float | None — optional, client autolevel
        preflight measurement; only saved into the debug bundle.
      - repeat_main_position: when present, must agree with the session-owned
        automatic same-seat trust repeat.

    Why strip layers before sweeping: if a correction or preference EQ is
    loaded, the sweep traverses that layer and the resulting curve reflects
    the user's taste or the old correction, not the raw room. The carrier
    keeps the topology-owned speaker graph (crossovers, driver EQ, delays,
    gains, limiters) and strips only Layer B/C.
    """
    from jasper.correction.session import (
        DEFAULT_REPEAT_MAIN_POSITION,
        DEFAULT_ROOM_POSITION_COUNT,
        ROOM_POSITION_COUNT_CHOICES,
        SessionState,
    )
    from jasper.correction.strategy import (
        DEFAULT_CORRECTION_STRATEGY_ID,
        DEFAULT_TARGET_PROFILE_ID,
        HOUSEHOLD_CORRECTION_STRATEGY_IDS,
        TARGET_PROFILES,
    )
    readiness = _room_readiness()
    if not readiness.allowed:
        # Ruling S10: an unproven or stale speaker decision is a loud
        # disclosure, never a stop. This used to raise 409/503 here, so a
        # metadata edit or an unminted receipt could keep the household from
        # measuring a speaker that was playing fine. What still holds is the
        # other half — the run may not CLAIM the authority it could not read:
        # `readiness.authority_binding` carries the un-vouched answer forward,
        # and `_room_readiness().blocker` keeps surfacing on the idle screen
        # and in `/envelope` for as long as it is true.
        log_event(
            logger,
            "correction.start_unproven_speaker_readiness",
            reason=readiness.reason,
            code=str((readiness.blocker or {}).get("code") or ""),
            level=logging.WARNING,
        )
    authority_binding = readiness.authority_binding

    body = _read_json_body(handler)
    blocking_state = _reserve_start_slot()
    if blocking_state is not None:
        log_event(
            logger,
            "correction.start_rejected",
            reason="active_session",
            state=blocking_state,
            level=logging.WARNING,
        )
        raise RequestConflict(
            "measurement already in progress; wait for the current sweep "
            "or reset before starting again"
        )

    try:
        total_raw = body.get("total_positions", DEFAULT_ROOM_POSITION_COUNT)
        if not isinstance(total_raw, int) or isinstance(total_raw, bool):
            raise ValueError("total_positions must be a supported count")
        total_positions = total_raw
        if total_positions not in ROOM_POSITION_COUNT_CHOICES:
            raise ValueError("total_positions must be a supported count")
        target_choice = str(
            body.get("target_choice", DEFAULT_TARGET_PROFILE_ID)
        )
        if target_choice not in TARGET_PROFILES:
            raise ValueError("target_choice must be a registered Room target")
        strategy_choice = str(
            body.get("strategy_choice", DEFAULT_CORRECTION_STRATEGY_ID)
        )
        if strategy_choice not in HOUSEHOLD_CORRECTION_STRATEGY_IDS:
            raise ValueError(
                "strategy_choice must be an authorized household strategy"
            )
        noise_floor_db_raw = body.get("noise_floor_db")
        calibration_id = str(body.get("calibration_id") or "").strip()
        input_device = _sanitize_input_device(body.get("input_device"))
        repeat_raw = body.get(
            "repeat_main_position",
            DEFAULT_REPEAT_MAIN_POSITION,
        )
        if repeat_raw is not DEFAULT_REPEAT_MAIN_POSITION:
            raise ValueError(
                "repeat_main_position must use the automatic trust check"
            )
        repeat_main_position = DEFAULT_REPEAT_MAIN_POSITION
        noise_floor_db: float | None
        try:
            noise_floor_db = (
                float(noise_floor_db_raw)
                if noise_floor_db_raw is not None
                else None
            )
        except (TypeError, ValueError):
            noise_floor_db = None

        mic_calibration = None
        if calibration_id:
            from jasper.audio_measurement.calibration import load_calibration_record
            mic_calibration = load_calibration_record(
                calibration_id,
                root=_calibration_root(),
            )

        mismatch = _calibration_device_mismatch(mic_calibration, input_device)
        if mismatch is not None:
            log_event(
                logger,
                "correction.start_rejected",
                reason="calibration_device_mismatch",
                provider=getattr(mic_calibration, "provider", ""),
                level=logging.WARNING,
            )
            raise ValueError(mismatch)

        from jasper.correction import browser_audio

        browser_report = browser_audio.assess_browser_audio_path(
            input_device=input_device,
            expected_sample_rate=REQUIRED_SAMPLE_RATE,
            has_mic_calibration=mic_calibration is not None,
        ).to_dict()
        if browser_report.get("failed") is True:
            issue_codes = [
                issue.get("code")
                for issue in browser_report.get("issues", [])
                if isinstance(issue, dict) and issue.get("severity") == "fail"
            ]
            log_event(
                logger,
                "correction.start_rejected",
                reason="browser_audio_path_failed",
                issue_codes=",".join(
                    str(code) for code in issue_codes if code
                ),
                level=logging.WARNING,
            )
            raise ValueError(
                browser_report.get("summary")
                or "browser audio path is not safe for measurement"
            )

        cam = _camilla()
        prior_session = _get_or_create_session()
        _run_async(
            prior_session.restore_level_match_volume(_household_level_door()),
            timeout=5.0,
        )
        sess = _replace_session(
            total_positions=total_positions,
            target_choice=target_choice,
            strategy_choice=strategy_choice,
            mic_calibration=mic_calibration,
            input_device=input_device,
            repeat_main_position=repeat_main_position,
        )
        sess.noise_floor_db = noise_floor_db
        sess.room_authority_binding = authority_binding

        # A second copy of the browser-audio refusal used to sit here, re-reading
        # ``sess.browser_audio_report``. It could not fire: MeasurementSession
        # builds that report by calling the same pure
        # ``browser_audio.assess_browser_audio_path`` with the same three inputs
        # -- this ``input_device``, ``mic_calibration is not None``, and
        # ``SessionConfig.sample_rate`` (48000), which equals
        # ``REQUIRED_SAMPLE_RATE`` -- and neither input is reassigned between
        # the two points. So it was always the verdict the block above had
        # already raised on.

        from jasper.correction.runtime_safety import CorrectionRuntimeSafetyError
        from jasper.sound.graph_carrier import CarrierCannotHostEq

        try:
            baseline_payload = _run_graph_mutation(
                _load_measurement_baseline(
                    sess,
                    cam,
                    expected_authority_binding=authority_binding,
                ),
            )
        except CarrierCannotHostEq:
            logger.warning("/start: measurement baseline rejected by graph carrier")
            raise
        except CorrectionRuntimeSafetyError:
            # It subclasses RuntimeError, so the arm below would re-raise it as
            # a BARE RuntimeError and the dispatcher's typed arm would never
            # see it — an unsafe graph would reach the household as an untyped
            # 500. Matters more now that `/start` no longer refuses ahead of
            # this point: this is the surface that answers for an unready
            # speaker, so it has to keep its type.
            logger.warning("/start: measurement baseline refused as unsafe")
            raise
        except RuntimeError as exc:
            logger.exception("/start: measurement baseline load rejected")
            raise RuntimeError(str(exc)) from None
        except Exception:  # noqa: BLE001
            logger.exception("/start: measurement baseline load failed")
            raise RuntimeError(
                "could not load speaker measurement baseline before measuring"
            ) from None
        sess.current_correction_at_start = baseline_payload.get(
            "current_correction_at_start"
        )

        try:
            _run_async(sess.begin_noise_capture(), timeout=3.0)
            state_started = sess.state == SessionState.NEEDS_NOISE_CAPTURE
        except concurrent.futures.TimeoutError:
            state_started = False

        if state_started:
            # Browser permission + device selection are human-paced. The
            # ordinary upload watchdog resumes when the first noise upload
            # actually begins, after setup and level matching are done.
            sess.suspend_capture_timeout()
            _clear_start_slot()
        else:
            _clear_start_slot()
            log_event(
                logger,
                "correction.start_state_wait_timeout",
                session=sess.session_id,
                level=logging.WARNING,
            )

        snapshot = sess.snapshot()
        return {
            "session_id": sess.session_id,
            "state": sess.state.value,
            "total_positions": sess.total_positions,
            "target_choice": sess.target_choice,
            "strategy_choice": sess.strategy_choice,
            "target_profile": snapshot.get("target_profile"),
            "correction_strategy": snapshot.get("correction_strategy"),
            "input_device": sess.input_device,
            "browser_audio_report": sess.browser_audio_report,
            "mic_calibration": (
                sess.mic_calibration.public_metadata()
                if sess.mic_calibration
                else None
            ),
            "current_correction_at_start": sess.current_correction_at_start,
            "measurement_config_path": baseline_payload.get(
                "measurement_config_path"
            ),
        }
    except Exception:  # noqa: BLE001
        _clear_start_slot()
        raise


def _room_graph_artifact_path(sess: Any, label: str) -> Path:
    """Return a collision-free managed config path for one Room transaction."""

    cfg = getattr(sess, "cfg", None)
    config_dir = Path(
        getattr(cfg, "config_dir", None)
        or "/var/lib/camilladsp/configs"
    )
    token = re.sub(
        r"[^A-Za-z0-9]",
        "",
        str(getattr(sess, "session_id", "session")),
    ) or "session"
    return config_dir / f"sound_{label}_{token}_{time.time_ns()}.yml"


def _running_graph_snapshot_text(
    raw: str,
    current_path: str | Path,
    *,
    carrier: Any | None = None,
) -> str:
    """Make Camilla's comment-free active_raw reloadable with provenance.

    CamillaDSP's active_raw is the graph-content authority but drops YAML
    comments. Preserve only the bounded JTS ``# Source:`` marker from the
    durable path so the graph carrier can distinguish a safe Active baseline
    from transient commissioning graphs. All executable graph content remains
    the fresh Camilla readback.
    """

    source_line = None
    try:
        for line in Path(current_path).read_text(encoding="utf-8").splitlines():
            if line.startswith("# Source: ") and len(line) <= 256:
                source_line = line
                break
    except OSError:
        pass
    # PR #1009's one-time recovery shape is a protected active-leader pipe
    # graph stamped with the generic sound marker. Resolve it while the
    # original durable name is still available; the collision-free snapshot
    # name intentionally cannot trigger that filename-scoped compatibility
    # rule later.
    if carrier is None:
        from jasper.sound.graph_carrier import carrier_for_loaded_config

        carrier = carrier_for_loaded_config(
            current_path,
            config_dir=Path(current_path).parent,
        )
    if carrier.kind == "active_leader_program_bake":
        from jasper.active_speaker.camilla_yaml import ACTIVE_PROGRAM_BAKE_SOURCE

        source_line = f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}"
    text = raw.rstrip() + "\n"
    if source_line:
        body = "\n".join(
            line for line in text.splitlines()
            if not line.startswith("# Source: ")
        )
        return f"{source_line}\n{body.rstrip()}\n"
    return text


def _running_graph_body(text: str) -> str:
    """Executable snapshot body, excluding the one JTS provenance comment."""

    return "\n".join(
        line for line in text.splitlines()
        if not line.startswith("# Source: ")
    ).strip()


async def _snapshot_running_room_graph(
    sess: Any,
    cam: Any,
    *,
    current_path: str | Path | None = None,
    bass_profile_summary: Mapping[str, Any] | None = None,
) -> tuple[Path, Path, Mapping[str, Any]]:
    """Persist one validated, content-stable copy of Camilla's running graph."""

    from jasper.atomic_io import atomic_write_text
    from jasper.correction.runtime_safety import assert_correction_graph_safe
    from jasper.dsp_apply import validate_camilla_config
    from jasper.sound.graph_carrier import (
        CarrierCannotHostEq,
        carrier_for_loaded_config,
    )

    current = current_path or await cam.get_config_file_path(best_effort=False)
    if not current:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    carrier = carrier_for_loaded_config(
        current,
        config_dir=Path(current).parent,
    )
    if carrier.kind == "unknown":
        raise CarrierCannotHostEq(
            "unknown_config",
            "CamillaDSP is running a configuration JTS didn't generate, so "
            "Room cannot preserve it for exact restoration.",
        )
    if bass_profile_summary is None:
        live_authority = await _classify_live_bass_extension_graph(cam)
        bass_profile_summary = live_authority.details[
            "bass_extension_profile_summary"
        ]
    raw = await cam.get_active_config_raw(best_effort=False)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("CamillaDSP did not report a running graph")
    text = _running_graph_snapshot_text(raw, current, carrier=carrier)
    assert_correction_graph_safe(
        text,
        bass_profile_summary=bass_profile_summary,
    )
    snapshot = _room_graph_artifact_path(sess, "snapshot")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        snapshot,
        text,
        mode=0o640,
        group_from_parent=True,
    )
    validation = validate_camilla_config(snapshot)
    if not validation.ok_to_apply:
        snapshot.unlink(missing_ok=True)
        raise RuntimeError(
            "CamillaDSP's running graph could not be validated for exact "
            f"restoration: {validation.error or validation.status.value}"
        )
    return Path(current), snapshot, bass_profile_summary


async def _load_measurement_baseline(
    sess: Any,
    cam: Any,
    *,
    expected_authority_binding: tuple[bool | None, str | None, str | None],
) -> dict[str, Any]:
    """Load a topology-preserving measurement graph for this correction run.

    The graph carrier is the single bridge between "whatever CamillaDSP is
    running" and "emit the same speaker topology with different program-domain
    layers." Passing ``room_peqs=[]`` and ``SoundProfile(enabled=False)`` strips
    old room correction and preference EQ while keeping crossovers/protection.
    """

    from jasper.correction.runtime_safety import (
        CorrectionRuntimeSafetyError,
        assert_correction_graph_safe,
    )
    from jasper.dsp_apply import DspApplyError, apply_dsp_config
    from jasper.correction.status import describe_current_config
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import (
        CarrierCannotHostEq,
        carrier_for_loaded_config,
    )
    from jasper.sound.profile import SoundProfile

    sess.cfg.config_dir.mkdir(parents=True, exist_ok=True)
    out_path = sess.cfg.config_dir / (
        f"correction_measurement_{sess.session_id}_{int(sess.started_at)}.yml"
    )
    # The measurement graph must capture the SAME program tap fan-in is feeding,
    # else under shm_ring it would measure a dead loopback. Thread the coupling.
    coupling_capture_kwargs = coupling_capture_kwargs_from_env()

    async def _prepare_config() -> dict[str, Any]:
        # apply_dsp_config invokes prepare while /start owns the shared
        # DSP-writer lock. Re-read Active's decision here so the graph being
        # re-emitted cannot rely on a Layer-A sample taken before reservation.
        bass_profile_summary = await _assert_room_authority_current(
            cam,
            expected_authority_binding,
        )
        anchor = await cam.get_config_file_path(best_effort=False)
        if not anchor:
            raise RuntimeError("CamillaDSP did not report a loaded config path")
        _, restore_path, _ = await _snapshot_running_room_graph(
            sess,
            cam,
            current_path=anchor,
            bass_profile_summary=bass_profile_summary,
        )
        carrier = carrier_for_loaded_config(
            restore_path,
            config_dir=sess.cfg.config_dir,
        )
        result = carrier.reemit(
            SoundProfile(enabled=False),
            room_peqs=[],
            out_path=out_path,
            profile_id=f"measurement-{sess.session_id}",
            fanin_coupling_capture_kwargs=coupling_capture_kwargs,
        )
        assert_correction_graph_safe(
            result.yaml,
            bass_profile_summary=bass_profile_summary,
        )
        sess.pre_measurement_config_path = Path(anchor)
        sess.pre_measurement_restore_path = restore_path
        return {
            # apply_dsp_config must roll back to immutable graph content, not
            # the mutable durable filename Camilla happened to report.
            "prior_config_path": str(restore_path),
            "room_peq_count": result.room_peq_count,
            "sound_filter_count": 0,
        }

    try:
        state = await apply_dsp_config(
            source="correction_measurement",
            candidate_path=out_path,
            load_config=lambda path: cam.set_config_file_path(
                path,
                best_effort=False,
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=True,
            ),
            prepare=_prepare_config,
            room_peq_count=0,
            sound_filter_count=0,
        )
    except DspApplyError as exc:
        if isinstance(
            exc.__cause__,
            (CarrierCannotHostEq, CorrectionRuntimeSafetyError),
        ):
            raise exc.__cause__ from exc
        raise
    sess.measurement_config_path = out_path
    descriptor = describe_current_config(
        sess.pre_measurement_restore_path,
        config_dir=sess.cfg.config_dir,
        base_config_path=sess.cfg.base_config_path,
    )
    log_event(
        logger,
        "correction.measurement_baseline_loaded",
        session=sess.session_id,
        prior=str(sess.pre_measurement_config_path),
        restore=str(sess.pre_measurement_restore_path),
        candidate=str(out_path),
        op_id=state.op_id,
    )
    return {
        "current_correction_at_start": descriptor,
        "measurement_config_path": str(out_path),
        "prior_config_path": str(sess.pre_measurement_config_path),
        "restore_config_path": str(sess.pre_measurement_restore_path),
        "last_dsp_apply": state.to_dict(),
    }


def _handle_next_position(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /next-position: request pre-sweep noise for the next
    multi-position measurement. Only valid in NEEDS_NEXT_POSITION
    state.

    The sweep itself starts after the browser uploads
    `noise/p<N>_pre.wav` to `/upload-noise`.
    """
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess.state != SessionState.NEEDS_NEXT_POSITION:
        raise RuntimeError(
            f"cannot advance to next position from state {sess.state.value}"
        )

    _run_async(sess.begin_noise_capture(), timeout=3.0)

    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
    }


def _handle_verify(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /verify: re-measure after Apply to see the actual effect
    of the correction. One-position only; result lands in
    verify_curve / verify_metrics. Same stale-state-avoidance wait
    as /next-position."""
    from jasper.correction import coordinator, playback
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    cam = _camilla()

    async def _run_verify_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with coordinator.measurement_window():
                await sess.start_verify_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("verify sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(
        _run_session_background_audio(sess, _run_verify_sweep),
        _ensure_loop(),
    )

    _run_async(
        sess.state_changed_from(
            {SessionState.APPLIED, SessionState.VERIFIED},
        ),
        timeout=6.0,
    )

    return {"session_id": sess.session_id, "state": sess.state.value}


def _wait_for_new_autolevel_run(
    sess: Any,
    previous_data: Any,
    future: Any,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Wait for ``run()`` to replace terminal/idle autolevel data."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = sess.autolevel
        if current is not previous_data:
            return current.snapshot()
        if future.done():
            break
        time.sleep(0.05)
    try:
        _run_async(sess.cancel_autolevel(), timeout=1.0)
    except Exception:  # noqa: BLE001
        logger.warning("could not cancel a stalled autolevel start", exc_info=True)
    raise RequestConflict("the measurement level check could not start")


def _handle_autolevel_start(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/start: ramp CamillaDSP main_volume upward
    while a continuous 1 kHz tone plays, until the iPhone client
    POSTs to /autolevel/lock (or the ramp tops out and we report
    `maxed_out`).

    Client behavior:
      1. POST /autolevel/start (kicks off the background task).
      2. Watch the live mic-level meter via AudioWorklet.
      3. When the captured mic RMS lands in the target range
         (computed by the browser from the pre-sweep noise floor),
         POST /autolevel/lock.
      4. Poll GET /status; `autolevel.status` becomes `locked`,
         `maxed_out`, `cancelled`, or `error`.
    """
    from jasper.correction import coordinator, playback
    from jasper.correction.session import AutolevelStatus, SessionState

    sess = _get_or_create_session()
    if (
        sess.state != SessionState.NEEDS_NOISE_CAPTURE
        or not bool(getattr(sess, "local_capture_setup_bound", False))
    ):
        raise RequestConflict(
            "microphone setup must be complete before level matching"
        )
    retryable_statuses = {
        AutolevelStatus.IDLE,
        AutolevelStatus.CANCELLED,
        AutolevelStatus.ERROR,
        AutolevelStatus.MAXED_OUT,
    }
    if sess.autolevel.status not in retryable_statuses:
        raise RequestConflict(
            "the measurement level is already locked or still running"
        )
    previous_data = sess.autolevel

    cam = _camilla()
    from jasper.volume_owner import ClaimKind, volume_owner

    owner = volume_owner()
    if owner is None:
        log_event(
            logger,
            "correction.autolevel_owner_absent",
            level=logging.CRITICAL,
        )
        raise RequestConflict("the speaker volume owner is not available")

    async def _run_autolevel() -> None:
        try:
            async with coordinator.measurement_window():
                # Tone source amplitude = -12 dBFS, matching the sweep
                # amplitude. Earlier this was -6 dBFS — 6 dB louder
                # than the actual sweep, which made the autolevel
                # phase startlingly loud AND inflated the user's
                # expectation of how loud the measurement sweep would
                # be. With -12 dBFS, the tone and sweep are the same
                # loudness so leveling-to-tone calibrates leveling-to-
                # sweep directly.
                tone_wav = playback._ensure_tone_wav(
                    freq_hz=1000.0,
                    duration_s=15.0,  # safety > max ramp duration
                    dbfs=-12.0,
                    sample_rate=48000,
                )
                player = playback.TonePlayer(tone_wav)

                async def _get_vol() -> float:
                    v = await cam.get_volume_db(best_effort=False)
                    return float(v) if v is not None else 0.0

                async def _set_vol(db: float) -> None:
                    # W10 routed. The ramp's FIRST write is the quiet start
                    # level, which is exactly when the claim should be taken;
                    # every write after it MOVES that held claim rather than
                    # re-taking one, so the fader never passes through the
                    # household level between steps.
                    global _AUTOLEVEL_CLAIM
                    if _AUTOLEVEL_CLAIM is None:
                        _AUTOLEVEL_CLAIM = await owner.acquire_level(
                            ClaimKind.SESSION_MEASUREMENT, float(db)
                        )
                        return
                    _AUTOLEVEL_CLAIM = await owner.relevel(
                        _AUTOLEVEL_CLAIM, float(db)
                    )

                await sess.run_autolevel(
                    reservation_token=reserved,
                    get_main_volume_db=_get_vol,
                    set_main_volume_db=_set_vol,
                    play_continuous_tone=player.play,
                    cancel_tone=player.cancel,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("autolevel run failed: %s", e)
        finally:
            # A run that did not end LOCKED/MAXED_OUT leaves no measurement
            # level for the sweeps to play at, so its claim dies with it here
            # rather than waiting for an apply/reset that may never come. A
            # run that DID lock keeps the claim, because the level it locked is
            # what the sweeps are about to use.
            if sess.autolevel.status not in {
                AutolevelStatus.LOCKED,
                AutolevelStatus.MAXED_OUT,
            }:
                stranded = _take_autolevel_claim()
                if stranded is not None:
                    await owner.release(stranded)
            await sess.release_autolevel_run_reservation(reserved)

    reserved = _run_async(sess.reserve_autolevel_run(), timeout=2.0)
    if not reserved:
        raise RequestConflict("the measurement level check is already running")
    try:
        future = asyncio.run_coroutine_threadsafe(
            _run_autolevel(), _ensure_loop()
        )
    except RuntimeError:
        _run_async(
            sess.release_autolevel_run_reservation(reserved),
            timeout=2.0,
        )
        raise
    started = _wait_for_new_autolevel_run(sess, previous_data, future)

    return {"started": True, "autolevel": started}


def _handle_autolevel_lock(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/lock: signal the autolevel task to stop
    ramping and freeze main_volume at its current value. The
    locked level is what subsequent sweeps will play through."""
    sess = _get_or_create_session()
    fired = _run_async(sess.lock_autolevel(), timeout=2.0)
    return {"locked": bool(fired), "autolevel": sess.autolevel.snapshot()}


def _handle_autolevel_cancel(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/cancel: abort the autolevel run and restore
    main_volume to whatever it was before the ramp started."""
    sess = _get_or_create_session()
    fired = _run_async(sess.cancel_autolevel(), timeout=2.0)
    return {"cancelled": bool(fired), "autolevel": sess.autolevel.snapshot()}


def _handle_test_tone(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /test-tone: play a 5-second 1 kHz sine through the music
    chain so the user can adjust their amp's volume by watching the
    live mic level meter. Pauses renderers + voice loop for the tone
    duration via the same measurement_window the sweep uses.

    Synchronous-feeling from the browser's POV (it returns once the
    tone has finished playing) so the polling state machine doesn't
    have to track a "test tone in progress" sub-state.
    """
    from jasper.correction import coordinator, playback

    body = _read_json_body(handler)
    duration_s = max(1.0, min(15.0, float(body.get("duration_s", 5.0))))

    async def _run_test_tone() -> None:
        async with coordinator.measurement_window():
            await playback.play_test_tone(duration_s=duration_s)

    _run_async(_run_test_tone(), timeout=duration_s + 30.0)
    return {"played": True, "duration_s": duration_s}


def _calibration_payload(record) -> dict[str, Any]:
    from jasper.audio_measurement import calibration
    return {
        "calibration": record.public_metadata(),
        "preview": calibration.preview_curve(record.curve),
    }


def _handle_calibration_models(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import SUPPORTED_MODELS
    return {
        "models": [
            {"key": key, **value}
            for key, value in SUPPORTED_MODELS.items()
        ]
    }


def _handle_calibration_fetch(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import fetch_vendor_calibration

    body = _read_json_body(handler)
    model = str(body.get("model") or "").strip()
    serial = str(body.get("serial") or "").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    record = fetch_vendor_calibration(
        model_key=model,
        serial=serial,
        orientation=orientation,
        root=_calibration_root(),
    )
    _save_household_mic(record, serial=serial)
    return _calibration_payload(record)


def _handle_calibration_upload(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import (
        DEFAULT_SIGN_CONVENTION,
        store_calibration,
    )

    body = _read_json_body(
        handler,
        max_bytes=MAX_CALIBRATION_UPLOAD_JSON_BYTES,
    )
    text = str(body.get("content") or "")
    filename = str(body.get("filename") or "uploaded-calibration.txt")
    model = str(body.get("model") or "other").strip() or "other"
    label = str(body.get("label") or "Other calibrated mic").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    # The page's own control defaults to "response" because that is what a
    # measurement-mic calibration file states (see the upload card's help
    # copy and jasper.audio_measurement.calibration.SUPPORTED_MODELS); a
    # caller that omits the field gets the same answer, not the opposite one.
    sign_convention = (
        str(body.get("sign_convention") or DEFAULT_SIGN_CONVENTION).strip()
        or DEFAULT_SIGN_CONVENTION
    )
    record = store_calibration(
        text=text,
        provider="manual_upload",
        model=model,
        label=label,
        source=f"uploaded:{filename}",
        orientation=orientation,
        sign_convention=sign_convention,
        root=_calibration_root(),
    )
    _save_household_mic(record)
    return _calibration_payload(record)






def _handle_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /status: snapshot the current session + currently-loaded
    CamillaDSP config descriptor. `current_correction` is best-effort
    (returns None if CamillaDSP is unreachable) so the page still
    renders something useful when the daemon is restarting."""
    from jasper.dsp_apply import last_dsp_apply_state

    sess = _get_or_create_session()
    snap = sess.snapshot()
    current_config, presentation = _current_config_presentation(sess)
    snap["current_config"] = current_config
    snap["current_correction"] = current_config.get("current_correction")
    snap["current_correction_presentation"] = presentation
    snap["last_dsp_apply"] = last_dsp_apply_state()
    return snap


def _current_config_presentation(sess: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the current Camilla descriptor and its homeowner presentation."""

    from jasper.correction.status import (
        current_correction_presentation,
        describe_current_config,
    )

    cam = _camilla()
    try:
        path = _run_async(
            cam.get_config_file_path(best_effort=True), timeout=2.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("status: get_config_file_path failed")
        path = None
    current_config = describe_current_config(
        path,
        config_dir=sess.cfg.config_dir,
        base_config_path=sess.cfg.base_config_path,
    )
    return current_config, current_correction_presentation(current_config)


def _handle_entry_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """Lightweight idle refresh: screen/state, readiness, and config banner."""

    from jasper.correction import envelope

    sess = _get_or_create_session()
    _current_config, presentation = _current_config_presentation(sess)
    return {
        "screen": envelope.screen_for_session(sess),
        "state": sess.state.value,
        "readiness_blocker": _room_readiness().blocker,
        "current_correction_presentation": presentation,
    }


def _handle_envelope(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /envelope: the server-computed screen envelope for the current
    session. It is a pure presentation read alongside the unchanged
    mechanism snapshot at /status. The browser renders the envelope's exact
    ordered section list and closed action vocabulary without owning a
    second screen policy."""
    from jasper.correction import envelope

    sess = _get_or_create_session()
    screen = envelope.screen_for_session(sess)
    readiness_blocker = None
    if screen == envelope.SCREEN_IDLE:
        readiness_blocker = _room_readiness().blocker

    # Session discovery reads every bundle today, so it is intentionally
    # confined to idle/result static edges. Active screens are fetched every
    # 900 ms and must never inherit this directory scan.
    reports_available = False
    if screen in envelope.REPORT_SECTION_SCREENS:
        from jasper.correction.bundles import list_bundles

        try:
            reports_available = bool(
                list_bundles(sess.cfg.sessions_dir, limit=1)
            )
        except OSError as exc:
            # Reports are optional evidence, never a reason to strand the
            # measurement entry/result screen when storage is unavailable.
            log_event(
                logger,
                "correction.report_discovery_failed",
                session=getattr(sess, "session_id", ""),
                error_type=type(exc).__name__,
                level=logging.WARNING,
            )

    envelope_kwargs: dict[str, Any] = {}
    if screen == envelope.SCREEN_IDLE:
        # Pass an explicit decision only when this read observed idle. If the
        # session races from active back to idle before the pure builder reads
        # it, the omitted argument takes the builder's fail-closed path rather
        # than accidentally treating `None` as a positive readiness decision.
        envelope_kwargs["readiness_blocker"] = readiness_blocker
    return envelope.build_envelope_logged(
        sess,
        reports_available=reports_available,
        **envelope_kwargs,
    )


def _handle_sessions(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /sessions: list recent session bundles for debugging /
    future UI history. Returns the parsed info.json for each entry,
    sorted by started_at desc; capped at 20. Bundles without a
    parseable info.json (in-progress writes, crashed mid-state) are
    skipped silently."""
    from jasper.correction.bundles import list_bundles

    sess = _get_or_create_session()
    return {"sessions": list_bundles(sess.cfg.sessions_dir, limit=20)}


def _handle_session_report(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /session-report?id=<session_id>: return a read-only,
    browser-safe measurement report built from one session bundle.

    This intentionally returns metadata and derived evidence only. Raw
    recordings stay in the private bundle for operator/CLI workflows.
    """
    from . import correction_report

    sess = _get_or_create_session()
    query = parse_qs(urlparse(handler.path).query)
    session_id = (query.get("id") or [""])[0]
    try:
        payload = correction_report.build_session_report_payload(
            sessions_dir=sess.cfg.sessions_dir,
            session_id=session_id,
        )
    except correction_report.InvalidSessionId as e:
        raise BadRequest(str(e)) from e
    log_event(
        logger,
        "correction.session_report",
        session=payload.get("session_id") or session_id,
    )
    return payload


def _handle_session_delete(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /session/delete: delete one historical measurement bundle."""
    import shutil

    from . import correction_report

    sess = _get_or_create_session()
    body = _read_json_body(handler)
    session_id = str(body.get("id") or "")
    try:
        bundle_dir = correction_report.resolve_session_bundle_dir(
            sess.cfg.sessions_dir,
            session_id,
        )
    except correction_report.InvalidSessionId as e:
        raise BadRequest(str(e)) from e
    current_state = getattr(getattr(sess, "state", None), "value", None)
    if (
        session_id == getattr(sess, "session_id", None)
        and current_state in _BUNDLE_DELETE_BLOCKED_STATES
    ):
        raise RequestConflict(
            "cannot delete the measurement bundle for an active session"
        )
    shutil.rmtree(bundle_dir)
    log_event(
        logger,
        "correction.session_bundle_deleted",
        session=session_id,
        bundle=bundle_dir,
    )
    return {"deleted": True, "session_id": session_id}


def _read_wav_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int = MAX_WAV_BODY_BYTES,
) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError as e:
        raise BadRequest("invalid Content-Length") from e
    if length <= 0:
        raise BadRequest("empty body")
    if length > max_bytes:
        raise BadRequest(f"WAV body too large ({length} bytes)")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise BadRequest("incomplete WAV body")
    return raw


def _handle_local_capture_setup(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /local-capture/setup: bind the realized browser input.

    The browser asks for microphone permission after the run is reserved.
    This narrow setup write makes the selected device/calibration the live
    session authority before any audio upload.
    """
    from jasper.audio_measurement.calibration import load_calibration_record
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess.state != SessionState.NEEDS_NOISE_CAPTURE:
        raise RequestConflict("microphone setup is not available now")

    body = _read_json_body(handler)
    requested_session_id = str(body.get("session_id") or "")
    if requested_session_id != sess.session_id:
        raise RequestConflict("this room-correction run is no longer current")
    input_device = _sanitize_input_device(body.get("input_device"))
    if input_device is None:
        raise ValueError("select a microphone before continuing")

    calibration_id = str(body.get("calibration_id") or "").strip()
    mic_calibration = (
        load_calibration_record(calibration_id, root=_calibration_root())
        if calibration_id
        else None
    )
    mismatch = _calibration_device_mismatch(mic_calibration, input_device)
    if mismatch is not None:
        raise ValueError(mismatch)

    try:
        browser_report = _run_async(
            sess.bind_local_capture_setup(
                mic_calibration=mic_calibration,
                input_device=input_device,
            ),
            timeout=3.0,
        )
    except RuntimeError as exc:
        raise RequestConflict("microphone setup is not available now") from exc

    log_event(
        logger,
        "correction.local_capture_setup_bound",
        session=sess.session_id,
        calibrated=mic_calibration is not None,
        browser_audio_level=str(browser_report.get("level") or ""),
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "input_device": sess.input_device,
        "browser_audio_report": browser_report,
        "mic_calibration": (
            mic_calibration.public_metadata() if mic_calibration else None
        ),
    }


def _handle_upload_noise(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /upload-noise: persist pre-sweep silence, then play sweep."""
    from jasper.correction.session import AutolevelStatus, SessionState

    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")
    if sess.state != SessionState.NEEDS_NOISE_CAPTURE:
        raise RuntimeError(
            f"cannot accept noise capture from state {sess.state.value}"
        )
    if not bool(getattr(sess, "local_capture_setup_bound", False)):
        raise RequestConflict(
            "bind the local microphone setup before uploading room noise"
        )
    if (
        sess.autolevel.status != AutolevelStatus.LOCKED
        or bool(getattr(sess, "autolevel_run_in_progress", False))
    ):
        raise RequestConflict(
            "complete and lock the measurement level check before measuring"
        )

    _run_async(sess.resume_capture_timeout_on_loop(), timeout=2.0)
    body = _read_wav_body(handler)
    captured_path = sess.noise_capture_path_for_position(sess.current_position)
    captured_path.parent.mkdir(parents=True, exist_ok=True)
    captured_path.write_bytes(body)
    _run_async(sess.on_noise_capture_uploaded(captured_path), timeout=10.0)
    _schedule_measurement_sweep(
        sess,
        _camilla(),
        from_state=SessionState.NEEDS_NOISE_CAPTURE,
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
        "noise_reports": sess.noise_reports,
        "acoustic_quality": (
            (sess.acoustic_quality or {}).get("summary")
            if sess.acoustic_quality
            else None
        ),
    }


def _handle_repeat_position(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /repeat-position: play the optional same-seat repeat."""
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess.state != SessionState.NEEDS_REPEAT_CAPTURE:
        raise RuntimeError(
            f"cannot repeat main seat from state {sess.state.value}"
        )
    _schedule_repeat_sweep(
        sess,
        _camilla(),
        from_state=SessionState.NEEDS_REPEAT_CAPTURE,
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
    }


def _handle_upload_capture(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /upload-capture: read the WAV body, write to disk, run
    the analysis pipeline. Routes to either the multi-position
    capture path (if state == AWAITING_CAPTURE) or the verify path
    (if state == AWAITING_VERIFY_CAPTURE)."""
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")

    body = _read_wav_body(handler)

    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        captured_path = sess.verify_capture_path()
    elif sess.state == SessionState.AWAITING_REPEAT_CAPTURE:
        captured_path = sess.repeat_capture_path_for_position(0)
    else:
        captured_path = sess.capture_path_for_position(sess.current_position)
    captured_path.parent.mkdir(parents=True, exist_ok=True)
    captured_path.write_bytes(body)

    auto_reverted = False
    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        _run_async(
            sess.on_verify_capture_uploaded(captured_path), timeout=30.0,
        )
        # P4: a CONFIRMED-regression verdict auto-reverts. The verdict was
        # computed inside on_verify_capture_uploaded (pure, no CamillaDSP); the
        # rollback happens here where the CamillaDSP callbacks live, riding the
        # SAME reset target the /reset button uses (Layer B removed, speaker
        # DSP + preference preserved). Every other verdict is a no-op.
        auto_reverted = _maybe_auto_revert(sess)
    elif sess.state == SessionState.AWAITING_REPEAT_CAPTURE:
        _run_async(
            sess.on_repeat_capture_uploaded(captured_path), timeout=30.0,
        )
    else:
        _run_async(sess.on_capture_uploaded(captured_path), timeout=30.0)

    # The upload response is a mechanism acknowledgement, not a second
    # presentation contract. The browser refreshes the server envelope for
    # curves, verdict, nudges, sections, and actions.
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
        "auto_reverted": auto_reverted,
    }










def _handle_crossover_capture_cancel() -> dict[str, Any]:
    """Stop Crossover capture work and keep its slot until cleanup completes.

    The Stop button is already hidden once the rendered status turns terminal
    (crossover/main.js's ``CAPTURE_STOPPABLE`` gate), but a poll-cycle race can
    still let a click reach the server after the capture finished on its own
    (it completed, or another tab already stopped it). ``_request_capture_stop``
    raises a diagnostic message for that case; map it to a plain-language
    sentence here rather than leaking it to the page.
    """

    try:
        capture = _request_capture_stop("crossover_v2:")
    except ValueError:
        raise ValueError(
            "This measurement already stopped — nothing more to do here."
        ) from None
    return {"capture": capture}


def _handle_crossover_v2_position_ready(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/position-ready — a GATED session's position release.

    Whoever moved the microphone read ``capture.position_pending`` off the
    envelope, went to the stated angle, waited their own settle time, and is now
    saying so. Releasing admits the held ``begin_capture`` and the capture
    starts. Two shapes reach here and the verb does not care which: the remote
    tier's external driver, and the person holding the tape on a hand-walked
    wired round (#2879).

    ``index`` is REQUIRED and checked against what is actually pending: a caller
    retrying this POST after its capture already started must not release the
    NEXT position, which is the one way an untargeted release could quietly
    measure a pose the microphone never reached. A retry that still names the
    pending index is idempotent.
    """
    raw = _read_json_body(handler)
    if "index" not in raw:
        raise BadRequest("index is required")
    raw_index = raw["index"]
    # A REAL integer, not merely something ``int()`` accepts. ``int(1.5)`` is 1
    # and ``int(True)`` is 1, so a lenient parse would silently coerce a
    # malformed index into a VALID one and release a position the driver never
    # named — the same class of harm the pending-index check exists to prevent,
    # arriving through the parser instead. One wire shape per meaning, the rule
    # ``parse_begin_capture`` already holds this protocol to.
    if isinstance(raw_index, bool) or not isinstance(raw_index, int):
        raise BadRequest("index must be an integer")
    index = int(raw_index)
    with _session_lock:
        gate = _capture_position_gate
    if gate is None:
        raise ValueError(
            "no remote measurement is waiting for the microphone right now"
        )
    released = gate.release(index)
    return {"ok": True, "released": released}


def _handle_crossover_v2_complete(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/complete — the wired all-spots-measured signal (D1).

    The wired session's stand-in for the phone's authenticated
    complete-capture-set event (#2662 W2b): the driver (or the W3 wizard
    surface) says the household is done measuring, the held pre-apply group
    closes, and the fit runs. Only a live WIRED session holds the signal — a
    a finished session drops it with the slot — so "nothing waiting" is a conflict
    (stale caller), the position-ready shape.
    """
    _read_json_body(handler)  # no fields consumed; drains the request body
    with _session_lock:
        request_complete = _capture_complete_request
    if request_complete is None:
        raise ValueError(
            "no wired measurement is waiting for an all-spots-measured "
            "confirmation right now"
        )
    request_complete()
    return {"ok": True}


def _handle_crossover_v2_retake(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/retake — the wired session's per-take retake.

    The local stand-in for the phone's ``begin_capture {retake: true}``: the
    household (or the W3 wizard surface) says the take that just completed
    should be measured again. The walk re-opens THAT slot the next time it is
    waiting on a person — a held begin, or the held-set window — on the
    same terms.

    **No ``index``, and that is the contract rather than a shortcut.** The
    rule is that a retake names the slot which JUST COMPLETED
    (``retakes_the_just_accepted_slot``: ``index == accepted_count``), and the
    walk is the only thing that knows that number — it is a worker-thread
    local, not a published one. Accepting an index here would mint a second
    answer to "which slot", and the only thing a caller could do with it is
    disagree. The signal says WHAT the household wants; WHICH slot stays the
    walk's own fact.

    Only a live session holds the signal, and a finished session drops it
    with the slot, so "nothing waiting" is a conflict (stale caller), the
    position-ready shape. Whether the retake is then ADMISSIBLE (a take exists
    to replace, the plan's attempts are not spent, the slot's extras ledger
    still has room) is the walk's decision, journalled as
    ``event=correction.crossover_v2_wired_retake_refused``: a refused retake
    leaves the household with the take they already had, which is why it is
    never a session death.
    """
    _read_json_body(handler)  # no fields consumed; drains the request body
    with _session_lock:
        request_retake = _capture_retake_request
    if request_retake is None:
        raise ValueError(
            "no wired measurement is waiting to re-take a spot right now"
        )
    request_retake()
    return {"ok": True}


def _handle_crossover_v2_capture(
    handler: BaseHTTPRequestHandler,
    *,
    verify_only: bool,
    idle_hold: Callable[[str], AbstractContextManager[Any]] = no_hold,
) -> dict[str, Any]:
    """POST /crossover/v2/session | /crossover/v2/verify (Wave 5a).

    Thin dispatch over :mod:`jasper.web.correction_crossover_v2` — the v2 host
    module owns gating, conductor construction, seam bindings, and the plan
    runner; this bridges it into the shared capture slot/lifecycle machinery
    (``_run_capture``) exactly as the other hosted crossover
    captures do.

    ``idle_hold`` covers the one background lifetime a v2 session still owns:
    the capture runner (through ``_run_capture``). It serves no HTTP
    request, and it is the flow the 600 s idle exit actually killed (issue
    #1854). It used to reach a SECOND lifetime — the auto-apply worker thread
    the runner spawned — which the two-stage split removed: the apply is now a
    household POST served in-request, so the tracker's ordinary
    in-flight-request accounting holds the process for it.
    """
    raw = _read_json_body(handler)

    from . import correction_crossover_backend, correction_crossover_v2 as v2host

    blocking = _crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(
            f"another measurement is in progress ({blocking}) — finish it "
            "before starting a crossover measurement session"
        )
    status = correction_crossover_backend.status_payload()
    prepared = v2host.prepare_v2_session(
        raw,
        status=status,
        run_async=_run_async,
        camilla_factory=_camilla,
        verify_only=verify_only,
    )
    kind = CaptureKind(
        label=prepared.label,
        open=prepared.open,
        run_and_consume=prepared.run_and_consume,
        request_stop=prepared.request_stop,
        position_gate=prepared.position_gate,
        request_complete=prepared.request_complete,
        request_retake=prepared.request_retake,
    )
    return {"capture": _run_capture(kind, idle_hold=idle_hold)}


def _handle_crossover_v2_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /crossover/v2/apply: apply the reviewed v2 measured candidate.

    Reads the same ``status_payload()`` the session preparers do, because the
    apply now runs the stage-2 openability preflight server-side (two-stage
    commission work order D3): a speaker that cannot open its post-apply check
    must not be corrected and left ungraded.
    """
    raw = _read_json_body(handler)

    from . import correction_crossover_backend, correction_crossover_v2 as v2host

    return v2host.handle_v2_apply(
        raw,
        _run_async,
        _camilla,
        status=correction_crossover_backend.status_payload(),
    )


def _handle_crossover_v2_republish(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/republish: re-publish a banked candidate by fingerprint.

    Touches no DSP and holds no capture — it replaces the durable session
    document around the published-candidate slot (host-owned apply keys
    carried forward) and moves no graph — so unlike its apply sibling it
    needs neither ``_run_async`` nor ``_camilla`` nor the stage-2
    ``status_payload()``. The apply door still runs every gate it always did,
    on the next request.
    """
    raw = _read_json_body(handler)

    from . import correction_crossover_v2_republish as republish

    return republish.handle_v2_republish(raw)


def _handle_crossover_v2_decline(
    handler: BaseHTTPRequestHandler,
) -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/v2/decline: the review screen's "Keep current sound".

    Touches no DSP and holds no capture, so unlike its apply/restore siblings it
    needs neither ``_run_async`` nor ``_camilla`` — it records a decision and
    re-renders. The capture snapshot rides the response for the same reason
    ``/crossover/reset``'s does: the page renders one envelope per round trip.
    """
    raw = _read_json_body(handler)

    from . import correction_crossover_flow

    return correction_crossover_flow.handle_v2_decline(
        raw,
        capture=_get_capture_slot_for("crossover_v2:"),
    )


def _handle_crossover_reset() -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/reset: in-flow "start over" for the crossover flow.

    Unlike ``_handle_crossover_capture_cancel``, an unstarted capture is the
    COMMON case here (most Start-over clicks happen between measurements,
    not mid-capture), so a "nothing to stop" ``ValueError`` is swallowed
    rather than surfaced. Any crossover-owned capture or level-match ramp is
    requested to stop first; the actual state clear
    (``correction_crossover_flow.handle_reset``) fails closed if that stop
    has not finished draining yet, rather than racing it.
    """

    try:
        _request_capture_stop("crossover_v2:")
    except ValueError:
        pass

    from . import correction_crossover_flow

    return correction_crossover_flow.handle_reset(
        capture=_get_capture_slot_for("crossover_v2:"),
    )


def _maybe_restore_main_volume(sess, cam) -> None:
    """If autolevel ran and locked a measurement-friendly level,
    restore main_volume to the pre-autolevel value after the
    measurement workflow completes (apply or reset). This keeps the
    user's listening level intact across what otherwise would be a
    surprising "music is quieter now" experience.

    Idempotent — skips silently if no autolevel ran in this session.
    """
    # Runs inside the apply/reset `finally`, so the ENTIRE body is
    # best-effort — nothing here may raise, or it would mask the original
    # apply/reset error. The single guard covers the lazy import and the
    # autolevel-state reads too, not just the restore call. A failed restore
    # can strand the volume at the measurement level, but that is logged
    # loudly and is better than swallowing the real error.
    try:
        from jasper.correction.session import AutolevelStatus, SessionState
        from jasper.volume_owner import volume_owner

        owner = volume_owner()
        if owner is None:
            # This process registers one at startup (``web/__main__.main``),
            # so None is a registration defect rather than a state to handle.
            # Loud and skipped: minting a second owner here would be the
            # arbitration failure wave 5 exists to delete, and there is no
            # second write path to fall back to by design.
            log_event(
                logger,
                "correction.autolevel_restore_owner_absent",
                level=logging.CRITICAL,
            )
            return

        restore_level_match = getattr(sess, "restore_level_match_volume", None)
        if callable(restore_level_match):
            async def _restore_level_match() -> bool:
                return await restore_level_match(owner.declare_household_level_db)

            if _run_async(_restore_level_match(), timeout=5.0):
                logger.info(
                    "restored main_volume after the level-match workflow"
                )
                return

        al = sess.autolevel
        if al.original_main_volume_db is None:
            return
        # Only restore when autolevel had a "ran and finished" outcome.
        # If still RAMPING or IDLE, don't interfere.
        if al.status not in {
            AutolevelStatus.LOCKED,
            AutolevelStatus.MAXED_OUT,
        }:
            return
        # Don't restore mid-measurement. We run in apply()/reset()'s finally,
        # so this also fires when one was REJECTED from a transient state — a
        # stale /reset during a sweep, which the server refuses. The sweep
        # still needs the ramped level; dropping it underneath an active
        # measurement would corrupt the capture. Restore only once the
        # workflow has settled (idle / applied / verified / failed).
        if sess.state in {
            SessionState.PREPARING,
            SessionState.SWEEPING,
            SessionState.ANALYZING,
            SessionState.VERIFYING,
        }:
            return

        # THE happy-path restore. The autolevel controller's own
        # `restore_listening_volume_if_ramped` is called only from
        # `session.py`'s `_fail` and its post-VERIFIED arm, neither of which is
        # on the apply path, and it latches `restored` BEFORE its await, so it
        # is one-shot even when its write fails. This is the retry, and on a
        # clean autolevel -> sweep -> /apply run it is the only thing that
        # returns the household to its level.
        #
        # Routed: the household level is DECLARED, not written. Under a
        # higher-ranked claim the declaration is recorded and lands when that
        # claim releases, instead of a blind write racing it.
        #
        # And when the ramp's own session-measurement claim is still held —
        # the ordinary case, since the level it locked is what the sweeps
        # played at — the release and the re-declaration are ONE call, so the
        # fader lands on the household level in a single write instead of
        # stepping through whatever was declared before it.
        claim = _take_autolevel_claim()
        if claim is not None:
            _run_async(
                owner.release(
                    claim, household_level_db=al.original_main_volume_db
                ),
                timeout=5.0,
            )
            in_effect = True
        else:
            in_effect = _run_async(
                owner.declare_household_level_db(al.original_main_volume_db),
                timeout=5.0,
            )
        log_event(
            logger,
            "correction.autolevel_restore_declared",
            level=logging.INFO if in_effect else logging.WARNING,
            to_db=f"{al.original_main_volume_db:.1f}",
            released_claim=claim is not None,
            in_effect=in_effect,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "main_volume restore after autolevel workflow failed "
            "(volume may be left at the measurement level)",
        )


def _handle_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /apply: write YAML + reload CamillaDSP. Restores
    pre-autolevel main_volume if autolevel was used."""
    sess = _get_or_create_session()
    # No confidence pre-check here. Until the nanny burn-down
    # (docs/measurement-loop-doctrine.md deviation (d)) this raised a 422
    # before ``_camilla()`` whenever the confidence report held a
    # ``fail``-severity finding — a prediction about how good the evidence was
    # refusing a reversible, measurable experiment, which is not on the
    # doctrine's closed hard-stop list. The doubt now rides to the household as
    # a ``warn`` nudge on the envelope (``jasper.correction.envelope._nudges``)
    # and the apply proceeds. What still bounds this path is structural and
    # unchanged: the session state machine, the room-authority binding checked
    # in ``prepare_guard``, and the volume restore below.
    cam = _camilla()

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    async def _get() -> str | None:
        return await cam.get_config_file_path(best_effort=True)

    try:
        _run_graph_mutation(
            sess.apply(
                _set,
                camilla_get_config=_get,
                prepare_guard=lambda: _assert_room_authority_current(
                    cam,
                    sess.room_authority_binding,
                ),
            )
        )
    finally:
        # Audio-safety: autolevel may have ramped main_volume well above the
        # listening level for measurement SNR. Restore it even if apply()
        # raised, so a failed apply never strands the speaker loud.
        _maybe_restore_main_volume(sess, cam)
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "config_path": (
            str(sess.config_path) if sess.config_path else None
        ),
    }


# --- P6: the tuning LLM surfaced in the flow (per-tap, confirm-gated) ---
#
# Each of these makes at most one PAID call, only on an explicit user tap
# (no polling — the envelope's `tuning_llm` block gates the button, but
# the paid call happens only here). The surface is hidden with a nudge
# when no OpenAI key is configured; if a request still arrives without a
# key, the availability preflight returns the closed 409 setup-unavailable
# failure. Provider/advisor request failures remain closed 400 responses.

def _require_tuning_key() -> None:
    from jasper.calibration_agent.key_provisioning import tuning_llm_available

    if not tuning_llm_available():
        raise TuningSetupUnavailable(
            "the tuning assistant needs an OpenAI key — add one at /voice"
        )


def _handle_interpret(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /interpret: one paid call. Read-only "explain my room"."""
    from jasper.calibration_agent import correction_advisor

    _require_tuning_key()
    body = _read_json_body(handler)
    user_message = body.get("message")
    if user_message is not None and not isinstance(user_message, str):
        raise BadRequest("message must be a string")
    sess = _get_or_create_session()

    def _advisor_call(
        *,
        user_message: str | None,
        timeout_sec: float,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        return correction_advisor.interpret(
            sess,
            user_message=user_message,
            timeout_sec=timeout_sec,
            max_output_tokens=max_output_tokens,
        )

    try:
        return correction_tuning.interpret(
            _advisor_call, user_message=user_message,
        )
    except correction_tuning.TuningBusy as exc:
        raise RequestConflict(str(exc)) from exc
    except correction_tuning.TuningProviderError as exc:
        raise BadRequest(str(exc)) from exc


def _handle_propose(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /propose: one paid call. The confirm-gated proposer.

    Nothing is applied here — proposals are validated + deterministically
    simulated, and returned with what the simulation predicts for the UI
    to surface for user confirmation. Applying happens only via
    /propose/apply.
    """
    from jasper.calibration_agent import correction_advisor

    _require_tuning_key()
    body = _read_json_body(handler)
    user_message = body.get("message")
    if user_message is not None and not isinstance(user_message, str):
        raise BadRequest("message must be a string")
    sess = _get_or_create_session()

    def _advisor_call(
        *,
        user_message: str | None,
        timeout_sec: float,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        return correction_advisor.propose(
            sess,
            user_message=user_message,
            timeout_sec=timeout_sec,
            max_output_tokens=max_output_tokens,
        )

    try:
        return correction_tuning.propose(
            _advisor_call, user_message=user_message,
        )
    except correction_tuning.TuningBusy as exc:
        raise RequestConflict(str(exc)) from exc
    except correction_tuning.TuningProviderError as exc:
        raise BadRequest(str(exc)) from exc


def _handle_propose_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /propose/apply: apply a confirmed correction proposal.

    NO paid call. The body carries the proposed ``correction_peqs`` (from
    a prior /propose response) and an explicit ``confirm: true``. The
    server RE-VALIDATES the set against the active strategy caps, then
    populates ``session.peqs`` and routes through the EXISTING apply path
    (the same simulate/headroom/re-clip apply any correction gets).

    The simulation rides along as DISCLOSURE — predicted curve, predicted
    improvement, ring Q and summed boost against their ceilings — and
    refuses nothing. What holds this path is the strategy caps re-checked
    below, the explicit confirm, and the emitter's re-clip at apply.
    """
    from jasper.calibration_agent import proposal_sim, response as advisor_response
    from jasper.correction.session import PEQJSON, SessionState

    body = _read_json_body(handler)
    if body.get("confirm") is not True:
        raise BadRequest("apply requires explicit confirm: true")
    raw_peqs = body.get("correction_peqs")
    if not isinstance(raw_peqs, list) or not raw_peqs:
        raise BadRequest("correction_peqs must be a non-empty list")

    sess = _get_or_create_session()
    if sess.state != SessionState.READY:
        raise RequestConflict(
            f"cannot apply a proposal from state {sess.state.value}; "
            "the correction must be in the review (READY) state"
        )
    from jasper.correction import failures

    # The confidence pre-check that stood here went with the nanny burn-down,
    # for the reason `_handle_apply` records; the doubt reaches the household
    # as a nudge instead. The bounds below are the ones that were always
    # load-bearing on this path, and they are untouched.
    # Re-validate schema + bounds against the ACTIVE strategy caps.
    from jasper.correction import strategy as _strategy
    strat = _strategy.resolve_correction_strategy(
        getattr(sess, "strategy_choice", None)
        or _strategy.DEFAULT_CORRECTION_STRATEGY_ID
    )
    bounds = strat.to_dict()
    packet = {"correction": {"strategy_bounds": bounds}}
    validation = advisor_response.validate_advisor_response(
        {
            "artifact_schema_version": advisor_response.RESPONSE_SCHEMA_VERSION,
            "kind": "jts_advisor_response",
            "action_plan": [{
                "type": advisor_response.ACTION_PROPOSE_CORRECTION_PEQ,
                "correction_peqs": raw_peqs,
                "rationale": "user-confirmed proposal re-check",
            }],
        },
        advisor_context=packet,
    )
    if not validation["accepted"]:
        return {
            "applied": False,
            "failure": failures.public_failure(
                failures.TUNING_PROPOSAL_REJECTED,
            ),
            "reason": "proposal failed re-validation against strategy caps",
            "issues": validation["issues"],
            "session_id": sess.session_id,
            "state": sess.state.value,
        }
    validated_peqs = validation["validated_action_plan"][0]["correction_peqs"]

    # Simulate server-side for the disclosure numbers; a client cannot
    # author them for us.
    sim = proposal_sim.simulate_correction_proposal(
        validated_peqs,
        measured=getattr(sess, "measured_curve", None),
        baseline=getattr(sess, "position1_curve", None)
        or getattr(sess, "measured_curve", None),
        target=getattr(sess, "target_curve", None),
        max_total_boost_db=float(bounds.get("max_total_boost_db", 0.0)),
        # Fallback routed through the room-correction boundary SSOT rather
        # than re-declared, so an advisor proposal simulated without explicit
        # bounds is judged against the same ceiling the designer used
        # (issue #1787).
        f_high_hz=float(bounds.get("f_high_hz", room_boundary.ROOM_BOUNDARY_DEFAULT_HZ)),
    )
    # Bounds re-checked and the household confirmed: swap in the proposed
    # filters and route through the SAME apply path any correction uses
    # (which re-clips headroom at emit).
    log_event(
        logger,
        "correction.tuning_apply",
        session_id=sess.session_id,
        filter_count=len(validated_peqs),
        sim_rms_delta_db=sim.predicted_rms_delta_db,
        sim_issues=[i.code for i in sim.issues],
    )
    sess.peqs = [
        PEQJSON(freq_hz=p["freq_hz"], q=p["q"], gain_db=p["gain_db"])
        for p in validated_peqs
    ]
    result = _handle_apply(handler)
    # Derive success from the actual outcome, never stamp it: session.apply
    # deliberately swallows the CamillaDSP-rejected-reload failure (state ->
    # FAILED, no exception raised), and claiming "applied" while the speaker
    # kept its previous sound would be a dishonest success message.
    result["applied"] = result.get("state") == "applied"
    if not result["applied"]:
        result["failure"] = failures.public_failure(
            failures.CORRECTION_UPDATE_FAILED,
        )
        result["reason"] = "couldn't apply — the speaker kept its previous sound"
    result["simulation"] = sim.to_dict()
    return result


def _accepts_target_config_path(fn: Any) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    if "target_config_path" in params:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _handle_reset(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /reset: cancel a measurement or strip active room correction.

    If a measurement is in progress (or failed before apply), restore the graph
    that was active before `/start`. Once a correction is applied, reset means
    "remove Layer B" — re-emit the current graph with room PEQs cleared while
    preserving topology-owned speaker DSP and current preference EQ.
    """
    sess = _get_or_create_session()
    cam = _camilla()

    reset_intent = None
    if hasattr(sess, "begin_autolevel_reset"):
        reset_intent = _run_async(sess.begin_autolevel_reset(), timeout=45.0)
    else:
        # Duck-typed test/legacy sessions retain the old seam. Production
        # MeasurementSession uses the atomic reset intent above.
        autolevel_status = getattr(
            getattr(sess, "autolevel", None), "status", None
        )
        autolevel_active = bool(
            getattr(
                sess,
                "autolevel_run_in_progress",
                getattr(autolevel_status, "value", None) == "ramping",
            )
        )
        if autolevel_active:
            _run_async(sess.cancel_autolevel_and_wait(), timeout=7.0)

    try:
        if hasattr(sess, "stop_background_audio_for_reset"):
            _run_async(sess.stop_background_audio_for_reset(), timeout=45.0)
        _run_graph_mutation(_run_locked_room_reset(sess, cam))
    finally:
        # Audio-safety: restore the pre-autolevel listening level even if
        # reset() raised (see _handle_apply).
        try:
            _maybe_restore_main_volume(sess, cam)
        finally:
            if reset_intent is not None:
                _run_async(sess.end_autolevel_reset(reset_intent), timeout=2.0)
    return {"session_id": sess.session_id, "state": sess.state.value}


async def _pre_measurement_restore_target(sess: Any, cam: Any) -> Path | None:
    """Prior graph to restore only while this measurement still owns Camilla."""
    state_value = getattr(getattr(sess, "state", None), "value", None)
    if state_value in {"idle", "applied", "verified"}:
        return None
    prior = getattr(sess, "pre_measurement_config_path", None)
    restore = getattr(sess, "pre_measurement_restore_path", None)
    if not prior or not restore:
        return None

    current = await cam.get_config_file_path(best_effort=False)
    if not current:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    measurement = getattr(sess, "measurement_config_path", None)
    owned_path = Path(measurement) if measurement else Path(prior)
    prior_path = Path(prior)
    restore_path = Path(restore)
    if Path(current) in {owned_path, restore_path}:
        return restore_path
    if Path(current) == prior_path:
        # A durable Active filename can be overwritten by a blocked candidate
        # without CamillaDSP loading those new bytes. Compare the daemon's
        # running graph with Start's immutable snapshot; filename equality by
        # itself is not evidence that either the old or new content is active.
        raw = await cam.get_active_config_raw(best_effort=False)
        try:
            saved = restore_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                "Room's immutable predecessor snapshot is unavailable"
            ) from exc
        if _running_graph_body(raw) == _running_graph_body(saved):
            return restore_path

    # A legal DSP writer may publish a newer Active graph after Room Start.
    # The shared lock makes this read stable; never use Room's saved predecessor
    # once Camilla has moved away from Room's own measurement graph. The caller
    # will instead strip Room from the fresh current graph, preserving new A.
    log_event(
        logger,
        "correction.pre_measurement_predecessor_superseded",
        session=getattr(sess, "session_id", None),
        current=str(current),
        room_owned=str(owned_path),
        saved_predecessor=str(prior),
        immutable_restore=str(restore_path),
        level=logging.WARNING,
    )
    return None


async def _resolve_reset_target_async(sess: Any, cam: Any) -> Path:
    """Resolve the graph to restore for a reset / auto-revert.

    The single source of truth for "what should the speaker load when we undo
    room correction," shared by ``POST /reset`` (user-driven) and the P4
    confirmed-regression auto-revert (deterministic). If a measurement is
    mid-flight and Camilla still runs Room's measurement graph, restore the
    pre-``/start`` graph. If another legal writer has since published a graph,
    or once a correction is applied/verified, re-emit that current topology
    with room PEQs cleared (Layer B removed, speaker DSP + preference EQ
    preserved). A re-emit failure may retain only the observably managed,
    no-Room graph captured from Camilla's active_raw before re-emit; otherwise
    reversal fails loudly without claiming that Layer B was removed.
    """
    cfg = getattr(sess, "cfg", None)
    base_config_path = getattr(
        cfg,
        "base_config_path",
        Path("/etc/camilladsp/outputd-cutover.yml"),
    )
    target = await _pre_measurement_restore_target(sess, cam)
    if target is None:
        (
            _current,
            current_snapshot,
            bass_profile_summary,
        ) = await _snapshot_running_room_graph(sess, cam)
        try:
            target = await _write_no_room_correction_config(
                sess,
                cam,
                current_snapshot_path=current_snapshot,
                bass_profile_summary=bass_profile_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "reset/auto-revert: no-room re-emit failed; checking the "
                "fresh current graph",
            )
            from jasper.correction.status import describe_current_config

            config_dir = Path(
                getattr(cfg, "config_dir", None)
                or "/var/lib/camilladsp/configs"
            )
            # This immutable snapshot was captured and safety-validated before
            # the failed re-emit wrote its separate candidate. Never re-read a
            # mutable current filename here: it may be the rejected output.
            target = current_snapshot
            descriptor = describe_current_config(
                str(target),
                config_dir=config_dir,
                base_config_path=Path(base_config_path),
            )
            fallback_kind = descriptor.get("kind")
            fallback_is_no_room = (
                descriptor.get("managed") is True
                and descriptor.get("current_correction") is None
                and fallback_kind
                in {"base", "active_speaker", "sound_preference"}
            )
            if not fallback_is_no_room:
                log_event(
                    logger,
                    "correction.reset_fallback_rejected",
                    session=getattr(sess, "session_id", None),
                    target=str(target),
                    kind=fallback_kind,
                    managed=descriptor.get("managed"),
                    room_correction_present=isinstance(
                        descriptor.get("current_correction"),
                        dict,
                    ),
                    level=logging.ERROR,
                )
                raise RuntimeError(
                    "Room correction could not be removed because no verified "
                    "no-Room graph is available; the current graph remains "
                    "loaded"
                ) from exc
            log_event(
                logger,
                "correction.reset_fallback_selected",
                session=getattr(sess, "session_id", None),
                target=str(target),
                kind=fallback_kind,
                level=logging.WARNING,
            )
    return target


async def _run_locked_room_reset(
    sess: Any,
    cam: Any,
    *,
    automatic: bool = False,
) -> Any:
    """Resolve and load one Room reversal under the shared DSP-writer lock."""

    from jasper.dsp_apply import dsp_writer_lock

    cfg = getattr(sess, "cfg", None)
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None:
        raise RuntimeError("Room session has no CamillaDSP config directory")

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    operation = sess.auto_revert if automatic else sess.reset
    source = "correction_auto_revert" if automatic else "correction_reset"
    async with dsp_writer_lock(config_dir, source=source):
        # Restoration must not depend on fresh Room authority: its purpose is
        # to recover from a stale/failed Room session.  It does need to resolve
        # the no-Room carrier after admission so a legal Active writer cannot
        # swap Layer A between target construction and load.
        target = await _resolve_reset_target_async(sess, cam)
        kwargs = (
            {"target_config_path": target}
            if _accepts_target_config_path(operation)
            else {}
        )
        return await operation(_set, **kwargs)


def _maybe_auto_revert(sess: Any) -> bool:
    """Perform the P4 auto-revert when the verdict is a confirmed regression.

    Reads ``sess.acceptance_verdict``; only ``revert`` acts. Resolves the same
    reset target ``/reset`` uses and drives the session's ``auto_revert`` (which
    rides the existing ``reset()`` reversal). Returns True when a rollback ran.
    Best-effort: an auto-revert failure is logged and leaves the correction
    applied with the ``revert`` verdict still visible — the household can undo
    manually — rather than 500-ing the verify upload response. reset() itself
    fails the session loudly on a CamillaDSP rejection, so a failed revert is
    never silent.

    Failure honesty: when the attempt dies BEFORE the session could record an
    outcome (for example, target-resolution failure), a "failed" outcome is
    stamped here so the result screen says the correction is STILL APPLIED.
    The stamp never overwrites a recorded outcome; after any shared writer
    admission, graph mutation runs to a terminal result, so success is never
    reported as a timeout-driven cancellation.
    """
    if getattr(sess, "acceptance_verdict", None) != "revert":
        return False
    cam = _camilla()

    try:
        return bool(
            _run_graph_mutation(
                _run_locked_room_reset(sess, cam, automatic=True)
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "P4 auto-revert failed; correction left applied for manual undo",
        )
        if getattr(sess, "auto_revert_outcome", None) is None:
            sess.auto_revert_outcome = {"result": "failed", "at": time.time()}
        return False


async def _write_no_room_correction_config(
    sess: Any,
    cam: Any,
    *,
    current_snapshot_path: str | Path | None = None,
    bass_profile_summary: Mapping[str, Any] | None = None,
) -> Path:
    """Emit the current graph with room correction cleared.

    For passive/full-range graphs this is an ordinary sound config. For active
    baselines it remains an active graph. The candidate is session-unique so a
    validation failure cannot alter the durable filename Camilla is running.
    """

    from jasper.correction.runtime_safety import assert_correction_graph_safe
    from jasper.dsp_apply import validate_camilla_config
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.profile import load_profile

    cfg = getattr(sess, "cfg", None)
    config_dir = Path(
        getattr(cfg, "config_dir", Path("/var/lib/camilladsp/configs"))
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    if current_snapshot_path is None:
        (
            _current,
            snapshot_path,
            bass_profile_summary,
        ) = await _snapshot_running_room_graph(sess, cam)
    else:
        snapshot_path = Path(current_snapshot_path)
    # Never emit over Camilla's reported current filename. Some JTS writers use
    # durable names such as sound_current.yml; post-write validation failure
    # must leave that live predecessor's bytes untouched.
    out_path = _room_graph_artifact_path(sess, "reset")
    carrier = carrier_for_loaded_config(snapshot_path, config_dir=config_dir)
    profile = load_profile()
    result = carrier.reemit(
        profile,
        room_peqs=[],
        out_path=out_path,
        profile_id=f"correction-reset-{time.time_ns()}",
        fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
    )
    assert_correction_graph_safe(
        result.yaml,
        bass_profile_summary=bass_profile_summary,
    )
    validation = validate_camilla_config(out_path)
    if not validation.ok_to_apply:
        raise RuntimeError(
            "the generated no-Room graph failed CamillaDSP validation: "
            f"{validation.error or validation.status.value}"
        )
    log_event(
        logger,
        "correction.reset_no_room_config",
        current_snapshot=str(snapshot_path),
        candidate=str(out_path),
        room_peqs=result.room_peq_count,
    )
    return out_path


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(
            self, payload: dict[str, Any], *, status: int = 200,
        ) -> None:
            send_json_response(self, payload, status=status)

        def _serve_json_route(
            self, label: str,
            handler_fn: Callable[
                [BaseHTTPRequestHandler],
                dict[str, Any] | tuple[dict[str, Any], int],
            ],
        ) -> None:
            """Shared JSON GET-route wrapper: any handler failure surfaces
            as a 500 JSON error instead of a stack-trace page or a dead
            request thread — the poll posture /status, /envelope, and
            /sessions share (one wrapper so the blanket net isn't
            re-declared per route)."""
            try:
                result = handler_fn(self)
                payload, status = result if isinstance(result, tuple) else (result, 200)
                self._send_json(payload, status=int(status))
            except Exception as e:  # noqa: BLE001 — route-level 500 net
                logger.exception("%s failed", label)
                self._send_json({"error": str(e)}, status=500)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_text(self, text: str, *, status: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_client_error(
            self, message: str, *, status: int = 400,
        ) -> None:
            self._send_json({"error": message}, status=status)

        def _send_room_failure(
            self,
            failure: Mapping[str, Any],
            *,
            diagnostic: str,
            status: int,
        ) -> None:
            public = dict(failure)
            log_event(
                logger,
                "correction.homeowner_failure",
                code=str(public.get("code") or "unknown_failure"),
                retryable=bool(public.get("retryable")),
                status=int(status),
                diagnostic=diagnostic,
                level=logging.WARNING,
            )
            self._send_json(
                {"failure": public},
                status=status,
            )

        def _dispatch_balance(self, path: str) -> None:
            """POST /balance/* — the pair-balance walkthrough
            (balance_flow). /start additionally requires the
            correction session to be idle: both flows open
            measurement_window, and this is where the correction side
            of the mutual exclusion lives (the balance side lives in
            _reserve_start_slot)."""
            from . import balance_flow

            def _schedule(coro):
                return asyncio.run_coroutine_threadsafe(
                    coro, _ensure_loop())

            try:
                if path == "/balance/start":
                    blocked = _correction_start_blocker()
                    if blocked is not None:
                        self._send_json(
                            {"ok": False, "error": (
                                "a room-correction session is active "
                                f"({blocked})"
                            )},
                            status=HTTPStatus.CONFLICT)
                        return
                    payload, status = balance_flow.handle_start(
                        cfg["hostname"], _schedule)
                elif path == "/balance/ramp":
                    payload, status = balance_flow.handle_ramp(
                        self, _run_async, _schedule)
                elif path == "/balance/meter":
                    payload, status = balance_flow.handle_meter(self)
                elif path == "/balance/lock":
                    payload, status = balance_flow.handle_lock(self)
                elif path == "/balance/stop":
                    payload, status = balance_flow.handle_stop()
                elif path == "/balance/apply":
                    payload, status = balance_flow.handle_apply(self)
                else:  # /balance/reset
                    payload, status = balance_flow.handle_stop()
                self._send_json(payload, status=int(status))
            except Exception as e:  # noqa: BLE001
                logger.exception("%s failed", path)
                self._send_json({"ok": False, "error": str(e)},
                                status=500)

        def _dispatch_sync(self, path: str) -> None:
            """POST /sync/* — stereo-pair acoustic timing walkthrough."""
            from . import sync_flow

            def _schedule(coro):
                return asyncio.run_coroutine_threadsafe(
                    coro, _ensure_loop())

            try:
                if path == "/sync/start":
                    blocked = _correction_start_blocker()
                    if blocked is not None:
                        self._send_json(
                            {"ok": False, "error": (
                                "a room-correction session is active "
                                f"({blocked})"
                            )},
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                    payload, status = sync_flow.handle_start(
                        cfg["hostname"], _schedule)
                elif path == "/sync/play":
                    payload, status = sync_flow.handle_play(
                        _run_async, _schedule)
                elif path == "/sync/analyze":
                    try:
                        body = _read_wav_body(
                            self,
                            max_bytes=MAX_SYNC_WAV_BODY_BYTES,
                        )
                    except BadRequest as e:
                        self._send_json(
                            {"ok": False, "error": str(e)},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    payload, status = sync_flow.handle_analyze(body)
                elif path == "/sync/apply":
                    payload, status = sync_flow.handle_apply(self)
                else:
                    payload, status = sync_flow.handle_stop()
                self._send_json(payload, status=int(status))
            except Exception as e:  # noqa: BLE001
                logger.exception("%s failed", path)
                self._send_json({"ok": False, "error": str(e)}, status=500)

        def _dispatch_crossover(self, path: str) -> None:
            """POST /crossover/* — secure active-crossover measurement."""

            if path in {"/crossover/v2/session", "/crossover/v2/verify"}:
                # v2 commission sessions (Wave 5a). ValueError covers both the
                # host's typed CrossoverV2Refused (a subclass) and shared
                # precondition refusals — same contract as the capture routes.
                try:
                    self._send_json(
                        _handle_crossover_v2_capture(
                            self,
                            verify_only=(path == "/crossover/v2/verify"),
                            idle_hold=cfg["idle_hold"],
                        )
                    )
                except ValueError as e:
                    # Log the refusal so it is debuggable from the journal,
                    # not just visible as a 400 in the browser. A session-open
                    # refusal never reaches the envelope, because the envelope
                    # renders from a PERSISTED failure and the pre-flight
                    # deliberately refuses before any state is written. So the
                    # reason's own action rides the 400 body instead — the
                    # wizard renders it as a button beside the message, and the
                    # household is one click from the fix rather than one
                    # navigation plus one click. Same registry entry the
                    # hard-stop screen would have read.
                    from jasper.web.correction_crossover_v2 import (
                        refusal_next_action,
                    )

                    refusal_body: dict[str, Any] = {"ok": False, "error": str(e)}
                    action = refusal_next_action(e)
                    if action is not None:
                        refusal_body["next_action"] = action
                    log_event(
                        logger,
                        "correction.crossover_v2_refused",
                        level=logging.WARNING,
                        route=path,
                        reason=str(e),
                        code=str(getattr(e, "code", "") or ""),
                    )
                    self._send_json(
                        refusal_body,
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    # Issue #1833: a CrossoverV2FlowError raised SYNCHRONOUSLY
                    # inside prepare_v2_session's `_open` (the spec/index-map
                    # builders) reaches here, not the 400 arm above -- it is a
                    # RuntimeError subclass, so `except ValueError` misses it.
                    # `str(e)` then put a programmer string
                    # ("cloud_measure_positions must be 6..12, got 14") straight
                    # into the wizard's DOM. Route it through the ONE mapper the
                    # rest of this module already uses; it is the identity for
                    # everything outside the mapped families, so nothing else on
                    # this arm changes. The raw string still reaches the journal
                    # via logger.exception above.
                    logger.exception("%s failed", path)
                    self._send_json(
                        {"ok": False, "error": _capture_failure_message(e)},
                        status=500,
                    )
                return

            if path == "/crossover/v2/position-ready":
                # A release that names the wrong (or no) pending capture is a
                # CONFLICT, not a malformed request: the driver's view of the
                # session is simply stale, which is the ordinary outcome of a
                # retry that crossed a capture starting — so it answers 409,
                # the same status a refused transition maps to elsewhere here.
                try:
                    self._send_json(_handle_crossover_v2_position_ready(self))
                except BadRequest as e:
                    # A malformed body is a 400, and BadRequest subclasses
                    # ValueError — so it has to be claimed BEFORE the 409 arm
                    # below, which would otherwise report a parse failure as a
                    # stale-release conflict.
                    #
                    # It must also be ANSWERED here, not re-raised: this arm
                    # sits above ``_dispatch_crossover``'s own
                    # ``except BadRequest`` (that one guards the later routes
                    # inside its own ``try``), and ``do_POST`` calls this
                    # dispatcher bare — so a re-raise escapes into
                    # ``socketserver.BaseServer.handle_error``, which logs a
                    # traceback and drops the connection with NO response at
                    # all. The driver sees a closed socket instead of the
                    # reason its body was rejected.
                    self._send_client_error(str(e))
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.CONFLICT,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == "/crossover/v2/complete":
                # Same shape as position-ready: a malformed body is a 400
                # (BadRequest subclasses ValueError, so it must be claimed
                # first), while a signal with no wired session waiting is a
                # CONFLICT — a stale caller, not a malformed request.
                try:
                    self._send_json(_handle_crossover_v2_complete(self))
                except BadRequest as e:
                    self._send_client_error(str(e))
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.CONFLICT,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == "/crossover/v2/retake":
                # The completion signal's shape exactly, and for the same
                # reasons: 400 for a malformed body, 409 for a signal no wired
                # session is waiting for.
                try:
                    self._send_json(_handle_crossover_v2_retake(self))
                except BadRequest as e:
                    self._send_client_error(str(e))
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.CONFLICT,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == "/crossover/v2/apply":
                try:
                    payload = _handle_crossover_v2_apply(self)
                    # Finding N: a blocked apply must not read as success — the
                    # same "compute status from payload contents" shape
                    # the capture routes already use above.
                    self._send_json(
                        payload,
                        status=(
                            HTTPStatus.CONFLICT
                            if payload.get("status") == "blocked"
                            else HTTPStatus.OK
                        ),
                    )
                except ValueError as e:
                    # This arm answered 400 with the raw string and journaled
                    # NOTHING, which the session/verify arm above had already
                    # ruled a defect ("the 400 response is correct for the
                    # browser; the gap was purely observability" --
                    # test_crossover_v2_refusal_is_logged_not_silent). Every
                    # ValueError leaving here is now recorded; what differs is
                    # the SEVERITY, because the two halves are different events.
                    #
                    # A refusal (CrossoverV2Refused) or a malformed body
                    # (BadRequest) is the caller being told no -- WARNING, under
                    # the vocabulary the sibling already owns for "a v2 route
                    # refused", which likewise exempts neither. Anything else is
                    # the speaker faulting on its own apply path, which #2839's
                    # `allow_nan=False` refusal in `save_v2_state` made
                    # reachable -- ERROR, and named for what it is.
                    from jasper.web.correction_crossover_v2 import (
                        CrossoverV2Refused,
                    )
                    if isinstance(e, (BadRequest, CrossoverV2Refused)):
                        log_event(
                            logger,
                            "correction.crossover_v2_refused",
                            level=logging.WARNING,
                            route=path,
                            reason=str(e),
                            code=str(getattr(e, "code", "") or ""),
                        )
                    else:
                        log_event(
                            logger,
                            "correction.crossover_v2_apply_fault",
                            level=logging.ERROR,
                            error_type=type(e).__name__,
                            error=str(e),
                        )
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == "/crossover/v2/republish":
                try:
                    # No payload-derived status: every refusal is a
                    # CrossoverV2Refused (a ValueError -> 400 below), and a
                    # success only moves a pointer, so there is no third
                    # "blocked" outcome to classify like apply/restore have.
                    self._send_json(_handle_crossover_v2_republish(self))
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == "/crossover/v2/decline":
                try:
                    payload, status = _handle_crossover_v2_decline(self)
                    self._send_json(payload, status=int(status))
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            from . import correction_crossover_backend as crossover_backend

            volume_sensitive_routes = {
                "/crossover/reset",
            }
            lease = crossover_backend.level_lease()
            if (
                path in volume_sensitive_routes
                and lease.unresolved_volume_safety is not None
            ):
                self._send_json(
                    _crossover_volume_safety_refusal(),
                    status=HTTPStatus.CONFLICT,
                )
                return

            try:
                if path == "/crossover/recover-volume":
                    from jasper.camilla import CamillaUnavailable

                    # When the v2 session owns the unresolved (or
                    # crash-hydrated active) session volume, route to its
                    # plan's recover_unresolved — the legacy lease holds no
                    # unresolved state for a v2 session, so routing there
                    # instead would 409 crossover_volume_recovery_not_required
                    # and leave the volume_recovery screen's own button dead.
                    from . import correction_crossover_v2 as v2host

                    if v2host.v2_volume_recovery_active():
                        succeeded, recovery = v2host.recover_session_volume(
                            _run_async, _camilla
                        )
                        # A deferral is not a failure to recover, so it must
                        # not send the household after CamillaDSP: a live
                        # measurement session holds the fader and the restore
                        # lands when that session finishes.
                        if succeeded:
                            next_step = (
                                "Refresh and continue crossover commissioning."
                            )
                        elif recovery == v2host.RECOVERY_DEFERRED:
                            next_step = (
                                "A measurement session still holds the volume. "
                                "It is restored when that session finishes."
                            )
                        else:
                            next_step = (
                                "Stop playback and retry recovery when "
                                "CamillaDSP is available."
                            )
                        self._send_json(
                            {
                                "status": "recovered" if succeeded else "refused",
                                "recovery": recovery,
                                "next_step": next_step,
                            },
                            status=(
                                HTTPStatus.OK if succeeded else HTTPStatus.CONFLICT
                            ),
                        )
                        return

                    if lease.unresolved_volume_safety is None:
                        self._send_json(
                            {
                                "status": "refused",
                                "reason": "crossover_volume_recovery_not_required",
                                "next_step": "Refresh the crossover page.",
                            },
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                    cam = _camilla()
                    from jasper.volume_owner import volume_owner

                    recovery_owner = volume_owner()
                    if recovery_owner is None:
                        log_event(
                            logger,
                            "correction.crossover_level_volume_recovery_owner_absent",
                            level=logging.CRITICAL,
                        )
                        self._send_json(
                            {
                                "status": "refused",
                                "reason": "crossover_volume_recovery_unavailable",
                                "next_step": "Restart the speaker, then retry.",
                            },
                            status=HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                        return

                    # Routed: the recovery DECLARES the household level rather
                    # than writing the fader itself. The lease keeps its
                    # exact-then-emergency ladder and still proves each rung
                    # through its own readback below, which is what makes a
                    # declaration that was merely RECORDED under a higher-ranked
                    # claim read as "not yet safe" instead of clearing the
                    # durable intent early.
                    async def _set_recovery_volume(db: float) -> bool:
                        return await recovery_owner.declare_household_level_db(db)

                    async def _get_recovery_volume() -> float:
                        try:
                            value = await cam.get_volume_db(best_effort=False)
                        except CamillaUnavailable as exc:
                            raise RuntimeError(
                                "CamillaDSP is unavailable during volume recovery"
                            ) from exc
                        if value is None:
                            raise RuntimeError(
                                "CamillaDSP did not report the recovered volume"
                            )
                        return float(value)

                    try:
                        recovery = _run_async(
                            lease.recover_unresolved_volume_safety(
                                _set_recovery_volume,
                                _get_recovery_volume,
                            ),
                            timeout=_CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S,
                        )
                    except concurrent.futures.TimeoutError:
                        log_event(
                            logger,
                            "correction.crossover_level_volume_safety_recovery_timeout",
                            level=logging.ERROR,
                            timeout_s=_CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S,
                        )
                        recovery = (
                            crossover_backend.UnresolvedVolumeRecoveryResult.FAILED
                        )
                    succeeded = recovery is not (
                        crossover_backend.UnresolvedVolumeRecoveryResult.FAILED
                    )
                    self._send_json(
                        {
                            "status": "recovered" if succeeded else "refused",
                            "recovery": recovery.value,
                            "next_step": (
                                "Refresh and continue crossover commissioning."
                                if succeeded
                                else "Stop playback and retry recovery when CamillaDSP is available."
                            ),
                        },
                        status=(HTTPStatus.OK if succeeded else HTTPStatus.CONFLICT),
                    )
                    return

                if path == "/crossover/capture-cancel":
                    self._send_json(_handle_crossover_capture_cancel())
                    return

                if path == "/crossover/reset":
                    payload, status = _handle_crossover_reset()
                    self._send_json(payload, status=int(status))
                    return

                raise ValueError(f"unknown crossover route: {path}")
            except BadRequest as e:
                self._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except ValueError as e:
                self._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except (OSError, RuntimeError, TypeError) as e:
                logger.exception("%s failed", path)
                self._send_json({"ok": False, "error": str(e)}, status=500)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/",
                "/room",
                "/healthz",
                "/status",
                "/entry-status",
                "/envelope",
                "/sessions",
                "/session-report",
                "/calibration/models",
                "/crossover",
                "/measurements",
                "/measurements/data",
                "/crossover/status",
                "/crossover/envelope",
                "/bass",
                "/bass/status",
                "/balance",
                "/balance/status",
                "/sync",
                "/sync/status",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_read_request(self):
                return
            if bonded_follower_active() and path in _FOLLOWER_DELEGATED_PAGE_PATHS:
                ctx = begin_request(self)
                self._send_html(_render_follower_page(
                    cfg["hostname"], ctx["csrf_token"],
                ))
                return
            if path in {"/", "/room"}:
                ctx = begin_request(self)
                self._send_html(_render_page(
                    cfg["hostname"], ctx["csrf_token"], ctx["flash"],
                ))
                return
            if path == "/crossover":
                from . import correction_crossover_flow
                ctx = begin_request(self)
                self._send_html(
                    correction_crossover_flow.render_page(
                        cfg["hostname"], ctx["csrf_token"],
                    )
                )
                return
            if path == "/measurements":
                from . import correction_measurements
                ctx = begin_request(self)
                self._send_html(
                    correction_measurements.render_page(
                        cfg["hostname"], ctx["csrf_token"],
                    )
                )
                return
            if path == "/measurements/data":
                from jasper.active_speaker import bundles as active_bundles
                from . import correction_measurements

                query = parse_qs(urlparse(self.path).query)
                run_a_id = (query.get("a") or [""])[0] or None
                run_b_id = (query.get("b") or [""])[0] or None
                try:
                    self._send_json(correction_measurements.build_data(
                        sessions_dir=active_bundles.sessions_dir(),
                        run_a_id=run_a_id,
                        run_b_id=run_b_id,
                    ))
                except correction_measurements.MeasurementViewRequestError as exc:
                    self._send_client_error(str(exc))
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.exception("/measurements/data failed")
                    self._send_json({"error": str(exc)}, status=500)
                return
            if path == "/crossover/status":
                from . import correction_crossover_flow
                from . import correction_crossover_v2 as v2host

                def _crossover_status(_handler):
                    # W6.1 E3: lazy wall-clock-ceiling enforcement on read —
                    # a session volume that outlived its 1800 s ceiling is
                    # force-drained here (cheap in-memory stale check first).
                    _enforce_session_volume_ceiling(v2host)
                    return correction_crossover_flow.handle_status(
                        capture=_get_capture_slot_for("crossover_v2:"),
                    )

                self._serve_json_route(path, _crossover_status)
                return
            if path == "/crossover/envelope":
                from . import correction_crossover_flow
                from . import correction_crossover_v2 as v2host

                def _crossover_envelope(_handler):
                    # W6.1 E3: the wizard and remote driver both poll this route,
                    # so it promptly drains a walked-away or slow-driver session.
                    _enforce_session_volume_ceiling(v2host)
                    return correction_crossover_flow.handle_envelope(
                        capture=_get_capture_slot_for("crossover_v2:"),
                    )

                self._serve_json_route(path, _crossover_envelope)
                return
            if path == "/bass":
                from . import correction_bass_flow
                ctx = begin_request(self)
                self._send_html(
                    correction_bass_flow.render_page(
                        cfg["hostname"], ctx["csrf_token"],
                    )
                )
                return
            if path == "/bass/status":
                from . import correction_bass_flow
                self._serve_json_route(
                    path, lambda _handler: correction_bass_flow.handle_status(),
                )
                return
            if path == "/balance":
                from . import balance_flow
                ctx = begin_request(self)
                self._send_html(
                    balance_flow.render_page(ctx["csrf_token"]))
                return
            if path == "/balance/status":
                from . import balance_flow
                try:
                    self._send_json(balance_flow.handle_status())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/balance/status failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/sync":
                from . import sync_flow
                ctx = begin_request(self)
                self._send_html(sync_flow.render_page(ctx["csrf_token"]))
                return
            if path == "/sync/status":
                from . import sync_flow
                try:
                    self._send_json(sync_flow.handle_status())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/sync/status failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/healthz":
                body = b"ok\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/status":
                self._serve_json_route("/status", _handle_status)
                return
            if path == "/entry-status":
                self._serve_json_route("/entry-status", _handle_entry_status)
                return
            if path == "/envelope":
                self._serve_json_route("/envelope", _handle_envelope)
                return
            if path == "/sessions":
                self._serve_json_route("/sessions", _handle_sessions)
                return
            if path == "/session-report":
                try:
                    self._send_json(_handle_session_report(self))
                except BadRequest as e:
                    self._send_client_error(str(e))
                except FileNotFoundError as e:
                    self._send_client_error(str(e), status=404)
                except Exception as e:  # noqa: BLE001
                    from jasper.correction.bundles import BundleError
                    if isinstance(e, BundleError):
                        self._send_client_error(str(e), status=422)
                        return
                    logger.exception("/session-report failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/calibration/models":
                try:
                    self._send_json(_handle_calibration_models(self))
                except Exception as e:  # noqa: BLE001
                    logger.exception("/calibration/models failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path not in _POST_ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if bonded_follower_active() and not path.startswith("/crossover/"):
                log_event(
                    logger,
                    "correction.follower_content_dsp_blocked",
                    path=path,
                )
                self._send_json(
                    {
                        "error": (
                            "room correction is controlled on the pair "
                            "leader while this speaker is a follower"
                        ),
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return
            if path.startswith("/balance/"):
                self._dispatch_balance(path)
                return
            if path.startswith("/sync/"):
                self._dispatch_sync(path)
                return
            if path.startswith("/crossover/"):
                self._dispatch_crossover(path)
                return
            try:
                if path == "/start":
                    from jasper.correction import failures
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.sound.graph_carrier import CarrierCannotHostEq
                    try:
                        self._send_json(_handle_start(self))
                    except (CorrectionRuntimeSafetyError, CarrierCannotHostEq) as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.SPEAKER_MEASUREMENT_UNSAFE,
                                # The reachable cause of this refusal is now an
                                # unready speaker, so send the household where
                                # they can act on it rather than to a retry
                                # that will refuse again.
                                recovery_action={
                                    "label": "Open speaker setup",
                                    "href": "/sound/setup/",
                                },
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    except FileNotFoundError as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.MICROPHONE_SETUP_UNAVAILABLE,
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.BAD_REQUEST,
                        )
                    except ValueError as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.MEASUREMENT_SETUP_INVALID,
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.BAD_REQUEST,
                        )
                    except RequestConflict as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.MEASUREMENT_IN_PROGRESS,
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.CONFLICT,
                        )
                    return
                if path == "/next-position":
                    self._send_json(_handle_next_position(self))
                    return
                if path == "/repeat-position":
                    self._send_json(_handle_repeat_position(self))
                    return
                if path == "/verify":
                    self._send_json(_handle_verify(self))
                    return
                if path == "/test-tone":
                    self._send_json(_handle_test_tone(self))
                    return
                if path == "/autolevel/start":
                    try:
                        self._send_json(_handle_autolevel_start(self))
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/autolevel/lock":
                    self._send_json(_handle_autolevel_lock(self))
                    return
                if path == "/autolevel/cancel":
                    self._send_json(_handle_autolevel_cancel(self))
                    return
                if path == "/local-capture/setup":
                    try:
                        self._send_json(_handle_local_capture_setup(self))
                    except (FileNotFoundError, ValueError) as e:
                        self._send_client_error(str(e))
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/upload-capture":
                    from jasper.audio_measurement import quality

                    try:
                        self._send_json(_handle_upload_capture(self))
                    except quality.CaptureQualityError as e:
                        sess = _get_or_create_session()
                        self._send_json({
                            "error": str(e),
                            "session_id": sess.session_id,
                            "state": sess.state.value,
                            "current_position": sess.current_position,
                            "total_positions": sess.total_positions,
                            "capture_quality": sess.capture_quality,
                            "verify_quality": sess.verify_quality,
                            "browser_audio_report": getattr(
                                sess, "browser_audio_report", None,
                            ),
                            "runtime_integrity": _runtime_integrity_summary(sess),
                        }, status=422)
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/upload-noise":
                    try:
                        self._send_json(_handle_upload_noise(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/calibration/fetch":
                    try:
                        self._send_json(_handle_calibration_fetch(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    except Exception as e:  # noqa: BLE001
                        from jasper.audio_measurement.calibration import (
                            CalibrationNotFoundError,
                            CalibrationUpstreamError,
                        )
                        if isinstance(e, CalibrationNotFoundError):
                            self._send_client_error(str(e), status=404)
                        elif isinstance(e, CalibrationUpstreamError):
                            self._send_client_error(str(e), status=502)
                        else:
                            raise
                    return
                if path == "/calibration/upload":
                    try:
                        self._send_json(_handle_calibration_upload(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/apply":
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.sound.graph_carrier import CarrierCannotHostEq
                    try:
                        self._send_json(_handle_apply(self))
                    except (CarrierCannotHostEq, CorrectionRuntimeSafetyError) as e:
                        self._send_client_error(
                            str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    return
                if path == "/reset":
                    # Local import keeps session/numpy off the socket-activated
                    # process's import path (mirrors the other handlers).
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.correction.session import SessionBusyError
                    try:
                        self._send_json(_handle_reset(self))
                    except CorrectionRuntimeSafetyError as e:
                        self._send_client_error(
                            str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    except SessionBusyError as e:
                        # Rejected because a sweep/analysis is mid-flight — a
                        # state conflict (409), not a server error (500).
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/session/delete":
                    try:
                        self._send_json(_handle_session_delete(self))
                    except BadRequest as e:
                        self._send_client_error(str(e))
                    except FileNotFoundError as e:
                        self._send_client_error(str(e), status=404)
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/interpret":
                    from jasper.correction import failures
                    try:
                        self._send_json(_handle_interpret(self))
                    except BadRequest as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.TUNING_REQUEST_FAILED,
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.BAD_REQUEST,
                        )
                    except correction_tuning.SpendCapExceeded as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.TUNING_SPEND_LIMIT,
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.TOO_MANY_REQUESTS,
                        )
                    except TuningSetupUnavailable as e:
                        self._send_room_failure(
                            failures.public_failure(failures.TUNING_UNAVAILABLE),
                            diagnostic=str(e),
                            status=HTTPStatus.CONFLICT,
                        )
                    except RequestConflict as e:
                        self._send_room_failure(
                            failures.public_failure(failures.TUNING_BUSY),
                            diagnostic=str(e),
                            status=HTTPStatus.CONFLICT,
                        )
                    return
                if path == "/propose":
                    from jasper.correction import failures
                    try:
                        self._send_json(_handle_propose(self))
                    except BadRequest as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.TUNING_REQUEST_FAILED,
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.BAD_REQUEST,
                        )
                    except correction_tuning.SpendCapExceeded as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.TUNING_SPEND_LIMIT,
                            ),
                            diagnostic=str(e),
                            status=HTTPStatus.TOO_MANY_REQUESTS,
                        )
                    except TuningSetupUnavailable as e:
                        self._send_room_failure(
                            failures.public_failure(failures.TUNING_UNAVAILABLE),
                            diagnostic=str(e),
                            status=HTTPStatus.CONFLICT,
                        )
                    except RequestConflict as e:
                        self._send_room_failure(
                            failures.public_failure(failures.TUNING_BUSY),
                            diagnostic=str(e),
                            status=HTTPStatus.CONFLICT,
                        )
                    return
                if path == "/propose/apply":
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.sound.graph_carrier import CarrierCannotHostEq
                    try:
                        self._send_json(_handle_propose_apply(self))
                    except BadRequest as e:
                        self._send_client_error(str(e))
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    except (CarrierCannotHostEq, CorrectionRuntimeSafetyError) as e:
                        self._send_client_error(
                            str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    return
            except BadRequest as e:
                self._send_client_error(str(e))
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                self._send_json({"error": str(e)}, status=500)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def make_server(
    target,
    *,
    hostname: str = "jts.local",
    idle_hold: Callable[[str], AbstractContextManager[Any]] = no_hold,
) -> ThreadingHTTPServer:
    """Build the wizard server. `target` is socket/tuple/int per
    _systemd.make_http_server's contract.

    ``idle_hold`` is ``main``'s ``IdleShutdownTracker.hold`` — the seam that
    lets a route keep the socket-activated process alive across background work
    it starts but does not await. Defaulting to ``_systemd.no_hold`` keeps a
    server built without an idle tracker (tests, direct invocation) behaving
    exactly as before."""
    from . import _systemd
    cfg = {"hostname": hostname, "idle_hold": idle_hold}
    return _systemd.make_http_server(target, _make_handler(cfg))


def _restore_capture_entry() -> None:
    """Converge an abandoned automatic capture sequence back to production.

    An automatic capture sequence leaves the persisted CamillaDSP path on the
    all-muted staged anchor between attempts; the production path is stashed
    durably (capture_entry_anchor). This runs at both in-process lifecycle
    exits — service start (`_claim_crossover_state_owners`, covering a
    previous process that crashed/restarted mid-sequence) and this process's
    own idle shutdown (`main`'s IdleShutdownTracker hook, covering the common
    abandon: the user closes the tab, correction-web idles out minutes later).
    Fail direction if it cannot run (CamillaDSP unreachable): the speaker
    stays on the all-muted anchor — muted, never loud — and the stash is
    retained for the next opportunity.
    """

    from jasper.active_speaker import web_commissioning

    _run_async(
        web_commissioning.restore_pending_capture_entry_config(
            camilla_factory=_camilla,
        ),
        timeout=15.0,
    )


def _idle_exit_restore_capture_entry() -> None:
    """Fail-soft idle-shutdown wrapper for :func:`_restore_capture_entry`."""

    try:
        _restore_capture_entry()
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "correction.capture_entry_restore_unavailable",
            level=logging.WARNING,
            boundary="idle_exit",
            reason=type(exc).__name__,
        )


async def _restore_protected_neutral_program_graph() -> None:
    """Converge an abandoned inline R15 program graph to its boot anchor.

    ``protected_neutral_program_origin`` is a tri-state and BOTH positive
    answers are ours (True = the emitted shape, False = our namespace MUTATED);
    the persisted config is the SSOT either way. None is left alone. Distinct
    events so a mutated graph reads as drift.
    """

    from jasper.active_speaker.camilla_yaml import protected_neutral_program_origin
    from jasper.active_speaker.crossover_v2.composition import confirm_graph_is_live
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.dsp_apply import dsp_writer_lock

    cam = _camilla()
    async with dsp_writer_lock(
        DEFAULT_CAMILLA_CONFIG_DIR,
        source="crossover_v2_program_startup_recovery",
    ):
        origin = protected_neutral_program_origin(
            await cam.get_active_config_raw(best_effort=True)
        )
        if origin is None:
            return
        config_path = await cam.get_config_file_path(best_effort=False)
        # "None" is the STRING that reader returns for a null path.
        if not isinstance(config_path, str) or config_path in ("", "None"):
            raise RuntimeError("protected-neutral recovery anchor is unavailable")
        expected = Path(config_path).read_text(encoding="utf-8")
        await cam.set_active_config_raw(expected, best_effort=False)
        await confirm_graph_is_live(cam, expected)
        log_event(
            logger,
            "correction.crossover_v2_program_recovered" if origin
            else "correction.crossover_v2_program_mutated_recovered",
            config_path=config_path,
        )


def _claim_crossover_state_owners() -> None:
    """Retire prior-process Active work before this service accepts requests."""

    from jasper.active_speaker import repeat_admission
    from . import correction_crossover_backend

    claims = (
        (
            "correction.crossover_repeat_admission_unavailable",
            repeat_admission.claim_owner,
        ),
        (
            "correction.active_commissioning_run_unavailable",
            correction_crossover_backend.claim_commissioning_run_owner,
        ),
        (
            "correction.capture_entry_restore_unavailable",
            _restore_capture_entry,
        ),
    )
    for event, claim in claims:
        try:
            claim()
        except (OSError, RuntimeError, ValueError) as exc:
            log_event(
                logger,
                event,
                level=logging.ERROR,
                reason=type(exc).__name__,
            )
    # Fail-closed once the active graph identifies R15's inline program shape.
    # It does NOT stop audio (other sources reach CamillaDSP through fan-in) —
    # it buys "do not open a NEW session on a bad graph". Second layer:
    # set_active_config_raw never repoints the persisted path, so a restart
    # reloads the anchor anyway (panel nits 1/2).
    # Raising leaves main() before the socket is served, which systemd bounds
    # at StartLimitBurst=20 / StartLimitIntervalSec=600 — bounded, not a loop.
    # Logged structurally first so the journal names the cause.
    from jasper.camilla import CamillaUnavailable

    try:
        _run_async(_restore_protected_neutral_program_graph(), timeout=15.0)
    except (OSError, RuntimeError, ValueError, CamillaUnavailable) as exc:
        log_event(
            logger,
            "correction.crossover_v2_program_recovery_failed",
            level=logging.ERROR,
            reason=type(exc).__name__,
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-correction-web",
        description="HTTPS correction measurement hub at /correction/ for the JTS speaker",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_CORRECTION_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_CORRECTION_WEB_PORT", "8770")),
    )
    parser.add_argument(
        "--hostname",
        default=os.environ.get("JASPER_HOSTNAME", "jts.local"),
        help="speaker hostname used in the cert-download fallback link",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Correction and crossover applies swap the live graph from this process,
    # so their swap duck needs a canonical target to release to.
    from jasper.volume_coordinator import install_env_canonical_target_provider

    install_env_canonical_target_provider()

    # Socket Accept=no + one service ExecStart make this the sole lifecycle
    # boundary that may retire unfinished work from a previous process.
    _claim_crossover_state_owners()

    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    # The idle exit is exactly the abandoned-sequence moment (user closed the
    # tab, no requests for the threshold AND no work in flight) — the daemon's
    # last in-process chance to converge a capture sequence parked on the
    # all-muted anchor back to production before the process goes away. The
    # hook is bounded (_run_async timeout) and exception-guarded by the
    # tracker; on a deferred/failed restore the durable stash survives for the
    # next service-start claim boundary.
    #
    # The tracker is built BEFORE the server so `tracker.hold` can ride the
    # handler cfg: a route that spawns background work (the measurement
    # sessions) holds it for that work's whole lifetime, which is
    # what makes "idle" mean abandoned again (issue #1854).
    tracker = _systemd.IdleShutdownTracker(
        on_idle_exit=_idle_exit_restore_capture_entry,
    )
    server = make_server(
        target, hostname=args.hostname, idle_hold=tracker.hold,
    )
    _systemd.install_request_idle_bump(server.RequestHandlerClass, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-correction-web adopting systemd fd (hostname=%s)",
            args.hostname,
        )
    else:
        logger.info(
            "jasper-correction-web listening on http://%s:%d (hostname=%s)",
            args.host, args.port, args.hostname,
        )

    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
