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
from jasper.correction import coordinator
from jasper.correction.coordinator import (
    MeasurementWindowError,
    measurement_window,
)

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


@pytest.mark.asyncio
async def test_skip_both_is_noop(monkeypatch):
    """With voice and music isolation skipped, enter/exit does no I/O."""
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_long_window_renews_voice_measurement_lease(monkeypatch):
    """Human relay setup may exceed the voice daemon's 120 s safety timer."""
    uds_calls: list[str] = []

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", 0.01)

    async with measurement_window(skip_music_isolation=True):
        await asyncio.sleep(0.035)

    assert uds_calls.count("MEASURE_PAUSE") >= 2
    assert uds_calls[-1] == "MEASURE_RESUME"


@pytest.mark.asyncio
async def test_lease_refresh_failure_retries_and_still_restores(monkeypatch):
    """A malformed/empty renewal cannot strand voice paused."""
    uds_calls: list[str] = []
    pause_calls = 0

    async def fake_uds(path, cmd, **kw):
        nonlocal pause_calls
        uds_calls.append(cmd)
        if cmd == "STATUS":
            return {"state": "WAKE"}
        if cmd == "MEASURE_PAUSE":
            pause_calls += 1
            if pause_calls == 2:
                raise RuntimeError("empty response")
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", 0.01)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_RETRY_SEC", 0.005)

    async with measurement_window(skip_music_isolation=True):
        await asyncio.sleep(0.03)

    assert pause_calls >= 3
    assert "MEASURE_RESUME" in uds_calls


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_measurement_gate_refuses_unconfirmed_selection(monkeypatch):
    async def wrong_gate(*_args, **_kwargs):
        return {"active_source": "airplay", "test_source": None}

    monkeypatch.setattr(coordinator, "_mux_socket_command", wrong_gate)

    with pytest.raises(MeasurementWindowError, match="did not confirm"):
        await REAL_ACQUIRE_MEASUREMENT_GATE()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_long_window_renews_mux_gate_even_without_voice_pause(monkeypatch):
    gate_calls: list[str] = []

    async def acquire() -> None:
        gate_calls.append("acquire")

    async def release(**_kwargs) -> None:
        gate_calls.append("release")

    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", acquire)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_REFRESH_SEC", 0.01)

    async with measurement_window(
        skip_voice_pause=True,
    ):
        await asyncio.sleep(0.035)

    assert gate_calls.count("acquire") >= 2
    assert gate_calls[-1] == "release"


def test_mux_gate_refresh_deadline_precedes_mux_lease_expiry():
    assert 0 < coordinator.MEASUREMENT_GATE_REFRESH_SEC
    assert (
        coordinator.MEASUREMENT_GATE_REFRESH_SEC
        < coordinator.MEASUREMENT_GATE_ABORT_SEC
        < mux_module.FANIN_TEST_LEASE_SEC
    )


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_gate_release_failure_surfaces(monkeypatch):
    async def release(**_kwargs) -> None:
        raise MeasurementWindowError("gate stuck")

    monkeypatch.setattr(coordinator, "_release_measurement_gate", release)

    with pytest.raises(MeasurementWindowError, match="gate stuck"):
        async with measurement_window(skip_voice_pause=True):
            pass


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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

@pytest.mark.asyncio
async def test_voice_daemon_unreachable_is_tolerated(monkeypatch):
    """If voice_daemon is not running, that means there's no session
    to interrupt and no WakeLoop to pause. The mux-isolated window opens."""

    async def fake_uds(path, cmd, **kw):
        raise FileNotFoundError("no voice daemon")

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window():
        pass


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    await entered_restore.wait()

    # B must be refused while A's restore is still in flight.
    with pytest.raises(MeasurementWindowError, match="already in progress"):
        async with measurement_window(
            skip_voice_pause=True, skip_music_isolation=True,
        ):
            pass

    release.set()
    await task_a
    assert coordinator._window_active is False


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_abort_target_falls_back_to_entering_task_when_none_registered(monkeypatch):
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
