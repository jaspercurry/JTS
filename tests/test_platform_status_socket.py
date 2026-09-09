# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the shared STATUS-socket reader.

Exercises `jasper.platform.status_socket` against a tiny in-process
Unix-socket server that speaks the same `STATUS\\n` → JSON protocol the fan-in
and outputd control sockets do, so both consumers (the artifact writer and the
harness) share one verified mechanic.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from jasper import audio_validation
from jasper.cli import system_soak
from jasper.control import audio_health
from jasper.control.airplay_health import AirPlayHealthSampler
from jasper.fanin.status import read_fanin_status
from jasper.platform import status_socket
from tests._socket_paths import short_socket_path_fixture as _short_sock_path_fixture
from tests.status_socket_fixtures import DribblingStatusSocket, FakeStatusSocket

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
    with caplog.at_level("DEBUG"):
        result = status_socket.read_status_socket_or_none(
            str(tmp_path / "nope.sock"), timeout=1.0, event="test.socket_unavailable"
        )

    assert result is None
    assert any(rec.jasper_event == "test.socket_unavailable" for rec in caplog.records)


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


@pytest.mark.parametrize("connect_sec, send_sec, read_sec", [(0.8, 0.4, 0), (0.3, 0.3, 0.5)])
def test_reader_shares_one_deadline_across_connect_send_and_read(
    monkeypatch, connect_sec, send_sec, read_sec,
):
    class TimedSocket(FakeStatusSocket):
        now = 0.0

        def spend(self, seconds):
            self.now += min(seconds, self.timeout)
            if seconds > self.timeout:
                raise TimeoutError

        def connect(self, path):
            self.spend(connect_sec)
            super().connect(path)

        def sendall(self, data):
            self.spend(send_sec)
            super().sendall(data)

        def recv(self, size):
            self.spend(read_sec)
            return super().recv(size)

    fake = TimedSocket(payload=b"{}")
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)
    monkeypatch.setattr(status_socket.time, "monotonic", lambda: fake.now)

    with pytest.raises(TimeoutError):
        status_socket.read_status_socket("/run/test.sock", timeout=1.0)

    assert fake.now == pytest.approx(1.0)
    assert fake.closed is True


@pytest.mark.parametrize("failure_stage", ["connect", "sendall", "recv"])
def test_reader_closes_the_socket_on_failure(monkeypatch, failure_stage):
    fake = FakeStatusSocket()
    monkeypatch.setattr(fake, failure_stage, Mock(side_effect=OSError))
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    with pytest.raises(OSError):
        status_socket.read_status_socket("/run/test.sock", timeout=2.0)

    assert fake.closed is True


def test_reader_decodes_lossily_rather_than_raising_on_a_stray_byte(monkeypatch):
    fake = FakeStatusSocket(payload=b'{"note":"\xff","ok":true}')
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    assert status_socket.read_status_socket("/run/test.sock")["ok"] is True


_DRIBBLE_TIMEOUT_SEC = 0.3
_DRIBBLE_WALL_SLACK_SEC = 2.0


def _call_audio_validation(sock_path: Path) -> dict | None:
    return audio_validation.query_outputd_status(sock_path, timeout=_DRIBBLE_TIMEOUT_SEC)


def _call_airplay_health(sock_path: Path) -> dict | None:
    return AirPlayHealthSampler._read_fanin_status(str(sock_path), timeout_sec=_DRIBBLE_TIMEOUT_SEC)


def _run_on_daemon_thread(call, sock_path: Path, *, join_timeout: float):
    """Run `call(sock_path)` on a daemon thread; return (finished, result).

    A daemon thread rather than a bounded executor: if `call` never returns
    (the regression this test exists to catch), joining with a timeout lets
    THIS test fail promptly instead of hanging the whole suite, and the
    daemon flag means the leaked thread cannot block interpreter exit.
    """
    box: list[object] = []
    thread = threading.Thread(target=lambda: box.append(call(sock_path)), daemon=True)
    thread.start()
    thread.join(timeout=join_timeout)
    if thread.is_alive():
        return False, None
    return True, box[0]


@pytest.mark.parametrize(
    "label, call",
    [
        ("audio_validation.query_outputd_status", _call_audio_validation),
        ("airplay_health.AirPlayHealthSampler._read_fanin_status", _call_airplay_health),
        ("fanin.read_fanin_status", lambda path: read_fanin_status(
            str(path), timeout_sec=_DRIBBLE_TIMEOUT_SEC,
        )),
        ("audio_health._read_local_status", lambda path: audio_health._read_local_status(
            str(path), timeout_sec=_DRIBBLE_TIMEOUT_SEC,
        )),
        ("system_soak._status_socket", lambda path: system_soak._status_socket(
            str(path), timeout=_DRIBBLE_TIMEOUT_SEC,
        )),
    ],
)
def test_converged_caller_bounds_a_dribbling_status_server(label, call):
    with DribblingStatusSocket(interval_seconds=0.1) as sock_path:
        started = time.monotonic()
        finished, result = _run_on_daemon_thread(
            call, sock_path, join_timeout=_DRIBBLE_TIMEOUT_SEC + _DRIBBLE_WALL_SLACK_SEC
        )
        elapsed = time.monotonic() - started

    assert finished, (
        f"{label} did not return within the total deadline "
        f"(still blocked after {elapsed:.2f}s)"
    )
    assert result is None, f"{label} should fall through to None, not a partial reply"


@pytest.mark.parametrize("consumer", [
    read_fanin_status, audio_health._read_local_status, system_soak._status_socket,
])
@pytest.mark.parametrize("body, expected", [(b"{}", {}), (b"{} ", None), (b"[]", None), (b"x", None)])
def test_converged_consumers_keep_limits_and_failure_policy(monkeypatch, consumer, body, expected):
    fake = FakeStatusSocket(chunks=[body[:1], body[1:], b""])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    assert consumer("/run/test.sock", max_bytes=2) == expected
    assert fake.closed is True


@pytest.mark.parametrize("consumer, cap", [
    (read_fanin_status, 64 * 1024),
    (audio_health._read_local_status, 256 * 1024),
    (system_soak._status_socket, 64 * 1024),
    (status_socket.read_status_socket, 1024 * 1024),
])
@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_status_consumers_keep_default_caps(monkeypatch, consumer, cap, extra_bytes):
    body = b"{}" + b" " * (cap - 2 + extra_bytes)
    fake = FakeStatusSocket(chunks=[body[i:i + 8192] for i in range(0, len(body), 8192)] + [b""])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: fake)

    if consumer is status_socket.read_status_socket and extra_bytes:
        with pytest.raises(OSError):
            consumer("/run/test.sock")
    else:
        assert consumer("/run/test.sock") == (None if extra_bytes else {})
    assert fake.closed is True
