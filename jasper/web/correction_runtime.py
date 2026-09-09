# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-correction wizard: the runtime floor under its other three modules.

Owns the single background asyncio loop and the sync-to-async bridge onto it,
the per-request CamillaController factory, both bounded request-body readers
(JSON and WAV) with their caps, and the two request exceptions the routes
raise.

:mod:`jasper.web.correction_capture`, :mod:`jasper.web.correction_handlers`
and :mod:`jasper.web.correction_setup` all import this module; it imports none
of them, which is what keeps the four acyclic.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any

from ..log_event import log_event

from ._common import (
    JsonBodyError,
    read_json_object,
)


#: One logger for this wizard's modules (correction_setup,
#: correction_handlers, correction_capture, correction_runtime) so every
#: event= line keeps the journal name operators already grep.
logger = logging.getLogger("jasper.web.correction_setup")

MAX_JSON_BODY_BYTES = 64 * 1024
MAX_CALIBRATION_UPLOAD_JSON_BYTES = 1024 * 1024
# Browser captures are mono 16-bit PCM at 48 kHz. A normal 10 s sweep
# upload is ~1 MB; 32 MB leaves generous room for measurement-window
# setup latency while still avoiding unbounded reads in the Pi web
# process. nginx's client_max_body_size must stay >= the largest of these
# four caps so the app, not a raw 413, refuses an oversized body.
MAX_WAV_BODY_BYTES = 32 * 1024 * 1024
MAX_SYNC_WAV_BODY_BYTES = 2 * 1024 * 1024

# Exact set/readback plus the emergency set/readback each use Camilla's bounded
# reconnect contract. Keep the HTTP owner alive for the complete sequence.
CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S = 45.0
RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S = CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S


class BadRequest(ValueError):
    """Client supplied an invalid request body."""


class RequestConflict(RuntimeError):
    """Client request conflicts with the current correction session state."""


# Lazy-init on first use so importing this module is cheap (lets `python -m
# jasper.web.correction_setup --help` work without spinning up a loop). The
# lock exists only to keep loop creation single-creator: every caller must
# reach the loop through `ensure_loop`, because two loops means two capture
# owners.
# ORDERING: never enter this bridge (ensure_loop / run_async /
# run_graph_mutation) while holding correction_capture._session_lock — loop
# work takes that lock itself (`_set_capture_slot`), so holding it across the
# bridge stalls the loop and every capture-state reader with it.
_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
# Set by the loop thread as run_forever()'s first callback. Thread liveness,
# not `_loop.is_running()`, decides re-creation: is_running() reads False
# between Thread.start() and run_forever(), so gating on it let a second
# caller build a second loop despite the lock.
_loop_running = threading.Event()


def _run_loop(loop: asyncio.AbstractEventLoop, running: threading.Event) -> None:
    loop.call_soon(running.set)
    loop.run_forever()


def ensure_loop() -> asyncio.AbstractEventLoop:
    """Start (or reuse) a single background asyncio loop, returned already
    running. The HTTP handlers schedule coroutines onto it via
    `run_coroutine_threadsafe`."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop_thread is None or not _loop_thread.is_alive():
            _loop_running.clear()
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_run_loop,
                args=(_loop, _loop_running),
                name="jasper-correction-loop",
                daemon=True,
            )
            _loop_thread.start()
        loop = _loop
    # Outside the lock: a caller that arrives during startup waits for the one
    # loop rather than holding the create-once decision behind it.
    _loop_running.wait()
    return loop


def run_async(coro, *, timeout: float | None = 60.0):
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

    fut = asyncio.run_coroutine_threadsafe(_tracked(), ensure_loop())
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # A timed-out HTTP/poll thread no longer owns a useful result. Cancel
        # the loop task so delayed measurement audio cannot start after the
        # caller has already reported failure. Owning coroutines retain their
        # bounded/shielded rollback in ``finally`` blocks.
        fut.cancel()
        if not drained.wait(RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S):
            log_event(
                logger,
                "correction.async_cancel_drain_timeout",
                level=logging.CRITICAL,
                timeout_s=RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S,
            )
            # A terminal response must never release measurement ownership
            # while its graph/volume finalizer can still mutate the speaker.
            # The threshold above is an observability alarm, not permission to
            # abandon cleanup; fail closed until the owner actually drains.
            drained.wait()
        raise


def run_graph_mutation(coro):
    """Wait for one Room-owned graph mutation to reach a terminal result.

    CamillaController bounds and drains each transport attempt. Shared writer-
    lock admission is currently blocking and remains a Shared-owned bounded-
    admission gap. Once admitted, adding a second outer deadline here could
    cancel between graph load and rollback/state persistence, so Room waits for
    the transaction's terminal result.
    """

    return run_async(coro, timeout=None)


def read_json_body(
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


def read_wav_body(
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


def camilla_controller() -> "Any":
    """Construct a CamillaController against the configured host/port.
    Factored so tests can monkeypatch a single seam — and so the
    /start reset path doesn't drift from the /apply + /reset paths.

    Never memoized: concurrent requests each own their own controller.
    """
    from jasper.camilla import primary_controller
    return primary_controller()
