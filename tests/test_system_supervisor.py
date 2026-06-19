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
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from jasper.control.system_supervisor import (
    SystemSupervisor,
    _control_health_response_alive,
    _read_reboot_state,
    snapshot,
    start_supervisor,
)


# ---------- policy tests ----------


class _FakeSupervisor(SystemSupervisor):
    """Drives `_tick` with scripted probe + reboot outcomes."""

    def __init__(self, **kw) -> None:
        # Never touch the real /var/lib/jasper from a unit test. A unique
        # temp path per instance keeps reboot-persistence isolated and lets
        # a fresh instance simulate a post-reboot process reading the same
        # file (cross-instance persistence test).
        kw.setdefault(
            "reboot_state_path",
            Path(tempfile.gettempdir())
            / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json",
        )
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
        self.sshd_probe_enabled = True
        self.sshd_probe_calls = 0
        self._current_tick = None

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

    async def _run_all_probes(self) -> tuple[bool, str | None]:
        self._current_tick = self._pop_results()
        try:
            return await super()._run_all_probes()
        finally:
            self._current_tick = None

    async def should_probe_sshd(self) -> bool:
        return self.sshd_probe_enabled

    async def probe_sshd(self) -> bool:
        self.sshd_probe_calls += 1
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


async def test_disabled_sshd_probe_skips_to_other_liveness_checks():
    """A speaker with sshd disabled is healthy if the other probes pass.
    The sshd probe must not consume the failure budget or reboot the box."""
    sup = _FakeSupervisor()
    sup.sshd_probe_enabled = False
    sup.probe_results = [(False, True, True)] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.sshd_probe_calls == 0
    assert sup.reboot_calls == 0
    assert sup.consecutive_failures == 0
    assert sup.last_failed_probe is None


async def test_disabled_sshd_probe_still_reboots_on_other_probe_failure():
    """Skipping sshd is not disabling the supervisor: jasper-control or
    loadavg failures still trip the same threshold."""
    sup = _FakeSupervisor()
    sup.sshd_probe_enabled = False
    sup.probe_results = [(True, False, True)] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.sshd_probe_calls == 0
    assert sup.last_failed_probe == "jasper_control"
    assert sup.reboot_calls == 1


async def test_sshd_disabled_policy_logs_single_skip_breadcrumb(caplog):
    class _DisabledSshdPolicy(SystemSupervisor):
        async def _detect_sshd_probe_enabled(self) -> tuple[bool, str | None]:
            return False, "ssh.service:disabled,sshd.service:not-found"

    sup = _DisabledSshdPolicy(
        interval_sec=0.0,
        jitter_sec=0.0,
        cold_start_sec=0.0,
        reboot_state_path=(
            Path(tempfile.gettempdir())
            / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
        ),
    )
    with caplog.at_level("INFO", logger="jasper.control.system_supervisor"):
        assert await sup.should_probe_sshd() is False
        assert await sup.should_probe_sshd() is False
    skipped = [
        r for r in caplog.records
        if "event=system_supervisor.sshd_probe_skipped" in r.getMessage()
    ]
    assert len(skipped) == 1
    assert "ssh.service:disabled,sshd.service:not-found" in skipped[0].getMessage()


async def test_sshd_policy_skips_when_common_units_disabled_or_missing():
    class _StatusPolicy(SystemSupervisor):
        async def _systemctl_is_enabled(self, unit: str) -> str:
            return {
                "ssh.service": "disabled",
                "sshd.service": "not-found",
            }[unit]

    sup = _StatusPolicy(
        interval_sec=0.0,
        jitter_sec=0.0,
        cold_start_sec=0.0,
        reboot_state_path=(
            Path(tempfile.gettempdir())
            / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
        ),
    )
    enabled, reason = await sup._detect_sshd_probe_enabled()
    assert enabled is False
    assert reason == "ssh.service:disabled,sshd.service:not-found"


async def test_sshd_port_zero_env_disables_only_sshd_probe(caplog):
    class _PortZeroSupervisor(SystemSupervisor):
        def __init__(self) -> None:
            super().__init__(
                interval_sec=0.0,
                jitter_sec=0.0,
                cold_start_sec=0.0,
                reboot_state_path=(
                    Path(tempfile.gettempdir())
                    / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
                ),
            )
            self.sshd_probe_calls = 0

        async def probe_sshd(self) -> bool:
            self.sshd_probe_calls += 1
            return False

        async def probe_jasper_control(self) -> bool:
            return True

        async def probe_loadavg(self) -> bool:
            return True

    with patch.dict(os.environ, {"JASPER_SYSTEM_SUPERVISOR_SSHD_PORT": "0"}):
        sup = _PortZeroSupervisor()
    with caplog.at_level("INFO", logger="jasper.control.system_supervisor"):
        await sup._tick()
    assert sup.sshd_probe_calls == 0
    assert sup.last_probe_ok is True
    assert any(
        "event=system_supervisor.sshd_probe_skipped reason=port_disabled"
        in r.getMessage()
        for r in caplog.records
    )


async def test_probe_exception_counts_as_failure():
    """If a probe raises (network error, OS error), it counts as a
    failure but doesn't crash the supervisor."""
    sup = _FakeSupervisor()
    sup.probe_results = [(OSError("simulated"), True, True)] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 1


def test_control_health_overload_response_counts_as_alive():
    """429 from jasper-control's admission gate is alive-but-shedding.

    Counting it as dead would let the concurrency cap trigger the T5.2
    reboot path under a diagnostics/read burst.
    """
    assert _control_health_response_alive(b"HTTP/1.1 200 OK\r\n") is True
    assert (
        _control_health_response_alive(
            b"HTTP/1.1 429 Too Many Requests\r\n",
        )
        is True
    )
    assert _control_health_response_alive(b"HTTP/1.1 503 Unavailable\r\n") is False


def test_resilience_doc_mentions_control_overload_probe_semantics():
    """The operator-facing T5.2 doc must match the 429 liveness contract."""
    doc = (Path(__file__).resolve().parents[1] / "docs/HANDOFF-resilience.md")
    text = doc.read_text(encoding="utf-8")
    assert "429 Too Many Requests" in text
    assert "alive-but-shedding" in text


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


async def test_reboot_window_survives_simulated_reboot_via_persisted_state():
    """The T5.2 fix: a SECOND supervisor instance constructed after a
    simulated reboot still suppresses a reboot inside the 24h window,
    because the last-reboot wall-clock time is persisted to disk and
    loaded on construction. Without persistence this is the infinite
    reboot-loop bug — the fresh process forgets it just rebooted."""
    state_path = (
        Path(tempfile.gettempdir())
        / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
    )
    # First "boot": the supervisor wedges and reboots at wall-clock t=1000.
    sup1 = _FakeSupervisor(rate_limit_sec=86_400.0, reboot_state_path=state_path)
    sup1.now = 1000.0
    sup1.probe_results = [(False, True, True)] * 3
    for _ in range(3):
        await sup1._tick()
    assert sup1.reboot_calls == 1
    assert _read_reboot_state(state_path) == 1000.0  # persisted

    # Second "boot" a few minutes later (CLOCK_MONOTONIC would have reset
    # to ~0; wall-clock has advanced). A brand-new instance reads the
    # persisted time on construction and must still be inside the window.
    sup2 = _FakeSupervisor(rate_limit_sec=86_400.0, reboot_state_path=state_path)
    assert sup2.last_reboot_at == 1000.0  # loaded from disk on construction
    sup2.now = 1000.0 + 210.0  # ~3.5 min later — well inside 24h
    sup2.probe_results = [(False, True, True)] * 3
    for _ in range(3):
        await sup2._tick()
    assert sup2.reboot_calls == 0           # SUPPRESSED across the reboot
    assert sup2.suppressed_count >= 1


async def test_reboot_window_persists_across_instance_past_window_allows_reboot():
    """Counterpart to the persistence test: once the persisted last-reboot
    time is older than the window, a fresh instance is allowed to reboot —
    persistence rate-limits, it doesn't permanently wedge recovery."""
    state_path = (
        Path(tempfile.gettempdir())
        / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
    )
    sup1 = _FakeSupervisor(rate_limit_sec=86_400.0, reboot_state_path=state_path)
    sup1.now = 1000.0
    sup1.probe_results = [(False, True, True)] * 3
    for _ in range(3):
        await sup1._tick()
    assert sup1.reboot_calls == 1

    sup2 = _FakeSupervisor(rate_limit_sec=86_400.0, reboot_state_path=state_path)
    sup2.now = 1000.0 + 86_400.0 + 1.0   # just past the 24h window
    sup2.probe_results = [(False, True, True)] * 3
    for _ in range(3):
        await sup2._tick()
    assert sup2.reboot_calls == 1


def test_restored_reboot_state_logs_breadcrumb(caplog):
    """When construction loads a persisted last-reboot time, the supervisor
    emits exactly one info breadcrumb so an operator reading the journal can
    see the process just came back from a supervisor-driven reboot. Mirrors
    the mic-mute "restored from /var/lib/jasper/..." startup-log idiom."""
    state_path = (
        Path(tempfile.gettempdir())
        / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
    )
    # A recent persisted reboot (now minus a small offset) so the logged age
    # is a small, sane non-negative number against the real wall clock.
    reboot_at = round(time.time() - 42.0)
    state_path.write_text(
        '{"last_reboot_at": %d.0}' % reboot_at, encoding="utf-8",
    )

    with caplog.at_level("INFO", logger="jasper.control.system_supervisor"):
        sup = SystemSupervisor(reboot_state_path=state_path)
    assert sup.last_reboot_at == float(reboot_at)
    restored = [
        r for r in caplog.records
        if "event=system_supervisor.reboot_state_restored" in r.getMessage()
    ]
    assert len(restored) == 1
    msg = restored[0].getMessage()
    assert "last_reboot_at=%d" % reboot_at in msg
    assert "age=" in msg and "s" in msg.split("age=", 1)[1]


def test_no_persisted_reboot_state_logs_no_breadcrumb(caplog):
    """The breadcrumb fires only when a usable timestamp was restored. A
    fresh install (no state file) must not log a spurious restore line."""
    state_path = (
        Path(tempfile.gettempdir())
        / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
    )
    # File deliberately absent.
    with caplog.at_level("INFO", logger="jasper.control.system_supervisor"):
        sup = SystemSupervisor(reboot_state_path=state_path)
    assert sup.last_reboot_at is None
    assert not [
        r for r in caplog.records
        if "event=system_supervisor.reboot_state_restored" in r.getMessage()
    ]


@pytest.mark.parametrize("contents", [None, "{ not json", '{"last_reboot_at": "nope"}', "[]"])
async def test_corrupt_or_missing_reboot_state_fails_open(contents):
    """Fail-open safety: a missing, malformed, or wrong-shaped state file
    must NEVER block a genuinely-needed reboot. last_reboot_at loads as
    None and the very first threshold crossing is allowed to reboot."""
    state_path = (
        Path(tempfile.gettempdir())
        / f"jts-test-supervisor-reboot-{uuid.uuid4().hex}.json"
    )
    if contents is not None:
        state_path.write_text(contents, encoding="utf-8")
    # else: leave the file absent (missing-file case).
    sup = _FakeSupervisor(rate_limit_sec=86_400.0, reboot_state_path=state_path)
    assert sup.last_reboot_at is None       # failed open
    sup.now = 5000.0
    sup.probe_results = [(False, True, True)] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.reboot_calls == 1            # reboot NOT blocked


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
