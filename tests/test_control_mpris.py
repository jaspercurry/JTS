"""Tests for jasper.control.mpris — the shared shairport-sync MPRIS
PlaybackStatus probe.

The bug class under test (audit C4): `asyncio.wait_for(proc.communicate(),
...)` with no kill on timeout leaks one live busctl process per probe
under a DBus stall, and catching only (FileNotFoundError, TimeoutError)
lets a spawn OSError (EAGAIN/ENOMEM on a loaded Pi) propagate — which
500'd the whole fail-soft /state aggregate.

The subprocess is faked at the module's `asyncio.create_subprocess_exec`
seam so no real busctl/DBus is needed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from jasper.control import mpris
from jasper.control.shairport_supervisor import ShairportSupervisor


class _FakeProc:
    """Minimal asyncio-subprocess stand-in."""

    def __init__(self, *, stdout: bytes = b"", returncode: int = 0,
                 hang: bool = False):
        self._stdout = stdout
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_spawn(monkeypatch, proc: _FakeProc):
    async def fake_spawn(*argv, **kwargs):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)


async def test_playing_true(monkeypatch):
    _patch_spawn(monkeypatch, _FakeProc(stdout=b's "Playing"\n'))
    assert await mpris.shairport_playing() is True


async def test_playing_false_on_paused(monkeypatch):
    _patch_spawn(monkeypatch, _FakeProc(stdout=b's "Paused"\n'))
    assert await mpris.shairport_playing() is False


async def test_nonzero_exit_is_unknown(monkeypatch):
    _patch_spawn(monkeypatch, _FakeProc(stdout=b"", returncode=1))
    assert await mpris.shairport_playing() is None


async def test_spawn_oserror_is_unknown_not_a_crash(monkeypatch):
    """The full OSError family (not just FileNotFoundError) must be
    swallowed: EAGAIN under load was the audit's escape path."""
    async def fake_spawn(*argv, **kwargs):
        raise OSError(11, "Resource temporarily unavailable")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    assert await mpris.shairport_playing() is None


async def test_busctl_missing_is_unknown(monkeypatch):
    async def fake_spawn(*argv, **kwargs):
        raise FileNotFoundError("busctl")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    assert await mpris.shairport_playing() is None


async def test_timeout_kills_and_reaps_the_child(monkeypatch):
    """DBus stall shape: communicate() never returns. The probe must
    return None within the timeout AND kill the child — otherwise one
    busctl leaks per /state poll for as long as the stall lasts."""
    proc = _FakeProc(hang=True)
    _patch_spawn(monkeypatch, proc)
    result = await asyncio.wait_for(
        mpris.shairport_playing(timeout=0.05), timeout=2.0,
    )
    assert result is None
    assert proc.killed is True


async def test_kill_on_exited_child_is_tolerated(monkeypatch):
    """Child won the race and exited between cancel and kill():
    ProcessLookupError must be swallowed."""
    proc = _FakeProc(hang=True)

    def racing_kill():
        proc.killed = True
        raise ProcessLookupError()

    proc.kill = racing_kill
    _patch_spawn(monkeypatch, proc)
    assert await mpris.shairport_playing(timeout=0.05) is None
    assert proc.killed is True


# ---- caller semantics --------------------------------------------------

async def test_supervisor_gate_fails_safe_to_active_on_unknown(monkeypatch):
    sup = ShairportSupervisor()
    monkeypatch.setattr(
        "jasper.control.shairport_supervisor.mpris.shairport_playing",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        sup, "is_shairport_unit_active", AsyncMock(return_value=True),
    )
    assert await sup.is_session_active() is True


async def test_supervisor_gate_bypasses_fail_safe_when_unit_is_dead(monkeypatch):
    sup = ShairportSupervisor()
    monkeypatch.setattr(
        "jasper.control.shairport_supervisor.mpris.shairport_playing",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        sup, "is_shairport_unit_active", AsyncMock(return_value=False),
    )
    assert await sup.is_session_active() is False


async def test_supervisor_gate_maps_playing_through(monkeypatch):
    sup = ShairportSupervisor()
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "jasper.control.shairport_supervisor.mpris.shairport_playing", probe,
    )
    assert await sup.is_session_active() is False
    probe.return_value = True
    assert await sup.is_session_active() is True
