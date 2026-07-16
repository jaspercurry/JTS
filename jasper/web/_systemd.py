# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""systemd socket-activation + idle-shutdown for setup wizards.

Each wizard runs as its own systemd service paired with a .socket unit.
systemd binds the listening port(s) and hands the file descriptor(s) to
us via the LISTEN_FDS / LISTEN_PID environment variables; we adopt the
listening fd without re-binding. After IDLE_SHUTDOWN_SEC of no incoming
requests across all our adopted sockets, the process exits cleanly.
systemd's socket stays in LISTEN state throughout, so the next request
re-activates the service without losing any connections.

Why bother:
  Setup wizards (Spotify OAuth, voice provider, room correction,
  Bluetooth pair, dial onboarding, AirPlay sync mode) are touched
  maybe once a month, but each was costing ~10-30 MB Pss resident
  24/7. With socket activation, the daemon exits after 10 min idle
  and only re-spawns when a tab actually opens the page — saving
  ~60-90 MB Pss combined across the wizard processes.

Trade-off:
  ~300-800 ms cold-start latency on the first request after idle
  (Python interpreter + the wizard's imports + handler construction).
  For OAuth/pairing flows the user is already context-switching to
  their phone; this is invisible. Subsequent requests in the same
  session are instant.

Implementation notes:
  - Accept=no in the .socket unit (single daemon per .socket, not one
    Python interpreter per connection — startup cost forbids that).
  - Type=notify in the .service unit; this module's notify_ready()
    must be called after the listener is up so systemd marks the
    service READY and nginx's proxy queues unblock.
  - The .service should NOT have Restart=always (that would defeat
    idle-exit by respawning immediately). Restart=on-failure keeps
    the failure path covered without fighting the lifecycle.
  - WatchdogSec=30s in the .service unit; the idle tracker sends
    WATCHDOG=1 every ~15 s. If the daemon hangs, systemd kills it
    and the socket activation re-spawns on next request.

Reference: gfxmonk.net/2012/05/15/systemd-socket-activation-in-python.html
plus the Trixie systemd.socket(5) manpage. See PR comments for the
full prior-art survey (Cockpit, FPM, systemd-socket-proxyd).
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from http.server import ThreadingHTTPServer

# Per sd_listen_fds(3) — fds passed by systemd start at 3.
SD_LISTEN_FDS_START = 3

# 10-minute idle exit matches Cockpit's pattern. Setup wizards see
# bursts of activity (OAuth flow takes ~2 min) then go cold for weeks.
DEFAULT_IDLE_SHUTDOWN_SEC = 600.0

# Send WATCHDOG=1 at half the systemd WatchdogSec= interval per
# sd_notify(3) guidance. The wizards' .service units use WatchdogSec=30s.
DEFAULT_WATCHDOG_NOTIFY_SEC = 15.0


def adopt_systemd_sockets() -> list[socket.socket]:
    """Return sockets handed off by systemd, or [] if not activated.

    Order matches the order of ListenStream= directives in the
    .socket unit. Use socket.getsockname() on each to disambiguate
    when one daemon handles multiple ports (see jasper.web.__main__).

    Honors the LISTEN_PID check to avoid claiming fds inherited
    from a parent that already accepted them.
    """
    pid_str = os.environ.get("LISTEN_PID")
    fds_str = os.environ.get("LISTEN_FDS")
    if not pid_str or not fds_str:
        return []
    try:
        if int(pid_str) != os.getpid():
            return []
        n = int(fds_str)
    except ValueError:
        return []
    sockets: list[socket.socket] = []
    for i in range(n):
        # socket.fromfd() dups the fd, so the original (systemd-owned)
        # stays put. The dup is what we use to accept connections.
        s = socket.fromfd(
            SD_LISTEN_FDS_START + i, socket.AF_INET, socket.SOCK_STREAM,
        )
        sockets.append(s)
    return sockets


def make_http_server(target, handler_cls) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer for either an int port (legacy
    direct bind) or a pre-bound socket.socket (systemd handoff)."""
    if isinstance(target, socket.socket):
        srv = ThreadingHTTPServer(("", 0), handler_cls, bind_and_activate=False)
        srv.socket = target
        srv.server_address = target.getsockname()
        return srv
    if isinstance(target, tuple) and len(target) == 2:
        return ThreadingHTTPServer(target, handler_cls)
    if isinstance(target, int):
        return ThreadingHTTPServer(("127.0.0.1", target), handler_cls)
    raise TypeError(
        f"make_http_server: target must be socket, (host, port) tuple, "
        f"or int port; got {type(target).__name__}"
    )


def _notify(message: str) -> None:
    """Send a datagram to systemd's NOTIFY_SOCKET. No-op if unset."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(message.encode("utf-8"), addr)
    except OSError:
        pass
    finally:
        s.close()


def notify_ready() -> None:
    """Tell systemd the service is up. Required for Type=notify units."""
    _notify("READY=1")


def notify_stopping() -> None:
    """Tell systemd we're exiting cleanly."""
    _notify("STOPPING=1")


def notify_watchdog() -> None:
    """Ping systemd's watchdog. Suppress hung-daemon kill."""
    _notify("WATCHDOG=1")


class IdleShutdownTracker:
    """Tracks completed activity and exits only when no request is in flight.

    Usage::

        tracker = IdleShutdownTracker()
        install_request_idle_bump(MyHandler, tracker)
        tracker.start()
        # ... server.serve_forever()

    The installed handler hooks mark a parsed request active before dispatch
    and inactive in ``handle_one_request``'s ``finally`` block. ``log_request``
    also bumps the timestamp for every response. The background thread polls
    every WATCHDOG_NOTIFY_SEC, sends WATCHDOG=1, and `os._exit(0)`'s only when
    no request is active and completed activity exceeds the threshold.

    `os._exit` rather than `sys.exit` because the latter raises
    SystemExit which serve_forever() catches and resumes. We need
    the process gone so systemd's .socket can rearm.
    """

    def __init__(
        self,
        idle_threshold_sec: float = DEFAULT_IDLE_SHUTDOWN_SEC,
        watchdog_period_sec: float = DEFAULT_WATCHDOG_NOTIFY_SEC,
        on_idle_exit=None,
    ) -> None:
        self._lock = threading.Lock()
        self._last_request = time.monotonic()
        self._active_requests = 0
        self._idle_threshold = idle_threshold_sec
        self._watchdog_period = watchdog_period_sec
        self._stopped = False
        # Optional zero-arg callable run once, exception-guarded, after the
        # idle decision and before os._exit — the wizard's last in-process
        # chance to converge state it left mid-flow (e.g. correction-web's
        # abandoned-capture production restore). Keep hooks bounded: the
        # process is exiting and a slow hook delays the socket rearm.
        self._on_idle_exit = on_idle_exit
        self._thread = threading.Thread(
            target=self._run, name="jasper-web-idle", daemon=True,
        )

    def start(self) -> None:
        """Start the background timer. No-op if already started."""
        if not self._thread.is_alive():
            self._thread.start()

    def bump(self) -> None:
        """Reset the idle counter. Call from every request path."""
        with self._lock:
            self._last_request = time.monotonic()

    def request_started(self) -> None:
        """Mark one parsed request active before its route handler runs."""
        with self._lock:
            self._active_requests += 1
            self._last_request = time.monotonic()

    def request_finished(self) -> None:
        """Release one active request and begin a fresh idle interval."""
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_request = time.monotonic()

    def _idle_status(self) -> tuple[bool, float, int]:
        """Return ``(expired, idle_seconds, active_requests)`` atomically."""
        with self._lock:
            active = self._active_requests
            idle = time.monotonic() - self._last_request
        return active == 0 and idle >= self._idle_threshold, idle, active

    def stop(self) -> None:
        """Stop the timer without exiting. For tests."""
        self._stopped = True

    def _run(self) -> None:
        log = logging.getLogger("jasper.web._systemd")
        while not self._stopped:
            time.sleep(self._watchdog_period)
            if self._stopped:
                return
            notify_watchdog()
            expired, idle, _active = self._idle_status()
            if expired:
                log.info(
                    "systemd idle-exit: no requests for %.0fs (threshold %.0fs)",
                    idle, self._idle_threshold,
                )
                if self._on_idle_exit is not None:
                    try:
                        self._on_idle_exit()
                    except Exception:  # noqa: BLE001 - the exit must proceed.
                        log.exception("idle-exit hook failed; exiting anyway")
                notify_stopping()
                # os._exit, not sys.exit — see class docstring.
                os._exit(0)


def install_request_idle_bump(handler_cls, tracker: IdleShutdownTracker) -> None:
    """Track parsed requests from dispatch start through handler completion.

    ``BaseHTTPRequestHandler`` calls ``parse_request`` only after it has read a
    request line, so an idle keep-alive connection does not count as active.
    The ``handle_one_request`` finally hook covers exceptions and responses
    that never reach ``send_response``. ``log_request`` retains the historical
    activity bump for every emitted status, including 404 and 500.
    """
    original_log_request = handler_cls.log_request
    original_parse_request = handler_cls.parse_request
    original_handle_one_request = handler_cls.handle_one_request
    marker = "_jts_idle_request_active"

    def log_and_bump(self, code="-", size="-"):
        tracker.bump()
        return original_log_request(self, code, size)

    def parse_and_track(self):
        parsed = original_parse_request(self)
        if parsed:
            tracker.request_started()
            setattr(self, marker, True)
        return parsed

    def handle_and_release(self):
        try:
            return original_handle_one_request(self)
        finally:
            if getattr(self, marker, False):
                setattr(self, marker, False)
                tracker.request_finished()

    handler_cls.log_request = log_and_bump
    handler_cls.parse_request = parse_and_track
    handler_cls.handle_one_request = handle_and_release
