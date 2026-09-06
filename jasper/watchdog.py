# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""systemd watchdog heartbeat with progress-sentinel guard.

This is Tier 1 of the JTS resilience ladder. Pairs with
`Type=notify` + `WatchdogSec=N` in the daemon's systemd unit:

  - The daemon's work loop calls `Heartbeat.bump()` every time it
    successfully completes one unit of useful work (a processed mic
    frame, a wake-loop iteration, etc.).
  - A background heartbeat thread wakes every `interval_sec` and
    notifies systemd `WATCHDOG=1` ONLY if `now - last_progress` is
    under `stale_threshold_sec`. If the loop wedges (PortAudio blocked
    in a syscall, Python deadlock, etc.), the heartbeat stops patting,
    systemd's `WatchdogSec=` timer expires, and the unit's `Restart=`
    policy brings the daemon back with a fresh process (see
    deploy/systemd/jasper-aec-bridge.service).

The heartbeat thread only reads the sentinel, so it adds no GIL
contention to the work loop.

A wedge inside a blocking C call can hold the GIL indefinitely, so
Python's own signal handler never runs and SIGTERM does nothing --
only the watchdog timer's own recovery path gets the daemon back; see
the Tier 1+2 block in deploy/systemd/jasper-aec-bridge.service.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Optional

from .log_event import log_event

logger = logging.getLogger(__name__)


class Heartbeat:
    """Progress-sentinel-driven systemd watchdog notifier."""

    def __init__(
        self,
        stale_threshold_sec: float = 5.0,
        interval_sec: float = 10.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stale_threshold = stale_threshold_sec
        self._interval = interval_sec
        self._monotonic = monotonic
        self._last_progress = monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._notifier = _make_notifier()

    @property
    def enabled(self) -> bool:
        return self._notifier is not None

    def bump(self) -> None:
        """Mark forward progress. Cheap; safe to call every frame."""
        self._last_progress = self._monotonic()

    def start(self) -> None:
        """Send `READY=1` and start the heartbeat thread.

        No-op if sdnotify isn't available (e.g. running outside
        systemd or the package isn't installed)."""
        if self._notifier is None:
            return
        self._notifier.notify("READY=1")
        self._thread = threading.Thread(
            target=self._run, name="watchdog-heartbeat", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal STOPPING and join the heartbeat thread.

        Idempotent. Daemon shutdown paths should call this in a
        `finally:` block so systemd sees the clean exit signal."""
        if self._notifier is None:
            return
        self._stop.set()
        try:
            self._notifier.notify("STOPPING=1")
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        # `Event.wait()` returns True if stop was set, False on timeout.
        # Suppression is reported on its edges only: the tick cadence is not
        # news, and systemd kills the unit while it holds.
        suppressed_ticks = 0
        while not self._stop.wait(self._interval):
            since = self._monotonic() - self._last_progress
            if since < self._stale_threshold:
                if suppressed_ticks:
                    log_event(logger, "watchdog.heartbeat_resumed",
                              suppressed_ticks=suppressed_ticks)
                    suppressed_ticks = 0
                try:
                    self._notifier.notify("WATCHDOG=1")
                except Exception:  # noqa: BLE001
                    # Don't crash the heartbeat thread on a transient
                    # socket error — try again next tick.
                    logger.exception("sdnotify WATCHDOG=1 failed")
            else:
                if not suppressed_ticks:
                    log_event(logger, "watchdog.heartbeat_suppressed",
                              level=logging.WARNING,
                              stalled_for_s=f"{since:.1f}")
                suppressed_ticks += 1


def _make_notifier():
    """Return a pure-Python sdnotify notifier, or None if unavailable.

    Returns None when:
      - the `sdnotify` package isn't installed
      - `NOTIFY_SOCKET` isn't set in the environment (we're not
        running under `Type=notify` systemd, e.g. in tests, a
        REPL, or a manual `python -m` invocation)
    """
    if not os.environ.get("NOTIFY_SOCKET"):
        return None
    try:
        import sdnotify  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "sdnotify package not installed; watchdog heartbeat disabled. "
            "Install with: pip install sdnotify"
        )
        return None
    return sdnotify.SystemdNotifier()
