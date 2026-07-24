# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the conftest `socketserver` shutdown-latency shim.

The shim, and why it exists, is documented once in `tests/conftest.py` —
see the "socketserver shutdown latency" block there. This file only pins
it, because otherwise it is invisible infrastructure that a future conftest
edit could drop while every test still passes, just slowly.

Three properties, none of which may regress:

1. the shim is installed (the default is not the stdlib's 0.5 s);
2. teardown does not actually wait a stdlib poll interval;
3. an explicit `poll_interval=` still overrides it.

(3) is asserted by intercepting the forwarding call rather than by timing.
A timing assertion there is a genuine race, not merely a slow test:
`ThreadingMixIn.process_request` spawns the worker thread and returns
immediately, so the client can finish its request and call `shutdown()`
before the accept loop re-enters `selector.select()`. The loop then exits
on `while not self.__shutdown_request` having never consulted
`poll_interval`, and the measured elapsed time collapses to ~0.1 ms.
Measured at 2/60 whole-file runs on an idle 10-core box — and it fires
*more* when unloaded, so it is a GIL/scheduler race, not a load artifact.
Across the three-version matrix that is roughly a 1-in-10 chance of a
spurious red `ci`, which is unacceptable on the sole required check.
"""

from __future__ import annotations

import socketserver
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from . import conftest
from .conftest import SERVER_POLL_INTERVAL_SEC


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Silence the per-request stderr line."""


def _cycle(poll_interval: float | None = None) -> float:
    """Start a server, serve one request, and time the teardown."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        args=() if poll_interval is None else (poll_interval,),
    )
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.read() == b"ok"
        started = time.perf_counter()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    return time.perf_counter() - started


def test_default_poll_interval_is_the_fast_shim() -> None:
    """The conftest shim must be installed, not the stdlib 0.5 s default."""

    assert SERVER_POLL_INTERVAL_SEC <= 0.05
    assert socketserver.BaseServer.serve_forever.__defaults__ == (
        SERVER_POLL_INTERVAL_SEC,
    )


def test_teardown_does_not_wait_a_stdlib_poll_interval() -> None:
    """Behavioural guard: teardown must not cost ~0.5 s per server.

    Safe direction of the timing comparison, and the only one asserted by
    timing. `min()` of three cycles cannot be inflated by scheduler noise,
    and the threshold sits ~18x clear of the shimmed regime (max observed
    ~12 ms) while a stdlib-default teardown parks at a ~500 ms median. Zero
    failures in 60 whole-file runs, loaded and unloaded.
    """

    assert min(_cycle() for _ in range(3)) < 0.2


def test_an_explicit_poll_interval_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim rebinds only the DEFAULT; an explicit argument overrides it.

    Tests that genuinely want the lazy cadence keep passing `poll_interval=`
    — `tests/test_control_server.py`'s heartbeat test is the live example —
    so the shim must forward the caller's value rather than clamp it.

    Asserted by interception, NOT by timing: the shim resolves
    `_stdlib_serve_forever` as a module global at call time, so replacing
    that global captures exactly what gets forwarded. Timing this instead
    is the race described in the module docstring.
    """

    forwarded: list[float] = []

    def _spy(_server: object, poll_interval: float) -> None:
        forwarded.append(poll_interval)

    monkeypatch.setattr(conftest, "_stdlib_serve_forever", _spy)

    socketserver.BaseServer.serve_forever(object(), 0.5)
    socketserver.BaseServer.serve_forever(object())

    assert forwarded == [0.5, SERVER_POLL_INTERVAL_SEC]
