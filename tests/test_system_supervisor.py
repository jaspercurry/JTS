"""Unit tests for jasper.control.system_supervisor (T5.2).

Drives `_tick` directly with a probe/reboot trio overridden on a
subclass, sidestepping the `run()` sleep loop entirely. Pins the
policy contract:

  - 3 consecutive probe failures → exactly one reboot
  - Any single probe success in the window resets the counter
  - Rate limit blocks a second reboot in-window
  - Probe exception → counted as a failure
  - Probes evaluated in order (sshd → jasper_control → loadavg);
    first failure short-circuits and is named in `last_failed_probe`

A separate group exercises the snapshot() + start_supervisor() shape.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from jasper.control.system_supervisor import (
    SystemSupervisor,
    snapshot,
    start_supervisor,
)


# ---------- policy tests ----------


class _FakeSupervisor(SystemSupervisor):
    """Drives `_tick` with scripted probe + reboot outcomes."""

    def __init__(self, **kw) -> None:
        super().__init__(
            interval_sec=0.0,
            jitter_sec=0.0,
            cold_start_sec=0.0,
            **kw,
        )
        # Each entry is a tuple (sshd, control, loadavg) for one tick.
        # An entry of None means "all probes pass" (shortcut).
        # An entry of (a, b, c) lets you set each individually; if any
        # value is a BaseException, that probe raises.
        self.probe_results: list = []
        self.reboot_calls = 0
        self.now: float = 0.0

    def _pop_results(self) -> tuple:
        if not self.probe_results:
            raise AssertionError(
                "_FakeSupervisor.probe_results exhausted — test "
                "under-scripted; each _tick consumes one entry"
            )
        result = self.probe_results.pop(0)
        if result is None:
            return (True, True, True)
        return result

    async def probe_sshd(self) -> bool:
        # Lazy: only pop on the first probe of a tick. We model this
        # by treating each tick's tuple as the source for all 3.
        self._current_tick = self._pop_results()
        v = self._current_tick[0]
        if isinstance(v, BaseException):
            raise v
        return v

    async def probe_jasper_control(self) -> bool:
        v = self._current_tick[1]
        if isinstance(v, BaseException):
            raise v
        return v

    async def probe_loadavg(self) -> bool:
        v = self._current_tick[2]
        if isinstance(v, BaseException):
            raise v
        return v

    async def reboot_system(self) -> None:
        self.reboot_calls += 1

    def _now(self) -> float:
        return self.now


async def test_all_probes_pass_keeps_counter_zero():
    sup = _FakeSupervisor()
    sup.probe_results = [None, None, None]
    for _ in range(3):
        await sup._tick()
    assert sup.consecutive_failures == 0
    assert sup.reboot_calls == 0
    assert sup.last_probe_ok is True
    assert sup.last_failed_probe is None


async def test_three_consecutive_failures_trigger_one_reboot():
    """The whole point of T5.2: 3 failures in a row → clean reboot.
    Exactly one reboot per threshold crossing — not 3 separate ones."""
    sup = _FakeSupervisor()
    # Each tick: sshd fails, others would not be called (short-circuit).
    sup.probe_results = [(False, True, True)] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 1
    assert sup.consecutive_failures == 0   # reset after reboot
    assert sup.reboot_count == 1
    assert sup.last_reboot_at is not None


async def test_recovery_before_threshold_resets_counter():
    """Failure-failure-success pattern: no reboot."""
    sup = _FakeSupervisor()
    sup.probe_results = [
        (False, True, True),
        (False, True, True),
        (True, True, True),  # recovered
    ]
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 0
    assert sup.consecutive_failures == 0


async def test_failure_attribution_each_probe_type():
    """When sshd / jasper_control / loadavg fails individually, the
    failed_probe name is recorded so the operator can debug."""
    sup = _FakeSupervisor()
    sup.probe_results = [(False, True, True)]
    await sup._tick()
    assert sup.last_failed_probe == "sshd"

    sup.probe_results = [(True, False, True)]
    await sup._tick()
    assert sup.last_failed_probe == "jasper_control"

    sup.probe_results = [(True, True, False)]
    await sup._tick()
    assert sup.last_failed_probe == "loadavg"


async def test_probe_exception_counts_as_failure():
    """If a probe raises (network error, OS error), it counts as a
    failure but doesn't crash the supervisor."""
    sup = _FakeSupervisor()
    sup.probe_results = [(OSError("simulated"), True, True)] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 1


async def test_rate_limit_blocks_second_reboot_in_window():
    """After the first reboot, even sustained probe failures don't
    trigger a second one within the rate-limit window."""
    sup = _FakeSupervisor(rate_limit_sec=60.0)
    sup.probe_results = [(False, True, True)] * 6
    # First three: reboot
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 1
    # Next three (still in window): suppressed
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 1
    assert sup.suppressed_count >= 1


async def test_rate_limit_lifts_after_window():
    """Past the rate-limit window, a fresh failure storm CAN reboot."""
    sup = _FakeSupervisor(rate_limit_sec=10.0)
    sup.probe_results = [(False, True, True)] * 6
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 1
    # Advance time past the window
    sup.now += 11.0
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 2


async def test_single_success_during_failure_streak_does_not_trigger_reboot():
    """failure → failure → success → failure → failure: counter
    bounces and never reaches threshold."""
    sup = _FakeSupervisor()
    sup.probe_results = [
        (False, True, True),
        (False, True, True),
        (True, True, True),    # reset
        (False, True, True),
        (False, True, True),
    ]
    for _ in range(5):
        await sup._tick()
    assert sup.reboot_calls == 0


async def test_snapshot_returns_expected_fields():
    sup = _FakeSupervisor()
    sup.probe_results = [(False, True, True)]
    await sup._tick()
    snap = sup.snapshot()
    assert snap["enabled"] is True
    assert "consecutive_failures" in snap
    assert "reboot_count" in snap
    assert "last_failed_probe" in snap
    assert "last_probe_at" in snap
    assert "suppressed_count" in snap


# ---------- module-level start_supervisor / snapshot ----------


def test_snapshot_disabled_when_no_supervisor():
    """Module-level snapshot before start_supervisor() returns
    {enabled: False}."""
    # The module-level _supervisor singleton may have been set by
    # a prior test in the suite. Force a clean slate.
    import jasper.control.system_supervisor as mod
    saved = mod._supervisor
    saved_thread = mod._supervisor_thread
    mod._supervisor = None
    mod._supervisor_thread = None
    try:
        snap = snapshot()
        assert snap == {"enabled": False}
    finally:
        mod._supervisor = saved
        mod._supervisor_thread = saved_thread


def test_start_supervisor_respects_disabled_env():
    """Operator escape hatch: JASPER_SYSTEM_SUPERVISOR=disabled
    must turn the supervisor off without changing the deploy."""
    import jasper.control.system_supervisor as mod
    saved = mod._supervisor
    saved_thread = mod._supervisor_thread
    mod._supervisor = None
    mod._supervisor_thread = None
    try:
        with patch.dict(os.environ,
                        {"JASPER_SYSTEM_SUPERVISOR": "disabled"}):
            result = start_supervisor()
        assert result is None
        assert mod._supervisor is None
    finally:
        mod._supervisor = saved
        mod._supervisor_thread = saved_thread


def test_start_supervisor_idempotent():
    """Calling start_supervisor twice doesn't spawn a second thread."""
    import jasper.control.system_supervisor as mod
    saved = mod._supervisor
    saved_thread = mod._supervisor_thread
    mod._supervisor = None
    mod._supervisor_thread = None
    try:
        # Replace the asyncio loop runner with a no-op so we don't
        # actually start a real loop.
        with patch.object(SystemSupervisor, "run") as mock_run:
            async def noop():
                await asyncio.sleep(0)
            mock_run.return_value = noop()
            with patch.dict(os.environ,
                            {"JASPER_SYSTEM_SUPERVISOR": "auto"}):
                t1 = start_supervisor()
                t2 = start_supervisor()
        assert t1 is t2   # same thread object on second call
    finally:
        mod._supervisor = saved
        mod._supervisor_thread = saved_thread


def test_start_supervisor_unrecognised_value_falls_back_to_auto():
    """JASPER_SYSTEM_SUPERVISOR=on (or other unrecognised value) →
    starts anyway with a warning. Same pattern as ShairportSupervisor.
    Without this, a typo in the env file would silently disable
    protection."""
    import jasper.control.system_supervisor as mod
    saved = mod._supervisor
    saved_thread = mod._supervisor_thread
    mod._supervisor = None
    mod._supervisor_thread = None
    try:
        with patch.object(SystemSupervisor, "run") as mock_run:
            async def noop():
                await asyncio.sleep(0)
            mock_run.return_value = noop()
            with patch.dict(os.environ,
                            {"JASPER_SYSTEM_SUPERVISOR": "on"}):
                t = start_supervisor()
        assert t is not None   # started anyway
        assert mod._supervisor is not None
    finally:
        mod._supervisor = saved
        mod._supervisor_thread = saved_thread


# ---------- /proc/loadavg probe ----------


@pytest.mark.asyncio
async def test_probe_loadavg_succeeds_in_normal_conditions():
    """Sanity check: on a healthy host /proc/loadavg reads quickly
    and the probe returns True. (Mocked path for non-Linux dev hosts.)"""
    sup = SystemSupervisor()
    # On macOS dev hosts /proc/loadavg doesn't exist; mock the
    # synchronous reader to return a sane string.
    with patch("jasper.control.system_supervisor._read_loadavg",
               return_value="0.50 0.40 0.30 1/100 1\n"):
        result = await sup.probe_loadavg()
    assert result is True
