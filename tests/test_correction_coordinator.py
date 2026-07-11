# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""measurement_window() coordinator: pause/resume contract.

We can't run real systemctl or talk to a real voice_daemon UDS in a
unit test, so we patch _systemctl and _voice_uds_command. The
contract being verified:

  1. With both skips, enter/exit is a no-op (smoke test for the
     plumbing).
  2. systemctl stop is called for each renderer on enter, start on
     exit.
  3. MEASURE_PAUSE is sent on enter, RESUME on exit.
  4. An exception inside the with-block still triggers RESUME and
     systemctl start (finally clause).
  5. An active voice session at precondition-check time raises
     MeasurementWindowError before anything is paused.
"""
from __future__ import annotations

import asyncio

import pytest

from jasper.correction import coordinator
from jasper.correction.coordinator import (
    MeasurementWindowError,
    measurement_window,
)


@pytest.mark.asyncio
async def test_skip_both_is_noop(monkeypatch):
    """With skip_voice_pause + skip_renderer_pause, no IO touches
    the real system. Enter and exit cleanly."""
    systemctl_calls: list[tuple[str, str]] = []
    uds_calls: list[str] = []

    async def fake_systemctl(action, svc):
        systemctl_calls.append((action, svc))

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_systemctl", fake_systemctl)
    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window(
        skip_voice_pause=True, skip_renderer_pause=True,
    ):
        pass

    assert systemctl_calls == []
    assert uds_calls == []


@pytest.mark.asyncio
async def test_pause_and_resume_renderers(monkeypatch):
    systemctl_calls: list[tuple[str, str]] = []
    uds_calls: list[str] = []

    async def fake_systemctl(action, svc):
        systemctl_calls.append((action, svc))

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_systemctl", fake_systemctl)
    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window(
        renderers_to_pause=("librespot.service", "shairport-sync.service"),
    ):
        pass

    # Stop on enter, start on exit. Order matters: stops happen
    # sequentially, starts in parallel (gather).
    assert ("stop", "librespot.service") in systemctl_calls
    assert ("stop", "shairport-sync.service") in systemctl_calls
    assert ("start", "librespot.service") in systemctl_calls
    assert ("start", "shairport-sync.service") in systemctl_calls
    # MEASURE_PAUSE first (after STATUS check), MEASURE_RESUME last.
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

    async with measurement_window(skip_renderer_pause=True):
        await asyncio.sleep(0.035)

    assert uds_calls.count("MEASURE_PAUSE") >= 2
    assert uds_calls[-1] == "MEASURE_RESUME"


@pytest.mark.asyncio
async def test_lease_refresh_failure_retries_and_still_restores(monkeypatch):
    """A malformed/empty renewal cannot strand voice or renderers paused."""
    uds_calls: list[str] = []
    systemctl_calls: list[tuple[str, str]] = []
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

    async def fake_systemctl(action, service):
        systemctl_calls.append((action, service))

    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)
    monkeypatch.setattr(coordinator, "_systemctl", fake_systemctl)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", 0.01)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_RETRY_SEC", 0.005)

    async with measurement_window(renderers_to_pause=("renderer.service",)):
        await asyncio.sleep(0.03)

    assert pause_calls >= 3
    assert "MEASURE_RESUME" in uds_calls
    assert ("start", "renderer.service") in systemctl_calls


def test_default_renderer_pause_list_covers_music_sources():
    """Correction sweeps need the fan-in music chain silent. Keep this
    list in sync when a playback source gets added."""
    assert coordinator.DEFAULT_RENDERERS_TO_PAUSE == (
        "librespot.service",
        "shairport-sync.service",
        "bluealsa-aplay.service",
        "jasper-usbsink.service",
    )


@pytest.mark.asyncio
async def test_resume_runs_even_on_exception(monkeypatch):
    """The whole point of the finally clause: a crash inside the
    measurement should not leave the speaker silent."""
    systemctl_calls: list[tuple[str, str]] = []
    uds_calls: list[str] = []

    async def fake_systemctl(action, svc):
        systemctl_calls.append((action, svc))

    async def fake_uds(path, cmd, **kw):
        uds_calls.append(cmd)
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_systemctl", fake_systemctl)
    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    with pytest.raises(RuntimeError, match="boom"):
        async with measurement_window(
            renderers_to_pause=("librespot.service",),
        ):
            raise RuntimeError("boom")

    # RESUME and renderer-restart still fired.
    assert "MEASURE_RESUME" in uds_calls
    assert ("start", "librespot.service") in systemctl_calls


@pytest.mark.asyncio
async def test_active_voice_session_blocks_window(monkeypatch):
    """Refuse to start a measurement if a voice session is active —
    yanking it would orphan the user's turn."""
    systemctl_calls: list[tuple[str, str]] = []

    async def fake_systemctl(action, svc):
        systemctl_calls.append((action, svc))

    async def fake_uds(path, cmd, **kw):
        if cmd == "STATUS":
            return {"state": "SESSION", "spend_allowed": True}
        return {"result": "ok"}

    monkeypatch.setattr(coordinator, "_systemctl", fake_systemctl)
    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    with pytest.raises(MeasurementWindowError, match="Voice session"):
        async with measurement_window():
            pass

    # Nothing was paused — the precondition fails before any IO.
    assert systemctl_calls == []


@pytest.mark.asyncio
async def test_voice_daemon_unreachable_is_tolerated(monkeypatch):
    """If voice_daemon is not running, that means there's no session
    to interrupt and no WakeLoop to pause. The window opens
    successfully (renderers still get paused)."""
    systemctl_calls: list[tuple[str, str]] = []

    async def fake_systemctl(action, svc):
        systemctl_calls.append((action, svc))

    async def fake_uds(path, cmd, **kw):
        raise FileNotFoundError("no voice daemon")

    monkeypatch.setattr(coordinator, "_systemctl", fake_systemctl)
    monkeypatch.setattr(coordinator, "_voice_uds_command", fake_uds)

    async with measurement_window(
        renderers_to_pause=("librespot.service",),
    ):
        pass

    # Renderers were paused/restored; voice was skipped.
    assert ("stop", "librespot.service") in systemctl_calls
    assert ("start", "librespot.service") in systemctl_calls


@pytest.mark.asyncio
async def test_concurrent_measurement_window_is_rejected(monkeypatch):
    """Only one window may be open. A second concurrent window would let
    whichever exits first send MEASURE_RESUME + restart renderers while the
    other is still measuring, corrupting its capture. The second entry fails
    fast; the flag is released when the first closes."""
    monkeypatch.setattr(coordinator, "_window_active", False)  # clean slate

    async with measurement_window(skip_voice_pause=True, skip_renderer_pause=True):
        with pytest.raises(MeasurementWindowError, match="already in progress"):
            async with measurement_window(
                skip_voice_pause=True, skip_renderer_pause=True,
            ):
                pass

    # Flag released after the outer window closed — a later window opens fine.
    assert coordinator._window_active is False
    async with measurement_window(skip_voice_pause=True, skip_renderer_pause=True):
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
        async with measurement_window(skip_renderer_pause=True):
            pass
    assert coordinator._window_active is False


@pytest.mark.asyncio
async def test_window_flag_released_even_if_renderer_restart_raises(monkeypatch):
    """A restore step raising in the finally (e.g. systemctl missing) must
    still release the mutex — otherwise one failed restart wedges every
    future measurement with 'already in progress' until process restart."""
    monkeypatch.setattr(coordinator, "_window_active", False)

    async def fake_systemctl(action, svc):
        if action == "start":
            raise FileNotFoundError("systemctl missing")

    monkeypatch.setattr(coordinator, "_systemctl", fake_systemctl)

    with pytest.raises(FileNotFoundError):
        async with measurement_window(
            skip_voice_pause=True, renderers_to_pause=("librespot.service",),
        ):
            pass
    assert coordinator._window_active is False


@pytest.mark.asyncio
async def test_window_b_blocked_while_window_a_restore_in_flight(monkeypatch):
    """The mutex must stay held across window A's restore I/O, released only
    once the renderer restart completes. Clearing it earlier would let a
    queued window B `systemctl stop` the renderers A is mid-`systemctl start`
    of — the corruption the mutex exists to prevent."""
    monkeypatch.setattr(coordinator, "_window_active", False)
    entered_restore = asyncio.Event()
    release = asyncio.Event()

    async def slow_systemctl(action, svc):
        if action == "start":
            entered_restore.set()
            await release.wait()  # hold window A inside its restore

    monkeypatch.setattr(coordinator, "_systemctl", slow_systemctl)

    async def window_a():
        async with measurement_window(
            skip_voice_pause=True, renderers_to_pause=("librespot.service",),
        ):
            pass

    task_a = asyncio.create_task(window_a())
    await entered_restore.wait()  # A is now mid-restore (systemctl start pending)

    # B must be refused while A's restore is still in flight.
    with pytest.raises(MeasurementWindowError, match="already in progress"):
        async with measurement_window(
            skip_voice_pause=True, skip_renderer_pause=True,
        ):
            pass

    release.set()
    await task_a
    assert coordinator._window_active is False
