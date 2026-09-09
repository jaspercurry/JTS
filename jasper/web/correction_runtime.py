# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-correction wizard: the runtime floor under its other three modules.

Owns the single background asyncio loop and the sync-to-async bridge onto it,
the per-request CamillaController factory, the bounded JSON body reader, and
the three request exceptions the routes raise.

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

# Exact set/readback plus the emergency set/readback each use Camilla's bounded
# reconnect contract. Keep the HTTP owner alive for the complete sequence.
CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S = 45.0
RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S = CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S


class BadRequest(ValueError):
    """Client supplied an invalid request body."""


class RequestConflict(RuntimeError):
    """Client request conflicts with the current correction session state."""


class TuningSetupUnavailable(RequestConflict):
    """The optional tuning assistant has no configured model credential."""


# Lazy-init on first use so importing this module is cheap (lets `python -m
# jasper.web.correction_setup --help` work without spinning up a loop). The
# lock exists only to keep loop creation single-creator: every caller must
# reach the loop through `ensure_loop`, because two loops means two capture
# owners.
_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def ensure_loop() -> asyncio.AbstractEventLoop:
    """Start (or reuse) a single background asyncio loop. The HTTP
    handlers schedule coroutines onto it via
    `run_coroutine_threadsafe`."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or not _loop.is_running():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_loop.run_forever,
                name="jasper-correction-loop",
                daemon=True,
            )
            _loop_thread.start()
    return _loop


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


def camilla_controller() -> "Any":
    """Construct a CamillaController against the configured host/port.
    Factored so tests can monkeypatch a single seam — and so the
    /start reset path doesn't drift from the /apply + /reset paths.

    Never memoized: concurrent requests each own their own controller.
    """
    from jasper.camilla import primary_controller
    return primary_controller()
