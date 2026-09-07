# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable AF_UNIX STATUS-socket test doubles: a one-response server and
an in-process fake socket.

Deliberately free of any ``jasper`` import so a leaf unit test for
:mod:`jasper.platform.status_socket` can use it without dragging a
package it does not exercise into the run.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import TracebackType


class _AcceptLoopSocket:
    """Shared AF_UNIX accept-loop plumbing for the server fixtures below.

    Binds under a fresh tempdir, runs the accept loop on a background
    thread, and tears both down on exit. Subclasses supply per-connection
    behaviour via :meth:`_handle_connection` and may narrow
    ``_serve_errors`` to the exceptions their own handler can raise.
    """

    _serve_errors: tuple[type[BaseException], ...] = (OSError,)

    def __init__(self, *, name: str) -> None:
        # Use the process temp root (short on macOS and writable in sandboxed
        # test runs) rather than pytest's deeply nested ``tmp_path``.
        self._tmpdir = tempfile.TemporaryDirectory(prefix="jts-status-")
        self.path = Path(self._tmpdir.name) / name
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._errors: list[BaseException] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Path:
        self._thread.start()
        assert self._ready.wait(timeout=2), f"socket did not bind: {self.path}"
        if self._errors:
            raise self._errors[0]
        return self.path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.2)
                client.connect(str(self.path))
        except OSError:
            pass
        self._thread.join(timeout=2)
        self._tmpdir.cleanup()
        if exc_type is None and self._errors:
            raise self._errors[0]

    def _before_accept_loop(self) -> None:
        """Hook run once, after bind/listen and before the accept loop."""

    def _handle_connection(self, connection: socket.socket) -> None:
        raise NotImplementedError

    def _serve(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(self.path))
                server.listen()
                server.settimeout(0.1)
                self._ready.set()
                self._before_accept_loop()
                while not self._stop.is_set():
                    try:
                        connection, _ = server.accept()
                    except TimeoutError:
                        continue
                    with connection:
                        self._handle_connection(connection)
        except self._serve_errors as error:
            self._errors.append(error)
            self._ready.set()


class JsonStatusSocket(_AcceptLoopSocket):
    """Serve a JSON payload to repeated ``STATUS`` requests on a short path."""

    _serve_errors = (OSError, AssertionError, TypeError)

    def __init__(
        self,
        payload: dict,
        *,
        name: str = "status.sock",
        accept_delay_seconds: float = 0,
    ) -> None:
        super().__init__(name=name)
        self.payload = payload
        self.accept_delay_seconds = accept_delay_seconds
        self.requests: list[bytes] = []

    def _before_accept_loop(self) -> None:
        if self.accept_delay_seconds:
            time.sleep(self.accept_delay_seconds)

    def _handle_connection(self, connection: socket.socket) -> None:
        request = connection.recv(1024)
        if self._stop.is_set() and not request:
            return
        self.requests.append(request)
        if request != b"STATUS\n":
            raise AssertionError(f"unexpected status request: {request!r}")
        try:
            connection.sendall(json.dumps(self.payload).encode("utf-8") + b"\n")
        except BrokenPipeError:
            pass


class DribblingStatusSocket(_AcceptLoopSocket):
    """Accept a ``STATUS`` request and dribble one byte every
    ``interval_seconds`` forever — never a complete reply, never a close.

    Models a wedged or malicious peer: a per-operation ``settimeout`` (what a
    hand-rolled reader arms) never fires because each byte arrives inside the
    window, so only a reader with a TOTAL deadline across the whole read can
    escape this without an outer test harness forcing it.
    """

    def __init__(self, *, name: str = "dribble.sock", interval_seconds: float = 0.1) -> None:
        super().__init__(name=name)
        self.interval_seconds = interval_seconds

    def _handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(1.0)
        try:
            connection.recv(1024)
        except OSError:
            return
        while not self._stop.is_set():
            try:
                connection.sendall(b"x")
            except OSError:
                break
            time.sleep(self.interval_seconds)


class FakeStatusSocket:
    """A ``socket.socket`` stand-in that replays canned recv chunks."""

    def __init__(
        self,
        payload: bytes = b"",
        error: OSError | None = None,
        *,
        chunks: list[bytes] | None = None,
        recv_error: OSError | None = None,
    ):
        self._chunks = list(chunks) if chunks is not None else [payload, b""]
        self._error = error
        self._recv_error = recv_error
        self.timeout = None
        self.connected_path = None
        self.sent: list[bytes] = []
        self.recv_sizes: list[int] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, path):
        self.connected_path = path
        if self._error is not None:
            raise self._error

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        self.recv_sizes.append(size)
        if self._recv_error is not None:
            raise self._recv_error
        return self._chunks.pop(0)

    def close(self):
        self.closed = True
