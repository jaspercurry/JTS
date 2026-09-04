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
import socket
import threading
from unittest.mock import Mock

import pytest

from jasper.route_latency import status_socket
from tests._socket_paths import short_socket_path_fixture as _short_sock_path_fixture
from tests.status_socket_fixtures import FakeStatusSocket

_IMPORTED_FIXTURES = (_short_sock_path_fixture,)


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

    with pytest.raises(ValueError):
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
    # place.
    assert status_socket.FANIN_STATUS_SOCKET == "/run/jasper-fanin/control.sock"
    assert status_socket.MUX_CONTROL_SOCKET_PATH == "/run/jasper-mux/control.sock"
    assert status_socket.OUTPUTD_STATUS_SOCKET == "/run/jasper-outputd/control.sock"


# ---- socket lifecycle: fragmentation, the byte cap, and the total deadline ---
#
# Driven through a fake socket rather than the in-process server above: a
# reply split across recv boundaries, one sized to the exact byte cap, and a
# stalled deadline are all states a cooperating server cannot stage reliably.


def test_reader_reassembles_a_fragmented_reply_and_closes(monkeypatch):
    fake = FakeStatusSocket(chunks=[b'{"ok":', b"true}", b""])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    result = status_socket.read_status_socket("/run/test.sock", timeout=1.25)

    assert result == {"ok": True}
    assert 0 < fake.timeout <= 1.25
    assert fake.connected_path == "/run/test.sock"
    assert fake.sent == [b"STATUS\n"]
    assert fake.recv_sizes == [65536, 65536, 65536]
    assert fake.closed is True


def test_reader_accepts_a_reply_of_exactly_the_byte_cap(monkeypatch):
    cap = status_socket._RESPONSE_MAX_BYTES
    pad = b"x" * (cap - len(b'{"pad":""}'))
    body = b'{"pad":"' + pad + b'"}'
    assert len(body) == cap
    chunks = [body[i:i + 65536] for i in range(0, cap, 65536)]
    fake = FakeStatusSocket(chunks=chunks + [b""])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    result = status_socket.read_status_socket("/run/test.sock", timeout=2.0)

    assert len(result["pad"]) == len(pad)
    assert fake.recv_sizes == [65536] * (len(chunks) + 1)
    assert fake.closed is True


def test_reader_rejects_a_reply_over_the_byte_cap(monkeypatch):
    fake = FakeStatusSocket(chunks=[b"x" * 65536] * 16 + [b"y"])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    with pytest.raises(OSError):
        status_socket.read_status_socket("/run/test.sock", timeout=2.0)

    assert fake.recv_sizes == [65536] * 17
    assert fake.closed is True


def test_reader_enforces_a_total_deadline_not_a_per_recv_one(monkeypatch):
    fake = FakeStatusSocket(chunks=[b"x", b"y", b""])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)
    monkeypatch.setattr(
        status_socket.time, "monotonic", Mock(side_effect=[0.0, 0.0, 0.1, 0.2, 1.1])
    )

    with pytest.raises(TimeoutError):
        status_socket.read_status_socket("/run/test.sock", timeout=1.0)

    assert fake.recv_sizes == [65536]
    assert fake.closed is True


@pytest.mark.parametrize("failure_stage", ["connect", "recv"])
def test_reader_closes_the_socket_on_failure(monkeypatch, failure_stage):
    error = OSError(f"{failure_stage} failed")
    fake = FakeStatusSocket(
        error=error if failure_stage == "connect" else None,
        recv_error=error if failure_stage == "recv" else None,
    )
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    with pytest.raises(OSError):
        status_socket.read_status_socket("/run/test.sock", timeout=2.0)

    assert fake.closed is True


def test_reader_decodes_lossily_rather_than_raising_on_a_stray_byte(monkeypatch):
    fake = FakeStatusSocket(payload=b'{"note":"\xff","ok":true}')
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    assert status_socket.read_status_socket("/run/test.sock")["ok"] is True
