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

Architecture (per docs/HANDOFF-correction.md):
  - stdlib `ThreadingHTTPServer` — same pattern as voice_setup,
    spotify_setup, dial_setup. No FastAPI / ASGI dependency.
  - Single in-memory `MeasurementSession` (jasper.correction.session)
    drives the multi-step state machine.
  - Browser polls GET /status every 500 ms while work is active, the
    presentation envelope every 900 ms on active screens, and lightweight
    entry facts every 10 s while idle — simpler than SSE in stdlib and bounded
    for state transitions that take seconds.
  - Background asyncio loop in a daemon thread bridges the sync HTTP
    handlers to the async session methods.
  - HTTP routes (after nginx strips the /correction/ prefix): the full,
    maintained route list lives in docs/HANDOFF-correction.md (this
    module now serves far more routes than fit a comment table, and
    that doc is the source of truth kept in sync with do_GET/_POST_ROUTES).

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
import html
import inspect
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from jasper.active_speaker.test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES

from ..log_event import log_event
from . import correction_tuning

if TYPE_CHECKING:
    from jasper.capture_relay.client import RelayClient
    from jasper.capture_relay.correction_adapter import RelayCapture
    from jasper.capture_relay.session import PiCaptureSession
from ._common import (
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    reject_csrf,
    send_html_response,
)

logger = logging.getLogger(__name__)


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
MAX_CROSSOVER_WAV_BODY_BYTES = CROSSOVER_CAPTURE_MAX_WAV_BYTES
MAX_DEVICE_FIELD_CHARS = 160
_FOLLOWER_DELEGATED_PAGE_PATHS = frozenset({"/", "/room", "/balance", "/sync"})
_RETURN_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$"
)


class BadRequest(ValueError):
    """Client supplied an invalid request body."""


class RequestConflict(RuntimeError):
    """Client request conflicts with the current correction session state."""


class RoomRequestFailure(RequestConflict):
    """A rejected Room request with bounded homeowner presentation data."""

    def __init__(
        self,
        diagnostic: str,
        failure: Mapping[str, Any],
        *,
        status: HTTPStatus,
    ) -> None:
        super().__init__(diagnostic)
        self.failure = dict(failure)
        self.status = status


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

# Active phone-mic-relay capture surfaced in /status: {tap_link, status} or None.
# Set by POST /relay/capture, updated by its background runner. Guarded by
# _session_lock (same single-session scope).
_relay_capture: dict[str, Any] | None = None
_relay_stop_request: Callable[[], None] | None = None
_RELAY_STOPPABLE_STATUSES = frozenset({"starting", "awaiting_phone"})
_RELAY_IN_FLIGHT_STATUSES = _RELAY_STOPPABLE_STATUSES | {
    "finishing",
    "committing",
    "stopping",
}
# Bound the foreground relay registration so a slow/unreachable relay fails fast
# rather than hanging the request thread for RelayClient's 15 s default.
_RELAY_REGISTER_TIMEOUT_S = 10.0
# Repeating one host event is safe, but distinct progress and terminal events
# must preserve order in the relay's last-write-wins slot. One transient relay
# 5xx or socket timeout must not abort a guarded level walk, while retries stay
# tightly bounded so a dead relay still reaches the existing restore/Stop path.
_RELAY_HOST_EVENT_ATTEMPTS = 2
_RELAY_HOST_EVENT_RETRY_DELAY_S = 0.25
# Keep bounded level-control calls in submission order even after the awaiting
# coroutine reaches its wall-clock deadline.  A timed-out write stays in this
# single-worker queue, so an older progress event cannot complete after a newer
# terminal event and replace it in the relay's last-write-wins slot.
_RELAY_LEVEL_CONTROL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="correction-relay-control",
)
# Level-ramp status and Room host events share one serialized pump. Give those
# small control requests a separate WAN timeout: one retried level event plus
# the next status read can then block for at most 4.75 s, comfortably inside
# the ramp's default 8 s feed-loss guard. Registration retains its wider 10 s
# budget.
_RELAY_CONTROL_TIMEOUT_S = 1.5
_RELAY_LEVEL_PUMP_MAX_BLOCK_S = (
    (_RELAY_HOST_EVENT_ATTEMPTS + 1) * _RELAY_CONTROL_TIMEOUT_S
    + _RELAY_HOST_EVENT_RETRY_DELAY_S
)
# Exact set/readback plus the emergency set/readback each use Camilla's bounded
# reconnect contract. Keep the HTTP owner alive for the complete sequence.
_CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S = 45.0
_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S = _CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S
_ROOM_RELAY_RETURN_PATH = "/correction/room/"
_SUMMED_CAPTURE_UNAVAILABLE_REASON = "active_summed_persisted_admission_unavailable"
# Require a short rolling ambient window before the Pi starts the level tone.
# A single USB-mic startup block is too noisy to become the trust-floor SSOT;
# ten 200 ms samples gives a stable two-second median while keeping setup
# bounded and well inside the relay's rolling three-second sample window.
_RELAY_LEVEL_AMBIENT_MIN_SAMPLES = 10
_ROOM_SWEEP_PHONE_FAILURE = "the speaker could not complete this measurement"


async def _run_relay_control_request(
    call: Callable[..., Any],
    *args: Any,
    hard_timeout_s: float | None = None,
    preserve_write_order: bool = False,
) -> Any:
    """Run one blocking relay request with an optional wall-clock deadline."""

    executor = _RELAY_LEVEL_CONTROL_EXECUTOR if hard_timeout_s is not None else None
    request = asyncio.get_running_loop().run_in_executor(executor, call, *args)
    if hard_timeout_s is None:
        return await request
    done, _pending = await asyncio.wait(
        {request},
        timeout=hard_timeout_s,
    )
    if not done:
        # A running thread cannot be killed safely, but the level-control pump
        # must not wait for it.  Preserve writes in the FIFO executor so every
        # newer event runs after this one; queued status reads are safe to drop.
        if preserve_write_order:
            request.add_done_callback(_consume_relay_control_result)
        else:
            request.cancel()
        raise asyncio.TimeoutError
    return request.result()


def _consume_relay_control_result(request: asyncio.Future[Any]) -> None:
    """Retrieve a detached ordered write result after its caller timed out."""

    if request.cancelled():
        return
    request.exception()


async def _post_relay_host_event(
    client: Any,
    pi_session: Any,
    payload: Mapping[str, Any],
    *,
    hard_timeout_s: float | None = None,
) -> None:
    """Publish one idempotent host event with one bounded transient retry."""

    from jasper.capture_relay.client import RelayError

    for attempt in range(1, _RELAY_HOST_EVENT_ATTEMPTS + 1):
        try:
            await _run_relay_control_request(
                client.post_host_event,
                pi_session.session_id,
                pi_session.pull_token,
                payload,
                hard_timeout_s=hard_timeout_s,
                preserve_write_order=hard_timeout_s is not None,
            )
            return
        except RelayError as exc:
            retryable = exc.status == 429 or exc.status >= 500
            if not retryable or attempt >= _RELAY_HOST_EVENT_ATTEMPTS:
                raise
        except OSError:
            if attempt >= _RELAY_HOST_EVENT_ATTEMPTS:
                raise
        log_event(
            logger,
            "capture_relay.host_event_retry",
            level=logging.WARNING,
            session_id=pi_session.session_id,
            attempt=attempt,
        )
        await asyncio.sleep(_RELAY_HOST_EVENT_RETRY_DELAY_S)


def _bounded_relay_control_client(client: Any) -> Any:
    """Clone the production relay client onto the narrow control deadline."""

    from jasper.capture_relay.client import RelayClient

    return (
        client.with_timeout(_RELAY_CONTROL_TIMEOUT_S)
        if isinstance(client, RelayClient)
        else client  # injected deterministic test double
    )


async def _post_room_sweep_host_event(
    control_client: Any,
    pi_session: Any,
    payload: Mapping[str, Any],
) -> None:
    """Publish ordered Room progress without discarding ambiguous captures."""

    from jasper.capture_relay.client import RelayError

    try:
        await _post_relay_host_event(
            control_client,
            pi_session,
            payload,
            hard_timeout_s=_RELAY_CONTROL_TIMEOUT_S,
        )
        return
    except RelayError as exc:
        # The Worker rejects 4xx before committing the event. A final 4xx
        # (including 429 after the bounded retry) is therefore definitive:
        # do not play a sweep whose recorder session is gone or unauthorized.
        if exc.status < 500:
            raise
        reason = f"RelayError:{exc.status}"
    except (asyncio.TimeoutError, OSError) as exc:
        # The request may have committed before its response timed out. Its
        # detached write remains ordered, and run_capture's ready blob is the
        # authoritative completion signal. Do not discard a valid WAV merely
        # because this progress acknowledgement is unconfirmed.
        reason = type(exc).__name__
    log_event(
        logger,
        "capture_relay.room_sweep_host_event",
        level=logging.WARNING,
        session_id=pi_session.session_id,
        phase=payload.get("phase"),
        result="unconfirmed",
        reason=reason,
    )


def _summed_capture_unavailable(*, ingress: str) -> dict[str, Any]:
    """Refuse legacy browser/raw summed capture before it can touch audio."""

    log_event(
        logger,
        "active_speaker.web_summed_capture_ingress_refused",
        status="refused",
        reason=_SUMMED_CAPTURE_UNAVAILABLE_REASON,
        ingress=ingress,
        audio_emitted=False,
    )
    return {
        "status": "refused",
        "reason": _SUMMED_CAPTURE_UNAVAILABLE_REASON,
        "audio_emitted": False,
        "issues": [
            {
                "code": _SUMMED_CAPTURE_UNAVAILABLE_REASON,
                "message": (
                    "combined crossover capture is available only through "
                    "the trusted internal commissioning host"
                ),
            }
        ],
        "next_step": (
            "Continue with isolated-driver commissioning; summed capture "
            "remains internal-host-only."
        ),
    }


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
    "/relay/level-match",
    "/relay/capture",
    "/relay/verify",
    "/calibration/fetch",
    "/calibration/upload",
    "/apply",
    "/reset",
    "/session/delete",
    "/interpret",
    "/propose",
    "/propose/apply",
    "/crossover/driver-test",
    "/crossover/driver-confirm",
    "/crossover/driver-abort",
    "/crossover/summed-test",
    "/crossover/driver-capture-sweep",
    "/crossover/summed-capture-sweep",
    "/crossover/summed-capture",
    "/crossover/level-match",
    "/crossover/region-geometry",
    "/crossover/candidate",
    "/crossover/relay-capture",
    "/crossover/relay-cancel",
    "/crossover/apply",
    "/crossover/restore",
    "/crossover/recover-volume",
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
    "/sync/relay-capture",
    "/sync/apply",
    "/sync/stop",
    "/sync/reset",
})


def _set_relay_capture(value: dict[str, Any] | None) -> None:
    global _relay_capture, _relay_stop_request
    with _session_lock:
        _relay_capture = value
        if value is None or value.get("status") not in _RELAY_IN_FLIGHT_STATUSES:
            _relay_stop_request = None


def _get_relay_capture() -> dict[str, Any] | None:
    with _session_lock:
        return dict(_relay_capture) if _relay_capture else None


def _get_relay_capture_for(*kind_prefixes: str) -> dict[str, Any] | None:
    """Return relay state only to the flow that owns it.

    The process has one hardware-safe relay slot, but room, sync, and crossover
    pages must never render one another's phone link or waiting state.
    """
    relay = _get_relay_capture()
    if relay is None:
        return None
    kind = str(relay.get("kind") or "")
    return relay if any(kind.startswith(prefix) for prefix in kind_prefixes) else None


def _active_relay_phase() -> str | None:
    """Return the in-flight global relay phase that excludes DSP apply."""

    relay = _get_relay_capture()
    if relay is None or relay.get("status") not in _RELAY_IN_FLIGHT_STATUSES:
        return None
    return f"relay:{str(relay.get('kind') or 'measurement')}"


def _begin_relay_capture(
    kind_label: str,
    *,
    request_stop: Callable[[], None] | None = None,
) -> bool:
    """Atomically claim the single relay-capture slot. Returns False if one is
    already in flight (so a double-tap can't spawn two relay sessions + a file
    race for one position — mirrors /autolevel's "already in progress" guard).
    The slot is released by `_set_relay_capture(None)` on a failed open, or by the
    background runner setting `complete`/`failed`."""
    global _relay_capture, _relay_stop_request
    with _session_lock:
        if (
            _relay_capture
            and _relay_capture.get("status") in _RELAY_IN_FLIGHT_STATUSES
        ):
            return False
        _relay_capture = {"status": "starting", "kind": kind_label}
        _relay_stop_request = request_stop
        return True


def _publish_relay_waiting(kind_label: str, tap_link: str) -> dict[str, Any]:
    """Publish a registered link without overwriting a concurrent Stop."""

    global _relay_capture
    with _session_lock:
        relay = _relay_capture
        if (
            relay is None
            or relay.get("kind") != kind_label
            or relay.get("status") not in {"starting", "stopping"}
        ):
            raise RuntimeError("phone capture ownership changed during registration")
        status = "awaiting_phone" if relay.get("status") == "starting" else "stopping"
        _relay_capture = {**relay, "tap_link": tap_link, "status": status}
        return dict(_relay_capture)


def _request_relay_stop(*kind_prefixes: str) -> dict[str, Any]:
    """Signal the active matching relay owner and expose Stop as in progress.

    The owner publishes ``stopped`` only after its transport worker, audio
    player, and rollback have all drained. Keeping ``stopping`` in the global
    slot prevents a second run from entering during cleanup.
    """

    global _relay_capture
    with _session_lock:
        relay = _relay_capture
        if relay is None or relay.get("status") not in _RELAY_STOPPABLE_STATUSES:
            raise ValueError("no matching phone capture is running")
        kind = str(relay.get("kind") or "")
        if not any(kind.startswith(prefix) for prefix in kind_prefixes):
            raise ValueError("no matching phone capture is running")
        callback = _relay_stop_request
        if callback is None:
            raise RuntimeError("this phone capture cannot be stopped safely")
        try:
            # Request callbacks are deliberately non-blocking signals. Fire
            # one under the same lock as the public state so another tab can
            # never observe ``stopping`` before the owner is actually signaled.
            callback()
        except (OSError, RuntimeError, ValueError) as exc:
            _relay_capture = {
                **relay,
                "status": "failed",
                "error": "the measurement stop signal failed",
            }
            raise RuntimeError("the measurement stop signal failed") from exc
        _relay_capture = {**relay, "status": "stopping"}
        return dict(_relay_capture)


def _begin_relay_commit(kind_label: str) -> bool:
    """Atomically choose evidence commit over a concurrent Stop request.

    ``False`` means Stop won the same lock first, so the caller must not write
    evidence. A missing/different owner is a failure, not a safe cancellation.
    Once this returns ``True``, the capture is no longer stoppable and retains
    the shared slot until its synchronous persistence call reaches a terminal
    result.
    """

    global _relay_capture, _relay_stop_request
    with _session_lock:
        relay = _relay_capture
        if relay is None or relay.get("kind") != kind_label:
            raise RuntimeError("phone capture ownership changed before evidence commit")
        if relay.get("status") == "stopping":
            return False
        if relay.get("status") not in _RELAY_STOPPABLE_STATUSES | {"finishing"}:
            raise RuntimeError("phone capture is not ready to commit evidence")
        _relay_capture = {**relay, "status": "committing"}
        _relay_stop_request = None
        return True


def _begin_relay_finishing(kind_label: str) -> bool:
    """Atomically end the Stop window after playback and rollback finish.

    ``False`` means Stop won the same lock first. Once this returns ``True``,
    the phone owns bounded recorder close/encryption/upload and the host cannot
    delete its relay session underneath an in-flight PUT.
    """

    global _relay_capture, _relay_stop_request
    with _session_lock:
        relay = _relay_capture
        if relay is None or relay.get("kind") != kind_label:
            raise RuntimeError("phone capture ownership changed before upload")
        if relay.get("status") == "stopping":
            return False
        if relay.get("status") not in _RELAY_STOPPABLE_STATUSES:
            raise RuntimeError("phone capture is not ready to finish")
        _relay_capture = {**relay, "status": "finishing"}
        _relay_stop_request = None
        return True


@dataclass(frozen=True)
class RelayCaptureKind:
    """Per-flow plug for the generic relay orchestrator (`_run_relay_capture`).

    Each measurement flow (room sweep, sync, crossover, …) injects only what is
    flow-specific — how to mint+register its relay capture, and how to run it +
    consume the verified WAV (play its stimulus on `armed`, then analyze). The
    orchestrator owns everything common: the single-slot re-entrancy guard,
    bounded registration, the `/status.relay` holder, and the background-task
    lifecycle. Adding a kind is a descriptor, not a fourth copy of the handler.

    `open(client, relay_base, capture_origin, return_url) -> RelayCapture`
    mints+registers the kind's `capture_spec`; `run_and_consume(client,
    pi_session)` awaits the phone capture (with the kind's stimulus as the
    `on_armed` callback) and feeds the verified WAV to the kind's existing
    analysis seam.
    """

    label: str
    open: Callable[[RelayClient, str, str, str], "RelayCapture"]
    run_and_consume: Callable[[RelayClient, PiCaptureSession], Awaitable[None]]
    request_stop: Callable[[], None] | None = None


def _request_local_return_url(
    handler: BaseHTTPRequestHandler | None,
    path: str,
) -> str:
    """Build the local Pi URL the phone should return to after upload.

    The POST has already passed `guard_mutating_request`, but this helper still
    rejects host-shaped surprises before embedding the value in the public capture
    spec. Prefer the exact Host the user's browser reached (`jts5.local`,
    `jts5.local:port`, or a LAN IP); fall back to the configured hostname for
    tests/non-browser callers.
    """
    raw_host = ""
    if handler is not None:
        raw_host = str(handler.headers.get("Host") or "").strip().rstrip(".")
    fallback_host = str(os.environ.get("JASPER_HOSTNAME") or "jts.local").strip()
    fallback_host = re.sub(r"^https?://", "", fallback_host).strip("/").rstrip(".")
    host = raw_host if _RETURN_HOST_RE.match(raw_host) else fallback_host
    if not _RETURN_HOST_RE.match(host):
        host = "jts.local"
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"http://{host}{clean_path}"


def _run_relay_capture(
    kind: RelayCaptureKind,
    relay_base: str,
    *,
    return_url: str,
) -> dict[str, Any]:
    """Own the common relay-capture lifecycle for any kind. The caller has already
    gated on the relay being configured and run the kind's own state/calibration
    prechecks; this claims the slot, registers, spawns the background runner, and
    surfaces the tap-link. Mirrors the room handler's prior inline body so room
    behavior is unchanged — kinds just differ by their injected open/run."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.client import RelayClient
    from jasper.capture_relay.health import relay_registration_token_from_env

    if not _begin_relay_capture(kind.label, request_stop=kind.request_stop):
        raise ValueError("a phone-mic relay capture is already in progress")
    capture_origin = correction_adapter.capture_origin_from_env()
    spawned = False
    try:
        # Register in the foreground (the session must exist before the phone opens
        # the tap-link), bounded so a slow/unreachable relay fails fast.
        client = RelayClient(
            relay_base,
            timeout=_RELAY_REGISTER_TIMEOUT_S,
            registration_token=relay_registration_token_from_env(),
        )
        rc = kind.open(client, relay_base, capture_origin, return_url)

        async def _run() -> None:
            from jasper.capture_relay.session import CaptureStopped

            try:
                await kind.run_and_consume(client, rc.pi_session)
                relay = _get_relay_capture()
                if (
                    relay is not None
                    and relay.get("kind") == kind.label
                    and relay.get("status") == "stopping"
                ):
                    raise CaptureStopped("capture stopped")
                _set_relay_capture(
                    {"tap_link": rc.tap_link, "status": "complete", "kind": kind.label}
                )
            except (asyncio.CancelledError, CaptureStopped):
                _set_relay_capture({
                    "tap_link": rc.tap_link,
                    "status": "stopped",
                    "kind": kind.label,
                    "error": "Measurement stopped safely.",
                })
                log_event(
                    logger,
                    "capture_relay.adapter_stopped",
                    kind=kind.label,
                )
            except Exception as exc:  # noqa: BLE001 — surface loudly; never crash the loop
                # run_capture already logs event=capture_relay.failed with a
                # traceback; this outer net also flips /status.relay to failed and
                # carries the operator-facing reason (e.g. a device/calibration
                # mismatch) so the jts3/jts5 status page can show why.
                log_event(
                    logger,
                    "capture_relay.adapter_failed",
                    level=logging.WARNING,
                    exc_info=True,
                    kind=kind.label,
                    reason=type(exc).__name__,
                )
                _set_relay_capture({
                    "tap_link": rc.tap_link,
                    "status": "failed",
                    "kind": kind.label,
                    "error": str(exc),
                })

        waiting = _publish_relay_waiting(kind.label, rc.tap_link)
        asyncio.run_coroutine_threadsafe(_run(), _ensure_loop())
        spawned = True
        return {"tap_link": rc.tap_link, "status": waiting["status"]}
    finally:
        if not spawned:
            _set_relay_capture(None)  # release the slot on any early failure


def _require_relay_base() -> str:
    """Return the configured relay origin, or raise the gated-off ValueError.

    Called FIRST by every relay endpoint so an operator can still set
    JASPER_CAPTURE_RELAY_BASE=disabled/off/0/none and keep the on-Pi flow
    byte-identical. Fresh installs seed https://relay.jasper.tech because phone
    microphone access needs a publicly trusted HTTPS capture page. Also narrows
    the value from str|None to str for the register call."""
    from jasper.capture_relay.health import relay_base_from_env

    relay_base = relay_base_from_env()
    if relay_base is None:
        raise ValueError(
            "phone-mic relay capture is not configured — set "
            "JASPER_CAPTURE_RELAY_BASE (and deploy the relay + capture page), or "
            "use the on-Pi /correction/ capture flow"
        )
    return relay_base


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


def _crossover_direct_audio_blocking_phase() -> str | None:
    """Block stale direct audio actions while any relay owns the speaker."""

    return _active_relay_phase() or _crossover_blocking_phase()


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


# ----------------------------------------------------------------------
# Page body (canonical design system).
# ----------------------------------------------------------------------
#
# /correction/ is a restyle-in-place migration onto the canonical look:
# the document shell is canonical_page() (app.css + CSRF meta + icon
# sprite); the chrome is canonical_header() + the shared .btn / card
# vocabulary. The page's mechanism layer — getUserMedia mic capture, the
# AudioWorklet level meter, the measurement-sweep + autolevel + verify
# state machine driven by polling GET /status, the canvas chart, and the
# session-report reader — ships as /assets/correction/js/main.js. The
# server-owned GET /envelope contract controls whole-page membership and
# order; the browser has no parallel screen-to-section policy.
#
# getUserMedia requires a secure context; /correction/ is served over
# HTTPS with the speaker's local certificate. The back link is an absolute
# http://<host>/ so the Home affordance lands on the plain-HTTP dashboard
# rather than trying HTTPS on /. Page-specific styling lives in
# /assets/correction/correction.css.


_PAGE_BODY = """
__HEADER__
<main class="page correction-stack" data-required-sr="__REQUIRED_SR__" data-capture-relay-enabled="__CAPTURE_RELAY_ENABLED__" data-level-trust-margin-db="__LEVEL_TRUST_MARGIN_DB__">
__TABS__
<p class="page-sub">Measure your room with a phone and apply the result to the speaker.</p>

<!-- Stepped-wizard chrome (P3b). Server-computed screen envelope (GET
     /envelope) drives everything here: which step you're on, the one
     plain-language verdict, homeowner nudges (a sentence + severity, never
     a block), the single primary action (always live — nudges never
     disable it), and the step indicator. The workflow sections below stay;
     the router shows the ones the current step needs. -->
<section id="wizard-chrome" class="wizard-chrome" aria-live="polite">
  <ol id="wizard-steps" class="wizard-steps" aria-label="Room correction steps"></ol>
  <p id="wizard-verdict" class="wizard-verdict"></p>
  <div id="wizard-nudges" class="wizard-nudges"></div>
  <button id="wizard-next" type="button" class="btn btn--primary hidden"></button>
  <button id="cancel-measurement" type="button" class="btn btn--danger hidden">Cancel measurement</button>
</section>

<div id="envelope-sections" class="correction-sections">
<section id="current-correction" data-envelope-section="current-correction" class="flat hidden" aria-live="polite">
  <span class="label" id="current-correction-label">Checking current correction…</span>
  <button id="current-correction-reset" type="button" class="btn btn--danger hidden">Reset correction</button>
</section>

<!-- P6 tuning assistant. The envelope's sections list owns top-level
     visibility; tuning_llm fills the nudge/actions inside it. The paid call
     happens ONLY on a tap. -->
<section id="tuning-panel" data-envelope-section="tuning" class="tuning-panel hidden" aria-live="polite">
  <h2 class="tuning-title">Tuning assistant</h2>
  <p id="tuning-nudge" class="tuning-nudge hidden"></p>
  <div id="tuning-actions" class="tuning-actions hidden">
    <button id="tuning-interpret" type="button" class="btn">Explain my room</button>
    <button id="tuning-propose" type="button" class="btn">Suggest a tweak</button>
  </div>
  <p id="tuning-status" class="tuning-status hidden"></p>
  <div id="tuning-explanation" class="tuning-explanation hidden"></div>
  <p id="tuning-provenance" class="tuning-provenance hidden"></p>
  <div id="tuning-proposals" class="tuning-proposals"></div>
</section>

<section id="readiness-blocker" data-envelope-section="readiness-blocker" class="info-card hidden" role="alert">
  <p id="readiness-blocker-message"></p>
  <a id="readiness-blocker-action" class="btn hidden" href=""></a>
</section>

<section id="capture-handoff" data-envelope-section="capture-handoff" class="info-card hidden" aria-live="polite">
  <p id="capture-handoff-copy" class="hint"></p>
  <div id="relay-link-row" class="relay-link-row hidden">
    <a id="relay-tap-link" class="btn btn--primary" href="#" target="_blank" rel="noopener">Open phone capture</a>
  </div>
  <p id="relay-status" class="relay-status"></p>
</section>

<section id="placement" data-envelope-section="placement" class="info-card hidden">
  <h2 class="section__title">Place the microphone</h2>
  <p id="placement-instruction">Put the phone or microphone at head height where you normally listen. For a phone, lay it flat screen up, point the bottom edge toward the speakers, and remove its case. Keep the room quiet.</p>
  <div id="position-prompt" class="note-box hidden">
    <p style="margin:0; font-weight:600">Move to position <span id="position-current">2</span> of <span id="position-total">__DEFAULT_ROOM_POSITION_COUNT__</span>.</p>
    <p class="hint" style="margin-top:0.3em">Move about 30 cm from the previous position, keep the microphone at ear height, then continue.</p>
  </div>
</section>

<section id="local-certificate-warning" data-envelope-section="local-certificate-warning" class="info-card hidden" role="note">
  Your browser will warn about the speaker's local certificate — continue past it.
</section>

<section id="capture-setup" data-envelope-section="capture-setup" class="mic-panel hidden">
  <h2 style="margin-top:0">Microphone</h2>
  <div class="mic-grid">
    <div id="local-input-row" class="mic-row local-capture-only">
      <label for="input-device-select">Input device
        <select id="input-device-select">
          <option value="" disabled selected>Detecting microphones…</option>
        </select>
      </label>
      <button id="refresh-inputs" type="button" class="btn btn--ghost">Refresh microphones</button>
    </div>
    <p id="local-input-hint" class="hint local-capture-only" style="margin:0">Your USB measurement mic should appear automatically. Tap <strong>Refresh microphones</strong> if it doesn’t, then select it before <strong>Allow microphone</strong>.</p>

    <label for="mic-model-select">Calibration
      <select id="mic-model-select">
        <option value="">None / phone built-in</option>
        __MIC_MODEL_OPTIONS__
        <option value="other">Other calibrated mic</option>
      </select>
    </label>

    <div id="serial-row" class="mic-row hidden">
      <label for="mic-serial">Serial number
        <input id="mic-serial" type="text" inputmode="text" autocomplete="off"
               placeholder="e.g. 700-1234">
      </label>
      <button id="fetch-calibration" type="button" class="btn btn--ghost">Fetch calibration</button>
    </div>

    <div id="upload-row" class="mic-row hidden">
      <label for="calibration-file">Calibration file
        <input id="calibration-file" type="file" accept=".txt,.cal,.frd,.csv,.omm,text/plain">
      </label>
      <label for="mic-orientation">Orientation
        <select id="mic-orientation">
          <option value="0deg">0° / pointed at speaker</option>
          <option value="90deg">90° / upright</option>
          <option value="unknown">Unknown</option>
        </select>
      </label>
      <label for="calibration-sign">File values are
        <select id="calibration-sign">
          <option value="correction">dB correction to add</option>
          <option value="response">mic response to invert</option>
        </select>
      </label>
      <button id="upload-calibration" type="button" class="btn btn--ghost">Upload calibration</button>
    </div>
    <p id="calibration-status" class="mic-status">No calibration loaded. This is okay for a quick check; use a calibrated microphone before relying on the final result.</p>
    <p id="calibration-preview" class="cal-preview hidden"></p>
  </div>

<div id="constraints" class="hidden" aria-live="polite">
  <h2>Capture settings</h2>
  <p class="hint">JTS checks that this browser can record a clean measurement. Continue when every row reads <span class="ok">✓ ok</span>.</p>
  <table class="constraint-table">
    <thead><tr><th>Setting</th><th>Requested</th><th>Actual</th><th>Status</th></tr></thead>
    <tbody id="constraint-rows"></tbody>
  </table>
  <div id="err-banner" class="err-banner hidden"></div>
  <div id="browser-audio-report" class="browser-audio-card hidden"></div>

  <h2>Live mic level</h2>
  <p class="hint">Speak near the microphone. The meter should move with your voice.</p>
  <div class="level-bar-track" aria-label="microphone level">
    <div id="level-bar-fill" class="level-bar-fill"></div>
  </div>
</div>
</section>

<section id="run-defaults" data-envelope-section="run-defaults" class="info-card hidden">
  <div class="run-defaults-line">
    <p id="run-defaults-summary">__RUN_DEFAULTS_SUMMARY__</p>
    <span aria-hidden="true">—</span>
    <button id="change-run-defaults" type="button" class="btn btn--ghost" aria-controls="measurement-options" aria-expanded="false">Change</button>
  </div>
  <p id="repeat-main-position-disclosure" class="hint">__REPEAT_MAIN_POSITION_DISCLOSURE__</p>
  <div id="measurement-options" class="hidden">
    <label for="positions-select">Positions to measure</label>
    <select id="positions-select" form="dummy">
      __ROOM_POSITION_OPTIONS__
    </select>
    <p class="hint" style="margin-top:0.3em">More positions describe more of the listening area. We'll guide you through each one.</p>

    <label for="target-select" style="margin-top:0.6em">Target curve</label>
    <select id="target-select" form="dummy">
      __TARGET_PROFILE_OPTIONS__
    </select>

    <label for="strategy-select" style="margin-top:0.6em">Correction strategy</label>
    <select id="strategy-select" form="dummy">
      __CORRECTION_STRATEGY_OPTIONS__
    </select>
    <p class="hint" style="margin-top:0.3em">Balanced is the recommended household setting. Safe makes fewer, gentler adjustments.</p>
    <button id="local-capture-fallback" type="button" class="btn btn--ghost">Use this device's microphone</button>
  </div>
</section>

<section id="level-check" data-envelope-section="level-check" class="info-card hidden">
  <h2 class="section__title">Check measurement level</h2>
  <p style="display:flex; gap:0.6em; flex-wrap:wrap">
    <button id="autolevel-lock" type="button" class="btn btn--primary hidden">Lock now</button>
    <button id="autolevel-cancel" type="button" class="btn btn--danger hidden">Cancel</button>
  </p>
  <p id="autolevel-hint" class="hint" style="margin-top:0.4em">The speaker slowly raises a short test tone until the microphone hears a clear measurement level, then stops automatically. If it sounds comfortably loud first, choose <strong>Lock now</strong>. This takes only a few seconds.</p>
  <p class="hint" style="margin-top:0.4em">JTS temporarily pauses your current sound settings so it can measure the room clearly. They return unless you apply the new correction.</p>
  <div id="autolevel-status" class="note-box hidden">
    <p style="margin:0; font-weight:600" id="autolevel-line">Auto-leveling…</p>
    <p class="hint" style="margin-top:0.3em" id="autolevel-detail"></p>
  </div>
</section>

<section id="position-capture" data-envelope-section="position-capture" class="info-card hidden">
  <h2 class="section__title">Measure this position</h2>
  <p>Music pauses automatically while the speaker plays the test sweep.</p>
  <div id="quality-banner" class="quality-banner hidden"></div>
</section>

<section id="measurement-review" data-envelope-section="measurement-review" class="hidden">
  <div id="result-section" class="hidden">
    <h3>Frequency response</h3>
    <div class="chart-controls">
      <label><input id="chart-show-filter" type="checkbox" checked> filter effect</label>
    </div>
    <div class="chart-wrap"><canvas id="chart"></canvas></div>
    <p class="hint">
      <span style="color:#d44">red</span> = measured (averaged across positions),
      <span style="color:#888">gray dashed</span> = target,
      <span style="color:#1db954">green</span> = predicted post-correction.
      <span style="color:#2b7bb9">blue dashed</span> = filter effect.
      After Verify: <span style="color:#a050d0">purple dashed</span> = post-correction measurement,
      with the measured before→after gap shaded
      <span style="color:#1db954">green</span> where it moved toward target and
      <span style="color:#d68200">amber</span> where it moved away.
    </p>
    <button id="reset-correction" type="button" class="btn btn--danger hidden">Reset correction</button>
  </div>
</section>

<section id="apply-status" data-envelope-section="apply-status" class="info-card hidden">
  <p>Room correction is applied. The next measurement checks whether it helped.</p>
</section>

<section id="verification" data-envelope-section="verification" class="info-card hidden">
  <p>Return the microphone to the main seat for a fresh comparison.</p>
</section>

<section id="result-proof" data-envelope-section="result-proof" class="hidden"></section>

<section id="reports" data-envelope-section="reports" class="report-panel hidden">
  <h2>Measurement reports</h2>
  <p class="hint">Read-only evidence from previous sessions. Raw measurement recordings are private and stay on the speaker unless you delete the bundle.</p>
  <button id="load-sessions" type="button" class="btn btn--ghost">Load recent reports</button>
  <div id="session-history" class="session-list"></div>
  <div id="session-report" class="session-report hidden"></div>
</section>
</div>
</main>
<script type="module" src="/assets/correction/js/main.js"></script>
"""


def _render_follower_page(hostname: str, csrf_token: str = "") -> bytes:
    leader_url = bonded_follower_leader_web_url("/correction/")
    leader_link = (
        '<a class="btn btn--primary" href="'
        + html.escape(leader_url)
        + '">Open leader correction</a>'
        if leader_url else ""
    )
    header = canonical_header(
        "Room correction",
        back_href="http://{host}/".format(host=hostname),
    )
    body = f"""
{header}
<main class="page">
  <section class="info-card info-card--accent" role="note">
    <h2 class="section__title">Room correction is controlled by the pair leader</h2>
    <p class="form-hint">This speaker is an active follower. Room correction,
    balance, and sync measurements are content calibration for the paired
    playback image, so run them from the leader while the pair is active.</p>
    <div class="actions">
      {leader_link}
      <a class="btn" href="/rooms/">Manage pair</a>
    </div>
  </section>
</main>
"""
    return canonical_page(
        "Room correction — JTS speaker",
        body,
        csrf_token=csrf_token,
    )


def _render_page(hostname: str, csrf_token: str = "", flash: str = "") -> bytes:
    if bonded_follower_active():
        return _render_follower_page(hostname, csrf_token)
    from jasper.audio_measurement.calibration import (
        SUPPORTED_MODELS,
        model_label_aliases,
    )
    from jasper.correction.strategy import (
        DEFAULT_CORRECTION_STRATEGY_ID,
        DEFAULT_TARGET_PROFILE_ID,
        household_correction_strategy_options,
        target_profile_options,
    )
    from jasper.correction.envelope import room_position_label
    from jasper.correction.session import (
        DEFAULT_ROOM_POSITION_COUNT,
        ROOM_POSITION_COUNT_CHOICES,
    )
    from jasper.audio_measurement.ramp import MeasurementRamp

    # data-aliases carries the registry's label tokens to the wizard so it can
    # infer the model from a device label without a hardcoded client-side map.
    mic_model_options = "\n        ".join(
        '<option value="{key}" data-aliases="{aliases}">{label}</option>'.format(
            key=html.escape(key, quote=True),
            aliases=html.escape(",".join(model_label_aliases(key)), quote=True),
            label=html.escape(spec["label"]),
        )
        for key, spec in SUPPORTED_MODELS.items()
    )
    target_profile_options_html = "\n      ".join(
        '<option value="{key}"{selected}>{label}</option>'.format(
            key=html.escape(str(spec["target_id"]), quote=True),
            selected=(
                " selected"
                if spec["target_id"] == DEFAULT_TARGET_PROFILE_ID
                else ""
            ),
            label=html.escape(str(spec["label"])),
        )
        for spec in target_profile_options()
    )
    correction_strategy_options_html = "\n      ".join(
        '<option value="{key}"{selected}>{label}</option>'.format(
            key=html.escape(str(spec["strategy_id"]), quote=True),
            selected=(
                " selected"
                if spec["strategy_id"] == DEFAULT_CORRECTION_STRATEGY_ID
                else ""
            ),
            label=html.escape(str(spec["label"])),
        )
        for spec in household_correction_strategy_options()
    )
    room_position_options_html = "\n      ".join(
        (
            '<option value="{count}" data-summary-label="{summary_label}"'
            '{selected}>{label}</option>'
        ).format(
            count=count,
            summary_label=html.escape(room_position_label(count), quote=True),
            selected=(" selected" if count == DEFAULT_ROOM_POSITION_COUNT else ""),
            label=html.escape(
                (
                    "1 position — quick check"
                    if count == 1
                    else f"{count} positions"
                    + (
                        " — recommended"
                        if count == DEFAULT_ROOM_POSITION_COUNT
                        else ""
                    )
                )
            ),
        )
        for count in ROOM_POSITION_COUNT_CHOICES
    )
    # The server-owned envelope fills both after the first presentation read.
    run_defaults_summary = ""
    repeat_main_position_disclosure = ""
    from jasper.capture_relay import correction_adapter
    capture_relay_enabled = correction_adapter.relay_enabled()
    # Absolute http:// back link: /correction/ is HTTPS but the dashboard at /
    # is plain HTTP, so a relative "/" would try HTTPS on the root and fail.
    header = canonical_header(
        "Correction",
        back_href="http://{host}/".format(host=hostname),
    )
    from .correction_hub import section_tabs

    body = (
        _PAGE_BODY
        .replace("__HEADER__", header)
        .replace("__TABS__", section_tabs("room"))
        .replace("__REQUIRED_SR__", str(REQUIRED_SAMPLE_RATE))
        .replace(
            "__DEFAULT_ROOM_POSITION_COUNT__",
            str(DEFAULT_ROOM_POSITION_COUNT),
        )
        .replace("__RUN_DEFAULTS_SUMMARY__", run_defaults_summary)
        .replace("__ROOM_POSITION_OPTIONS__", room_position_options_html)
        .replace(
            "__REPEAT_MAIN_POSITION_DISCLOSURE__",
            repeat_main_position_disclosure,
        )
        .replace(
            "__CAPTURE_RELAY_ENABLED__",
            "1" if capture_relay_enabled else "0",
        )
        .replace(
            "__LEVEL_TRUST_MARGIN_DB__",
            format(MeasurementRamp.from_env().trust_margin_db, ".6g"),
        )
        .replace("__MIC_MODEL_OPTIONS__", mic_model_options)
        .replace("__TARGET_PROFILE_OPTIONS__", target_profile_options_html)
        .replace("__CORRECTION_STRATEGY_OPTIONS__", correction_strategy_options_html)
    )
    return canonical_page(
        "Room correction — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/correction.css",
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
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError as e:
        raise BadRequest("invalid Content-Length") from e
    if length <= 0:
        return {}
    if length > max_bytes:
        raise BadRequest(f"JSON body too large ({length} bytes)")
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise BadRequest("JSON body must be UTF-8") from e
    except json.JSONDecodeError as e:
        raise BadRequest(f"invalid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise BadRequest("JSON body must be an object")
    return data


def _camilla() -> "Any":
    """Construct a CamillaController against the configured host/port.
    Factored so tests can monkeypatch a single seam — and so the
    /start reset path doesn't drift from the /apply + /reset paths.
    """
    from jasper.camilla import CamillaController
    return CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )


def _calibration_root() -> Path:
    return Path(
        os.environ.get(
            "JASPER_CORRECTION_CALIBRATION_DIR",
            "/var/lib/jasper/correction/calibration_mics",
        )
    )


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


def _run_relay_measurement_sweep(
    sess: Any,
    cam: Any,
    *,
    client: RelayClient,
    pi_session: PiCaptureSession,
    repeat: bool = False,
) -> None:
    """Play one relay-triggered Room sweep and publish progress to the phone.

    The old relay flow relied on a fixed phone-side recording window. The phone
    now records until it sees ``phase=sweep_complete`` from the Pi, then keeps
    the spec's post-roll. This function therefore blocks until the actual sweep
    path returns, while still using the same measurement_window and
    MeasurementSession transition code as the local browser flow.
    """
    from jasper.correction import coordinator, playback

    control_client = _bounded_relay_control_client(client)

    async def _host_event(phase: str, **extra: Any) -> None:
        payload = {
            "phase": phase,
            "position": (
                1
                if repeat
                else int(getattr(sess, "current_position", 0)) + 1
            ),
            "total_positions": int(getattr(sess, "total_positions", 1)),
            "capture_kind": "repeat" if repeat else "measurement",
            **extra,
        }
        await _post_room_sweep_host_event(
            control_client,
            pi_session,
            payload,
        )

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        async with coordinator.measurement_window():
            if not await sess.ensure_level_match_volume(
                lambda db: cam.set_volume_db(db, best_effort=False)
            ):
                raise RuntimeError(
                    "the saved measurement level is unavailable; run the level "
                    "check again"
                )
            try:
                await _host_event("sweep_started")
                prepare = (
                    sess.prepare_and_play_repeat_sweep
                    if repeat
                    else sess.prepare_and_play_sweep
                )
                await prepare(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
                await _host_event("sweep_complete")
            finally:
                # The renderers resume when measurement_window exits. Restore
                # the household listening volume before that boundary, on every
                # success and failure path.
                await sess.restore_level_match_volume(
                    lambda db: cam.set_volume_db(db, best_effort=False)
                )

    try:
        _run_async(
            _run_session_background_audio(sess, _run_sweep),
            timeout=90.0,
        )
    except (concurrent.futures.TimeoutError, RuntimeError, OSError, ValueError):
        try:
            _run_async(
                _host_event(
                    "sweep_failed",
                    error=_ROOM_SWEEP_PHONE_FAILURE,
                    error_code="room_sweep_unavailable",
                ),
                timeout=_RELAY_LEVEL_PUMP_MAX_BLOCK_S + 1.0,
            )
        except (
            concurrent.futures.TimeoutError,
            RuntimeError,
            OSError,
            ValueError,
        ):
            logger.debug("could not publish relay sweep failure", exc_info=True)
        raise


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
            f'captured device "{label}" looks like the phone built-in mic, but '
            f"a {provider} measurement-mic calibration is loaded; select the USB "
            "measurement mic before measuring"
        )
    return None


def _relay_device_calibration_block(
    mic_calibration: Any, device: dict[str, Any] | None
) -> str | None:
    """Whether to REFUSE a phone-relay capture because a loaded mic calibration
    can't be trusted for the mic the phone actually used.

    A relay capture is recorded by whatever input the phone selected — its
    built-in mic, OR a USB-C measurement mic plugged into the phone. A loaded
    vendor calibration curve is valid only for that USB measurement mic, never the
    phone's built-in. We can't know which until the phone arms a recording, so
    this runs before playback and again post-capture against the phone-reported
    `device` (the same built-in-vs-USB decision the same-origin browser flow
    makes via `_calibration_device_mismatch`):

      - no calibration loaded            → allow (nothing to mis-apply);
      - calibration loaded, no device    → refuse (can't verify the mic — an older
                                            capture page, or a non-compliant client);
      - calibration loaded, device given → defer to `_calibration_device_mismatch`
                                            (refuse a built-in-mic label, allow the
                                            USB measurement mic the curve is for).

    Returns a refusal message, or None to allow. The calibration itself is applied
    Pi-side in the owning analysis path; this only gates whether the capture
    is trustworthy to analyze.
    """
    if mic_calibration is None:
        return None
    label = (device or {}).get("label") or (device or {}).get("browser_label")
    if not label:
        return (
            "a measurement-mic calibration is loaded, but the phone didn't report "
            "which mic it used — update the capture page, or remove the calibration "
            "to measure with the phone's own mic"
        )
    return _calibration_device_mismatch(mic_calibration, device)


@dataclass(frozen=True)
class _RelayLevelIdentity:
    """Mic + calibration identity acquired by the automatic level check."""

    calibration_id: str
    device_key: str


@dataclass(frozen=True)
class _RelaySetupBinding:
    """Pi-validated identity for one guided microphone setup."""

    binding_id: str
    sha256: str


def _setup_digest(setup: dict[str, Any]) -> str:
    raw = json.dumps(
        setup,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validated_relay_setup_binding(
    setup: dict[str, Any],
    identity: dict[str, Any] | None,
    *,
    expected_binding_id: str,
) -> _RelaySetupBinding:
    """Validate a full setup without mutating the current measurement owner."""
    if not isinstance(identity, dict):
        raise ValueError("the phone did not provide a setup identity")
    binding_id = str(identity.get("binding_id") or "")
    digest = str(identity.get("sha256") or "").lower()
    if identity.get("schema") != 1 or binding_id != expected_binding_id:
        raise ValueError("the phone setup belongs to a different measurement run")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("the phone setup identity is malformed")
    if not secrets.compare_digest(digest, _setup_digest(setup)):
        raise ValueError("the phone setup identity does not match its contents")
    return _RelaySetupBinding(binding_id=binding_id, sha256=digest)


def _bind_relay_setup(
    owner: Any,
    setup: dict[str, Any],
    identity: dict[str, Any] | None,
    *,
    expected_binding_id: str,
) -> _RelaySetupBinding:
    """Validate the full one-time setup and freeze its compact identity."""
    binding = _validated_relay_setup_binding(
        setup,
        identity,
        expected_binding_id=expected_binding_id,
    )
    owner.relay_setup_binding = binding
    return binding


def _assert_relay_setup_binding(
    owner: Any,
    compact_setup: dict[str, Any] | None,
    *,
    expected_binding_id: str,
) -> None:
    """Refuse stale/mutated follow-up links without resending raw setup."""
    claim = (
        compact_setup.get("binding")
        if isinstance(compact_setup, dict)
        else None
    )
    bound = getattr(owner, "relay_setup_binding", None)
    if not isinstance(bound, _RelaySetupBinding):
        raise ValueError("the microphone setup is no longer active; run level check")
    if not isinstance(claim, dict) or claim.get("schema") != 1:
        raise ValueError("the phone did not provide the frozen microphone setup")
    if (
        str(claim.get("binding_id") or "") != expected_binding_id
        or str(claim.get("binding_id") or "") != bound.binding_id
        or not secrets.compare_digest(
            str(claim.get("sha256") or "").lower(), bound.sha256
        )
    ):
        raise ValueError("the microphone setup changed; run the level check again")


def _relay_device_key(device: dict[str, Any] | None) -> str:
    values = dict(device or {})
    return str(
        values.get("actual_device_id_hash")
        or values.get("device_id_hash")
        or values.get("label")
        or values.get("browser_label")
        or ""
    ).casefold()


def _relay_level_identity(sess: Any) -> _RelayLevelIdentity:
    device = dict(getattr(sess, "input_device", None) or {})
    return _RelayLevelIdentity(
        calibration_id=str(
            getattr(
                getattr(sess, "mic_calibration", None), "calibration_id", ""
            )
            or ""
        ),
        device_key=_relay_device_key(device),
    )


def _assert_relay_level_identity(
    sess: Any,
    expected: _RelayLevelIdentity,
    *,
    device: dict[str, Any] | None = None,
) -> None:
    """Refuse a sweep if its mic/calibration differs from its level check."""
    current = _relay_level_identity(sess)
    if current.calibration_id != expected.calibration_id:
        raise ValueError(
            "the microphone calibration changed after level matching; run the "
            "level check again"
        )
    if device is None:
        return
    actual = _sanitize_input_device(device) or {}
    actual_key = _relay_device_key(actual)
    if expected.device_key and not actual_key:
        raise ValueError(
            "the phone did not identify the microphone used for the sweep; "
            "run the level check again"
        )
    if expected.device_key and actual_key != expected.device_key:
        raise ValueError(
            "the microphone changed after level matching; select the same "
            "microphone or run the level check again"
        )


async def _read_room_correction_readiness(cam: Any) -> dict[str, Any]:
    """Read Active's decision against CamillaDSP's fresh running graph."""
    from jasper.active_speaker.setup_status import read_active_speaker_setup_status
    from jasper.camilla import CamillaUnavailable

    try:
        running_raw = await cam.get_active_config_raw(best_effort=False)
    except CamillaUnavailable as exc:
        raise RuntimeError("the running CamillaDSP graph is unavailable") from exc
    if not isinstance(running_raw, str) or not running_raw.strip():
        raise RuntimeError("the running CamillaDSP graph is unavailable")
    return read_active_speaker_setup_status(active_config_text=running_raw)


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
    def authority_binding(self) -> tuple[bool, str, str | None] | None:
        """Opaque Active decision that Room may carry and compare only."""

        if not self.allowed or self.active is None or self.authority is None:
            return None
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
    from jasper.active_speaker.setup_status import (
        ROOM_AUTHORITY_AUTOMATIC_COMMISSIONING_RECEIPT,
        ROOM_AUTHORITY_MANUAL_APPLIED_PROFILE,
        ROOM_AUTHORITY_PASSIVE_NOT_REQUIRED,
        ROOM_ELIGIBILITY_SCHEMA_VERSION,
    )

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
                and (
                    (
                        allowed is True
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
    unavailable = not well_formed or acoustic_status == "unknown"
    public_code = (
        failures.SPEAKER_READINESS_UNAVAILABLE
        if unavailable
        else failures.SPEAKER_SETUP_INCOMPLETE
    )
    blocker = failures.public_failure(
        public_code,
        recovery_action=action or failures.ROOM_RETRY_ACTION,
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
            "correction_readiness_unavailable",
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
    expected: tuple[bool, str, str | None] | None,
) -> None:
    """Revalidate the accepted Active identity at a DSP-writer boundary."""

    if expected is None:
        raise RuntimeError("room correction authority binding is missing")
    current = _normalize_room_readiness(
        await _read_room_correction_readiness(cam),
    )
    if current.authority_binding == expected:
        return
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
    from jasper.correction import failures
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
        log_event(
            logger,
            "correction_start_rejected",
            reason=readiness.reason,
            level=logging.WARNING,
        )
        assert readiness.blocker is not None
        status = (
            HTTPStatus.SERVICE_UNAVAILABLE
            if readiness.blocker.get("code")
            == failures.SPEAKER_READINESS_UNAVAILABLE
            else HTTPStatus.CONFLICT
        )
        raise RoomRequestFailure(
            readiness.detail,
            readiness.blocker,
            status=status,
        )
    authority_binding = readiness.authority_binding
    if authority_binding is None:
        raise RuntimeError("speaker readiness omitted its authority binding")

    body = _read_json_body(handler)
    blocking_state = _reserve_start_slot()
    if blocking_state is not None:
        log_event(
            logger,
            "correction_start_rejected",
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
        from jasper.capture_relay import correction_adapter

        requested_transport = body.get("capture_transport")
        if requested_transport is None:
            capture_transport = (
                "relay" if correction_adapter.relay_enabled() else "local"
            )
        else:
            capture_transport = str(requested_transport)
            if capture_transport not in {"relay", "local"}:
                raise ValueError("capture_transport must be relay or local")
            if (
                capture_transport == "relay"
                and not correction_adapter.relay_enabled()
            ):
                raise ValueError("phone capture is not configured")
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
                "correction_start_rejected",
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
                "correction_start_rejected",
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
            prior_session.restore_level_match_volume(
                lambda db: cam.set_volume_db(db, best_effort=False)
            ),
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
        sess.capture_transport = capture_transport
        sess.noise_floor_db = noise_floor_db
        sess.room_authority_binding = authority_binding

        if sess.browser_audio_report.get("failed") is True:
            issue_codes = [
                issue.get("code")
                for issue in sess.browser_audio_report.get("issues", [])
                if isinstance(issue, dict) and issue.get("severity") == "fail"
            ]
            log_event(
                logger,
                "correction_start_rejected",
                reason="browser_audio_path_failed",
                issue_codes=",".join(str(code) for code in issue_codes if code),
                level=logging.WARNING,
            )
            raise ValueError(
                sess.browser_audio_report.get("summary")
                or "browser audio path is not safe for measurement"
            )

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

        reservation_transferred = False
        try:
            _run_async(sess.begin_noise_capture(), timeout=3.0)
            state_started = sess.state == SessionState.NEEDS_NOISE_CAPTURE
        except concurrent.futures.TimeoutError:
            state_started = False

        if state_started:
            if sess.capture_transport == "local":
                # Browser permission + device selection are human-paced. The
                # ordinary upload watchdog resumes when the first noise upload
                # actually begins, after setup and level matching are done.
                sess.suspend_capture_timeout()
            _clear_start_slot()
        else:
            _clear_start_slot()
            log_event(
                logger,
                "correction_start_state_wait_timeout",
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
        if not locals().get("reservation_transferred", False):
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
) -> tuple[Path, Path]:
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
    raw = await cam.get_active_config_raw(best_effort=False)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("CamillaDSP did not report a running graph")
    text = _running_graph_snapshot_text(raw, current, carrier=carrier)
    assert_correction_graph_safe(text)
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
    return Path(current), snapshot


async def _load_measurement_baseline(
    sess: Any,
    cam: Any,
    *,
    expected_authority_binding: tuple[bool, str, str | None],
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
        await _assert_room_authority_current(cam, expected_authority_binding)
        anchor = await cam.get_config_file_path(best_effort=False)
        if not anchor:
            raise RuntimeError("CamillaDSP did not report a loaded config path")
        _, restore_path = await _snapshot_running_room_graph(
            sess,
            cam,
            current_path=anchor,
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
        assert_correction_graph_safe(result.yaml)
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
    from jasper.camilla import CamillaController
    from jasper.correction import coordinator, playback
    from jasper.correction.session import AutolevelStatus, SessionState

    sess = _get_or_create_session()
    if (
        getattr(sess, "capture_transport", "local") != "local"
        or sess.state != SessionState.NEEDS_NOISE_CAPTURE
        or not bool(getattr(sess, "local_capture_setup_bound", False))
    ):
        raise RequestConflict(
            "local microphone setup must be complete before level matching"
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

    cam = CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )

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
                    await cam.set_volume_db(db, best_effort=True)

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
    return _calibration_payload(record)


def _handle_calibration_upload(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import store_calibration

    body = _read_json_body(
        handler,
        max_bytes=MAX_CALIBRATION_UPLOAD_JSON_BYTES,
    )
    text = str(body.get("content") or "")
    filename = str(body.get("filename") or "uploaded-calibration.txt")
    model = str(body.get("model") or "other").strip() or "other"
    label = str(body.get("label") or "Other calibrated mic").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    sign_convention = (
        str(body.get("sign_convention") or "correction").strip()
        or "correction"
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
    return _calibration_payload(record)


def _relay_calibration_from_setup(setup: dict[str, Any] | None) -> Any | None:
    """Materialize the phone wizard's calibration choice on the Pi.

    The phone cannot call the Pi directly, so serial/upload choices ride the
    relay event that arms the sweep. This mirrors the local `/calibration/*`
    handlers and returns the stored calibration record, or None for phone/no
    calibration.
    """
    calibration = setup.get("calibration") if isinstance(setup, dict) else None
    if not isinstance(calibration, dict):
        return None
    mode = str(calibration.get("mode") or "none").strip()
    if mode in ("", "none"):
        return None
    if mode == "serial":
        from jasper.audio_measurement.calibration import fetch_vendor_calibration

        return fetch_vendor_calibration(
            model_key=str(calibration.get("model") or "").strip(),
            serial=str(calibration.get("serial") or "").strip(),
            orientation=str(calibration.get("orientation") or "unknown").strip()
            or "unknown",
            root=_calibration_root(),
        )
    if mode == "upload":
        from jasper.audio_measurement.calibration import store_calibration

        filename = str(calibration.get("filename") or "uploaded-calibration.txt")
        return store_calibration(
            text=str(calibration.get("content") or ""),
            provider="manual_upload",
            model=str(calibration.get("model") or "other").strip() or "other",
            label=str(calibration.get("label") or filename).strip()
            or "Uploaded calibration",
            source=f"uploaded:{filename}",
            orientation=str(calibration.get("orientation") or "unknown").strip()
            or "unknown",
            sign_convention=(
                str(calibration.get("sign_convention") or "correction").strip()
                or "correction"
            ),
            root=_calibration_root(),
        )
    raise ValueError(f"unknown calibration mode: {mode}")


def _apply_relay_setup_to_session(sess: Any, setup: dict[str, Any] | None) -> None:
    """Apply phone microphone/calibration setup without changing Room policy."""
    if not isinstance(setup, dict):
        return
    if isinstance(setup.get("calibration"), dict):
        sess.mic_calibration = _relay_calibration_from_setup(setup)


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
    # Active phone-mic-relay capture, when one is in flight (tap-link + status).
    # None on the default on-Pi flow, so the page only shows the relay UI when the
    # operator has enabled it.
    snap["relay"] = _get_relay_capture_for("room_", "level_ramp:room")
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
    from jasper.capture_relay import correction_adapter
    from jasper.correction import envelope

    sess = _get_or_create_session()
    screen = envelope.screen_for_session(sess)
    readiness_blocker = None
    if screen == envelope.SCREEN_IDLE:
        readiness_blocker = _room_readiness().blocker

    # Capture path is a bounded presentation input while the idle page is
    # open. Once a run starts, the session's own transport is authoritative.
    # Relay is the fleet default when configured; the local HTTPS backup asks
    # for `capture_transport=local` on envelope refreshes after the user picks
    # it under Change.
    capture_transport = str(getattr(sess, "capture_transport", "local"))
    if screen == envelope.SCREEN_IDLE:
        query = parse_qs(urlparse(handler.path).query)
        requested = str((query.get("capture_transport") or [""])[0])
        relay_enabled = correction_adapter.relay_enabled()
        if requested == "local":
            capture_transport = "local"
        elif requested == "relay" and relay_enabled:
            capture_transport = "relay"
        else:
            capture_transport = "relay" if relay_enabled else "local"

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
                "correction_report_discovery_failed",
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
    relay_snapshot = _get_relay_capture_for("room_", "level_ramp:room")
    relay_capture_pending = bool(
        relay_snapshot
        and relay_snapshot.get("status") in {"starting", "awaiting_phone"}
    )
    return envelope.build_envelope_logged(
        sess,
        capture_transport=capture_transport,
        relay_capture_pending=relay_capture_pending,
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
        "correction_session_report",
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
        "correction_session_bundle_deleted",
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

    Local capture asks for microphone permission after the run is reserved.
    This narrow setup write makes the selected device/calibration the live
    session authority before any audio upload; relay setup remains owned by
    its versioned capture binding.
    """
    from jasper.audio_measurement.calibration import load_calibration_record
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if (
        getattr(sess, "capture_transport", "local") != "local"
        or sess.state != SessionState.NEEDS_NOISE_CAPTURE
    ):
        raise RequestConflict("local microphone setup is not available now")

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
        raise RequestConflict("local microphone setup is not available now") from exc

    log_event(
        logger,
        "correction_local_capture_setup_bound",
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
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")
    if sess.state != SessionState.NEEDS_NOISE_CAPTURE:
        raise RuntimeError(
            f"cannot accept noise capture from state {sess.state.value}"
        )
    if (
        getattr(sess, "capture_transport", "local") == "local"
        and not bool(getattr(sess, "local_capture_setup_bound", False))
    ):
        raise RequestConflict(
            "bind the local microphone setup before uploading room noise"
        )
    if getattr(sess, "capture_transport", "local") == "local":
        from jasper.correction.session import AutolevelStatus

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


def _handle_relay_capture(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /relay/capture: capture the current position via the cloud relay (the
    phone runs the capture page on jasper.tech) instead of a same-origin browser
    upload.

    The relay remains config-gated, but is the fresh-install Room default when
    configured. It mints a relay session, returns the phone tap-link, and runs
    the capture in the background: when the phone is recording (it drops
    `armed`), the Pi first verifies the level-check microphone identity and then
    plays the sweep through the SAME measurement_window()/
    prepare_and_play_sweep path the browser flow uses (loud-output safety +
    renderer/voice pause preserved). It then pulls + decrypts + verifies and
    feeds the WAV into the normal position or same-seat repeat seam at the
    identical 48 kHz / mono / 32 MB boundary as a same-origin upload.

    ON-DEVICE: the background sweep playback and the real measurement cannot be
    exercised hardware-free — only the config gate, the state guard, and the seam
    wiring are unit-tested. The relay Worker + capture page must be deployed and
    the phone must reach jasper.tech. Audible failure cues await a
    jasper-web -> jasper-voice cue bridge; until then failures surface on the
    capture page, on the jts.local status page (`relay.status`), and in
    `event=capture_relay.*` logs. This is the integration point the
    docs/phone-mic-relay-plan.md adapter step describes.
    """
    from jasper.capture_relay import correction_adapter
    from jasper.correction.session import SessionState

    relay_base = _require_relay_base()  # gated off until configured; inert otherwise

    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")
    # A relay capture owns either the next distinct position or the automatic
    # main-seat trust repeat. Freeze that mode before background work starts.
    if sess.state not in {
        SessionState.NEEDS_NOISE_CAPTURE,
        SessionState.NEEDS_REPEAT_CAPTURE,
    }:
        raise ValueError(
            "relay capture starts a measurement position or trust repeat; got "
            f"{sess.state.value}"
        )
    is_repeat = sess.state == SessionState.NEEDS_REPEAT_CAPTURE
    level_identity = _relay_level_identity(sess)
    if not level_identity.device_key:
        raise ValueError(
            "the level check did not identify its microphone; run it again"
        )

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_room_sweep_capture(
            client,
            position=1 if is_repeat else sess.current_position + 1,
            total_positions=sess.total_positions,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
            guided_setup=False,
            presentation_variant="trust_repeat" if is_repeat else "",
        )

    async def _run_and_consume(
        client: RelayClient, pi_session: PiCaptureSession
    ) -> None:
        # On `armed` (phone recording), play the sweep through the SAME
        # measurement_window()/prepare_and_play_sweep path the browser flow uses
        # (loud-output safety + renderer/voice pause preserved). run_capture's
        # default 120 s timeout is intentionally ~ the AWAITING_CAPTURE watchdog;
        # keep them aligned if either constant changes.
        capture_path = (
            sess.repeat_capture_path_for_position(0)
            if is_repeat
            else sess.capture_path_for_position(sess.current_position)
        )

        def _on_armed(state: Any) -> None:
            try:
                device = state.device if isinstance(state.device, dict) else None
                _assert_relay_level_identity(
                    sess,
                    level_identity,
                    device=device,
                )
                calibration_block = _relay_device_calibration_block(
                    sess.mic_calibration,
                    device,
                )
                if calibration_block is not None:
                    raise ValueError(calibration_block)
                if state.noise_floor:
                    try:
                        sess.noise_floor_db = float(
                            state.noise_floor.get("rms_dbfs")
                        )
                    except (TypeError, ValueError):
                        logger.debug(
                            "relay noise_floor ignored: %r",
                            state.noise_floor,
                        )
            except (RuntimeError, ValueError):
                try:
                    client.post_host_event(
                        pi_session.session_id,
                        pi_session.pull_token,
                        {
                            "phase": "sweep_failed",
                            "error": _ROOM_SWEEP_PHONE_FAILURE,
                            "error_code": "room_sweep_unavailable",
                        },
                    )
                except (RuntimeError, OSError, ValueError):
                    logger.debug("relay setup failure event failed", exc_info=True)
                raise
            _run_relay_measurement_sweep(
                sess,
                _camilla(),
                client=client,
                pi_session=pi_session,
                repeat=is_repeat,
            )

        try:
            result = await asyncio.to_thread(
                correction_adapter.run_and_store,
                client,
                pi_session,
                capture_path,
                on_armed=_on_armed,
            )
            # Device-aware calibration gate (the phone's mic is known only now):
            # refuse a loaded vendor curve on the phone's built-in mic, allow it
            # for the matching USB measurement mic.
            block = _relay_device_calibration_block(
                sess.mic_calibration, result.device
            )
            if block is not None:
                raise ValueError(block)
            _assert_relay_level_identity(
                sess, level_identity, device=result.device
            )
            if result.noise_floor:
                try:
                    rms_raw = result.noise_floor.get("rms_dbfs")
                    if rms_raw is None:
                        raise ValueError
                    sess.noise_floor_db = float(rms_raw)
                except (TypeError, ValueError):
                    logger.debug(
                        "relay noise_floor ignored: %r",
                        result.noise_floor,
                    )
            if is_repeat:
                await sess.on_repeat_capture_uploaded(capture_path)
            else:
                await sess.on_capture_uploaded(capture_path)
        finally:
            # Idempotent backstop for failures before the armed/sweep window.
            await sess.restore_level_match_volume(
                lambda db: _camilla().set_volume_db(db, best_effort=False)
            )

    kind = RelayCaptureKind(
        label="room_repeat" if is_repeat else "room_sweep",
        open=_open,
        run_and_consume=_run_and_consume,
    )
    relay = _run_relay_capture(
        kind,
        relay_base,
        return_url=_request_local_return_url(handler, _ROOM_RELAY_RETURN_PATH),
    )
    return {"session_id": sess.session_id, "state": sess.state.value, "relay": relay}


def _handle_relay_verify(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /relay/verify: record and analyze the real post-apply response."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_room_sweep_spec
    from jasper.correction.session import SessionState

    relay_base = _require_relay_base()
    sess = _get_or_create_session()
    if sess.state not in {SessionState.APPLIED, SessionState.VERIFIED}:
        raise ValueError("verification requires an applied room correction")
    last = sess.level_match_snapshot().get("last")
    ramp = last.get("ramp") if isinstance(last, dict) else None
    if not (
        isinstance(ramp, dict)
        and ramp.get("state") == "locked"
    ):
        raise ValueError("check the listening-position level before verification")
    level_identity = _relay_level_identity(sess)
    if not level_identity.device_key:
        raise ValueError(
            "the level check did not identify its microphone; run it again"
        )

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_room_sweep_spec(
                position=1,
                total_positions=1,
                guided_setup=False,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    async def _run_and_consume(
        client: RelayClient, pi_session: PiCaptureSession
    ) -> None:
        from jasper.capture_relay.session import purge, run_capture
        from jasper.correction import coordinator, playback

        cam = _camilla()
        capture_path = sess.verify_capture_path()
        control_client = _bounded_relay_control_client(client)

        async def _play_verify() -> None:
            async def _runtime_probe() -> dict[str, Any] | None:
                return await cam.get_runtime_status(best_effort=True)

            async with coordinator.measurement_window():
                if not await sess.ensure_level_match_volume(
                    lambda db: cam.set_volume_db(db, best_effort=False)
                ):
                    raise RuntimeError(
                        "the verification level is no longer active; run the "
                        "level check again"
                    )
                try:
                    await _post_room_sweep_host_event(
                        control_client,
                        pi_session,
                        {
                            "phase": "sweep_started",
                            "position": 1,
                            "total_positions": 1,
                            "capture_kind": "verify",
                        },
                    )
                    await sess.start_verify_sweep(
                        playback.play_sweep,
                        runtime_probe_async=_runtime_probe,
                    )
                    await _post_room_sweep_host_event(
                        control_client,
                        pi_session,
                        {
                            "phase": "sweep_complete",
                            "position": 1,
                            "total_positions": 1,
                            "capture_kind": "verify",
                        },
                    )
                finally:
                    await sess.restore_level_match_volume(
                        lambda db: cam.set_volume_db(db, best_effort=False)
                    )

        def _on_armed(state: Any) -> None:
            try:
                device = state.device if isinstance(state.device, dict) else None
                _assert_relay_level_identity(
                    sess,
                    level_identity,
                    device=device,
                )
                calibration_block = _relay_device_calibration_block(
                    sess.mic_calibration,
                    device,
                )
                if calibration_block is not None:
                    raise ValueError(calibration_block)
            except (RuntimeError, ValueError):
                try:
                    client.post_host_event(
                        pi_session.session_id,
                        pi_session.pull_token,
                        {
                            "phase": "sweep_failed",
                            "error": _ROOM_SWEEP_PHONE_FAILURE,
                            "error_code": "room_sweep_unavailable",
                        },
                    )
                except (RuntimeError, OSError, ValueError):
                    logger.debug(
                        "relay verify failure event failed",
                        exc_info=True,
                    )
                raise
            _run_async(
                _run_session_background_audio(sess, _play_verify),
                timeout=90.0,
            )

        try:
            result = await asyncio.to_thread(
                run_capture,
                client,
                pi_session,
                on_armed=_on_armed,
            )
            block = _relay_device_calibration_block(
                sess.mic_calibration, result.device
            )
            if block is not None:
                raise ValueError(block)
            _assert_relay_level_identity(
                sess, level_identity, device=result.device
            )
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_bytes(result.wav)
            await sess.on_verify_capture_uploaded(capture_path)
            await asyncio.to_thread(_maybe_auto_revert, sess)
        finally:
            try:
                await asyncio.to_thread(purge, client, pi_session)
            except (OSError, RuntimeError, ValueError):
                logger.debug("verify relay purge failed", exc_info=True)
            await sess.restore_level_match_volume(
                lambda db: cam.set_volume_db(db, best_effort=False)
            )

    relay = _run_relay_capture(
        RelayCaptureKind(
            label="room_verify",
            open=_open,
            run_and_consume=_run_and_consume,
        ),
        relay_base,
        return_url=_request_local_return_url(handler, _ROOM_RELAY_RETURN_PATH),
    )
    return {"session_id": sess.session_id, "state": sess.state.value, "relay": relay}


async def _run_relay_level_match(
    sess: Any,
    client: Any,
    pi_session: Any,
    *,
    geometry: str,
    run_token: str,
    setup_binding_id: str = "",
    context_id: str | None = None,
    tone_frequency_hz: float = 1000.0,
    prepare_tone: Callable[[], Any] | None = None,
    restore_tone: Callable[[Any], Any] | None = None,
    expected_level_identity: _RelayLevelIdentity | None = None,
    reuse_noise_floor: bool = True,
    stop_requested: Callable[[], bool] | None = None,
    stop_lock: Any = None,
    begin_commit: Callable[[], bool] | None = None,
) -> None:
    """Run one relay-fed level match without blocking the correction loop.

    ``MeasurementSession`` owns the level state and volume lease.  This adapter
    owns only transport: one cached status snapshot plus a serialized outbound
    host-event queue.  The ramp never performs a blocking relay request from its
    control loop and never gains a direct reference to the relay client.
    """
    from jasper.capture_relay.integrity import CaptureIntegrityError
    from jasper.capture_relay.session import (
        CaptureFailed,
        CaptureStopped,
        PhoneEventVerifier,
        purge,
    )
    from jasper.correction import coordinator, playback

    cached_status: dict[str, Any] = {}
    outbound: list[dict[str, Any]] = []
    stop_pump = asyncio.Event()
    pump_error: list[Exception] = []
    level_task: asyncio.Task[Any] | None = None
    stop_lock = stop_lock or threading.Lock()
    event_verifier = PhoneEventVerifier(pi_session)
    from jasper.capture_relay.client import RelayClient

    control_client = (
        client.with_timeout(_RELAY_CONTROL_TIMEOUT_S)
        if isinstance(client, RelayClient)
        else client  # injected deterministic test double
    )

    def _read_status() -> dict[str, Any]:
        return dict(cached_status)

    def _queue_host_event(payload: dict[str, Any]) -> None:
        outbound.append(dict(payload))

    async def _pump() -> None:
        unhealthy = False
        host_event_unconfirmed = False
        while not stop_pump.is_set():
            if stop_requested is not None and stop_requested():
                cancel_level_match = getattr(sess, "cancel_level_match", None)
                try:
                    cancelled = False
                    if callable(cancel_level_match):
                        cancelled = bool(await cancel_level_match())
                    if not cancelled and level_task is not None:
                        level_task.cancel()
                except (OSError, RuntimeError, ValueError) as exc:
                    pump_error.append(exc)
                else:
                    pump_error.append(CaptureStopped("capture stopped"))
                stop_pump.set()
                return
            try:
                # Publish at most one queued event before refreshing status.
                # Together with the narrow control client this keeps the real
                # elapsed retry+status budget below feed_timeout_s.
                if outbound:
                    payload = outbound.pop(0)
                    try:
                        await _post_relay_host_event(
                            control_client,
                            pi_session,
                            payload,
                            hard_timeout_s=_RELAY_CONTROL_TIMEOUT_S,
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        # A response timeout is ambiguous: the ordered write may
                        # already have committed. Status is the live microphone
                        # feed, so it must still get its turn this iteration;
                        # otherwise repeated slow acknowledgements alone can
                        # manufacture an eight-second feed-loss failure.
                        if not host_event_unconfirmed:
                            log_event(
                                logger,
                                "capture_relay.level_host_event",
                                level=logging.WARNING,
                                session_id=pi_session.session_id,
                                result="unconfirmed",
                                reason=type(exc).__name__,
                            )
                        host_event_unconfirmed = True
                    else:
                        if host_event_unconfirmed:
                            log_event(
                                logger,
                                "capture_relay.level_host_event",
                                session_id=pi_session.session_id,
                                result="recovered",
                            )
                        host_event_unconfirmed = False
                fresh = await _run_relay_control_request(
                    control_client.status,
                    pi_session.session_id,
                    pi_session.pull_token,
                    hard_timeout_s=_RELAY_CONTROL_TIMEOUT_S,
                )
                if isinstance(fresh, dict):
                    verified_event = event_verifier.verify(fresh.get("event"))
                    fresh = {**fresh, "event": verified_event}
                    cached_status.clear()
                    cached_status.update(fresh)
                if unhealthy:
                    unhealthy = False
                    logger.info("relay status pump recovered during level match")
            except CaptureIntegrityError as exc:
                log_event(
                    logger,
                    "capture_relay.phone_event_integrity_failed",
                    level=logging.WARNING,
                    session_id=pi_session.session_id,
                    kind=pi_session.spec.kind,
                    reason=str(exc),
                )
                try:
                    await _post_relay_host_event(
                        control_client,
                        pi_session,
                        {
                            "phase": "capture_incompatible",
                            "error": "capture control integrity check failed",
                        },
                        hard_timeout_s=_RELAY_CONTROL_TIMEOUT_S,
                    )
                except (OSError, RuntimeError, ValueError):
                    logger.warning(
                        "could not publish level-match integrity failure",
                        exc_info=True,
                    )
                pump_error.append(
                    CaptureFailed("capture control integrity check failed")
                )
                stop_pump.set()
                return
            except (OSError, RuntimeError, ValueError):
                if not unhealthy:
                    unhealthy = True
                    logger.warning(
                        "relay status pump failed during level match "
                        "(latched until recovery)",
                        exc_info=True,
                    )
            try:
                await asyncio.wait_for(stop_pump.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    pump_task = asyncio.create_task(_pump())
    cam = _camilla()
    player = None
    setup_tokens_seen: set[str] = set()
    page_compatible = False
    try:
        async with coordinator.measurement_window():
            from jasper.correction.level_match import parse_level_batch

            # The phone starts its meter before the Pi starts the tone. Build a
            # deduplicated token-scoped ambient window so ordinary room noise
            # cannot satisfy the tone target and repeated relay polls cannot
            # manufacture sample count.
            initial_noise_floor = (
                getattr(sess, "noise_floor_db", None)
                if reuse_noise_floor
                else None
            )
            ambient_samples: dict[tuple[int, int], float] = {}
            # The relay runner starts when the link is minted, not when the
            # household opens it. Give the sequential setup + Start tap a
            # human-scale bounded window; the ramp itself owns its much shorter
            # acoustic safety timeout once samples begin.
            deadline = asyncio.get_running_loop().time() + 480.0
            while True:
                if pump_error:
                    raise pump_error[0]
                event = cached_status.get("event")
                if isinstance(event, dict) and not page_compatible and any(
                    key in event
                    for key in ("setup_validate", "level_refused", "level_batch")
                ):
                    from jasper.capture_relay.session import (
                        CapturePageIncompatible,
                        validate_capture_page,
                    )

                    try:
                        validate_capture_page(
                            event.get("capture_page"), pi_session.spec
                        )
                    except CapturePageIncompatible:
                        log_event(
                            logger,
                            "capture_relay.page_incompatible",
                            level=logging.WARNING,
                            session_id=pi_session.session_id,
                            expected_protocol=pi_session.spec.capture_protocol_version,
                            observed_protocol=(event.get("capture_page") or {}).get(
                                "capture_protocol_version"
                            ),
                            observed_build=(event.get("capture_page") or {}).get(
                                "capture_page_build"
                            ),
                        )
                        raise
                    page_compatible = True
                    log_event(
                        logger,
                        "capture_relay.page_compatible",
                        session_id=pi_session.session_id,
                        protocol=pi_session.spec.capture_protocol_version,
                        page_build=(event.get("capture_page") or {}).get(
                            "capture_page_build"
                        ),
                    )
                if isinstance(event, dict) and event.get("setup_validate"):
                    setup_token = str(event.get("setup_token") or "")
                    if setup_token and setup_token not in setup_tokens_seen:
                        setup_tokens_seen.add(setup_token)
                        setup = event.get("setup")
                        try:
                            if not isinstance(setup, dict):
                                raise ValueError("the phone setup is missing")
                            if setup_binding_id:
                                candidate_binding = _validated_relay_setup_binding(
                                    setup,
                                    event.get("setup_identity"),
                                    expected_binding_id=setup_binding_id,
                                )
                                existing_binding = getattr(
                                    sess, "relay_setup_binding", None
                                )
                                if (
                                    existing_binding is not None
                                    and candidate_binding != existing_binding
                                ):
                                    raise ValueError(
                                        "the microphone setup changed; run the "
                                        "level check again"
                                    )
                                if existing_binding is None:
                                    previous_calibration = getattr(
                                        sess, "mic_calibration", None
                                    )
                                    try:
                                        _apply_relay_setup_to_session(sess, setup)
                                    except (RuntimeError, ValueError):
                                        sess.mic_calibration = previous_calibration
                                        raise
                                    sess.relay_setup_binding = candidate_binding
                            else:
                                _apply_relay_setup_to_session(sess, setup)
                        except (RuntimeError, ValueError) as exc:
                            response = {
                                "phase": "setup_validation_failed",
                                "setup_token": setup_token,
                                "error": str(exc),
                            }
                        else:
                            response = {
                                "phase": "setup_validated",
                                "setup_token": setup_token,
                            }
                        await _post_relay_host_event(
                            control_client,
                            pi_session,
                            response,
                            hard_timeout_s=_RELAY_CONTROL_TIMEOUT_S,
                        )
                        if response["phase"] == "setup_validation_failed":
                            raise ValueError(str(response["error"]))
                refusal = (
                    event.get("level_refused")
                    if isinstance(event, dict)
                    else None
                )
                if (
                    isinstance(refusal, dict)
                    and str(refusal.get("run_token") or "") == run_token
                ):
                    reason = str(refusal.get("reason") or "unsupported_microphone")
                    if reason == "agc_not_proven_off":
                        raise RuntimeError(
                            "this browser cannot prove automatic microphone gain "
                            "is disabled; use a supported browser or USB measurement "
                            "microphone"
                        )
                    raise RuntimeError(f"the phone refused the level check: {reason}")
                samples = parse_level_batch(
                    event if isinstance(event, dict) else {},
                    run_token=run_token,
                )
                if samples:
                    batch = (
                        event.get("level_batch") if isinstance(event, dict) else None
                    )
                    context = (
                        batch.get("context") if isinstance(batch, dict) else None
                    )
                    if isinstance(context, dict):
                        setup = context.get("setup")
                        if setup_binding_id:
                            _assert_relay_setup_binding(
                                sess,
                                setup if isinstance(setup, dict) else None,
                                expected_binding_id=setup_binding_id,
                            )
                        elif isinstance(setup, dict):
                            _apply_relay_setup_to_session(sess, setup)
                        device = context.get("device")
                        if isinstance(device, dict):
                            candidate_device = _sanitize_input_device(device)
                            if (
                                expected_level_identity is not None
                                and _relay_device_key(candidate_device)
                                != expected_level_identity.device_key
                            ):
                                raise ValueError(
                                    "the microphone changed between driver level "
                                    "checks; use the same microphone"
                                )
                            sess.input_device = candidate_device
                    if (
                        expected_level_identity is not None
                        and _relay_level_identity(sess).calibration_id
                        != expected_level_identity.calibration_id
                    ):
                        raise ValueError(
                            "the microphone calibration changed between driver "
                            "level checks; run the level check again"
                        )
                    mismatch = _relay_device_calibration_block(
                        getattr(sess, "mic_calibration", None),
                        getattr(sess, "input_device", None),
                    )
                    if mismatch is not None:
                        raise ValueError(mismatch)
                    if initial_noise_floor is None:
                        for sample in samples:
                            value = float(sample.rms_dbfs)
                            if math.isfinite(value):
                                ambient_samples.setdefault(
                                    (int(sample.seq), int(sample.t_client_ms)),
                                    value,
                                )
                        if (
                            len(ambient_samples)
                            < _RELAY_LEVEL_AMBIENT_MIN_SAMPLES
                        ):
                            await asyncio.sleep(0.1)
                            continue
                        ordered = sorted(ambient_samples.values())
                        initial_noise_floor = ordered[len(ordered) // 2]
                        sess.noise_floor_db = initial_noise_floor
                        log_event(
                            logger,
                            "correction.level_match_ambient_baseline",
                            session_id=getattr(sess, "session_id", None),
                            geometry=geometry,
                            sample_count=len(ordered),
                            rms_dbfs=f"{initial_noise_floor:.1f}",
                            spread_db=f"{ordered[-1] - ordered[0]:.1f}",
                        )
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        "the phone did not provide an ambient level baseline"
                    )
                await asyncio.sleep(0.1)

            from jasper.audio_measurement.excitation import (
                AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
            )

            if stop_requested is not None and stop_requested():
                raise CaptureStopped("capture stopped")
            prepared_tone = await prepare_tone() if prepare_tone is not None else None
            try:
                if pump_error:
                    raise pump_error[0]
                if stop_requested is not None and stop_requested():
                    raise CaptureStopped("capture stopped")
                tone_wav = playback._ensure_tone_wav(
                    freq_hz=tone_frequency_hz,
                    duration_s=90.0,
                    dbfs=AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
                    sample_rate=48000,
                )
                player = playback.TonePlayer(tone_wav)
                from jasper.camilla import CamillaUnavailable

                async def _get_volume() -> float:
                    try:
                        value = await cam.get_volume_db(best_effort=False)
                    except CamillaUnavailable as exc:
                        raise RuntimeError(
                            "CamillaDSP is unavailable during crossover leveling"
                        ) from exc
                    if value is None:
                        raise RuntimeError(
                            "CamillaDSP did not report the measurement volume"
                        )
                    return float(value)

                async def _set_volume(db: float) -> None:
                    try:
                        applied = await cam.set_volume_db(db, best_effort=False)
                    except CamillaUnavailable as exc:
                        raise RuntimeError(
                            "CamillaDSP is unavailable during crossover leveling"
                        ) from exc
                    if applied is False:
                        raise RuntimeError(
                            "CamillaDSP rejected the measurement volume"
                        )

                with stop_lock:
                    if stop_requested is not None and stop_requested():
                        raise CaptureStopped("capture stopped")
                    level_ports: dict[str, Any] = {}
                    if context_id is not None:
                        # CrossoverLevelLease owns a profile-scoped continuation;
                        # Room's explicit session port does not accept this key.
                        level_ports["context_id"] = context_id
                    level_task = asyncio.create_task(
                        sess.run_level_match(
                            geometry,
                            get_main_volume_db=_get_volume,
                            set_main_volume_db=_set_volume,
                            play_continuous_tone=player.play,
                            cancel_tone=player.cancel,
                            read_status=_read_status,
                            post_host_event=_queue_host_event,
                            noise_floor_dbfs=initial_noise_floor,
                            run_token=run_token,
                            **level_ports,
                        )
                    )
                try:
                    outcome = await level_task
                except asyncio.CancelledError:
                    if stop_requested is not None and stop_requested():
                        raise CaptureStopped("capture stopped") from None
                    raise
            finally:
                if restore_tone is not None and prepared_tone is not None:
                    await restore_tone(prepared_tone)

        if stop_requested is not None and stop_requested():
            raise CaptureStopped("capture stopped")
        if not outcome.locked:
            detail = outcome.ramp.error or "safe measurement level was not reached"
            raise ValueError(detail)
        mismatch = _relay_device_calibration_block(
            getattr(sess, "mic_calibration", None),
            getattr(sess, "input_device", None),
        )
        if mismatch is not None:
            restore_level_match = getattr(sess, "restore_level_match_volume", None)
            if callable(restore_level_match):
                await restore_level_match(
                    lambda db: cam.set_volume_db(db, best_effort=False)
                )
            raise ValueError(mismatch)
        if begin_commit is not None and not begin_commit():
            raise CaptureStopped("capture stopped")
    except (
        OSError,
        RuntimeError,
        ValueError,
        asyncio.TimeoutError,
        concurrent.futures.TimeoutError,
    ) as exc:
        # Failures before the tone/ramp starts used to disappear when the relay
        # session was purged, leaving the phone waiting on an empty slot. Publish
        # the same terminal shape as the ramp and leave a short observation
        # window before cleanup.
        try:
            terminal_state = (
                "cancelled" if isinstance(exc, CaptureStopped) else "error"
            )
            await _post_relay_host_event(
                control_client,
                pi_session,
                {
                    "ramp": {
                        "state": terminal_state,
                        "terminal": True,
                        "run_token": run_token,
                        "error": str(exc),
                    }
                },
                hard_timeout_s=_RELAY_CONTROL_TIMEOUT_S,
            )
            await asyncio.sleep(0.75)
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "could not publish terminal level-match failure",
                exc_info=True,
            )
        raise
    finally:
        if player is not None:
            player.cancel()
        # Let the serialized pump flush the ramp's final queued state before
        # stopping it. This is bounded; relay TTL remains the cleanup backstop.
        flush_deadline = asyncio.get_running_loop().time() + 1.0
        while outbound and asyncio.get_running_loop().time() < flush_deadline:
            await asyncio.sleep(0.05)
        stop_pump.set()
        await pump_task
        try:
            await asyncio.to_thread(purge, client, pi_session)
        except (OSError, RuntimeError, ValueError):
            logger.debug("level-match relay purge failed", exc_info=True)


def _handle_relay_level_match(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /relay/level-match: lock the listening-position measurement level.

    The room session must already have loaded its topology-preserving
    measurement baseline via ``/start``.  The returned tap-link opens the
    trusted phone page, where mic/calibration setup precedes the meter-only
    ramp.  No WAV is uploaded by this relay kind.
    """
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_level_ramp_spec
    from jasper.correction.level_match import MicGeometry
    from jasper.correction.session import SessionState

    relay_base = _require_relay_base()
    sess = _get_or_create_session()
    allowed_states = {
        SessionState.NEEDS_NOISE_CAPTURE,
        SessionState.APPLIED,
        SessionState.VERIFIED,
    }
    if sess.state not in allowed_states:
        raise ValueError(
            "level matching must run immediately before a room or verification sweep"
        )
    run_token = secrets.token_urlsafe(18)
    setup_binding_id = str(sess.session_id)

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_level_ramp_spec(
                geometry_label=(
                    "main listening position for verification"
                    if sess.state in {SessionState.APPLIED, SessionState.VERIFIED}
                    else "main listening position"
                ),
                run_token=run_token,
                setup_binding_id=setup_binding_id,
                setup_collect_positions=False,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    async def _run(client: RelayClient, pi_session: PiCaptureSession) -> None:
        # NEEDS_NOISE_CAPTURE normally has a short local-browser upload
        # watchdog. Relay mic permission, calibration, placement, and gradual
        # level matching are deliberately human-paced, so pause that watchdog
        # only for this sub-flow. Restore a fresh bound afterward; the relay
        # capture adapter remains bounded and an abandoned next capture still
        # self-recovers without operator cleanup.
        sess.suspend_capture_timeout()
        try:
            await _run_session_background_audio(
                sess,
                lambda: _run_relay_level_match(
                    sess,
                    client,
                    pi_session,
                    geometry=MicGeometry.LISTENING_POSITION.value,
                    run_token=run_token,
                    setup_binding_id=setup_binding_id,
                ),
            )
        finally:
            sess.resume_capture_timeout()

    relay = _run_relay_capture(
        RelayCaptureKind(label="level_ramp:room", open=_open, run_and_consume=_run),
        relay_base,
        return_url=_request_local_return_url(handler, _ROOM_RELAY_RETURN_PATH),
    )
    return {"session_id": sess.session_id, "state": sess.state.value, "relay": relay}


def _open_commissioning_bundle_for_level_match(
    topology: Any,
    *,
    calibration_id: str,
) -> dict[str, Any] | None:
    """Open the active-speaker commissioning bundle for a new comparison set.

    Called immediately before ``measurement.start_active_comparison_set`` so
    the fresh ``session_id`` can be stamped into the comparison set it is
    about to mint. ``bundles.open_bundle`` is already fail-soft (returns
    ``None``, WARN-logged, on any I/O failure); this wrapper exists only so
    the call site reads as one step and is independently unit-testable.
    """

    from jasper.active_speaker import bundles as active_speaker_bundles

    return active_speaker_bundles.open_bundle(
        topology, calibration_id=calibration_id or "",
    )


def _activate_crossover_comparison_authorities(
    topology: Any,
    comparison_set: Mapping[str, Any],
) -> None:
    """Publish repeat and lifecycle authority as one fail-closed boundary."""

    from jasper.active_speaker import repeat_admission
    from jasper.active_speaker.measurement import clear_active_comparison_set
    from . import correction_crossover_backend as backend

    try:
        repeat_admission.activate(comparison_set)
        if comparison_set.get("bundle_session_id"):
            backend.begin_commissioning_run(comparison_set)
    except (OSError, RuntimeError, ValueError):
        clear_active_comparison_set(topology)
        repeat_admission.invalidate()
        raise


def _assert_crossover_reference_axis_level_action(
    status: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
) -> None:
    """Require one fixed-axis level request to equal the server next action."""

    from jasper.active_speaker.crossover_envelope import build_crossover_envelope

    action_status = dict(status)
    action_status["relay"] = None
    expected_action = build_crossover_envelope(action_status).get("next_action")
    expected_body = (
        expected_action.get("body")
        if isinstance(expected_action, Mapping)
        else None
    )
    if (
        not isinstance(expected_action, Mapping)
        or expected_action.get("id") != "level_match_reference_axis_driver"
        or not isinstance(expected_body, Mapping)
        or expected_body.get("capture_geometry") != "reference_axis"
        or str(expected_body.get("speaker_group_id") or "") != speaker_group_id
        or str(expected_body.get("role") or "").lower() != role.lower()
    ):
        raise ValueError(
            "the requested fixed-axis level check is not the server-owned next step"
        )


@asynccontextmanager
async def _fixed_axis_level_identity_guard(
    lease: Any,
    *,
    speaker_group_id: str,
    role: str,
):
    """Roll back fixed-axis identity when its shared level run fails."""

    snapshot = (
        getattr(lease, "relay_setup_binding", None),
        getattr(lease, "input_device", None),
        getattr(lease, "mic_calibration", None),
        getattr(lease, "noise_floor_db", None),
        getattr(lease, "context_id", None),
    )
    completed = False
    try:
        yield
        completed = True
    finally:
        if not completed:
            lease.discard_driver_level_outcome(
                speaker_group_id,
                role,
                capture_geometry="reference_axis",
            )
            (
                lease.relay_setup_binding,
                lease.input_device,
                lease.mic_calibration,
                lease.noise_floor_db,
                lease.context_id,
            ) = snapshot


def _handle_crossover_relay_level_match(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/level-match: acquire one geometry-scoped gain lease."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_level_ramp_spec
    from jasper.active_speaker.measurement import (
        clear_active_comparison_set,
        start_active_comparison_set,
    )
    from jasper.active_speaker.capture_geometry import (
        DRIVER_PLACEMENT_POLICY_ID,
        crossover_level_reference,
    )
    from jasper.active_speaker import web_commissioning
    from jasper.output_topology import load_output_topology

    from . import correction_crossover_backend as backend
    from . import correction_crossover_flow

    relay_base = _require_relay_base()
    status = backend.status_payload()
    if not status.get("active"):
        raise ValueError("this speaker has no active crossover to measure")
    raw_setup = status.get("setup")
    setup = raw_setup if isinstance(raw_setup, dict) else {}
    if setup.get("status") != "ready":
        raise ValueError(
            "finish and apply the protected active-speaker setup before measuring it"
        )
    blocking = _crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(f"another measurement is in progress ({blocking})")
    raw = _read_json_body(handler)
    requested_geometry = str(raw.get("capture_geometry") or "near_field").lower()
    fixed_axis_request = requested_geometry == "reference_axis"
    if requested_geometry not in {"near_field", "reference_axis"}:
        raise ValueError("crossover level geometry is unsupported")
    status = correction_crossover_flow.ensure_automatic_measurement_profile(
        status,
        _run_async,
        _camilla,
        status_loader=backend.status_payload,
    )
    raw_setup = status.get("setup")
    setup = raw_setup if isinstance(raw_setup, dict) else {}
    raw_setup_profile = setup.get("protected_profile")
    setup_profile = raw_setup_profile if isinstance(raw_setup_profile, dict) else {}
    context_id = str(setup_profile.get("candidate_fingerprint") or "") or None
    if context_id is None:
        raise ValueError(
            "protected speaker setup has no stable profile identity; reapply it "
            "before level matching"
        )

    applied_profile = status.get("applied_profile")
    if not isinstance(applied_profile, dict):
        raise ValueError(
            "the applied crossover snapshot is unavailable; reapply it before "
            "level matching"
        )
    recomposition = applied_profile.get("recomposition_snapshot")
    preset_payload = (
        recomposition.get("preset") if isinstance(recomposition, dict) else None
    )
    if not isinstance(preset_payload, dict):
        raise ValueError(
            "the applied crossover has no protected preset snapshot; reapply it "
            "before level matching"
        )
    from jasper.active_speaker.profile import ActiveSpeakerPreset

    preset = ActiveSpeakerPreset.from_mapping(preset_payload)
    lease = backend.level_lease()
    topology = load_output_topology()
    target_rows = (status.get("targets") or {}).get("drivers") or []
    level_targets: list[dict[str, Any]] = []
    for raw_target in target_rows:
        if not isinstance(raw_target, dict):
            continue
        group_id = str(raw_target.get("speaker_group_id") or "").strip()
        role = str(raw_target.get("role") or "").strip().lower()
        reference = crossover_level_reference(
            preset_payload,
            speaker_group_id=group_id,
            role=role,
        )
        excitation = web_commissioning.automatic_driver_excitation(
            topology,
            role,
            applied_profile=applied_profile,
        )
        commissioning_gain_db = excitation.get("commissioning_gain_db")
        target_fingerprint = str(raw_target.get("target_fingerprint") or "")
        if not target_fingerprint:
            raise ValueError(
                "the protected driver has no stable target identity; reapply "
                "the crossover before level matching"
            )
        if (
            excitation.get("status") != "ready"
            or not isinstance(commissioning_gain_db, (int, float))
        ):
            raise ValueError(
                str(
                    excitation.get("detail")
                    or "the protected driver gain is unavailable; reapply the "
                    "crossover before level matching"
                )
            )
        level_targets.append(
            {
                "target_id": reference.target_id,
                "speaker_group_id": reference.speaker_group_id,
                "role": reference.role,
                "geometry": reference.geometry,
                "tone_frequency_hz": reference.tone_frequency_hz,
                "placement_instruction": reference.placement_instruction,
                "commissioning_gain_db": float(commissioning_gain_db),
                "target_fingerprint": target_fingerprint,
            }
        )
    if not level_targets:
        raise ValueError("this speaker has no active drivers to level match")

    prior = lease.level_match_snapshot(current_context_id=context_id)
    if fixed_axis_request:
        requested_group = str(raw.get("speaker_group_id") or "").strip()
        requested_role = str(raw.get("role") or "").strip().lower()
        _assert_crossover_reference_axis_level_action(
            status,
            speaker_group_id=requested_group,
            role=requested_role,
        )
        target = next(
            (
                dict(item)
                for item in level_targets
                if item["speaker_group_id"] == requested_group
                and item["role"] == requested_role
            ),
            None,
        )
        if target is None:
            raise ValueError("the requested fixed-axis level target is not active")
        from jasper.active_speaker.capture_geometry import (
            comparison_set_valid,
            driver_level_geometry,
            reference_axis_driver_placement_instruction,
        )

        measurements = status.get("measurements")
        fixed_comparison_set = (
            measurements.get("active_comparison_set")
            if isinstance(measurements, Mapping)
            else None
        )
        if (
            not isinstance(fixed_comparison_set, Mapping)
            or not comparison_set_valid(fixed_comparison_set)
            or fixed_comparison_set.get("topology_id") != topology.topology_id
            or fixed_comparison_set.get("profile_context_id") != context_id
        ):
            raise ValueError(
                "the near-field comparison set is no longer current; restart "
                "the complete driver level check"
            )
        target["geometry"] = driver_level_geometry(
            requested_group, requested_role, "reference_axis"
        )
        target["placement_instruction"] = (
            reference_axis_driver_placement_instruction(requested_role)
        )
        continuing = False
        expected_level_identity = (
            _relay_level_identity(lease) if lease.input_device is not None else None
        )
    else:
        fixed_comparison_set = None
        continuing = (
            prior.get("context_id") == context_id
            and prior.get("targets") == level_targets
            and prior.get("ready") is not True
            and isinstance(prior.get("next_target"), dict)
        )
        target = dict(prior["next_target"]) if continuing else level_targets[0]
        expected_level_identity = _relay_level_identity(lease) if continuing else None
    run_token = secrets.token_urlsafe(18)
    setup_binding_id = context_id
    stop_event = threading.Event()
    stop_lock = threading.Lock()

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_level_ramp_spec(
                geometry_label=f"{target['role']} measurement position",
                placement_instruction=str(target["placement_instruction"]),
                tone_frequency_hz=float(target["tone_frequency_hz"]),
                run_token=run_token,
                setup_binding_id=setup_binding_id,
                setup_collect_positions=False,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    async def _prepare_driver_tone() -> dict[str, Any]:
        current_topology = load_output_topology()
        current_status = backend.status_payload()
        if fixed_axis_request:
            _assert_crossover_reference_axis_level_action(
                current_status,
                speaker_group_id=str(target["speaker_group_id"]),
                role=str(target["role"]),
            )
        correction_crossover_flow.validate_current_level_target_context(
            current_status,
            current_topology_id=current_topology.topology_id,
            expected_topology_id=topology.topology_id,
            expected_profile_context_id=context_id,
            speaker_group_id=str(target["speaker_group_id"]),
            role=str(target["role"]),
            expected_target_fingerprint=str(target["target_fingerprint"]),
        )
        return await web_commissioning.prepare_automatic_driver_level_match(
            topology,
            speaker_group_id=str(target["speaker_group_id"]),
            role=str(target["role"]),
            preset=preset,
            applied_profile=applied_profile,
            camilla_factory=_camilla,
        )

    async def _restore_driver_tone(prepared: dict[str, Any]) -> dict[str, Any]:
        return await web_commissioning.restore_automatic_driver_level_match(
            prepared,
            camilla_factory=_camilla,
        )

    async def _run(client: RelayClient, pi_session: PiCaptureSession) -> None:
        from jasper.capture_relay.session import CaptureStopped

        # `_run_relay_capture` has acquired the global relay slot before this
        # callback starts. A continuation preserves earlier driver locks; a
        # complete retune invalidates only after this run owns the slot.
        if not fixed_axis_request and not continuing:
            from jasper.active_speaker import repeat_admission

            repeat_admission.invalidate()
            lease.invalidate_comparison_context()
            clear_active_comparison_set(topology)
            log_event(
                logger,
                "correction.crossover_comparison_set_invalidated",
                reason="new_level_match_started",
                topology_id=topology.topology_id,
            )
        lease.configure_targets(level_targets)

        identity_guard = (
            _fixed_axis_level_identity_guard(
                lease,
                speaker_group_id=str(target["speaker_group_id"]),
                role=str(target["role"]),
            )
            if fixed_axis_request
            else nullcontext()
        )
        try:
            async with identity_guard:
                await _run_relay_level_match(
                    lease,
                    client,
                    pi_session,
                    geometry=str(target["geometry"]),
                    run_token=run_token,
                    setup_binding_id=setup_binding_id,
                    context_id=context_id,
                    tone_frequency_hz=float(target["tone_frequency_hz"]),
                    prepare_tone=_prepare_driver_tone,
                    restore_tone=_restore_driver_tone,
                    expected_level_identity=expected_level_identity,
                    reuse_noise_floor=False,
                    stop_requested=stop_event.is_set,
                    stop_lock=stop_lock,
                    begin_commit=lambda: _begin_relay_commit(
                        "level_ramp:crossover"
                    ),
                )
                binding = getattr(lease, "relay_setup_binding", None)
                identity = _relay_level_identity(lease)
                if (
                    binding is None
                    or not getattr(binding, "sha256", "")
                    or not identity.device_key
                ):
                    raise ValueError(
                        "the level check did not produce a complete microphone binding; "
                        "run it again"
                    )
                if fixed_axis_request:
                    assert fixed_comparison_set is not None
                    actual_device_sha256 = hashlib.sha256(
                        identity.device_key.encode("utf-8")
                    ).hexdigest()
                    expected_device_sha256 = str(
                        fixed_comparison_set.get("device_sha256") or ""
                    )
                    expected_calibration_id = str(
                        fixed_comparison_set.get("calibration_id") or ""
                    )
                    fixed_lock = lease.driver_sweep_locked_main_volume_db(
                        str(target["speaker_group_id"]),
                        str(target["role"]),
                        capture_geometry="reference_axis",
                    )
                    if (
                        actual_device_sha256 != expected_device_sha256
                        or identity.calibration_id != expected_calibration_id
                        or fixed_lock is None
                    ):
                        raise ValueError(
                            "the fixed-axis level check changed microphone identity, "
                            "calibration, or failed to lock; use the comparison-set "
                            "microphone and try again"
                        )
                    lease.context_id = context_id
                    from jasper.audio_measurement.ramp import (
                        LISTENING_POSITION_CAP_BUMP_DB,
                        LISTENING_POSITION_CAP_CEIL_DB,
                    )

                    log_event(
                        logger,
                        "correction.crossover_reference_axis_level_locked",
                        topology_id=topology.topology_id,
                        target_id=target["target_id"],
                        locked_main_volume_db=f"{fixed_lock:.1f}",
                        cap_bump_db=f"{LISTENING_POSITION_CAP_BUMP_DB:.1f}",
                        cap_ceil_db=f"{LISTENING_POSITION_CAP_CEIL_DB:.1f}",
                    )
                    return
        except CaptureStopped:
            lease.discard_driver_level_outcome(
                str(target["speaker_group_id"]),
                str(target["role"]),
                capture_geometry=requested_geometry,
            )
            raise
        lease.context_id = context_id
        level_snapshot = lease.level_match_snapshot(current_context_id=context_id)
        if level_snapshot.get("ready") is not True:
            next_target = level_snapshot.get("next_target")
            log_event(
                logger,
                "correction.crossover_driver_level_locked",
                topology_id=topology.topology_id,
                target_id=target["target_id"],
                next_target_id=(
                    next_target.get("target_id")
                    if isinstance(next_target, dict)
                    else None
                ),
                completed=len(level_snapshot.get("driver_level_locks") or {}),
                total=len(level_targets),
            )
            return
        driver_level_locks = lease.driver_level_locks()
        if (
            binding is None
            or not getattr(binding, "sha256", "")
            or not identity.device_key
            or len(driver_level_locks) != len(level_targets)
        ):
            raise ValueError(
                "the level check did not produce a complete microphone and "
                "volume binding; run it again"
            )
        bundle = _open_commissioning_bundle_for_level_match(
            topology, calibration_id=identity.calibration_id,
        )
        comparison_set = start_active_comparison_set(
            topology,
            profile_context_id=context_id,
            setup_sha256=str(binding.sha256),
            device_sha256=hashlib.sha256(
                identity.device_key.encode("utf-8")
            ).hexdigest(),
            calibration_id=identity.calibration_id,
            driver_level_locks=driver_level_locks,
            bundle_session_id=(bundle or {}).get("session_id"),
        )
        _activate_crossover_comparison_authorities(topology, comparison_set)
        if bundle is not None:
            from jasper.active_speaker import bundles as active_speaker_bundles

            active_speaker_bundles.attach_comparison_set(
                Path(str(bundle["bundle_dir"])),
                comparison_set_id=comparison_set["comparison_set_id"],
                comparison_set_fingerprint=comparison_set["fingerprint"],
            )
        log_event(
            logger,
            "correction.crossover_comparison_set_started",
            topology_id=topology.topology_id,
            comparison_set_id=comparison_set["comparison_set_id"],
            geometry_policy=DRIVER_PLACEMENT_POLICY_ID,
            calibrated=bool(identity.calibration_id),
            driver_locks=len(driver_level_locks),
            bundle_session=(bundle or {}).get("session_id"),
        )

    relay = _run_relay_capture(
        RelayCaptureKind(
            label="level_ramp:crossover",
            open=_open,
            run_and_consume=_run,
            request_stop=_request_stop,
        ),
        relay_base,
        return_url=_request_local_return_url(handler, "/correction/crossover/"),
    )
    return {"relay": relay, "level_match": lease.level_match_snapshot()}


def _handle_sync_relay_capture(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /sync/relay-capture: capture the sync markers via the cloud relay (the
    phone runs the capture page on jasper.tech) instead of a same-origin upload.

    CONFIG-GATED. The sync session window must already be open (the /sync/ Start
    button → handle_start), exactly as the browser flow requires before playing
    the marker. sync_flow owns the stimulus + analysis;
    this just bridges the relay transport through the shared orchestrator. The
    second real caller of the RelayCaptureKind seam — a new kind is a descriptor,
    not a new handler. ON-DEVICE: the acoustic marker capture is not exercised
    hardware-free (same status as the room relay)."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_sync_marker_spec

    from . import sync_flow

    relay_base = _require_relay_base()  # gated off until configured; inert otherwise
    session_token, err = sync_flow.relay_session_token()
    if err is not None:
        raise ValueError(err)
    assert session_token is not None

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_sync_marker_spec(),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    async def _run(client: RelayClient, pi_session: PiCaptureSession) -> None:
        await sync_flow.relay_run_and_consume(
            client,
            pi_session,
            session_token=session_token,
        )

    kind = RelayCaptureKind(
        label="sync_marker",
        open=_open,
        run_and_consume=_run,
    )
    return {
        "relay": _run_relay_capture(
            kind,
            relay_base,
            return_url=_request_local_return_url(handler, "/correction/sync"),
        )
    }


def _assert_crossover_driver_action(
    status: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
    capture_geometry: str,
) -> None:
    """Require one driver request to equal the server envelope next step."""

    from jasper.active_speaker.capture_geometry import DRIVER_CAPTURE_GEOMETRIES
    from jasper.active_speaker.crossover_envelope import build_crossover_envelope

    geometry = str(capture_geometry or "").lower()
    if geometry not in DRIVER_CAPTURE_GEOMETRIES:
        raise ValueError("driver capture geometry is unsupported")
    action_status = dict(status)
    action_status["relay"] = None
    expected_action = build_crossover_envelope(action_status).get("next_action")
    expected_body = (
        expected_action.get("body")
        if isinstance(expected_action, Mapping)
        else None
    )
    expected_geometry = (
        str(expected_body.get("capture_geometry") or "near_field").lower()
        if isinstance(expected_body, Mapping)
        else ""
    )
    if (
        not isinstance(expected_action, Mapping)
        or expected_action.get("id")
        not in {"measure_driver", "measure_reference_axis_driver"}
        or not isinstance(expected_body, Mapping)
        or str(expected_body.get("kind") or "") != "driver"
        or str(expected_body.get("speaker_group_id") or "") != speaker_group_id
        or str(expected_body.get("role") or "").lower() != role.lower()
        or expected_geometry != geometry
    ):
        raise ValueError(
            "the requested driver capture is not the server-owned next step"
        )


def _handle_crossover_region_geometry(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """Persist the explicit signed geometry for the server's current region."""

    raw = _read_json_body(handler)
    if set(raw) != {
        "expected_target_fingerprint",
        "signed_acoustic_path_difference_mm",
    }:
        raise ValueError("region geometry contains unsupported fields")
    if _active_relay_phase() is not None:
        raise ValueError("finish the current microphone capture first")
    from . import correction_crossover_backend

    return correction_crossover_backend.attest_commissioning_region_geometry(raw)


def _handle_crossover_candidate(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """Resume candidate publication after a measured-to-ready interruption."""

    if _read_json_body(handler):
        raise ValueError("measured candidate preparation accepts no browser fields")
    if _active_relay_phase() is not None:
        raise ValueError("finish the current microphone capture first")
    from . import correction_crossover_backend

    return correction_crossover_backend.prepare_commissioning_candidate()


def _post_crossover_relay_host_event(
    relay_base: str,
    session_id: str,
    pull_token: str,
    payload: dict[str, Any],
) -> Any:
    from jasper.capture_relay.client import RelayClient
    from jasper.capture_relay.health import relay_registration_token_from_env

    client = RelayClient(
        relay_base,
        timeout=_RELAY_REGISTER_TIMEOUT_S,
        registration_token=relay_registration_token_from_env(),
    )
    return client.post_host_event(session_id, pull_token, payload)


def _handle_crossover_summed_commissioning_relay(
    handler: BaseHTTPRequestHandler,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Register one recorder-only relay for the Active-owned summed host."""

    if set(raw) != {"kind"}:
        raise ValueError(
            "summed commissioning accepts no browser region, polarity, or delay fields"
        )
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_crossover_sweep_spec
    from jasper.capture_relay.session import CaptureStopped
    from jasper.audio_measurement.playback import PlaybackResult
    from jasper.active_speaker.commissioning_capture_producer import RawCaptureResult
    from jasper.active_speaker.test_signal_plan import (
        CROSSOVER_AMBIENT_DURATION_S,
        CROSSOVER_CAPTURE_HARD_TIMEOUT_S,
        CROSSOVER_CAPTURE_MAX_WAV_BYTES,
        SUMMED_SWEEP_DURATION_S,
    )
    from . import correction_crossover_backend, correction_crossover_flow

    lease = correction_crossover_backend.level_lease()
    if lease.unresolved_volume_safety is not None:
        return _crossover_volume_safety_refusal()
    calibration, expected_device_sha256 = (
        correction_crossover_backend.commissioning_recorder_binding()
    )
    relay_base = _require_relay_base()
    blocking = _crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(
            f"another measurement is in progress ({blocking}) — finish it "
            "before measuring the combined crossover"
        )
    verification = raw.get("kind") == "verification"
    expected = correction_crossover_backend.commissioning_region_status()
    expected_verification = expected.get("verification")
    next_capture = (
        expected_verification.get("next_target")
        if verification and isinstance(expected_verification, Mapping)
        else expected.get("next_capture")
    )
    expected_status = "applied_unverified" if verification else "collecting"
    if expected.get("status") != expected_status or not isinstance(
        next_capture, Mapping
    ):
        raise ValueError(
            str(
                expected.get("detail")
                or "the active commissioning run has no combined capture ready"
            )
        )
    expected_context = {
        "run_id": expected.get("run_id"),
        "owner_generation": expected.get("owner_generation"),
        "plan_fingerprint": expected.get("plan_fingerprint"),
    }
    driver_label = (
        "post-apply combined-response verification"
        if verification
        else "next server-selected crossover capture"
    )
    acknowledgement_binding = secrets.token_urlsafe(24)

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_crossover_sweep_spec(
                driver_label=driver_label,
                driver_role="summed",
                driver_capture_geometry="reference_axis",
                acknowledgement_binding=acknowledgement_binding,
                stimulus_duration_ms=int(round(SUMMED_SWEEP_DURATION_S * 1000)),
                ambient_duration_ms=int(
                    round(CROSSOVER_AMBIENT_DURATION_S * 1000)
                ),
                hard_timeout_ms=int(round(CROSSOVER_CAPTURE_HARD_TIMEOUT_S * 1000)),
                max_upload_bytes=CROSSOVER_CAPTURE_MAX_WAV_BYTES,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    stop_event = threading.Event()
    stop_lock = threading.Lock()

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        import asyncio

        acknowledgement_metadata: dict[str, Any] = {}

        def _validate_current_context() -> None:
            current = correction_crossover_backend.commissioning_region_status()
            current_context = {
                "run_id": current.get("run_id"),
                "owner_generation": current.get("owner_generation"),
                "plan_fingerprint": current.get("plan_fingerprint"),
            }
            current_verification = current.get("verification")
            current_next = (
                current_verification.get("next_target")
                if verification and isinstance(current_verification, Mapping)
                else current.get("next_capture")
            )
            if (
                current.get("status") != expected_status
                or current_context != expected_context
                or current_next != next_capture
            ):
                raise ValueError(
                    "the commissioning run or plan changed; create a new capture link"
                )
            current_calibration, current_device_sha256 = (
                correction_crossover_backend.commissioning_recorder_binding()
            )
            if (
                getattr(current_calibration, "calibration_id", None)
                != getattr(calibration, "calibration_id", None)
                or current_device_sha256 != expected_device_sha256
            ):
                raise ValueError(
                    "the commissioning microphone binding changed; create a new capture link"
                )

        def _validate_capture(result: Any) -> None:
            block = _relay_device_calibration_block(calibration, result.device)
            if block is not None:
                raise ValueError(block)
            actual_device = _sanitize_input_device(result.device) or {}
            actual_device_key = _relay_device_key(actual_device)
            if (
                not actual_device_key
                or hashlib.sha256(actual_device_key.encode("utf-8")).hexdigest()
                != expected_device_sha256
            ):
                raise ValueError(
                    "the microphone differs from the fixed-axis comparison set; "
                    "select the same microphone or restart driver measurement"
                )

        def _prepare_armed(state: Any, acknowledgement: Any) -> None:
            _validate_current_context()
            required = pi_session.spec.acknowledgement
            assert required is not None
            acknowledgement_metadata.clear()
            acknowledgement_metadata.update(
                {
                    "policy_id": required.id,
                    "acknowledgement_binding": required.binding_id,
                    "relay_session_id": pi_session.session_id,
                    "capture_page": state.capture_page,
                    "acknowledgement": acknowledgement,
                }
            )

        async def _raw_transport(play_once: Any) -> RawCaptureResult:
            async def _play_sequence(on_sweep_ready: Callable[[], None]) -> Any:
                await asyncio.sleep(CROSSOVER_AMBIENT_DURATION_S)
                await asyncio.to_thread(on_sweep_ready)
                return await play_once()

            def _validate_playback(result: Any) -> None:
                if not isinstance(result, PlaybackResult):
                    raise RuntimeError("admitted summed playback did not complete")

            try:
                result, _playback = (
                    await correction_crossover_flow.run_crossover_relay_transport(
                        client,
                        pi_session,
                        run_async=_run_async,
                        play_sequence=_play_sequence,
                        validate_playback=_validate_playback,
                        prepare_armed=_prepare_armed,
                        validate_capture=_validate_capture,
                        post_host_event=lambda session_id, pull_token, payload: (
                            _post_crossover_relay_host_event(
                                relay_base,
                                session_id,
                                pull_token,
                                payload,
                            )
                        ),
                        begin_finishing=lambda: _begin_relay_finishing(
                            "crossover_sweep:summed"
                        ),
                        begin_commit=lambda: _begin_relay_commit(
                            "crossover_sweep:summed"
                        ),
                        ambient_duration_s=CROSSOVER_AMBIENT_DURATION_S,
                        stop_event=stop_event,
                    )
                )
            except CaptureStopped as exc:
                raise asyncio.CancelledError from exc
            metadata = {
                "relay_session_id": pi_session.session_id,
                "device": result.device,
                "noise_floor": result.noise_floor,
                "setup": result.setup,
                "fixed_axis_acknowledgement": acknowledgement_metadata,
            }
            return RawCaptureResult(result.wav, metadata)

        from jasper.correction import coordinator

        async with coordinator.measurement_window():
            if verification:
                recorded = (
                    await correction_crossover_backend.capture_next_commissioning_verification(
                        _raw_transport,
                        camilla_factory=_camilla,
                    )
                )
            else:
                recorded = await correction_crossover_backend.capture_next_commissioning_region(
                    _raw_transport,
                    camilla_factory=_camilla,
                )
        log_event(
            logger,
            "correction.crossover_region_capture_recorded",
            run_id=expected_context["run_id"],
            plan_fingerprint=expected_context["plan_fingerprint"],
            group=recorded.get("speaker_group_id"),
            region=recorded.get("region_id"),
            evidence_kind=recorded.get("evidence_kind"),
            capture_fingerprint=recorded.get("capture_fingerprint"),
        )

    kind = RelayCaptureKind(
        label="crossover_sweep:summed",
        open=_open,
        run_and_consume=_run_and_consume,
        request_stop=_request_stop,
    )
    return {
        "relay": _run_relay_capture(
            kind,
            relay_base,
            return_url=_request_local_return_url(handler, "/correction/crossover/"),
        )
    }


def _handle_crossover_relay_capture(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/relay-capture: capture one active-crossover driver sweep.

    Driver requests retain the production isolated-driver path. Summed requests
    enter the strict Active-owned host, which chooses region, polarity, delay,
    graph, attempt, and ordinal; post-apply verification reuses the summed
    recorder path, and the browser supplies recorder bytes only.

    The third
    real caller of the RelayCaptureKind seam — a new kind is a descriptor, not a
    new orchestrator. The `crossover_sweep` spec + `correction_crossover_flow`'s
    relay run-and-consume own the stimulus + analysis; this bridges the relay
    transport through the shared orchestrator. Body:
    `{kind: "driver"|"summed"|"verification", speaker_group_id, role (driver
    only), capture_geometry (server-envelope
    driver action only)}`. ON-DEVICE: the acoustic capture is
    not exercised hardware-free (same status as the room/sync relay — H2).

    Measurement mutual-exclusion is SERVER-computed twice, never client-supplied:
    refused here at POST time while room correction / balance / sync is active
    (mirrors sync's `relay_precheck`), and re-checked at armed time inside the
    run-and-consume (the phone can arm minutes later — a sweep played over
    another measurement silently corrupts both captures)."""
    # The discriminator is the only browser input trusted far enough to select
    # an ingress. The summed branch rejects every former browser-owned DSP field
    # and delegates exact operation selection to the typed internal host.
    raw = _read_json_body(handler)

    from . import correction_crossover_flow

    kind_id = correction_crossover_flow.relay_kind_from_raw(raw)
    if kind_id in {"summed", "verification"}:
        return _handle_crossover_summed_commissioning_relay(handler, raw)

    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_crossover_sweep_spec
    from jasper.output_topology import load_output_topology

    from . import correction_crossover_backend

    lease = correction_crossover_backend.level_lease()
    if lease.unresolved_volume_safety is not None:
        return _crossover_volume_safety_refusal()
    relay_base = _require_relay_base()  # gated off until configured; inert otherwise
    blocking = _crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(
            f"another measurement is in progress ({blocking}) — finish it "
            "before starting a crossover relay capture"
        )
    requested_geometry_hint = str(
        raw.get("capture_geometry") or "near_field"
    ).lower()
    status = correction_crossover_backend.status_payload()
    applied_profile = status.get("applied_profile")
    if not isinstance(applied_profile, dict):
        raise ValueError(
            "the applied crossover snapshot is unavailable; reapply it before "
            "measuring"
        )
    topology = load_output_topology()
    setup = status.get("setup") if isinstance(status, dict) else None
    if (
        not status.get("active")
        or not isinstance(setup, dict)
        or setup.get("status") != "ready"
    ):
        raise ValueError(
            "protected speaker setup is no longer ready; finish it before "
            "capturing the crossover"
        )
    protected_profile = (
        setup.get("protected_profile") if isinstance(setup, dict) else None
    )
    profile_context_id = (
        str(protected_profile.get("candidate_fingerprint") or "")
        if isinstance(protected_profile, dict)
        else ""
    )
    applied_crossover = (
        setup.get("applied_crossover") if isinstance(setup, dict) else None
    )
    level = status.get("level_match") if isinstance(status, dict) else None
    if (
        not profile_context_id
        or not isinstance(applied_crossover, dict)
        or applied_crossover.get("valid") is not True
        or not isinstance(level, dict)
        or level.get("valid") is not True
        or str(level.get("context_id") or "") != profile_context_id
        or (
            level.get("ready") is not True
            and not (
                kind_id == "driver"
                and requested_geometry_hint == "reference_axis"
            )
        )
    ):
        raise ValueError(
            "speaker setup changed after level matching; run the crossover "
            "level check again"
        )
    calibration = getattr(lease, "mic_calibration", None)
    level_identity = _relay_level_identity(lease)
    if calibration is not None:
        raw["calibration_id"] = calibration.calibration_id
        raw["measurement_mode"] = "phase_aware"
    else:
        raw["measurement_mode"] = "magnitude_only"
    from jasper.active_speaker.capture_geometry import comparison_set_valid

    measurements = status.get("measurements")
    comparison_set = (
        measurements.get("active_comparison_set")
        if isinstance(measurements, dict)
        else None
    )
    if not isinstance(comparison_set, dict) or not comparison_set_valid(
        comparison_set
    ) or (
        str(comparison_set.get("topology_id") or "") != topology.topology_id
        or str(comparison_set.get("profile_context_id") or "")
        != profile_context_id
    ):
        raise ValueError(
            "the automatic crossover measurement set is no longer active; "
            "run the near-field level check again"
        )
    targets_raw = status.get("targets")
    targets: dict[str, Any] = targets_raw if isinstance(targets_raw, dict) else {}
    target_rows = targets.get("drivers" if kind_id == "driver" else "summed") or []
    requested_group = str(raw.get("speaker_group_id") or "")
    requested_role = str(raw.get("role") or "").lower()
    requested_geometry = (
        str(raw.get("capture_geometry") or "near_field").lower()
        if kind_id == "driver"
        else "reference_axis"
    )

    def _assert_server_owned_driver_action(
        current_status: Mapping[str, Any],
    ) -> None:
        if kind_id != "driver":
            return
        _assert_crossover_driver_action(
            current_status,
            speaker_group_id=requested_group,
            role=requested_role,
            capture_geometry=requested_geometry,
        )

    _assert_server_owned_driver_action(status)
    if kind_id == "driver":
        raw["capture_geometry"] = requested_geometry
    target = next(
        (
            item
            for item in target_rows
            if isinstance(item, dict)
            and str(item.get("speaker_group_id") or "") == requested_group
            and (
                kind_id != "driver"
                or str(item.get("role") or "").lower() == requested_role
            )
        ),
        None,
    )
    if not isinstance(target, dict):
        raise ValueError("the requested crossover measurement target is not active")
    raw["speaker_group_id"] = str(target.get("speaker_group_id") or "")
    if kind_id == "driver":
        raw["role"] = str(target.get("role") or "").lower()
        target_fingerprint = str(target.get("target_fingerprint") or "")
    else:
        raw.pop("role", None)
        target_fingerprint = str(target.get("group_fingerprint") or "")
    acknowledgement_binding = secrets.token_urlsafe(24)
    driver_label = correction_crossover_flow.relay_driver_label(raw)

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        from jasper.active_speaker.test_signal_plan import (
            CROSSOVER_AMBIENT_DURATION_S,
            CROSSOVER_CAPTURE_HARD_TIMEOUT_S,
            CROSSOVER_CAPTURE_MAX_WAV_BYTES,
            SUMMED_SWEEP_DURATION_S,
            driver_sweep_duration_s,
        )

        sweep_duration_s = (
            driver_sweep_duration_s(str(raw.get("role") or ""))
            if kind_id == "driver"
            else SUMMED_SWEEP_DURATION_S
        )
        return correction_adapter.open_capture(
            client,
            build_crossover_sweep_spec(
                driver_label=driver_label,
                driver_role=str(raw.get("role") or "summed"),
                driver_capture_geometry=requested_geometry,
                acknowledgement_binding=acknowledgement_binding,
                stimulus_duration_ms=int(round(sweep_duration_s * 1000)),
                ambient_duration_ms=int(
                    round(CROSSOVER_AMBIENT_DURATION_S * 1000)
                ),
                hard_timeout_ms=int(round(CROSSOVER_CAPTURE_HARD_TIMEOUT_S * 1000)),
                max_upload_bytes=CROSSOVER_CAPTURE_MAX_WAV_BYTES,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    def _post_host_event(session_id: str, pull_token: str, payload: dict[str, Any]):
        return _post_crossover_relay_host_event(
            relay_base,
            session_id,
            pull_token,
            payload,
        )

    def _validate_capture(result: Any) -> None:
        block = _relay_device_calibration_block(calibration, result.device)
        if block is not None:
            raise ValueError(block)
        _assert_relay_level_identity(
            lease, level_identity, device=result.device
        )

    async def _get_capture_volume() -> Any:
        from jasper.camilla import CamillaUnavailable

        try:
            return await _camilla().get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError(
                "CamillaDSP is unavailable during crossover capture"
            ) from exc

    async def _set_capture_volume(db: float) -> Any:
        from jasper.camilla import CamillaUnavailable

        try:
            return await _camilla().set_volume_db(db, best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError(
                "CamillaDSP is unavailable during crossover capture"
            ) from exc

    async def _prepare_capture_play() -> bool:
        if kind_id == "driver":
            return await lease.acquire_driver_sweep_volume(
                str(raw["speaker_group_id"]),
                str(raw["role"]),
                _get_capture_volume,
                _set_capture_volume,
                capture_geometry=requested_geometry,
            )
        return await lease.acquire_summed_sweep_volume(
            str(raw["speaker_group_id"]),
            _get_capture_volume,
            _set_capture_volume,
        )

    async def _restore_capture_play() -> bool:
        from .correction_crossover_backend import (
            UnresolvedVolumeRecoveryResult,
        )

        recovery = await lease.finish_sweep_volume(
            _set_capture_volume,
            _get_capture_volume,
        )
        if recovery is UnresolvedVolumeRecoveryResult.EXACT_RESTORED:
            return True
        if recovery is UnresolvedVolumeRecoveryResult.EMERGENCY_ATTENUATED:
            raise RuntimeError(
                "JTS could not restore the listening volume after measurement; "
                "it lowered output to a safe fallback. Set your volume again."
            )
        raise RuntimeError(
            "JTS could not restore or lower the measurement volume. Stop playback "
            "and reapply the speaker profile before listening."
        )

    def _driver_locked_main_volume_db() -> float | None:
        if kind_id != "driver":
            return None
        return lease.driver_sweep_locked_main_volume_db(
            str(raw["speaker_group_id"]),
            str(raw["role"]),
            capture_geometry=requested_geometry,
        )

    def _validate_current_context() -> None:
        current_topology = load_output_topology()
        current_status = correction_crossover_backend.status_payload()
        _assert_server_owned_driver_action(current_status)
        correction_crossover_flow.validate_current_capture_context(
            current_status,
            current_topology_id=current_topology.topology_id,
            expected_topology_id=topology.topology_id,
            expected_profile_context_id=profile_context_id,
            expected_comparison_set=comparison_set,
            kind=kind_id,
            speaker_group_id=str(raw["speaker_group_id"]),
            role=str(raw.get("role") or ""),
            capture_geometry=requested_geometry,
            expected_target_fingerprint=target_fingerprint,
        )

    def _reserve_repeat_attempt() -> Mapping[str, Any]:
        from jasper.active_speaker import repeat_admission
        from jasper.active_speaker.capture_geometry import driver_repeat_binding

        repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
            speaker_group_id=str(raw["speaker_group_id"]),
            role=str(raw["role"]),
            target_fingerprint=target_fingerprint,
            capture_geometry=requested_geometry,
        )

        return repeat_admission.reserve(
            comparison_set,
            target_id=repeat_target_id,
            target_fingerprint=repeat_target_fingerprint,
        )

    def _finish_failed_repeat_attempt(
        reservation: Mapping[str, Any], failure_type: str
    ) -> None:
        from jasper.active_speaker import repeat_admission
        from jasper.active_speaker import web_measurement
        from jasper.active_speaker.capture_geometry import driver_repeat_binding

        # Recording owns the ready -> aborted transition when final measurement
        # or admission-completion persistence fails. A second failed abort may
        # leave a truthful fail-closed ``ready`` state.
        # The relay boundary still sees and reports the original exception,
        # but must not try to finish the consumed reservation a second time.
        # Match the exact target/fingerprint/attempt before treating it as the
        # same terminal finalization.
        target_id, repeat_target_fingerprint = driver_repeat_binding(
            speaker_group_id=str(raw["speaker_group_id"]),
            role=str(raw["role"]),
            target_fingerprint=target_fingerprint,
            capture_geometry=requested_geometry,
        )
        if repeat_admission.reservation_is_finished(
            comparison_set,
            target_id=target_id,
            target_fingerprint=repeat_target_fingerprint,
            attempt=int(reservation.get("attempt") or 0),
        ):
            return

        if int(reservation.get("attempt") or 0) >= repeat_admission.MAX_ATTEMPTS:
            # The attempt may fail seconds after armed-time validation. Re-read
            # topology/profile/comparison before old accepted evidence can be
            # committed through the terminal fallback.
            _validate_current_context()
            finalized = web_measurement.finalize_driver_repeats_after_terminal_failure(
                comparison_set=comparison_set,
                speaker_group_id=str(raw["speaker_group_id"]),
                role=str(raw["role"]),
                target_fingerprint=target_fingerprint,
                capture_geometry=requested_geometry,
                reservation=reservation,
                failure_type=failure_type,
                repeat_store=lease,
            )
            if finalized is not None:
                return

        repeat_admission.finish(
            comparison_set,
            target_id=target_id,
            target_fingerprint=repeat_target_fingerprint,
            token=str(reservation.get("token") or ""),
            result={
                "accepted": False,
                "reject_reason": "capture_failed",
                "failure_type": str(failure_type)[:80],
                "phase": "transport",
            },
            status=repeat_admission.failure_status(reservation.get("attempt")),
        )

    stop_event = threading.Event()
    stop_lock = threading.Lock()

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    base_run_and_consume = correction_crossover_flow.build_crossover_relay_run_and_consume(
        raw,
        _run_async,
        _camilla,
        post_host_event=_post_host_event,
        # Server-side probe, re-evaluated fresh when the phone actually arms.
        blocking_phase=_crossover_blocking_phase,
        validate_capture=_validate_capture,
        prepare_play=_prepare_capture_play,
        restore_play=_restore_capture_play,
        driver_locked_main_volume_db=(
            _driver_locked_main_volume_db if kind_id == "driver" else None
        ),
        comparison_set=comparison_set,
        applied_profile=(
            applied_profile if isinstance(applied_profile, dict) else None
        ),
        target_fingerprint=target_fingerprint,
        validate_current_context=_validate_current_context,
        reserve_repeat_attempt=(
            _reserve_repeat_attempt if kind_id == "driver" else None
        ),
        finish_failed_repeat_attempt=(
            _finish_failed_repeat_attempt if kind_id == "driver" else None
        ),
        begin_finishing=lambda: _begin_relay_finishing(
            f"crossover_sweep:{kind_id}"
        ),
        begin_commit=lambda: _begin_relay_commit(
            f"crossover_sweep:{kind_id}"
        ),
        stop_event=stop_event,
        stop_lock=stop_lock,
    )

    kind = RelayCaptureKind(
        label=f"crossover_sweep:{kind_id}",
        open=_open,
        run_and_consume=base_run_and_consume,
        request_stop=_request_stop,
    )
    return {
        "relay": _run_relay_capture(
            kind,
            relay_base,
            return_url=_request_local_return_url(handler, "/correction/crossover/"),
        )
    }


def _handle_crossover_relay_cancel() -> dict[str, Any]:
    """Stop Crossover relay work and keep its slot until cleanup completes."""

    relay = _request_relay_stop("crossover_sweep:", "level_ramp:crossover")
    return {"relay": relay}


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

        restore_level_match = getattr(sess, "restore_level_match_volume", None)
        if callable(restore_level_match):
            async def _restore_level_match() -> bool:
                return await restore_level_match(
                    lambda db: cam.set_volume_db(db, best_effort=False)
                )

            if _run_async(_restore_level_match(), timeout=5.0):
                logger.info(
                    "restored main_volume after relay level-match workflow"
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

        async def _restore() -> None:
            await cam.set_volume_db(
                al.original_main_volume_db, best_effort=True
            )

        _run_async(_restore(), timeout=5.0)
        logger.info(
            "restored main_volume to %.1f dB after autolevel workflow",
            al.original_main_volume_db,
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
    from jasper.correction import failures

    evidence_failure = failures.measurement_evidence_failure(
        getattr(sess, "confidence_report", None),
    )
    if evidence_failure is not None:
        raise RoomRequestFailure(
            "measurement confidence contains blocking evidence",
            evidence_failure,
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
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
    simulated, and returned with their sim verdict for the UI to surface
    for user confirmation. Applying happens only via /propose/apply.
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
    server RE-VALIDATES the set against the active strategy caps and
    RE-SIMULATES it (never trusting the client that it was accepted); only
    if the deterministic gate accepts AND the user confirmed does it
    populate ``session.peqs`` and route through the EXISTING apply path
    (the same simulate/headroom/re-clip apply any correction gets).
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

    evidence_failure = failures.measurement_evidence_failure(
        getattr(sess, "confidence_report", None),
    )
    if evidence_failure is not None:
        raise RoomRequestFailure(
            "measurement confidence contains blocking evidence",
            evidence_failure,
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    # Re-validate schema + bounds against the ACTIVE strategy caps.
    from jasper.correction import strategy as _strategy
    strat = _strategy.resolve_correction_strategy(
        getattr(sess, "strategy_choice", None)
        or _strategy.DEFAULT_CORRECTION_STRATEGY_ID
    )
    bounds = strat.to_dict()
    packet = {
        "advisor_policy": {"allowed_actions": [
            {"id": "propose_correction_peq_adjustment", "allowed": True, "reasons": []},
        ]},
        "correction": {"strategy_bounds": bounds},
    }
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

    # Re-SIMULATE server-side; a client cannot assert acceptance for us.
    sim = proposal_sim.simulate_correction_proposal(
        validated_peqs,
        measured=getattr(sess, "measured_curve", None),
        baseline=getattr(sess, "position1_curve", None)
        or getattr(sess, "measured_curve", None),
        target=getattr(sess, "target_curve", None),
        max_total_boost_db=float(bounds.get("max_total_boost_db", 0.0)),
        f_high_hz=float(bounds.get("f_high_hz", 350.0)),
    )
    if not sim.accepted:
        return {
            "applied": False,
            "failure": failures.public_failure(
                failures.TUNING_PROPOSAL_REJECTED,
            ),
            "reason": "proposal rejected by the deterministic simulation gate",
            "simulation": sim.to_dict(),
            "session_id": sess.session_id,
            "state": sess.state.value,
        }
    if sim.acceptance is None:
        # Fail-closed at the apply seam: the P4 acceptance judge could not
        # run (baseline/target curves were absent), so the promise "every
        # applied proposal is judged by the same acceptance evaluator" can't
        # hold. The propose PREVIEW stays lenient by design (a ring+headroom
        # only preview is honest there); applying without the judge is not.
        return {
            "applied": False,
            "failure": failures.public_failure(
                failures.TUNING_PROPOSAL_REJECTED,
            ),
            "code": "missing_acceptance_basis",
            "reason": (
                "proposal could not be judged against the room baseline "
                "(no baseline/target curves for the acceptance evaluator); "
                "not applying"
            ),
            "simulation": sim.to_dict(),
            "session_id": sess.session_id,
            "state": sess.state.value,
        }

    # Deterministic gate + explicit confirm both passed: swap in the
    # proposed filters and route through the SAME apply path any
    # correction uses (which re-clips headroom at emit).
    log_event(
        logger,
        "correction.tuning_apply",
        session_id=sess.session_id,
        filter_count=len(validated_peqs),
        sim_verdict=(sim.acceptance or {}).get("verdict"),
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
        _current, current_snapshot = await _snapshot_running_room_graph(sess, cam)
        try:
            target = await _write_no_room_correction_config(
                sess,
                cam,
                current_snapshot_path=current_snapshot,
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
        _current, snapshot_path = await _snapshot_running_room_graph(sess, cam)
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
    assert_correction_graph_safe(result.yaml)
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
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _serve_json_route(
            self, label: str, handler_fn: Callable[[BaseHTTPRequestHandler], dict[str, Any]],
        ) -> None:
            """Shared JSON GET-route wrapper: any handler failure surfaces
            as a 500 JSON error instead of a stack-trace page or a dead
            request thread — the poll posture /status, /envelope, and
            /sessions share (one wrapper so the blanket net isn't
            re-declared per route)."""
            try:
                self._send_json(handler_fn(self))
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
                "correction_homeowner_failure",
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

            # The relay capture has different response semantics (a dict +
            # ValueError → client error), and MUST be handled here inside the
            # /sync/ prefix dispatch — the main do_POST ladder never sees /sync/*.
            if path == "/sync/relay-capture":
                try:
                    self._send_json(_handle_sync_relay_capture(self))
                except ValueError as e:
                    self._send_client_error(str(e))
                return

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
                        body = _read_wav_body(self, max_bytes=2 * 1024 * 1024)
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

            if path in {
                "/crossover/summed-capture",
                "/crossover/summed-capture-sweep",
            }:
                # These legacy raw surfaces have no production summed-capture
                # authority. Refuse before importing the backend, obtaining its
                # level lease, or reading attacker-controlled JSON/WAV bytes.
                self._send_json(
                    _summed_capture_unavailable(ingress=path),
                    status=HTTPStatus.CONFLICT,
                )
                return

            if path == "/crossover/relay-capture":
                # One bounded discriminator selects the existing isolated path
                # or the strict internal summed host. Neither accepts browser
                # graph, region, polarity, delay, or admission authority.
                try:
                    payload = _handle_crossover_relay_capture(self)
                    self._send_json(
                        payload,
                        status=(
                            HTTPStatus.CONFLICT
                            if payload.get("status") == "refused"
                            else HTTPStatus.OK
                        ),
                    )
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == "/crossover/region-geometry":
                try:
                    self._send_json(_handle_crossover_region_geometry(self))
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == "/crossover/candidate":
                try:
                    self._send_json(_handle_crossover_candidate(self))
                except ValueError as e:
                    self._send_json(
                        {"ok": False, "error": str(e)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    logger.exception("%s failed", path)
                    self._send_json({"ok": False, "error": str(e)}, status=500)
                return

            from . import correction_crossover_flow
            from . import correction_crossover_backend as crossover_backend

            volume_sensitive_routes = {
                "/crossover/level-match",
                "/crossover/apply",
                "/crossover/driver-test",
                "/crossover/summed-test",
                "/crossover/driver-capture-sweep",
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

                    async def _set_recovery_volume(db: float) -> bool:
                        try:
                            return await cam.set_volume_db(db, best_effort=False)
                        except CamillaUnavailable as exc:
                            raise RuntimeError(
                                "CamillaDSP is unavailable during volume recovery"
                            ) from exc

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

                if path == "/crossover/relay-cancel":
                    self._send_json(_handle_crossover_relay_cancel())
                    return

                if path == "/crossover/level-match":
                    self._send_json(_handle_crossover_relay_level_match(self))
                    return

                if path == "/crossover/apply":
                    raw = _read_json_body(self)
                    payload, status = correction_crossover_flow.handle_apply(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_active_relay_phase(),
                    )
                    self._send_json(payload, status=int(status))
                    return

                if path == "/crossover/restore":
                    payload, status = correction_crossover_flow.handle_restore(
                        _run_async,
                        _camilla,
                        blocking_phase=_active_relay_phase(),
                    )
                    self._send_json(payload, status=int(status))
                    return

                raw = _read_json_body(self)
                if path == "/crossover/driver-test":
                    payload, status = correction_crossover_flow.handle_driver_test(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_crossover_direct_audio_blocking_phase(),
                    )
                elif path == "/crossover/driver-confirm":
                    payload, status = correction_crossover_flow.handle_driver_confirm(
                        raw,
                        _run_async,
                        _camilla,
                    )
                elif path == "/crossover/driver-abort":
                    payload, status = correction_crossover_flow.handle_driver_abort(
                        _run_async,
                        _camilla,
                    )
                elif path == "/crossover/summed-test":
                    payload, status = correction_crossover_flow.handle_summed_test(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_crossover_direct_audio_blocking_phase(),
                    )
                elif path == "/crossover/driver-capture-sweep":
                    payload, status = correction_crossover_flow.handle_driver_capture_sweep(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_crossover_direct_audio_blocking_phase(),
                    )
                else:
                    raise ValueError(f"unknown crossover route: {path}")
                self._send_json(payload, status=int(status))
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
            if path == "/crossover/status":
                from . import correction_crossover_flow
                try:
                    payload, status = correction_crossover_flow.handle_status(
                        relay=_get_relay_capture_for(
                            "crossover_sweep:", "level_ramp:crossover"
                        ),
                    )
                    self._send_json(payload, status=int(status))
                except (OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.exception("/crossover/status failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/crossover/envelope":
                from . import correction_crossover_flow
                try:
                    payload, status = correction_crossover_flow.handle_envelope(
                        relay=_get_relay_capture_for(
                            "crossover_sweep:", "level_ramp:crossover"
                        ),
                    )
                    self._send_json(payload, status=int(status))
                except (OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.exception("/crossover/envelope failed")
                    self._send_json({"error": str(e)}, status=500)
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
                try:
                    payload, status = correction_bass_flow.handle_status()
                    self._send_json(payload, status=int(status))
                except (OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.exception("/bass/status failed")
                    self._send_json({"error": str(e)}, status=500)
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
                    except RoomRequestFailure as e:
                        self._send_room_failure(
                            e.failure,
                            diagnostic=str(e),
                            status=e.status,
                        )
                    except (CorrectionRuntimeSafetyError, CarrierCannotHostEq) as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.SPEAKER_MEASUREMENT_UNSAFE,
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
                if path == "/relay/capture":
                    from jasper.correction import failures
                    try:
                        self._send_json(_handle_relay_capture(self))
                    except (OSError, RuntimeError, ValueError) as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.PHONE_CAPTURE_UNAVAILABLE,
                            ),
                            diagnostic=str(e),
                            status=(
                                HTTPStatus.CONFLICT
                                if isinstance(e, ValueError)
                                else HTTPStatus.SERVICE_UNAVAILABLE
                            ),
                        )
                    return
                if path == "/relay/level-match":
                    from jasper.correction import failures
                    try:
                        self._send_json(_handle_relay_level_match(self))
                    except (OSError, RuntimeError, ValueError) as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.PHONE_CAPTURE_UNAVAILABLE,
                            ),
                            diagnostic=str(e),
                            status=(
                                HTTPStatus.CONFLICT
                                if isinstance(e, ValueError)
                                else HTTPStatus.SERVICE_UNAVAILABLE
                            ),
                        )
                    return
                if path == "/relay/verify":
                    from jasper.correction import failures
                    try:
                        self._send_json(_handle_relay_verify(self))
                    except (OSError, RuntimeError, ValueError) as e:
                        self._send_room_failure(
                            failures.public_failure(
                                failures.PHONE_CAPTURE_UNAVAILABLE,
                            ),
                            diagnostic=str(e),
                            status=(
                                HTTPStatus.CONFLICT
                                if isinstance(e, ValueError)
                                else HTTPStatus.SERVICE_UNAVAILABLE
                            ),
                        )
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
                    except RoomRequestFailure as e:
                        self._send_room_failure(
                            e.failure,
                            diagnostic=str(e),
                            status=e.status,
                        )
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
                    except RoomRequestFailure as e:
                        self._send_room_failure(
                            e.failure,
                            diagnostic=str(e),
                            status=e.status,
                        )
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
    target, *, hostname: str = "jts.local",
) -> ThreadingHTTPServer:
    """Build the wizard server. `target` is socket/tuple/int per
    _systemd.make_http_server's contract."""
    from . import _systemd
    cfg = {"hostname": hostname}
    return _systemd.make_http_server(target, _make_handler(cfg))


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
            "correction.crossover_level_run_unavailable",
            correction_crossover_backend.claim_level_run_owner,
        ),
        (
            "correction.active_commissioning_run_unavailable",
            correction_crossover_backend.claim_commissioning_run_owner,
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

    # Socket Accept=no + one service ExecStart make this the sole lifecycle
    # boundary that may retire unfinished work from a previous process.
    _claim_crossover_state_owners()

    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target, hostname=args.hostname)

    handler_cls = server.RequestHandlerClass
    tracker = _systemd.IdleShutdownTracker()
    _systemd.install_request_idle_bump(handler_cls, tracker)
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
