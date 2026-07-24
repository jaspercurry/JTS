# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the conftest `socketserver` shutdown-latency shim.

23 test files spin a throwaway ThreadingHTTPServer per test. `shutdown()`
blocks until `serve_forever()` re-checks the shutdown flag, which happens
once per `poll_interval` — 0.5 s by default. That made teardown, not the
assertions, the dominant cost of those files (tests/test_control_server.py
alone: ~90 s across 220 tests, ~10 s after the shim).

Without a guard, the shim is invisible infrastructure that a future
conftest edit could drop while every test still passes — just slowly. So
this asserts the behaviour, not only the constant.
"""

from __future__ import annotations

import socketserver
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def _cycle(**thread_kwargs: object) -> float:
    """Start a server, serve one request, and time the teardown."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, **thread_kwargs
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

    Threshold sits well clear of both measured regimes (~9 ms shimmed,
    ~418 ms on the stdlib default), so it pins the win without being a
    timing-sensitive flake on a loaded CI runner.
    """

    assert min(_cycle() for _ in range(3)) < 0.2


def test_an_explicit_poll_interval_still_wins() -> None:
    """The shim rebinds only the DEFAULT; an explicit argument overrides it.

    Tests that genuinely want the lazy cadence keep passing
    `poll_interval=`, so the shim must not clamp it.
    """

    elapsed = _cycle(args=(0.5,))

    assert elapsed > 0.2
