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
