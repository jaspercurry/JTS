"""systemd watchdog heartbeat with progress-sentinel guard.

This is Tier 1 of the JTS resilience ladder. Pairs with
`Type=notify` + `WatchdogSec=N` in the daemon's systemd unit:

  - The daemon's work loop calls `Heartbeat.bump()` every time it
    successfully completes one unit of useful work (a processed mic
    frame, a wake-loop iteration, etc.).
  - A background heartbeat thread wakes every `interval_sec` and
    notifies systemd `WATCHDOG=1` ONLY if `now - last_progress`
    is under `stale_threshold_sec`. If the loop wedges (PortAudio
    blocked in a syscall, Python deadlock, etc.), the heartbeat
    stops patting, systemd's `WatchdogSec=` timer expires, and
    `Restart=on-watchdog` brings the daemon back with a fresh
    process.

The progress-sentinel pattern matters: a naive heartbeat thread
that just pats every N seconds masks hangs in the work loop. A
sentinel ensures the heartbeat reflects actual forward progress.
The thread reads-only — no GIL-contention concerns with the work
loop.

Failure mode that motivated this (2026-05-11):
  jasper-aec-bridge's `out_stream.write(clean)` blocked
  indefinitely after the LoopbackAEC kernel-side timer wedged.
  The main thread was inside a C call holding the GIL, so
  Python's signal handler never ran, `_shutdown.set()` was never
  called, systemd's SIGTERM did nothing, and after 90 s the
  SIGKILL corrupted snd-aloop kernel state requiring a reboot.
  With this Heartbeat in place, `bump()` would have stopped
  firing the moment the loop wedged; systemd would have killed
  the daemon within `WatchdogSec` and restarted it cleanly,
  never reaching SIGKILL.

Pure-Python `sdnotify` (no C extension); if the package is not
installed or `NOTIFY_SOCKET` is unset (i.e. we're running
outside systemd, e.g. in tests or interactive dev), the helper
no-ops gracefully.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class Heartbeat:
    """Progress-sentinel-driven systemd watchdog notifier."""

    def __init__(
        self,
        stale_threshold_sec: float = 5.0,
        interval_sec: float = 10.0,
    ) -> None:
        self._stale_threshold = stale_threshold_sec
        self._interval = interval_sec
        self._last_progress = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._notifier = _make_notifier()

    @property
    def enabled(self) -> bool:
        return self._notifier is not None

    def bump(self) -> None:
        """Mark forward progress. Cheap; safe to call every frame."""
        self._last_progress = time.monotonic()

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
        # Tick on a fixed cadence; check progress sentinel each tick.
        # `Event.wait()` returns True if stop was set, False on timeout.
        while not self._stop.wait(self._interval):
            since = time.monotonic() - self._last_progress
            if since < self._stale_threshold:
                try:
                    self._notifier.notify("WATCHDOG=1")
                except Exception:  # noqa: BLE001
                    # Don't crash the heartbeat thread on a transient
                    # socket error — try again next tick.
                    logger.exception("sdnotify WATCHDOG=1 failed")
            else:
                logger.warning(
                    "heartbeat suppressed: no progress for %.1fs "
                    "(threshold=%.1fs) — systemd will kill us soon",
                    since, self._stale_threshold,
                )


def _make_notifier():
    """Return an sdnotify notifier, or None if unavailable.

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
