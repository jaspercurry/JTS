# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the shared STATUS-socket reader.

Exercises `jasper.route_latency.status_socket` against a tiny in-process
Unix-socket server that speaks the same `STATUS\\n` → JSON protocol the fan-in
and outputd control sockets do, so both consumers (the artifact writer and the
harness) share one verified mechanic.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading

import pytest

from jasper.route_latency import status_socket


@pytest.fixture()
def short_sock_path():
    """A Unix-socket path short enough for AF_UNIX's ~104-char limit.

    pytest's ``tmp_path`` is too deep on macOS, so bind under a short mkdtemp
    dir and clean it up afterward.
    """

    d = tempfile.mkdtemp(prefix="jts-ss-")
    path = os.path.join(d, "control.sock")
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            os.rmdir(d)
        except OSError:
            pass


def _serve_once(sock_path: str, reply: bytes, *, expect_request: bytes = b"STATUS\n") -> threading.Thread:
    """Accept one connection on `sock_path`, read the request, send `reply`."""

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    def _run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                conn.recv(len(expect_request) + 8)
                conn.sendall(reply)
        finally:
            server.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_read_status_socket_returns_parsed_object(short_sock_path):
    payload = {"output": {"transport": "pipe"}, "counters": {"xruns": 0}}
    t = _serve_once(short_sock_path, json.dumps(payload).encode())

    result = status_socket.read_status_socket(short_sock_path, timeout=2.0)
    t.join(timeout=2.0)

    assert result == payload


def test_read_status_socket_raises_valueerror_on_non_object(short_sock_path):
    t = _serve_once(short_sock_path, b"[1, 2, 3]")

    with pytest.raises(ValueError, match="not an object"):
        status_socket.read_status_socket(short_sock_path, timeout=2.0)
    t.join(timeout=2.0)


def test_read_status_socket_raises_on_bad_json(short_sock_path):
    t = _serve_once(short_sock_path, b"not json")

    with pytest.raises(json.JSONDecodeError):
        status_socket.read_status_socket(short_sock_path, timeout=2.0)
    t.join(timeout=2.0)


def test_read_status_socket_raises_oserror_when_socket_absent(tmp_path):
    with pytest.raises(OSError):
        status_socket.read_status_socket(str(tmp_path / "nope.sock"), timeout=1.0)


def test_read_status_socket_or_none_returns_object(short_sock_path):
    payload = {"ok": True}
    t = _serve_once(short_sock_path, json.dumps(payload).encode())

    result = status_socket.read_status_socket_or_none(short_sock_path, timeout=2.0)
    t.join(timeout=2.0)

    assert result == payload


def test_read_status_socket_or_none_fails_soft_when_absent(tmp_path, caplog):
    # An unreachable socket is an expected snapshot state (daemon down); the
    # fail-soft wrapper returns None and logs at DEBUG rather than raising.
    with caplog.at_level("DEBUG"):
        result = status_socket.read_status_socket_or_none(
            str(tmp_path / "nope.sock"), timeout=1.0, event="test.socket_unavailable"
        )

    assert result is None
    assert any("test.socket_unavailable" in rec.getMessage() for rec in caplog.records)


def test_canonical_socket_paths_match_daemon_conventions():
    # Pin the well-known control-socket paths so a daemon move updates one
    # place. These mirror the two live route-health owners.
    assert status_socket.FANIN_STATUS_SOCKET == "/run/jasper-fanin/control.sock"
    assert status_socket.OUTPUTD_STATUS_SOCKET == "/run/jasper-outputd/control.sock"
