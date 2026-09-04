# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Owner-scoped fan-in isolation and voice pause/resume contract."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import jasper.mux as mux_module
from jasper import measurement_window as coordinator
from jasper.measurement_window import (
    MeasurementWindowError,
    measurement_window,
)

from ._async_wait import wait_signalled

REAL_ACQUIRE_MEASUREMENT_GATE = coordinator._acquire_measurement_gate
REAL_RELEASE_MEASUREMENT_GATE = coordinator._release_measurement_gate


@pytest.fixture(autouse=True)
def _stub_measurement_gate(monkeypatch):
    async def acquire_gate() -> None:
        return None

    async def release_gate(**_kwargs) -> None:
        return None

    monkeypatch.setattr(
        coordinator,
        "_acquire_measurement_gate",
        acquire_gate,
    )
    monkeypatch.setattr(
        coordinator,
        "_release_measurement_gate",
        release_gate,
    )


@pytest.fixture(autouse=True)
def hold_calls(monkeypatch):
    """Stub the third lease's transport, and hand the log to any test.

    Unstubbed, every window in this file would POST at a real jasper-control on
    loopback — which is a wasted syscall on a laptop and an actual (brief) hold
    on somebody's speaker if the suite is run on a Pi. The hold's own behaviour
    is covered in tests/test_measurement_hold.py.
    """
    calls: list[tuple[str, dict]] = []

    async def fake_hold_command(path: str, body: dict) -> tuple[int, dict]:
        calls.append((path, body))
        return 200, {"measurement": {"active": True, "owner": body.get("owner")}}

    monkeypatch.setattr(coordinator, "_measurement_hold_command", fake_hold_command)
    return calls


async def test_skip_both_leaves_only_the_volume_hold(monkeypatch, hold_calls):
    """With voice and music isolation skipped, no UDS traffic happens.

    The volume hold is deliberately NOT skippable — no consumer wants a
    measurement that lets the host slider walk the fader, so it gets no flag —
    and it is the one thing this window still does here.
    """
    uds_calls: list[str] = []

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window(
        skip_voice_pause=True, skip_music_isolation=True,
    ):
        pass

    assert uds_calls == []
    assert [path for path, _ in hold_calls] == [
        "/measurement/hold", "/measurement/release",
    ]


async def test_pause_and_resume_voice(monkeypatch):
    uds_calls: list[str] = []

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window():
        pass

    assert "STATUS" in uds_calls
    assert "MEASURE_PAUSE" in uds_calls
    assert "MEASURE_RESUME" in uds_calls
    pause_idx = uds_calls.index("MEASURE_PAUSE")
    resume_idx = uds_calls.index("MEASURE_RESUME")
    assert pause_idx < resume_idx


async def test_long_window_renews_voice_measurement_lease(monkeypatch):
    """Human capture setup may outlast the voice daemon's auto-clear timer."""
    uds_calls: list[str] = []
    lease_renewed = asyncio.Event()

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE" and uds_calls.count("MEASURE_PAUSE") >= 2:
            lease_renewed.set()
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", 0.01)

    # The refresh loop is real (the interval above is just shrunk to keep the
    # test fast) — wait on the observable the assertion names (a 2nd
    # MEASURE_PAUSE, i.e. one lease renewal) instead of a fixed wall-clock
    # window racing that loop. That fixed-window shape is what flaked under
    # load in the sibling lease-refresh test (#1909): scheduling jitter could
    # close the window before the renewal landed. The bound here is a
    # hang-breaker, not a timing assertion — see tests/_async_wait.py.
    async with measurement_window(skip_music_isolation=True):
        await wait_signalled(lease_renewed, "voice measurement lease renewal")

    assert uds_calls.count("MEASURE_PAUSE") >= 2
    assert uds_calls[-1] == "MEASURE_RESUME"


async def test_lease_refresh_failure_retries_and_still_restores(monkeypatch):
    """A malformed/empty renewal cannot strand voice paused."""
    uds_calls: list[str] = []
    pause_calls = 0
    third_pause_call = asyncio.Event()

    async def fake_uds(path, cmd, **kw):
        nonlocal pause_calls
        uds_calls.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE":
            pause_calls += 1
            if pause_calls == 2:
                raise RuntimeError("empty response")
            if pause_calls >= 3:
                third_pause_call.set()
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", 0.01)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_RETRY_SEC", 0.005)

    # The retry loop is real (both delays above are just shrunk to keep the
    # test fast) — wait on the observable the test cares about (a 3rd
    # MEASURE_PAUSE, proving the 2nd call's failure was retried) instead of a
    # fixed wall-clock sleep racing that same loop. A fixed window flaked
    # under load (#1909): scheduling jitter could leave pause_calls at 2 when
    # the window closed and the lease-refresh task got cancelled. The bound
    # here is a hang-breaker, not a timing assertion — see tests/_async_wait.py.
    async with measurement_window(skip_music_isolation=True):
        await wait_signalled(third_pause_call, "third MEASURE_PAUSE retry call")

    assert pause_calls >= 3
    assert "MEASURE_RESUME" in uds_calls


async def test_measurement_gate_uses_mux_owned_diagnostic_selection(monkeypatch):
    command = AsyncMock(return_value={
        "active_source": "correction",
        "test_source": "correction",
        "test_owner": "correction-measurement",
    })
    monkeypatch.setattr(coordinator, "_mux_socket_command", command)

    await REAL_ACQUIRE_MEASUREMENT_GATE()

    command.assert_awaited_once_with(
        "TEST_SELECT correction correction-measurement",
        timeout=3.0,
    )


async def test_measurement_gate_threads_custom_owner_through_acquire_and_release(
    monkeypatch,
):
    replies = iter([
        {
            "active_source": "correction",
            "test_source": "correction",
            "test_owner": "doctor-aec-probe",
        },
        {"active_source": "idle", "test_source": None, "test_owner": None},
    ])
    commands: list[str] = []

    async def command(value, **_kwargs):
        commands.append(value)
        return next(replies)

    monkeypatch.setattr(coordinator, "_mux_socket_command", command)

    await REAL_ACQUIRE_MEASUREMENT_GATE(gate_owner="doctor-aec-probe")
    await REAL_RELEASE_MEASUREMENT_GATE(gate_owner="doctor-aec-probe")

    assert commands == [
        "TEST_SELECT correction doctor-aec-probe",
        "TEST_RELEASE doctor-aec-probe",
    ]


async def test_measurement_gate_refuses_unconfirmed_selection(monkeypatch):
    async def wrong_gate(*_args, **_kwargs):
        return {"active_source": "airplay", "test_source": None}

    monkeypatch.setattr(coordinator, "_mux_socket_command", wrong_gate)

    with pytest.raises(MeasurementWindowError, match="did not confirm"):
        await REAL_ACQUIRE_MEASUREMENT_GATE()


async def test_measurement_gate_release_retries_until_explicitly_clear(monkeypatch):
    replies = iter([
        {
            "active_source": "correction",
            "test_source": "correction",
            "test_owner": "correction-measurement",
        },
        {"active_source": "idle", "test_source": None, "test_owner": None},
    ])
    calls: list[str] = []

    async def command(value, **_kwargs):
        calls.append(value)
        return next(replies)

    monkeypatch.setattr(coordinator, "_mux_socket_command", command)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_RETRY_SEC", 0)

    await REAL_RELEASE_MEASUREMENT_GATE()

    assert calls == [
        "TEST_RELEASE correction-measurement",
        "TEST_RELEASE correction-measurement",
    ]


async def test_indeterminate_acquire_cleanup_never_releases_other_owner(monkeypatch):
    calls: list[str] = []

    async def command(value, **_kwargs):
        calls.append(value)
        if value == "STATUS":
            return {
                "active_source": "correction",
                "test_source": "correction",
                "test_owner": "active-speaker-commissioning",
            }
        raise RuntimeError("owned by active-speaker-commissioning")

    monkeypatch.setattr(coordinator, "_mux_socket_command", command)

    await REAL_RELEASE_MEASUREMENT_GATE(allow_other_owner=True)

    assert calls == ["TEST_RELEASE correction-measurement", "STATUS"]


async def test_custom_owner_indeterminate_cleanup_never_releases_foreign_owner(
    monkeypatch,
):
    calls: list[str] = []

    async def command(value, **_kwargs):
        calls.append(value)
        if value == "STATUS":
            return {
                "active_source": "correction",
                "test_source": "correction",
                "test_owner": "correction-measurement",
            }
        raise RuntimeError("response lost")

    monkeypatch.setattr(coordinator, "_mux_socket_command", command)

    await REAL_RELEASE_MEASUREMENT_GATE(
        gate_owner="doctor-aec-probe",
        allow_other_owner=True,
    )

    assert calls == ["TEST_RELEASE doctor-aec-probe", "STATUS"]


async def test_indeterminate_acquire_always_runs_owner_scoped_cleanup(monkeypatch):
    cleanup_modes: list[bool] = []

    async def acquire() -> None:
        raise MeasurementWindowError("response lost")

    async def release(*, allow_other_owner: bool) -> None:
        cleanup_modes.append(allow_other_owner)

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    with pytest.raises(MeasurementWindowError, match="response lost"):
        async with measurement_window(
            skip_voice_pause=True,
        ):
            pytest.fail("an indeterminate acquire must not open the window")

    assert cleanup_modes == [True]


async def test_long_window_renews_mux_gate_even_without_voice_pause(monkeypatch):
    gate_calls: list[str] = []
    gate_renewed = asyncio.Event()

    async def acquire() -> None:
        gate_calls.append("acquire")
        if gate_calls.count("acquire") >= 2:
            gate_renewed.set()

    async def release(**_kwargs) -> None:
        gate_calls.append("release")

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_REFRESH_SEC", 0.01)

    # Mux-gate twin of test_long_window_renews_voice_measurement_lease: wait on
    # the observable the assertion names (a 2nd acquire, i.e. one gate renewal)
    # rather than racing the real refresh loop with a fixed wall-clock window.
    # See that test and tests/_async_wait.py for the pattern, and #1918 for
    # this test's exposure: the shape #1909 recorded flaking on other tests,
    # never observed failing here.
    async with measurement_window(
        skip_voice_pause=True,
    ):
        await wait_signalled(gate_renewed, "mux measurement gate renewal")

    assert gate_calls.count("acquire") >= 2
    assert gate_calls[-1] == "release"


async def test_custom_owner_is_used_for_acquire_renew_and_release(monkeypatch):
    gate_calls: list[str] = []
    renewed = asyncio.Event()

    async def acquire(*, gate_owner):
        gate_calls.append(f"acquire:{gate_owner}")
        if gate_calls.count("acquire:doctor-aec-probe") >= 2:
            renewed.set()

    async def release(*, gate_owner, allow_other_owner):
        gate_calls.append(f"release:{gate_owner}:{allow_other_owner}")

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_REFRESH_SEC", 0.01)

    async with measurement_window(
        gate_owner="doctor-aec-probe",
        skip_voice_pause=True,
    ):
        await wait_signalled(renewed, "doctor mux lease renewal")

    assert gate_calls[:2] == [
        "acquire:doctor-aec-probe",
        "acquire:doctor-aec-probe",
    ]
    assert gate_calls[-1] == "release:doctor-aec-probe:False"


async def test_custom_owner_lost_acquire_runs_owner_scoped_cleanup(monkeypatch):
    releases: list[tuple[str, bool]] = []

    async def acquire(*, gate_owner):
        assert gate_owner == "doctor-aec-probe"
        raise MeasurementWindowError("response lost")

    async def release(*, gate_owner, allow_other_owner):
        releases.append((gate_owner, allow_other_owner))

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    with pytest.raises(MeasurementWindowError, match="response lost"):
        async with measurement_window(
            gate_owner="doctor-aec-probe",
            skip_voice_pause=True,
        ):
            pytest.fail("an indeterminate acquire must not open the window")

    assert releases == [("doctor-aec-probe", True)]


def test_mux_gate_abort_ladder_fires_before_mux_lease_expiry():
    """Pin the ladder's worst-case abort time, not the constants' ordering.

    ``_refresh_measurement_gate_lease`` only *checks* its deadline after an
    acquire attempt fails, so the abort lands on the first check at or past
    ``MEASUREMENT_GATE_ABORT_SEC`` — never exactly on it. Ordering alone
    therefore proves nothing: at ``MEASUREMENT_GATE_ABORT_SEC = 55`` the old
    assertion (``20 < 55 < 60``) stays green while the real abort lands only
    once mux has already reopened household music into a live sweep. The
    sibling test below reproduces exactly that — two of its schedules abort
    at 60.0 s and 61.0 s against a 60 s lease.

    Two invariants, both derived from the constants themselves:

    * **Recovery.** A first failed acquire must land under the deadline, so a
      transient mux blip gets at least one retry instead of killing the
      window outright.
    * **Safety.** The check before the aborting one was under the deadline by
      definition, and the step that follows costs at most one back-off plus
      one full mux round trip. So for *every* failure schedule the abort
      fires strictly before ``ABORT + RETRY + COMMAND_TIMEOUT`` — measured
      from the coordinator's ``last_confirmed``.

      That is not yet the property we need. mux starts its lease *before*
      it replies (``jasper/mux.py``: ``_test_fanin_expires_at`` is set ahead
      of ``_fanin_select_label``, deliberately, so a lost response still
      leaves a recoverable claim), while ``last_confirmed`` is stamped only
      once the reply lands. mux's lease is therefore already ageing by up to
      one round trip before the coordinator's clock starts. So the sum that
      has to clear the lease carries ``COMMAND_TIMEOUT`` **twice**: once for
      the acquire that carries the abort past the deadline, once for the
      renewal round trip that set the reference point. Budget only one and
      ``ABORT = 50`` stays green here while the real lease age reaches
      60.6 s against a 60 s lease.

    This test pins the arithmetic. Its sibling below,
    ``test_gate_abort_ladder_stays_inside_its_modelled_bound``, pins the other
    half — that the loop still behaves the way the arithmetic models it.
    """

    refresh = coordinator.MEASUREMENT_GATE_REFRESH_SEC
    abort = coordinator.MEASUREMENT_GATE_ABORT_SEC
    retry = coordinator.MEASUREMENT_LEASE_RETRY_SEC
    command = coordinator.MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC
    assert 0 < refresh
    assert 0 < retry
    assert 0 < command

    # Recovery: the first failed check lands under the deadline.
    assert refresh + command < abort

    # Safety: the latest possible abort still precedes mux reopening music.
    # Two COMMAND_TIMEOUTs: the aborting acquire, and the renewal round trip
    # during which mux's lease was already running (see the docstring).
    assert abort + retry + 2 * command < mux_module.FANIN_TEST_LEASE_SEC


def _simulate_gate_abort(monkeypatch, acquire_costs: tuple[float, ...]) -> float:
    """Run the REAL refresh loop on a virtual clock; return the abort instant.

    ``asyncio.sleep`` advances a modelled clock instead of sleeping and
    ``time.monotonic`` reads it, so the answer is the loop's own decision
    point in modelled seconds — exact, fast, and indifferent to how loaded
    the test host is. Every acquire after the entering one burns the next
    cost from ``acquire_costs`` (cycled) and then fails, which is how a mux
    round trip that consumes part or all of its deadline is expressed.

    **Only the GATE ladder's sleeps may move that clock.** One modelled clock
    is shared by every task in the process, so a concurrent lease's ordinary
    ``sleep`` would be *added* to the ladder's timeline and shift the very
    decision point being measured — real sleeps overlap, this harness's single
    counter makes them additive. The volume-hold lease renews on its own
    schedule and is deliberately un-gated on its first acquire (a window must
    keep retrying a restarting jasper-control), so it always has a live task
    here. It is excluded by identifying its coroutine at creation; if that
    coroutine is ever renamed, ``hold_task`` stays empty and the assertion
    below fails loudly rather than the ladder silently mis-measuring.
    """

    clock = {"t": 0.0}
    aborted: list[float] = []
    real_sleep = asyncio.sleep
    real_create_task = asyncio.create_task
    hold_task: list[asyncio.Task] = []

    def tracking_create_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        if getattr(coro, "__name__", "") == "_refresh_measurement_hold":
            hold_task.append(task)
        return task

    async def fake_sleep(delay, *args, **kwargs):
        if not hold_task or asyncio.current_task() is not hold_task[0]:
            clock["t"] += float(delay)
        await real_sleep(0)  # yield without advancing the modelled clock

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

    calls = {"n": 0}

    async def acquire() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            return  # the enter-path acquire succeeds; the lease starts here
        clock["t"] += acquire_costs[(calls["n"] - 2) % len(acquire_costs)]
        raise MeasurementWindowError("mux unavailable")

    async def release(**_kwargs) -> None:
        return None

    class _RecordingTarget(coordinator.MeasurementAbortTarget):
        def abort(self, fallback):
            aborted.append(clock["t"])
            super().abort(fallback)

    async def hold_unavailable(path: str, body: dict) -> tuple[int, dict]:
        # This simulation is about the MUX ladder; the volume hold is not part
        # of it. Its task still exists and still sleeps (see the docstring) —
        # `fake_sleep` is what keeps those sleeps off the ladder's clock.
        raise OSError("jasper-control not reachable in this simulation")

    monkeypatch.setattr(
        coordinator, "_measurement_hold_command", hold_unavailable,
    )
    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "time", _Clock)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(asyncio, "create_task", tracking_create_task)

    async def drive() -> None:
        never = asyncio.Event()  # only the abort ends the body
        async with measurement_window(
            skip_voice_pause=True, abort_target=_RecordingTarget(),
        ):
            await never.wait()

    with pytest.raises((MeasurementWindowError, asyncio.CancelledError)):
        asyncio.run(drive())

    assert hold_task, (
        "the volume-hold refresh coroutine was not recognised, so its sleeps "
        "were charged to the gate ladder's clock — rename in measurement_window.py?"
    )
    assert aborted, "the ladder never aborted"
    return aborted[0]


@pytest.mark.parametrize(
    # Fractions of MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC, not bare seconds, so
    # the schedules keep their meaning if that deadline is ever retuned.
    "cost_fractions",
    [
        (0.0,),                     # socket missing — every acquire fails fast
        (1 / 3,),
        (5 / 6,),
        (1.0,),                     # every acquire burns its whole deadline
        (0.0, 1.0),                 # alternating fast/slow
        (1.0, 1 / 6, 2 / 3, 0.0),   # mixed — no schedule may exceed the bound
    ],
)
def test_gate_abort_ladder_stays_inside_its_modelled_bound(
    monkeypatch, cost_fractions,
):
    """The loop must still behave the way the arithmetic pin models it.

    The sibling test above asserts ``ABORT + RETRY + COMMAND_TIMEOUT`` clears
    mux's lease. That bound is only worth anything while the loop keeps its
    current shape — one back-off plus one acquire between deadline checks.
    Add a step to the ladder and the arithmetic would stay green while the
    real abort slid later, which is the exact failure this pin exists to
    stop. So drive the real loop and check the modelled abort against both
    the bound and mux's lease, over several failure schedules including
    mixed fast/slow ones (the worst case is not always the all-slow one).
    """

    command = coordinator.MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC
    costs = tuple(fraction * command for fraction in cost_fractions)
    bound = (
        coordinator.MEASUREMENT_GATE_ABORT_SEC
        + coordinator.MEASUREMENT_LEASE_RETRY_SEC
        + command
    )

    abort_at = _simulate_gate_abort(monkeypatch, costs)

    assert abort_at >= coordinator.MEASUREMENT_GATE_ABORT_SEC
    assert abort_at < bound
    assert abort_at < mux_module.FANIN_TEST_LEASE_SEC


async def test_sustained_mux_renewal_failure_aborts_before_lease_expiry(monkeypatch):
    acquire_calls = 0
    released: list[bool] = []

    async def acquire() -> None:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls > 1:
            raise MeasurementWindowError("mux unavailable")

    async def release(**_kwargs) -> None:
        released.append(True)

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_REFRESH_SEC", 0.005)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_RETRY_SEC", 0.005)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_ABORT_SEC", 0.02)

    started = time.monotonic()
    with pytest.raises(MeasurementWindowError, match="could not be renewed"):
        async with measurement_window(skip_voice_pause=True):
            await asyncio.sleep(1.0)

    assert time.monotonic() - started < 0.5
    assert acquire_calls >= 2
    assert released == [True]


async def test_measurement_gate_wraps_body_without_source_process_churn(monkeypatch):
    """The one mux gate is the complete music-isolation boundary."""

    events: list[str] = []

    async def acquire() -> None:
        events.append("gate-acquire")

    async def release(**_kwargs) -> None:
        events.append("gate-release")

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    async with measurement_window(skip_voice_pause=True):
        events.append("body")

    assert events == [
        "gate-acquire",
        "body",
        "gate-release",
    ]


async def test_gate_release_failure_surfaces(monkeypatch):
    async def release(**_kwargs) -> None:
        raise MeasurementWindowError("gate stuck")

    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    with pytest.raises(MeasurementWindowError, match="gate stuck"):
        async with measurement_window(skip_voice_pause=True):
            pass


async def test_measurement_releases_mux_gate_after_body_exception(monkeypatch):
    restored: list[bool] = []

    async def acquire() -> None:
        return None

    async def release(**_kwargs) -> None:
        restored.append(True)

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    with pytest.raises(RuntimeError, match="boom"):
        async with measurement_window(
            skip_voice_pause=True,
        ):
            raise RuntimeError("boom")

    assert restored == [True]


async def test_resume_runs_even_on_exception(monkeypatch):
    """The whole point of the finally clause: a crash inside the
    measurement should not leave the speaker silent."""
    uds_calls: list[str] = []

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    with pytest.raises(RuntimeError, match="boom"):
        async with measurement_window():
            raise RuntimeError("boom")

    assert "MEASURE_RESUME" in uds_calls


async def test_active_voice_session_blocks_window(monkeypatch):
    """Refuse to start a measurement if a voice session is active —
    yanking it would orphan the user's turn."""
    async def fake_uds(path, cmd, **kw):
        if cmd == "STATUS":
            return {"state": "SESSION", "spend_allowed": True}
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    with pytest.raises(MeasurementWindowError, match="Voice session"):
        async with measurement_window():
            pass

async def test_voice_daemon_unreachable_is_tolerated(monkeypatch):
    """If voice_daemon is not running, that means there's no session
    to interrupt and no WakeLoop to pause. The mux-isolated window opens."""

    async def fake_uds(path, cmd, **kw):
        raise FileNotFoundError("no voice daemon")

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window():
        pass


@pytest.mark.parametrize(
    "status",
    [
        FileNotFoundError("no voice daemon"),
        RuntimeError("empty response"),
        [],
        {},
        {"state": "UNKNOWN"},
        {"state": "SESSION"},
    ],
    ids=["unreachable", "malformed", "nonmapping", "missing", "unknown", "session"],
)
async def test_strict_voice_status_fails_before_mux_acquire(monkeypatch, status):
    acquired: list[bool] = []

    async def fake_uds(_path, cmd, **_kwargs):
        assert cmd == "STATUS"
        if isinstance(status, Exception):
            raise status
        return status

    async def acquire(*, gate_owner):
        acquired.append(True)

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)

    with pytest.raises(MeasurementWindowError):
        async with measurement_window(
            gate_owner="doctor-aec-probe",
            require_voice_pause=True,
        ):
            pytest.fail("strict STATUS failure must not open the window")

    assert acquired == []


@pytest.mark.parametrize(
    "pause_reply",
    [
        FileNotFoundError("no voice daemon"),
        RuntimeError("empty response"),
        [],
        {},
        {"result": "BUSY"},
        {"result": "ok"},
    ],
    ids=[
        "unreachable", "malformed", "nonmapping", "missing", "busy",
        "old-daemon-missing-drain-proof",
    ],
)
async def test_strict_pause_failure_resumes_and_releases_exact_owner(
    monkeypatch, pause_reply
):
    events: list[str] = []

    async def fake_uds(_path, cmd, **_kwargs):
        events.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE":
            if isinstance(pause_reply, Exception):
                raise pause_reply
            return pause_reply
        return {"result": "ok"}

    async def acquire(*, gate_owner):
        events.append(f"acquire:{gate_owner}")

    async def release(*, gate_owner, allow_other_owner):
        events.append(f"release:{gate_owner}:{allow_other_owner}")

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    with pytest.raises(MeasurementWindowError):
        async with measurement_window(
            gate_owner="doctor-aec-probe",
            require_voice_pause=True,
        ):
            pytest.fail("strict PAUSE failure must not open the window")

    assert events == [
        "STATUS",
        "acquire:doctor-aec-probe",
        "MEASURE_PAUSE",
        "MEASURE_RESUME",
        "release:doctor-aec-probe:False",
    ]


async def test_permissive_window_owns_cleanup_after_voice_drain_timeout(
    monkeypatch,
):
    calls: list[str] = []

    async def fake_uds(_path, cmd, **_kwargs):
        calls.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE":
            return {"result": "ok", "drained": False}
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window():
        calls.append("body")

    assert calls == [
        "STATUS",
        "MEASURE_PAUSE",
        "body",
        "MEASURE_RESUME",
    ]


async def test_permissive_window_accepts_old_daemon_pause_reply(monkeypatch):
    calls: list[str] = []

    async def fake_uds(_path, cmd, **_kwargs):
        calls.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window():
        calls.append("body")

    assert calls == ["STATUS", "MEASURE_PAUSE", "body", "MEASURE_RESUME"]


async def test_strict_window_orders_isolation_around_body(monkeypatch):
    events: list[str] = []

    async def fake_uds(_path, cmd, **_kwargs):
        events.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE":
            return {"result": "ok", "drained": True}
        return {"result": "ok"}

    async def acquire(*, gate_owner):
        events.append(f"acquire:{gate_owner}")

    async def release(*, gate_owner, allow_other_owner):
        events.append(f"release:{gate_owner}:{allow_other_owner}")

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    async with measurement_window(
        gate_owner="doctor-aec-probe",
        require_voice_pause=True,
    ):
        events.append("body")

    assert events == [
        "STATUS",
        "acquire:doctor-aec-probe",
        "MEASURE_PAUSE",
        "body",
        "MEASURE_RESUME",
        "release:doctor-aec-probe:False",
    ]


async def test_strict_voice_renewal_failure_aborts_and_restores(monkeypatch):
    events: list[str] = []
    pause_calls = 0

    async def fake_uds(_path, cmd, **_kwargs):
        nonlocal pause_calls
        events.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE":
            pause_calls += 1
            if pause_calls > 1:
                raise RuntimeError("renewal lost")
            return {"result": "ok", "drained": True}
        return {"result": "ok"}

    async def acquire(*, gate_owner):
        events.append(f"acquire:{gate_owner}")

    async def release(*, gate_owner, allow_other_owner):
        events.append(f"release:{gate_owner}:{allow_other_owner}")

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", 0.005)

    with pytest.raises(MeasurementWindowError, match="Voice isolation"):
        async with measurement_window(
            gate_owner="doctor-aec-probe",
            require_voice_pause=True,
        ):
            await asyncio.sleep(1.0)

    assert pause_calls >= 2
    assert "MEASURE_RESUME" in events
    assert "release:doctor-aec-probe:False" in events


async def test_strict_window_cancellation_restores_voice_and_mux(monkeypatch):
    events: list[str] = []
    entered = asyncio.Event()

    async def fake_uds(_path, cmd, **_kwargs):
        events.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE":
            return {"result": "ok", "drained": True}
        return {"result": "ok"}

    async def acquire(*, gate_owner):
        events.append(f"acquire:{gate_owner}")

    async def release(*, gate_owner, allow_other_owner):
        events.append(f"release:{gate_owner}:{allow_other_owner}")

    async def hold_window():
        async with measurement_window(
            gate_owner="doctor-aec-probe",
            require_voice_pause=True,
        ):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    task = asyncio.create_task(hold_window())
    await wait_signalled(entered, "strict window entry", producer=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[-2:] == [
        "MEASURE_RESUME",
        "release:doctor-aec-probe:False",
    ]


async def test_concurrent_measurement_window_is_rejected(monkeypatch):
    """Only one window may be open. A second concurrent window would let
    whichever exits first send MEASURE_RESUME + release the mux gate while the
    other is still measuring, corrupting its capture. The second entry fails
    fast; the flag is released when the first closes."""
    monkeypatch.setattr(coordinator, "_window_active", False)  # clean slate

    async with measurement_window(skip_voice_pause=True, skip_music_isolation=True):
        with pytest.raises(MeasurementWindowError, match="already in progress"):
            async with measurement_window(
                skip_voice_pause=True, skip_music_isolation=True,
            ):
                pass

    # Flag released after the outer window closed — a later window opens fine.
    assert coordinator._window_active is False
    async with measurement_window(skip_voice_pause=True, skip_music_isolation=True):
        pass


async def test_window_flag_released_when_precondition_fails(monkeypatch):
    """A precondition failure (active voice session) must clear the window
    flag, or every later measurement would falsely report 'already in
    progress'."""
    monkeypatch.setattr(coordinator, "_window_active", False)

    async def fake_uds(path, cmd, **kw):
        return {"state": "SESSION"}  # active voice session

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    with pytest.raises(MeasurementWindowError, match="Voice session"):
        async with measurement_window(skip_music_isolation=True):
            pass
    assert coordinator._window_active is False


async def test_window_flag_released_even_if_gate_release_raises(monkeypatch):
    """A failed gate release must not wedge the in-process mutex."""
    monkeypatch.setattr(coordinator, "_window_active", False)

    async def release(**_kwargs):
        raise MeasurementWindowError("gate stuck")

    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    with pytest.raises(MeasurementWindowError, match="gate stuck"):
        async with measurement_window(skip_voice_pause=True):
            pass
    assert coordinator._window_active is False


async def test_window_b_blocked_while_window_a_restore_in_flight(monkeypatch):
    """The mutex stays held until window A's mux-gate release completes."""
    monkeypatch.setattr(coordinator, "_window_active", False)
    entered_restore = asyncio.Event()
    release = asyncio.Event()

    async def slow_gate_release(**_kwargs):
        entered_restore.set()
        await release.wait()

    monkeypatch.setattr(coordinator, "_release_measurement_gate", slow_gate_release)

    async def window_a():
        async with measurement_window(skip_voice_pause=True):
            pass

    task_a = asyncio.create_task(window_a())
    await wait_signalled(entered_restore, "window A mux-gate release entered", producer=task_a)

    # B must be refused while A's restore is still in flight.
    with pytest.raises(MeasurementWindowError, match="already in progress"):
        async with measurement_window(
            skip_voice_pause=True, skip_music_isolation=True,
        ):
            pass

    release.set()
    await task_a
    assert coordinator._window_active is False


async def test_sustained_renewal_failure_aborts_via_registered_target(monkeypatch):
    """W6.1 gate should-fix: with an abort_target (a held session window), the
    isolation-loss abort cancels the REGISTERED play task — not the task that
    entered the window (the long-lived session runner, whose cancel would not
    stop an in-flight sweep) — and latches ``failed`` for the holder."""
    acquire_calls = 0

    async def acquire() -> None:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls > 1:
            raise MeasurementWindowError("mux unavailable")

    async def release(**_kwargs) -> None:
        return None

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_REFRESH_SEC", 0.005)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_RETRY_SEC", 0.005)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_ABORT_SEC", 0.02)

    target = coordinator.MeasurementAbortTarget()
    play_cancelled: list[bool] = []

    with pytest.raises(MeasurementWindowError, match="could not be renewed"):
        async with measurement_window(skip_voice_pause=True, abort_target=target):
            play = asyncio.create_task(asyncio.sleep(30.0))
            target.register(play)
            try:
                await play
            except asyncio.CancelledError:
                play_cancelled.append(True)
            finally:
                target.clear()

    # The PLAY task was cancelled (the session/entering task kept running to a
    # clean window exit — this test body IS that task and reached here), and
    # the latch tells the holder to refuse the next play.
    assert play_cancelled == [True]
    assert target.failed is True


async def test_abort_target_falls_back_to_entering_task_when_none_registered(monkeypatch,
):
    """Between plays (nothing registered) the abort still cancels the entering
    task — the pre-existing behavior — in addition to latching ``failed``."""
    acquire_calls = 0

    async def acquire() -> None:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls > 1:
            raise MeasurementWindowError("mux unavailable")

    async def release(**_kwargs) -> None:
        return None

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_REFRESH_SEC", 0.005)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_RETRY_SEC", 0.005)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_ABORT_SEC", 0.02)

    target = coordinator.MeasurementAbortTarget()
    with pytest.raises(MeasurementWindowError, match="could not be renewed"):
        async with measurement_window(skip_voice_pause=True, abort_target=target):
            await asyncio.sleep(30.0)  # entering task parked; nothing registered

    assert target.failed is True


class _PendingVoiceReader:
    """Reader whose readline() blocks on a future the test controls.

    Lets the cancellation-race test below resolve voice_daemon's reply at
    an exact, test-chosen event-loop tick instead of immediately -- the
    other tests in this file mock ``coordinator._voice_uds_command``
    itself, which bypasses the real function's UDS I/O (and therefore its
    ``asyncio.timeout`` call) entirely.
    """

    def __init__(self, reply: "asyncio.Future[bytes]") -> None:
        self._reply = reply

    async def readline(self) -> bytes:
        return await self._reply


class _NoopVoiceWriter:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


async def test_voice_uds_command_answers_cancellation_racing_the_reply(monkeypatch):
    """_voice_uds_command must terminate its caller when cancelled, even
    when voice_daemon's reply lands in the very same event-loop tick as
    the cancellation.

    Regression for #1952 (the #1935 class). CPython <= 3.11's
    asyncio.wait_for swallows a CancelledError that arrives in the tick its
    awaited future completes (Lib/asyncio/tasks.py: ``except
    CancelledError: if fut.done(): return fut.result()``). This call sits
    on _refresh_voice_lease's cancellation-only ``while True:`` body path
    (see the closure inside measurement_window() above), which
    measurement_window()'s finally cancels and then awaits unboundedly --
    a swallowed cancel here makes that task immortal and wedges
    MEASURE_RESUME / the mux-gate release / the module-level re-entrancy
    mutex for the life of the process.

    The race is constructed deterministically, not sampled: resolve the
    reply future and cancel() the task with no intervening await, so both
    wake-ups queue in the same event-loop tick. Mirrors
    test_mux.py::test_run_answers_cancellation_racing_a_wake_alert (#1935).
    """
    loop = asyncio.get_running_loop()
    reply: asyncio.Future[bytes] = loop.create_future()

    async def fake_open_unix_connection(_path):
        return _PendingVoiceReader(reply), _NoopVoiceWriter()

    monkeypatch.setattr(
        coordinator.asyncio, "open_unix_connection", fake_open_unix_connection,
    )

    task = asyncio.create_task(
        coordinator._voice_uds_command(
            "/tmp/voice.sock", "MEASURE_PAUSE", timeout=30.0,
        )
    )
    # Let the task open the (fake) connection, write, and park inside the
    # bounded wait for the reply before racing it. Measured empirically for
    # this exact call shape on 3.11.15: offset 0 never swallows (the
    # wrapped readline() task hasn't run its first step yet); offsets 1-5
    # swallow 100/100 on the pre-fix code. This is well inside that window.
    for _ in range(3):
        await asyncio.sleep(0)

    reply.set_result(b'{"result": "ok"}\n')
    task.cancel()

    done, pending = await asyncio.wait({task}, timeout=10.0)
    assert not pending, (
        "_voice_uds_command ignored cancellation and is still running -- a "
        "swallowed CancelledError makes _refresh_voice_lease's task "
        "immortal and wedges measurement_window() teardown (#1952)"
    )
    assert task.cancelled()


def test_every_deferred_relative_import_resolves():
    """The jasper-control reads are deferred, so nothing executes them here.

    A relative level means a different package at a different module depth, and
    every other test in this file stubs `_measurement_hold_command` out, so a
    wrong level survives the suite and fails first on hardware, inside
    `measurement_window()`'s entry, as an `ImportError` no caller classifies.
    """
    import ast
    import importlib.util
    from pathlib import Path

    source = Path(coordinator.__file__)
    package = coordinator.__name__.rpartition(".")[0]
    unresolved = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        base = package.split(".")[: len(package.split(".")) - node.level + 1]
        dotted = ".".join([*base, *([node.module] if node.module else [])])
        try:
            found = importlib.util.find_spec(dotted) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            unresolved.append(f"line {node.lineno}: {dotted}")

    assert not unresolved, (
        "relative import does not resolve from this module's depth: "
        + "; ".join(unresolved)
    )
