# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.web._systemd socket-activation helper.

Stdlib-only — runs in any Python 3.10+ environment, no Pi needed.
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import select
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
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    try:
        # Point SD_LISTEN_FDS_START at the listener's actual fd instead of
        # dup2'ing onto the hardcoded fd 3. fd 3 is held + guarded by some
        # parent processes (e.g. Claude Desktop's IPC pipe on macOS), and
        # any dup2 onto a guarded fd trips EXC_GUARD and kills the test.
        # socket.fromfd() inside adopt_systemd_sockets() dups the fd, so
        # the listener stays valid throughout.
        monkeypatch.setattr(
            _systemd, "SD_LISTEN_FDS_START", listener.fileno(),
        )
        monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
        monkeypatch.setenv("LISTEN_FDS", "1")
        adopted = _systemd.adopt_systemd_sockets()
        assert len(adopted) == 1
        assert adopted[0].getsockname()[0] == "127.0.0.1"
        assert adopted[0].getsockname()[1] == listener.getsockname()[1]
        adopted[0].close()
    finally:
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


def test_long_inflight_request_cannot_trigger_idle_exit() -> None:
    """A legal slow POST remains active beyond the ordinary idle threshold."""
    entered = threading.Event()
    release = threading.Event()

    class _H:
        def parse_request(self) -> bool:
            return True

        def handle_one_request(self) -> None:
            assert self.parse_request()
            entered.set()
            assert release.wait(timeout=2)

        def log_request(self, code="-", size="-") -> None:
            pass

    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)
    _systemd.install_request_idle_bump(_H, tracker)
    handler = _H()
    worker = threading.Thread(target=handler.handle_one_request)
    worker.start()
    assert entered.wait(timeout=1)
    with tracker._lock:
        tracker._last_request = time.monotonic() - 10
    expired, _idle, active = tracker._idle_status()
    assert active == 1
    assert expired is False

    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    with tracker._lock:
        tracker._last_request = time.monotonic() - 10
    expired, _idle, active = tracker._idle_status()
    assert active == 0
    assert expired is True


def test_hold_defers_the_idle_exit_while_background_work_runs() -> None:
    """A hold keeps the process alive across work that serves no request.

    The 2026-07-29 JTS3 incident (issue #1854): correction-web's browser capture
    measurement session runs on background workers and its mid-session traffic
    is all OUTBOUND to the capture, so the tracker saw zero inbound requests for
    600 s and `os._exit(0)`'d 5 s after the verify capture arrived. The hold is
    what makes "idle" mean abandoned again.

    Also pins the one-busy-counter design: a hold takes the SAME
    ``_active_requests`` an in-flight request takes, so ``_idle_status`` has
    exactly one input to consult and cannot disagree with itself.
    """
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)

    with tracker.hold("measurement-session"):
        assert tracker._active_requests == 1
        with tracker._lock:
            tracker._last_request = time.monotonic() - 600
        expired, idle, active = tracker._idle_status()
        assert idle >= 600
        assert active == 1
        assert expired is False


def test_releasing_a_hold_re_arms_the_ordinary_idle_exit() -> None:
    """Release restores normal expiry AND begins a fresh idle interval.

    The fresh interval is the same courtesy a finished request gets: a session
    that just ended is not retroactively idle for however long it ran.
    """
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)
    with tracker.hold("measurement-session"):
        with tracker._lock:
            tracker._last_request = time.monotonic() - 600
    assert tracker._active_requests == 0

    # Fresh interval: not immediately expired despite the 600 s backdate above.
    expired, idle, active = tracker._idle_status()
    assert active == 0
    assert idle < 1.0
    assert expired is False

    # ...and the ordinary threshold applies again from here.
    with tracker._lock:
        tracker._last_request = time.monotonic() - 600
    expired, _idle, active = tracker._idle_status()
    assert active == 0
    assert expired is True


def test_hold_is_released_when_the_body_raises() -> None:
    """A failing session must not leave the wizard immortal.

    Every terminal path of the correction runner is an exception path (user
    stop, capture timeout, begin-refused, the catch-all cleanup arm), so a hold
    that only released on success would leak on the common cases.
    """
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)

    with pytest.raises(RuntimeError, match="capture died"):
        with tracker.hold("measurement-session"):
            raise RuntimeError("capture died")

    assert tracker._active_requests == 0
    assert tracker._holds == {}
    with tracker._lock:
        tracker._last_request = time.monotonic() - 600
    expired, _idle, _active = tracker._idle_status()
    assert expired is True


def test_concurrent_holds_expire_only_after_the_last_release() -> None:
    """Holds nest: two overlapping background lifetimes.

    Labelled for the v2 session and its auto-apply worker when written; that
    worker is gone with auto-apply (two-stage PR-T3), so read the labels as a
    representative PAIR of overlapping holds rather than as a live pairing —
    what the tracker must get right (nesting, not double-releasing) is
    unchanged and is what this exercises."""
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)

    with tracker.hold("session"):
        with tracker.hold("auto-apply"):
            with tracker._lock:
                tracker._last_request = time.monotonic() - 600
            expired, _idle, active = tracker._idle_status()
            assert active == 2
            assert expired is False
        # Inner released; the outer session still holds the process.
        with tracker._lock:
            tracker._last_request = time.monotonic() - 600
        expired, _idle, active = tracker._idle_status()
        assert active == 1
        assert expired is False

    with tracker._lock:
        tracker._last_request = time.monotonic() - 600
    expired, _idle, active = tracker._idle_status()
    assert active == 0
    assert expired is True


def test_a_hold_may_be_taken_on_one_thread_and_released_on_another() -> None:
    """The gap-free pattern for a worker thread: acquire BEFORE ``start()``,
    release in the worker's own ``finally``.

    ``build_v2_run_and_consume``'s auto-apply worker depended on this before
    PR-T3 deleted it, and #1860's level-ramp holds depend on it now — a thread
    scheduled but not yet started, while the session runner drops its own hold,
    is exactly the window that would let the process exit mid-apply.
    """
    import contextlib

    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)
    stack = contextlib.ExitStack()
    stack.enter_context(tracker.hold("auto-apply"))
    assert tracker._active_requests == 1

    released = threading.Event()

    def _worker() -> None:
        try:
            with tracker._lock:
                tracker._last_request = time.monotonic() - 600
            expired, _idle, active = tracker._idle_status()
            assert active == 1
            assert expired is False
        finally:
            stack.close()
            released.set()

    worker = threading.Thread(target=_worker, name="auto-apply-test")
    worker.start()
    assert released.wait(timeout=2)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert tracker._active_requests == 0
    assert tracker._holds == {}


def test_the_leak_bound_keeps_its_margin_over_the_longest_legitimate_hold() -> None:
    """Contract: HOLD_LEAK_WARN_AFTER_SEC >= 2x the longest session a wizard sizes for.

    ``_systemd`` is shared by seven wizards and deliberately does NOT import
    the correction session model at runtime, so its bound is a literal derived
    by hand from ``session_volume_plan.MAX_WALL_CLOCK_CEILING_S`` — the hard cap
    on ``crossover_v2_flow.session_wall_clock_ceiling_s``. A test may import
    freely, so the relationship is pinned here instead of drifting silently.

    If this fails, the ceiling grew and the margin eroded. RAISE THE BOUND
    deliberately — do not relax the assertion. A leak alarm that fires on a
    legitimate commission is worse than no alarm at all, which is the whole
    reason the bound is 2x rather than snug (see the constant's own comment).
    """
    from jasper.active_speaker import session_volume_plan

    assert _systemd.HOLD_LEAK_WARN_AFTER_SEC >= (
        2 * session_volume_plan.MAX_WALL_CLOCK_CEILING_S
    ), (
        "the leak bound no longer clears 2x the longest session the volume "
        f"plan permits ({session_volume_plan.MAX_WALL_CLOCK_CEILING_S}s)"
    )


def test_no_hold_holds_nothing_and_matches_the_real_seam() -> None:
    """The explicit opt-out: same signature as ``hold``, no effect on the exit.

    Call sites choose between the two, so they must be interchangeable — and
    ``no_hold`` must genuinely not defer anything, or an opt-out would quietly
    become the immortality bug it exists to avoid.
    """
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)
    with tracker._lock:
        tracker._last_request = time.monotonic() - 600

    with _systemd.no_hold("capture:room_sweep"):
        expired, _idle, active = tracker._idle_status()
        assert active == 0
        assert expired is True, "no_hold defers nothing"
    assert tracker._holds == {}
    assert tracker._busy_since is None

    hold_params = set(
        inspect.signature(_systemd.IdleShutdownTracker.hold).parameters
    ) - {"self"}
    assert set(inspect.signature(_systemd.no_hold).parameters) == hold_params


def _deferred_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "idle-exit deferred" in r.getMessage()]


def test_two_holds_under_one_label_survive_until_the_last_release(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sessions can overlap under one label, so the label is refcounted.

    Production-reachable: ``_run_capture``'s runner frees the capture slot
    (`_set_capture_slot({"status": "complete"})`) BEFORE its ``finally``
    releases the hold, so the next session's POST can claim the slot and take a
    second hold under the same ``capture:<kind>`` label while the first is still
    unwinding. If release popped the label outright, the deferred-exit line
    would report `holds: none` while a hold was demonstrably outstanding.
    """
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)
    log = logging.getLogger("jasper.web._systemd")

    with tracker.hold("capture:crossover_v2:session"):
        with tracker.hold("capture:crossover_v2:session"):
            assert tracker._holds == {"capture:crossover_v2:session": 2}
        # One released; the label survives with the remaining holder.
        assert tracker._holds == {"capture:crossover_v2:session": 1}
        assert tracker._active_requests == 1
        with tracker._lock:
            tracker._last_request = time.monotonic() - 600
        expired, _idle, active = tracker._idle_status()
        assert active == 1
        assert expired is False
        # ...and the deferred line still names it rather than "none".
        with caplog.at_level(logging.INFO, logger="jasper.web._systemd"):
            tracker._log_deferred_exit(log, 600.0, 1)
        assert "capture:crossover_v2:session" in _deferred_records(caplog)[-1].getMessage()

    assert tracker._holds == {}


def test_a_long_busy_stretch_escalates_the_deferred_line_to_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A hold outstanding past the leak bound is a WARNING, not routine INFO.

    Pre-#1854 the 600 s `os._exit(0)` accidentally reaped a WEDGED background
    worker. It no longer does — a wedged CamillaDSP apply now pins the hold, so
    the process never idle-exits, `_idle_exit_restore_capture_entry` never runs,
    and the speaker can sit on the all-muted staged anchor until an operator
    restarts the unit. Nothing here reaps it (see the constant's comment); what
    changes is that the state stops looking like routine progress in the
    journal. The bound is sized so no legitimate session reaches it — a line
    that cried wolf on every Full-tier commission would be worse than none.
    """
    log = logging.getLogger("jasper.web._systemd")
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)
    monkeypatch.setattr(_systemd, "HOLD_LEAK_WARN_AFTER_SEC", 30.0)

    with caplog.at_level(logging.INFO, logger="jasper.web._systemd"):
        with tracker.hold("crossover-v2-auto-apply"):
            # Young stretch: routine, INFO.
            tracker._log_deferred_exit(log, 601.0, 1)
            records = _deferred_records(caplog)
            assert len(records) == 1
            assert records[0].levelno == logging.INFO
            assert "crossover-v2-auto-apply" in records[0].getMessage()

            # Age the stretch past the bound; same call, escalated level.
            caplog.clear()
            with tracker._lock:
                tracker._busy_since = time.monotonic() - 31
            tracker._deferred_logged_at = None
            tracker._log_deferred_exit(log, 601.0, 1)
            records = _deferred_records(caplog)
            assert len(records) == 1
            assert records[0].levelno == logging.WARNING
            message = records[0].getMessage()
            assert "LEAKED" in message
            assert "crossover-v2-auto-apply" in message, "names the leaking hold"
            # Regex, not "31s": the epoch is backdated against the real clock,
            # so a stall between that write and this call rounds the age up.
            assert re.search(r"busy for \d+s", message), "reports the stretch age"

            # Still rate-limited at WARNING — visible, never a flood.
            caplog.clear()
            tracker._log_deferred_exit(log, 616.0, 1)
            assert _deferred_records(caplog) == []

        # The stretch ends with the last hold, so a later one starts fresh.
        assert tracker._busy_since is None
        caplog.clear()
        with tracker.hold("capture:room_sweep"):
            tracker._deferred_logged_at = None
            tracker._log_deferred_exit(log, 601.0, 1)
            assert _deferred_records(caplog)[0].levelno == logging.INFO, (
                "a new busy stretch is not born already leaked"
            )


def test_busy_stretch_spans_requests_and_holds_without_a_gap() -> None:
    """One stretch, one epoch: overlapping tokens never restart the clock.

    The escalation asks "has this process been unable to go idle for too long",
    which is a property of the busy counter, not of any single label — so a
    request handing off to a hold (exactly what starting a capture session does)
    must not look like a fresh stretch.
    """
    tracker = _systemd.IdleShutdownTracker(idle_threshold_sec=1.0)

    tracker.request_started()
    started = tracker._busy_since
    assert started is not None

    with tracker.hold("capture:crossover_v2:session"):
        tracker.request_finished()  # the POST returns; the hold carries on
        assert tracker._active_requests == 1
        assert tracker._busy_since == started, "the stretch did not restart"

    assert tracker._active_requests == 0
    assert tracker._busy_since is None


def test_deferred_idle_exit_is_logged_and_rate_limited(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A held-open idle exit is visible, but costs at most one line per window.

    A leaked hold silently forfeits the ~10-30 MB Pss the idle exit returns on
    a 1 GB Pi, so it must be reported; the poll runs every ~15 s, so it must be
    rate-limited or a legitimate 20-minute session would spam the journal.
    """
    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=0.0, watchdog_period_sec=0.001,
    )
    polls: list[int] = []

    def _tick() -> None:
        polls.append(1)
        if len(polls) >= 3:
            tracker.stop()

    monkeypatch.setattr(_systemd, "notify_watchdog", _tick)
    monkeypatch.setattr(
        _systemd.os,
        "_exit",
        lambda _code: pytest.fail("must not exit while a hold is outstanding"),
    )

    with tracker.hold("measurement-session"):
        with caplog.at_level(logging.INFO, logger="jasper.web._systemd"):
            tracker._run()

    assert len(polls) == 3
    deferred = [
        r.getMessage() for r in caplog.records
        if "idle-exit deferred" in r.getMessage()
    ]
    assert len(deferred) == 1, "three polls, one line — the rate limit holds"
    assert "measurement-session" in deferred[0], "the leaking hold is named"

    # Prove the rate limit is what suppressed the other two: with a zero-length
    # window every poll reports.
    monkeypatch.setattr(_systemd, "DEFERRED_EXIT_LOG_PERIOD_SEC", 0.0)
    tracker._stopped = False
    polls.clear()
    caplog.clear()
    with tracker.hold("measurement-session"):
        with caplog.at_level(logging.INFO, logger="jasper.web._systemd"):
            tracker._run()
    deferred = [
        r.getMessage() for r in caplog.records
        if "idle-exit deferred" in r.getMessage()
    ]
    assert len(deferred) == 3


def test_idle_exit_hook_runs_before_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_idle_exit runs once, before os._exit; a failing hook never blocks exit.

    correction-web hangs its abandoned-capture production restore on this hook
    (the user closed the tab; idle shutdown is the daemon's last in-process
    chance to converge the speaker off the all-muted staged anchor).
    """

    class _Exit(Exception):
        pass

    events: list[str] = []
    monkeypatch.setattr(_systemd, "notify_watchdog", lambda: None)
    monkeypatch.setattr(
        _systemd, "notify_stopping", lambda: events.append("stopping")
    )
    monkeypatch.setattr(
        _systemd.os,
        "_exit",
        lambda _code: (_ for _ in ()).throw(_Exit()),
    )

    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=0.0,
        watchdog_period_sec=0.001,
        on_idle_exit=lambda: events.append("hook"),
    )
    with pytest.raises(_Exit):
        tracker._run()
    assert events == ["hook", "stopping"]

    # A raising hook must not block the exit (the process is going away).
    def broken_hook() -> None:
        events.append("broken-hook")
        raise RuntimeError("hook blew up")

    events.clear()
    tracker_broken = _systemd.IdleShutdownTracker(
        idle_threshold_sec=0.0,
        watchdog_period_sec=0.001,
        on_idle_exit=broken_hook,
    )
    with pytest.raises(_Exit):
        tracker_broken._run()
    assert events == ["broken-hook", "stopping"]


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


# ---------------------------------------------------------------------------
# Unserved inherited listeners (static .socket vs capability-gated wizards)
# ---------------------------------------------------------------------------


def _listener() -> socket.socket:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    return sock


def test_drain_ends_a_connection_on_an_unserved_port() -> None:
    """A connect to a drained listener ends at once and leaves no backlog.

    Both halves matter: the client seeing EOF is what lets nginx answer 502
    instead of waiting out proxy_read_timeout, and the empty backlog is what
    stops systemd re-triggering the unit the moment it idle-exits.
    """
    listener = _listener()
    port = listener.getsockname()[1]
    try:
        assert _systemd.drain_unclaimed_listeners([listener]) is not None

        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            client.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            client.settimeout(2.0)
            try:
                assert client.recv(64) == b""  # FIN: server hung up
            except ConnectionResetError:
                pass  # RST is the same answer to nginx
        finally:
            client.close()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not select.select([listener], [], [], 0.05)[0]:
                break
        assert not select.select([listener], [], [], 0.2)[0], (
            "backlog still readable — systemd would re-trigger the unit"
        )
    finally:
        listener.close()


def test_drain_is_a_noop_without_unserved_listeners() -> None:
    assert _systemd.drain_unclaimed_listeners([]) is None


def test_main_drains_exactly_the_ports_no_granted_wizard_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() hands the drain every inherited fd its granted specs skipped.

    Pins the selection, not the mechanism: with inherited sockets covering
    both a granted and an ungranted wizard, only the ungranted one is drained.
    """
    import dataclasses

    from jasper import volume_coordinator
    from jasper.web import __main__ as web_main

    served, unserved = _listener(), _listener()
    drained: list[list[socket.socket]] = []
    granted = dataclasses.replace(
        web_main.WIZARD_SPECS[0], make_server=lambda target: _FakeServer(),
    )

    monkeypatch.setattr(
        web_main._systemd, "adopt_systemd_sockets", lambda: [served, unserved],
    )
    monkeypatch.setattr(
        web_main._systemd, "drain_unclaimed_listeners",
        lambda socks: drained.append(list(socks)),
    )
    monkeypatch.setattr(web_main, "_specs_for_role", lambda role: (granted,))
    monkeypatch.setattr(web_main, "_active_install_role", lambda: "streambox")
    monkeypatch.setattr(
        volume_coordinator, "install_env_canonical_target_provider", lambda: None,
    )
    monkeypatch.setattr(
        web_main._systemd, "install_request_idle_bump", lambda cls, tracker: None,
    )
    monkeypatch.setattr(web_main._systemd, "notify_ready", lambda: None)
    monkeypatch.setattr(web_main._systemd, "notify_stopping", lambda: None)
    monkeypatch.setattr(
        web_main._systemd.IdleShutdownTracker, "start", lambda self: None,
    )
    # getsockname() is how main() maps an fd to a wizard, so point the spec at
    # the fake listener's real port through the env override it already honours.
    monkeypatch.setenv(granted.env_var, str(served.getsockname()[1]))

    try:
        assert web_main.main() == 0
        assert drained == [[unserved]]
    finally:
        served.close()
        unserved.close()


class _FakeServer:
    RequestHandlerClass = BaseHTTPRequestHandler

    def serve_forever(self) -> None:
        return None
