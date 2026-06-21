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

Tests use a fake notifier and tiny intervals (50 ms) so the
suite stays fast.
"""
from __future__ import annotations

import sys
import time
import types
from unittest.mock import MagicMock

import pytest

from jasper.watchdog import Heartbeat


@pytest.fixture
def fake_sdnotify(monkeypatch):
    """Inject a fake sdnotify module + NOTIFY_SOCKET into the
    environment so `Heartbeat._make_notifier` returns a controllable
    mock. Returns the mock notifier instance so tests can assert on
    `.notify()` calls."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    fake = MagicMock()
    fake_module = types.ModuleType("sdnotify")
    fake_module.SystemdNotifier = MagicMock(return_value=fake)
    monkeypatch.setitem(sys.modules, "sdnotify", fake_module)
    yield fake


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


def test_emits_ready_on_start(fake_sdnotify):
    hb = Heartbeat()
    assert hb.enabled
    hb.start()
    # Sleep just long enough for the thread to come up; we're not
    # waiting for a tick (interval default 10s).
    time.sleep(0.05)
    fake_sdnotify.notify.assert_any_call("READY=1")
    hb.stop()


def test_pats_when_progress_is_fresh(fake_sdnotify):
    """Progress bumped within stale_threshold → WATCHDOG=1 each tick."""
    hb = Heartbeat(stale_threshold_sec=1.0, interval_sec=0.05)
    hb.start()
    # Bump continuously over ~3 ticks. Each tick should see fresh
    # progress and pat.
    for _ in range(4):
        hb.bump()
        time.sleep(0.06)
    hb.stop()

    pat_calls = [
        c for c in fake_sdnotify.notify.call_args_list
        if c.args == ("WATCHDOG=1",)
    ]
    # 4 sleeps × 60 ms = 240 ms / 50 ms interval ≈ 3-5 ticks. Allow
    # some slop for scheduler jitter; the contract is "at least one".
    assert len(pat_calls) >= 2, (
        f"expected WATCHDOG=1 to fire on fresh-progress ticks, "
        f"got {len(pat_calls)} pat calls"
    )


def test_suppresses_pat_when_progress_is_stale(fake_sdnotify):
    """If the work loop hasn't bumped within stale_threshold,
    the heartbeat MUST NOT pat — that's the whole point of the
    sentinel. Otherwise we'd mask the wedge."""
    hb = Heartbeat(stale_threshold_sec=0.1, interval_sec=0.05)
    hb.start()
    # Don't call bump(). The initial last_progress is set at
    # construction, so we need to outlast the threshold.
    time.sleep(0.3)  # 6 ticks, all stale after the first
    hb.stop()

    pat_calls = [
        c for c in fake_sdnotify.notify.call_args_list
        if c.args == ("WATCHDOG=1",)
    ]
    # We allow at most 1 pat (the first tick might land just before
    # the threshold lapses depending on scheduler). The contract is
    # "doesn't keep patting" — definitely <6.
    assert len(pat_calls) <= 2, (
        f"expected stale heartbeat to suppress pats, "
        f"got {len(pat_calls)} pat calls"
    )


def test_emits_stopping_on_stop(fake_sdnotify):
    hb = Heartbeat()
    hb.start()
    hb.stop()
    fake_sdnotify.notify.assert_any_call("STOPPING=1")


def test_pat_resumes_after_progress_recovers(fake_sdnotify):
    """Sentinel pattern: a wedged-then-recovered loop should
    resume patting. The heartbeat is stateless re: past wedges —
    it only looks at `now - last_progress`."""
    hb = Heartbeat(stale_threshold_sec=0.1, interval_sec=0.05)
    hb.start()
    # Phase 1: no bumps for ~3 ticks → pats suppressed.
    time.sleep(0.2)
    pat_count_phase1 = sum(
        1 for c in fake_sdnotify.notify.call_args_list
        if c.args == ("WATCHDOG=1",)
    )
    # Phase 2: resume bumping → pats should resume.
    for _ in range(4):
        hb.bump()
        time.sleep(0.06)
    hb.stop()

    pat_count_total = sum(
        1 for c in fake_sdnotify.notify.call_args_list
        if c.args == ("WATCHDOG=1",)
    )
    assert pat_count_total > pat_count_phase1, (
        "expected pats to resume once progress recovers; "
        f"phase1={pat_count_phase1}, total={pat_count_total}"
    )


def test_stop_is_idempotent(fake_sdnotify):
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
    # Simulate the package being absent. Use a finder that always
    # raises ImportError for `sdnotify`.
    monkeypatch.setitem(sys.modules, "sdnotify", None)
    hb = Heartbeat()
    assert not hb.enabled
    hb.start()
    hb.bump()
    hb.stop()
