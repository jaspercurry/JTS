"""Unit tests for jasper.web._systemd socket-activation helper.

Stdlib-only — runs in any Python 3.10+ environment, no Pi needed.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from jasper.web import _systemd


def test_adopt_returns_empty_when_no_env() -> None:
    """No LISTEN_PID/LISTEN_FDS env vars → empty list, not an error.
    This is the legacy-direct-invocation path."""
    env_keys = ("LISTEN_PID", "LISTEN_FDS")
    saved = {k: os.environ.pop(k, None) for k in env_keys}
    try:
        assert _systemd.adopt_systemd_sockets() == []
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_adopt_ignores_wrong_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """LISTEN_PID != our pid → empty list. This guards against claiming
    fds inherited from a parent that already accepted them."""
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert _systemd.adopt_systemd_sockets() == []


def test_adopt_returns_sockets_for_matching_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LISTEN_PID matches and LISTEN_FDS is set, fds at
    SD_LISTEN_FDS_START get wrapped as socket objects."""
    # Build a real listening socket so we have a real fd to hand off.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    real_fd = listener.fileno()

    # Dup it to fd 3 (SD_LISTEN_FDS_START) to mimic what systemd does.
    target_fd = _systemd.SD_LISTEN_FDS_START
    saved = None
    try:
        if os.path.exists(f"/proc/self/fd/{target_fd}"):
            saved = os.dup(target_fd)
            os.close(target_fd)
        os.dup2(real_fd, target_fd)
        monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
        monkeypatch.setenv("LISTEN_FDS", "1")
        adopted = _systemd.adopt_systemd_sockets()
        assert len(adopted) == 1
        assert adopted[0].getsockname()[0] == "127.0.0.1"
        # Port matches the listener.
        assert adopted[0].getsockname()[1] == listener.getsockname()[1]
        adopted[0].close()
    finally:
        try:
            os.close(target_fd)
        except OSError:
            pass
        if saved is not None:
            os.dup2(saved, target_fd)
            os.close(saved)
        listener.close()


def test_make_http_server_from_int_port() -> None:
    """Legacy bind path: int port creates a server on 127.0.0.1."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw) -> None:
            pass

    srv = _systemd.make_http_server(0, _H)  # port 0 = ephemeral
    try:
        host, port = srv.server_address
        assert host == "127.0.0.1"
        assert port > 0
    finally:
        srv.server_close()


def test_make_http_server_from_tuple() -> None:
    """Explicit (host, port) tuple respected — for non-loopback binds."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw) -> None:
            pass

    srv = _systemd.make_http_server(("127.0.0.1", 0), _H)
    try:
        host, _port = srv.server_address
        assert host == "127.0.0.1"
    finally:
        srv.server_close()


def test_make_http_server_from_socket() -> None:
    """Pre-bound socket adopted without re-binding."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw) -> None:
            pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]

    srv = _systemd.make_http_server(sock, _H)
    try:
        assert srv.server_address[1] == port
        # The socket on the server is our sock (or a dup with the
        # same address).
        assert srv.socket.getsockname()[1] == port
    finally:
        srv.server_close()


def test_make_http_server_rejects_unknown_target() -> None:
    """Bad target type raises TypeError with a useful message."""

    class _H(BaseHTTPRequestHandler):
        pass

    with pytest.raises(TypeError, match="target must be"):
        _systemd.make_http_server("not-a-port", _H)


def test_idle_tracker_bump_resets_timer() -> None:
    """bump() resets last-request time; with short threshold + bumps,
    process never exits."""
    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=10.0, watchdog_period_sec=0.05,
    )
    tracker.start()
    try:
        # Bump faster than threshold; process must not exit.
        for _ in range(5):
            tracker.bump()
            time.sleep(0.02)
        # If we got here, _exit didn't fire. Sanity check the timestamp.
        with tracker._lock:
            assert time.monotonic() - tracker._last_request < 1.0
    finally:
        tracker.stop()


def test_install_request_idle_bump_patches_log_request() -> None:
    """install_request_idle_bump wraps log_request to bump tracker."""
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=999.0)

    bumps_before = 0
    bumps_after = 1

    class _H(BaseHTTPRequestHandler):
        def __init__(self) -> None:
            # Skip real init — we just want to test the method wrap.
            pass

        def log_request(self, code="-", size="-") -> None:  # type: ignore[override]
            nonlocal bumps_before
            bumps_before += 1

    _systemd.install_request_idle_bump(_H, tracker)

    # Fake an instance and call the patched method.
    h = _H()
    t0 = tracker._last_request
    time.sleep(0.01)
    h.log_request(200, 12)
    assert tracker._last_request > t0
    assert bumps_before == 1  # original was called


def test_notify_with_no_socket_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without NOTIFY_SOCKET env var, notify_* functions silently no-op.
    This is the test-run path: no systemd, no error."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    # All three should return without raising.
    _systemd.notify_ready()
    _systemd.notify_watchdog()
    _systemd.notify_stopping()


def test_notify_writes_datagram_to_socket() -> None:
    """When NOTIFY_SOCKET points to a unix datagram socket, messages
    actually land there. Receiver socket must be created first.

    AF_UNIX path is bounded to ~104 chars on macOS / 108 on Linux, so
    pytest's tmp_path (which lives deep under /private/var/folders/...)
    can blow that out. Use the abstract namespace where available
    (Linux), else a short /tmp filename. Skip if neither works."""
    import tempfile
    import uuid

    short_name = f"jts-test-{uuid.uuid4().hex[:8]}.sock"
    sock_path = os.path.join(tempfile.gettempdir(), short_name)
    if len(sock_path) > 100:
        pytest.skip(f"AF_UNIX path too long for this platform: {sock_path}")

    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        receiver.bind(sock_path)
    except OSError as e:
        pytest.skip(f"AF_UNIX bind failed: {e}")
    receiver.settimeout(1.0)
    try:
        os.environ["NOTIFY_SOCKET"] = sock_path
        _systemd.notify_ready()
        data, _ = receiver.recvfrom(64)
        assert data == b"READY=1"

        _systemd.notify_watchdog()
        data, _ = receiver.recvfrom(64)
        assert data == b"WATCHDOG=1"
    finally:
        os.environ.pop("NOTIFY_SOCKET", None)
        receiver.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass
