# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""systemd socket-activation + idle-shutdown for setup wizards.

Each wizard runs as its own systemd service paired with a .socket unit.
systemd binds the listening port(s) and hands the file descriptor(s) to
us via the LISTEN_FDS / LISTEN_PID environment variables; we adopt the
listening fd without re-binding. After IDLE_SHUTDOWN_SEC of no incoming
requests across all our adopted sockets — with no request in flight and
no background-work hold outstanding — the process exits cleanly.
systemd's socket stays in LISTEN state throughout, so the next request
re-activates the service without losing any connections.

Background work must hold the tracker:
  Inbound requests are the only activity this module can observe on its
  own. Work a request STARTS but does not await — the correction wizard's
  browser capture measurement session, whose mid-session traffic is all
  OUTBOUND to the capture — therefore looks exactly like an abandoned tab,
  and the process exits out from under it (2026-07-29 JTS3 incident,
  issue #1854: a verify capture died 5 s after arriving). Whoever owns
  that work's lifetime wraps it in `IdleShutdownTracker.hold()`, which
  takes the same busy counter an in-flight request takes.

Why bother:
  Setup wizards (Spotify OAuth, voice provider, room correction,
  Bluetooth pair, AirPlay sync mode) are touched
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

import contextlib
import logging
import os
import select
import socket
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

# Per sd_listen_fds(3) — fds passed by systemd start at 3.
SD_LISTEN_FDS_START = 3

# 10-minute idle exit matches Cockpit's pattern. Setup wizards see
# bursts of activity (OAuth flow takes ~2 min) then go cold for weeks.
DEFAULT_IDLE_SHUTDOWN_SEC = 600.0

# Send WATCHDOG=1 at half the systemd WatchdogSec= interval per
# sd_notify(3) guidance. The wizards' .service units use WatchdogSec=30s.
DEFAULT_WATCHDOG_NOTIFY_SEC = 15.0

# How often a held-open idle exit may be reported. The poll below runs every
# ~15 s, so an unrate-limited line would spam the journal for the whole of a
# long measurement session; but a LEAKED hold makes the process immortal and
# silently forfeits the ~10-30 MB Pss the idle exit exists to return on a 1 GB
# Pi, so it must never be invisible either.
DEFERRED_EXIT_LOG_PERIOD_SEC = 300.0

# Past this much CONTINUOUS busy time with nothing inbound, a hold is reported
# at WARNING instead of INFO: no legitimate work runs this long, so the process
# is stuck — it cannot idle-exit, and whatever its on-idle-exit hook converges
# never converges.
#
# Derivation (this module stays generic — it deliberately does NOT import the
# correction session model, so the number is a literal and this comment is the
# audit trail): the longest-lived hold any wizard takes today is
# correction-web's crossover-v2 capture session, whose own sizing is
# `session_volume_plan.MAX_WALL_CLOCK_CEILING_S` = 3600 s — the hard cap on
# `crossover_v2_flow.session_wall_clock_ceiling_s`. (NOT the capture
# `DEFAULT_TTL_S`: a cloud session legitimately outlasts that, which is why
# the ceiling had to be scaled per-entry in the first place.) A hold may then
# stand a little past the cap through the terminal drain / analysis / purge
# tails. Doubling the cap gives a bound nothing legitimate reaches, which is
# the property that matters — this line is a leak alarm, and one that cried
# wolf on every Full-tier commission would be worse than none. Escalation is
# the ONLY behavior change: nothing reaps the process, per
# `correction_setup._run_async`'s fail-closed invariant (a terminal response
# must never release measurement ownership while the graph/volume finalizer
# can still mutate the speaker).
HOLD_LEAK_WARN_AFTER_SEC = 7200.0


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


def drain_unclaimed_listeners(
    sockets: list[socket.socket],
) -> threading.Thread | None:
    """Accept-and-close on inherited listeners no wizard serves.

    The .socket units are static files: they bind every port the tier COULD
    host, while jasper.web.__main__ builds servers only for the wizards this
    tier's capabilities grant. A connection to an ungranted port completes
    into the backlog with nobody to accept it, so nginx waits out
    proxy_read_timeout for a 504 — and the pending connection keeps systemd's
    copy of the fd readable, so the daemon idle-exits and systemd re-triggers
    it at once, forever.

    Closing our own fd does NOT fix that: ``socket.fromfd`` dup'd it and
    systemd still holds the original, so the queued connection survives the
    close (measured). Accepting and closing is what empties the backlog; the
    client sees the connection end at once and nginx answers 502.

    Deliberately does NOT touch the idle tracker — a refused connection is
    not activity, and bumping would keep the process resident to serve
    nothing. Returns the drain thread, or None when there is nothing to
    drain.
    """
    if not sockets:
        return None
    thread = threading.Thread(
        target=_drain_forever,
        args=(list(sockets),),
        name="jasper-web-drain",
        daemon=True,
    )
    thread.start()
    return thread


def _drain_forever(sockets: list[socket.socket]) -> None:
    log = logging.getLogger("jasper.web._systemd")
    while True:
        try:
            ready, _, _ = select.select(sockets, [], [])
        except (OSError, ValueError):
            return  # a socket was closed under us; the process is going away
        for sock in ready:
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            try:
                # The close IS the refusal: nothing may run before it, or a
                # raise there leaves the client hanging on a leaked fd.
                conn.close()
                port = sock.getsockname()[1]
            except OSError:
                continue
            log.info(
                "jasper-web refused a connection on unserved port %d "
                "(no capability grants this wizard on this tier)",
                port,
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
    also bumps the timestamp for every response. Background work a request
    starts but does not await takes the same busy counter through
    :meth:`hold`. The background thread polls every WATCHDOG_NOTIFY_SEC, sends
    WATCHDOG=1, and `os._exit(0)`'s only when nothing is active and completed
    activity exceeds the threshold.

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
        # Labels of the outstanding holds — observability ONLY (the busy
        # decision reads _active_requests, which every hold also takes). A
        # deferred exit that names WHICH work is holding is the difference
        # between a five-minute and a two-hour diagnosis of a leak.
        self._holds: dict[str, int] = {}
        self._deferred_logged_at: float | None = None
        # Monotonic epoch of the CURRENT busy stretch — set when the busy
        # counter leaves zero, cleared when it returns. Deliberately not
        # per-label: what "no legitimate work runs this long" is asked of is
        # the process, and a stretch that never ends is the leak regardless of
        # which label owns it.
        self._busy_since: float | None = None
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

    def _take_busy_locked(self) -> None:
        """Take one busy token. The ONE place the busy stretch opens.

        Caller holds ``self._lock``. Shared by requests and holds so the
        counter and its epoch can never disagree about when the process
        stopped being idle.
        """
        if self._active_requests == 0:
            self._busy_since = time.monotonic()
        self._active_requests += 1
        self._last_request = time.monotonic()

    def _drop_busy_locked(self) -> None:
        """Release one busy token and begin a fresh idle interval.

        Caller holds ``self._lock``. The stretch closes only when the LAST
        token goes.
        """
        self._active_requests = max(0, self._active_requests - 1)
        if self._active_requests == 0:
            self._busy_since = None
        self._last_request = time.monotonic()

    def request_started(self) -> None:
        """Mark one parsed request active before its route handler runs."""
        with self._lock:
            self._take_busy_locked()

    def request_finished(self) -> None:
        """Release one active request and begin a fresh idle interval."""
        with self._lock:
            self._drop_busy_locked()

    @contextlib.contextmanager
    def hold(self, label: str = "") -> Iterator[None]:
        """Keep the process alive across background work that serves no request.

        Takes the SAME ``_active_requests`` counter an in-flight request takes,
        so the busy signal stays one value and :meth:`_idle_status` needs no
        second input. ``label`` is a short name for the work (it appears in the
        deferred-exit log line below); it never affects the decision.

        Why this exists: inbound requests were the only activity the tracker
        could observe, so work a request STARTS but does not await — the
        correction wizard's browser capture measurement session, whose mid-session
        traffic is all outbound — read as an abandoned tab and the process
        exited mid-flight (issue #1854).

        Release is guaranteed by ``finally``, including when the body raises.
        Entering on one thread and exiting on another is supported and is the
        gap-free pattern for work whose starter may finish first: take the hold
        BEFORE ``Thread.start()``, release it in that thread's own ``finally``.
        Release also begins a fresh idle interval, exactly as a finished
        request does — a session that just ended is not retroactively idle.
        """
        name = label or "background"
        with self._lock:
            self._take_busy_locked()
            self._holds[name] = self._holds.get(name, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._drop_busy_locked()
                # Two sessions can legitimately overlap under one label (the
                # capture slot frees before the hold does), so the label survives
                # until its LAST holder leaves.
                remaining = self._holds.get(name, 0) - 1
                if remaining > 0:
                    self._holds[name] = remaining
                else:
                    self._holds.pop(name, None)

    def _idle_status(self) -> tuple[bool, float, int]:
        """Return ``(expired, idle_seconds, active_requests)`` atomically.

        ``active_requests`` counts in-flight requests AND outstanding
        :meth:`hold` s — one busy counter, one decision.
        """
        with self._lock:
            active = self._active_requests
            idle = time.monotonic() - self._last_request
        return active == 0 and idle >= self._idle_threshold, idle, active

    def _log_deferred_exit(self, log: logging.Logger, idle: float, active: int) -> None:
        """Report an idle exit held open by in-flight work, rate-limited.

        The threshold has passed and the process would have exited; something
        is holding it. Reported at most once per DEFERRED_EXIT_LOG_PERIOD_SEC
        so a legitimately long session costs a handful of lines, while a leaked
        hold — which forfeits the idle exit's whole point — is never silent.

        Past HOLD_LEAK_WARN_AFTER_SEC of unbroken busy time the same line
        escalates to WARNING: at that age it is not a long session, it is a
        stuck one, and the process can no longer idle-exit OR run the
        on-idle-exit hook that converges what it left mid-flow. This escalates
        only — nothing here ends the process; a wedged worker is a bug to fix
        at its own layer, not something to reap out from under.

        One rate limiter covers both levels, which is deliberate: an INFO
        emitted just inside the bound delays the first WARNING by up to one
        DEFERRED_EXIT_LOG_PERIOD_SEC window. Accepted — the condition it
        reports is hours old by then, and a second timer per level would buy
        five minutes at the cost of two things to reason about.

        Called only from the timer thread, so the rate-limit timestamp needs no
        lock; the counters it reads do.
        """
        now = time.monotonic()
        if (
            self._deferred_logged_at is not None
            and now - self._deferred_logged_at < DEFERRED_EXIT_LOG_PERIOD_SEC
        ):
            return
        self._deferred_logged_at = now
        with self._lock:
            holds = ",".join(sorted(self._holds)) or "none"
            busy_for = (
                0.0 if self._busy_since is None else now - self._busy_since
            )
        leaked = busy_for >= HOLD_LEAK_WARN_AFTER_SEC
        note = (
            f" — busy past {HOLD_LEAK_WARN_AFTER_SEC:.0f}s, so this is a "
            f"LEAKED hold, not a long session: the process can no longer "
            f"idle-exit and its on-idle-exit hook cannot run"
        ) if leaked else ""
        log.log(
            logging.WARNING if leaked else logging.INFO,
            "systemd idle-exit deferred: %d active requests/holds after %.0fs "
            "idle, busy for %.0fs (threshold %.0fs, holds: %s)%s",
            active, idle, busy_for, self._idle_threshold, holds, note,
        )

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
            expired, idle, active = self._idle_status()
            # Would have exited, but something is still in flight. (`active`
            # non-zero is exactly why `expired` is False here.)
            if active and idle >= self._idle_threshold:
                self._log_deferred_exit(log, idle, active)
            if expired:
                log.info(
                    "systemd idle-exit: no requests for %.0fs (threshold %.0fs)",
                    idle, self._idle_threshold,
                )
                if self._on_idle_exit is not None:
                    # Same specific tuple the service-start claim boundary
                    # catches around this hook's restore (correction_setup):
                    # _run_async timeouts are TimeoutError (an OSError), and
                    # CamillaUnavailable is a RuntimeError. Hooks are expected
                    # to be fail-soft themselves; os._exit below still runs.
                    try:
                        self._on_idle_exit()
                    except (OSError, RuntimeError, ValueError):
                        log.exception("idle-exit hook failed; exiting anyway")
                notify_stopping()
                # os._exit, not sys.exit — see class docstring.
                os._exit(0)


def no_hold(label: str = "") -> contextlib.AbstractContextManager[None]:
    """The null :meth:`IdleShutdownTracker.hold` — takes nothing, holds nothing.

    The explicit counterpart to a real hold, so a call site that spawns
    background work states which it wants instead of inheriting a default. Use
    it when the work cannot outlive the request that started it, or when there
    is no tracker at all (tests, direct/legacy invocation).

    Same signature as ``tracker.hold`` so the two are interchangeable at the
    seam. Named rather than passing ``contextlib.nullcontext`` directly:
    ``nullcontext`` reads as a value, not as a decision about process
    lifetime.
    """
    del label  # accepted for signature parity with hold(); nothing to record
    return contextlib.nullcontext()


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
