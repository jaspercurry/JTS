# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `jasper.watchdog.Heartbeat`.

The progress-sentinel pattern is load-bearing for the JTS
resilience contract: a naive heartbeat thread that pats systemd
every N seconds would mask exactly the bug it's meant to catch
(work loop wedged while the heartbeat thread keeps running).
These tests pin the contract:

  - When the work loop bumps recently, the heartbeat fires
    `WATCHDOG=1` on each tick.
  - When the work loop hasn't bumped within the stale threshold,
    the heartbeat suppresses `WATCHDOG=1` so systemd's
    `WatchdogSec=` timer expires and `Restart=on-watchdog`
    revives the daemon.
  - When `NOTIFY_SOCKET` is unset (not running under systemd),
    the helper degrades to a no-op without raising.

Staleness is decided against a clock the test owns, so a fresh or
stale verdict holds however long the box takes to reach the next
tick, and every wait below is on a transition the heartbeat itself
makes. Nothing here is gated on elapsed wall-clock time, so load
makes this file slower, never red (#2658).
"""
from __future__ import annotations

import logging
import sys
import threading
import types
from collections import Counter

import pytest

from jasper.watchdog import Heartbeat

from ._log_events import event_field_maps

#: Hang backstop, never a timing assertion — same bound and the same
#: reasoning as tests/_async_wait.DEFAULT_SIGNAL_TIMEOUT_S.
WEDGED_S = 10.0

#: Real-time cadence of the heartbeat thread. Nothing asserts on it; it
#: only sets how promptly the loop re-reads the test's clock.
TICK_S = 0.01

TICK = "clock read"  #: any read — __init__/bump read it too, so snapshot
                     #: the count before waiting and never bump before an await.


class Transitions:
    """Counts what the heartbeat observably did — messages it sent, ticks
    its loop ran — and blocks until a given count is reached."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._seen: Counter[str] = Counter()

    def record(self, what: str) -> None:
        with self._cv:
            self._seen[what] += 1
            self._cv.notify_all()

    def count(self, what: str) -> int:
        with self._cv:
            return self._seen[what]

    def await_count(self, what: str, wanted: int) -> None:
        with self._cv:
            reached = self._cv.wait_for(
                lambda: self._seen[what] >= wanted, WEDGED_S
            )
            if not reached:
                raise AssertionError(
                    f"heartbeat reached {self._seen[what]} × {what}, "
                    f"expected {wanted}"
                )


class FakeClock:
    """The monotonic source the heartbeat reads, under test control."""

    def __init__(self, transitions: Transitions) -> None:
        self._transitions = transitions
        self.now = 0.0

    def __call__(self) -> float:
        self._transitions.record(TICK)
        return self.now


@pytest.fixture
def transitions(monkeypatch):
    """Inject a fake sdnotify module + NOTIFY_SOCKET into the environment
    so `_make_notifier` returns a notifier that records what it was sent."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    seen = Transitions()
    module = types.ModuleType("sdnotify")
    module.SystemdNotifier = lambda: types.SimpleNamespace(notify=seen.record)
    monkeypatch.setitem(sys.modules, "sdnotify", module)
    return seen


@pytest.fixture
def build(transitions):
    """Build heartbeats on a clock the test owns, stopped on teardown so a
    failed assertion cannot leak a thread. Construction and `start()` stay
    separate so a test can set the clock in between, pinning the first
    tick's verdict instead of racing the thread for it."""
    built: list[Heartbeat] = []

    def build_one(clock: FakeClock, *, stale_threshold_sec: float) -> Heartbeat:
        hb = Heartbeat(
            stale_threshold_sec=stale_threshold_sec,
            interval_sec=TICK_S,
            monotonic=clock,
        )
        built.append(hb)
        return hb

    yield build_one
    for hb in built:
        hb.stop()


def test_no_notify_socket_disables_heartbeat(monkeypatch):
    """Outside systemd, the helper must no-op silently — running
    `jasper-aec-bridge` from a REPL or test runner should not crash."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    hb = Heartbeat()
    assert not hb.enabled
    # Public API must not raise when disabled.
    hb.start()
    hb.bump()
    hb.stop()


def test_emits_ready_on_start(transitions):
    """`start()` sends READY=1 on the caller's thread before the heartbeat
    thread exists, so the message has landed once it returns."""
    hb = Heartbeat()
    assert hb.enabled
    hb.start()
    try:
        assert transitions.count("READY=1") == 1
    finally:
        hb.stop()


def test_pats_when_progress_is_fresh(transitions, build):
    """Progress bumped within stale_threshold → WATCHDOG=1 each tick."""
    clock = FakeClock(transitions)
    hb = build(clock, stale_threshold_sec=1.0)
    # Without the bump the sentinel still holds construction time and every
    # tick reads stale; with it the loop sits 0.1 s into a 1.0 s threshold
    # and stays there for as long as the test takes.
    clock.now = 10.0
    hb.bump()
    clock.now = 10.1
    hb.start()

    transitions.await_count("WATCHDOG=1", 3)


def test_emits_stopping_on_stop(transitions):
    hb = Heartbeat()
    hb.start()
    hb.stop()
    assert transitions.count("STOPPING=1") == 1


def test_pat_resumes_after_progress_recovers(transitions, build):
    """Sentinel pattern, both halves: while the work loop hasn't bumped
    within stale_threshold the heartbeat MUST NOT pat — otherwise it masks
    the wedge — and once progress recovers it resumes, being stateless re:
    past wedges and looking only at `now - last_progress`."""
    clock = FakeClock(transitions)
    hb = build(clock, stale_threshold_sec=0.1)
    clock.now = 10.0
    hb.start()

    # Wedged: never bumped, and the clock is past the threshold before the
    # first tick, so every tick's verdict is stale — including that one.
    transitions.await_count(TICK, transitions.count(TICK) + 3)
    assert transitions.count("WATCHDOG=1") == 0

    # Recovered: one bump re-freshens the sentinel and pats resume.
    hb.bump()
    transitions.await_count("WATCHDOG=1", 3)


def test_suppression_speaks_once_per_wedge_not_once_per_tick(
    transitions, build, caplog,
):
    """A wedge holds until systemd kills the unit, so the suppression is one
    line per episode, not one per `interval_sec`. Delete with the events.
    """
    caplog.set_level(logging.INFO, logger="jasper.watchdog")
    clock = FakeClock(transitions)
    hb = build(clock, stale_threshold_sec=0.1)
    clock.now = 10.0
    hb.start()

    transitions.await_count(TICK, transitions.count(TICK) + 3)
    hb.bump()
    transitions.await_count("WATCHDOG=1", 1)
    hb.stop()

    (suppressed,) = event_field_maps(caplog, "watchdog.heartbeat_suppressed")
    assert float(suppressed["stalled_for_s"]) >= 0.1
    (resumed,) = event_field_maps(caplog, "watchdog.heartbeat_resumed")
    assert int(resumed["suppressed_ticks"]) >= 3


def test_stop_is_idempotent(transitions):
    """Daemon shutdown paths call stop() in `finally:`; calling it
    again from a signal handler must not raise."""
    hb = Heartbeat()
    hb.start()
    hb.stop()
    hb.stop()  # second call must not crash


def test_disabled_when_sdnotify_not_installed(monkeypatch):
    """If `sdnotify` is missing AND NOTIFY_SOCKET is set, the helper
    must log a warning and degrade gracefully — not crash the daemon."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    # A None entry is the documented way to make `import sdnotify` raise.
    monkeypatch.setitem(sys.modules, "sdnotify", None)
    hb = Heartbeat()
    assert not hb.enabled
    hb.start()
    hb.bump()
    hb.stop()
