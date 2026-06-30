# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Stage-4b-iv lean lane wiring in jasper.mux.

Covers: default-OFF byte-identical _tick, the enter/leave ladders, the
fail-loud -> buffered fallback, and the no-restart-storm enter-block. The
CamillaDSP swap and the usbsink FIFO arm are stubbed — this is the decision +
ladder logic, hardware-free.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jasper.mux import Mux, Source


class _FakeHandoff:
    def __init__(self, prev, current):
        from jasper.music_sources import VolumeMode

        self.prev_source = prev
        self.current_source = current
        self.reason = "test"
        self.level = 50
        self.prev_mode = VolumeMode.CAMILLA_MASTER
        self.current_mode = VolumeMode.CAMILLA_MASTER
        self.guard_db = -25.0
        self.camilla_before_db = 0.0
        self.push_ok = None
        self.settled_ms = 0
        self.result = "ok"
        self.detail = ""

    @property
    def ok(self):
        return True


class _FakeVolumeCoordinator:
    async def prepare_source_handoff(self, prev, current, *, reason):
        return _FakeHandoff(prev, current)

    async def finalize_source_handoff(self, handoff):
        return True

    async def aclose(self):
        pass


@pytest.fixture
def patched_probes(monkeypatch):
    spotify = AsyncMock(return_value=False)
    airplay = AsyncMock(return_value=False)
    bluetooth = AsyncMock(return_value=False)
    usbsink = AsyncMock(return_value=False)
    monkeypatch.setattr("jasper.mux.spotify_playing", spotify)
    monkeypatch.setattr("jasper.mux.airplay_playing", airplay)
    monkeypatch.setattr("jasper.mux.bluetooth_playing", bluetooth)
    monkeypatch.setattr("jasper.mux.usbsink_playing", usbsink)
    return SimpleNamespace(
        spotify=spotify, airplay=airplay, bluetooth=bluetooth, usbsink=usbsink,
    )


def _stub_probes(probes, *, spotify=False, airplay=False, bluetooth=False, usbsink=False):
    probes.spotify.return_value = spotify
    probes.airplay.return_value = airplay
    probes.bluetooth.return_value = bluetooth
    probes.usbsink.return_value = usbsink


def _make_mux(tmp_path, *, lean_enabled: bool) -> Mux:
    m = Mux(
        librespot_state_path=str(tmp_path / "librespot.state.json"),
        volume_coordinator=_FakeVolumeCoordinator(),
        mode_state_path=str(tmp_path / "mux_mode.json"),
    )
    m._fanin_select = AsyncMock(return_value={})
    m._fanin_auto = AsyncMock(return_value={})
    m._fanin_none = AsyncMock(return_value={})
    m._pause = AsyncMock()
    m._usbsink_set_preempt = AsyncMock()
    # Force the parsed-once flag regardless of the ambient env.
    m._lean_enabled = lean_enabled
    # Stub the two CamillaDSP I/O seams + the FIFO arm so nothing touches HW.
    m._lean_apply_config = AsyncMock()
    m._lean_restore_config = AsyncMock()
    return m


# --------------------------------------------------------------------------
# Default-OFF: byte-identical (no lean calls at all)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_off_never_calls_lean(tmp_path, patched_probes, monkeypatch):
    monkeypatch.delenv("JASPER_LEAN_LANE", raising=False)
    m = _make_mux(tmp_path, lean_enabled=False)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()  # USB sole source + winner
    assert m._winner is Source.USBSINK
    # The disabled flag short-circuits before the shared route policy.
    m._lean_apply_config.assert_not_awaited()
    m._lean_restore_config.assert_not_awaited()
    assert m._in_lean is False


@pytest.mark.asyncio
async def test_default_off_leave_is_noop_when_not_in_lean(tmp_path, patched_probes):
    m = _make_mux(tmp_path, lean_enabled=False)
    _stub_probes(patched_probes, airplay=True)
    await m._tick()
    m._lean_restore_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_off_does_not_even_call_source_route_policy(
    tmp_path, patched_probes, monkeypatch,
):
    # Byte-identical proof: with the flag off, _settle_lean returns before the
    # policy function is ever consulted — zero new behavior on the hot path.
    called = {"n": 0}
    real = __import__(
        "jasper.mux", fromlist=["decide_source_low_latency_route"]
    ).decide_source_low_latency_route

    def spy(**kw):
        called["n"] += 1
        return real(**kw)

    monkeypatch.setattr("jasper.mux.decide_source_low_latency_route", spy)
    m = _make_mux(tmp_path, lean_enabled=False)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert called["n"] == 0


# --------------------------------------------------------------------------
# Enabled: enter-lean on exclusive USB winner
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enter_lean_on_exclusive_usb(tmp_path, patched_probes, monkeypatch):
    arm = _patch_arm(monkeypatch, ok=True)
    m = _make_mux(tmp_path, lean_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._winner is Source.USBSINK
    assert m._in_lean is True
    m._lean_apply_config.assert_awaited_once()
    # FIFO armed to "fifo".
    assert arm.calls and arm.calls[0][0] == "fifo"


@pytest.mark.asyncio
async def test_enter_lean_idempotent_across_ticks(tmp_path, patched_probes, monkeypatch):
    _patch_arm(monkeypatch, ok=True)
    m = _make_mux(tmp_path, lean_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    await m._tick()
    await m._tick()
    # Entered exactly once; subsequent ticks short-circuit on _in_lean.
    m._lean_apply_config.assert_awaited_once()


# --------------------------------------------------------------------------
# Leave-lean: a second source joins -> not exclusive -> buffered
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leave_lean_when_second_source_joins(tmp_path, patched_probes, monkeypatch):
    arm = _patch_arm(monkeypatch, ok=True)
    m = _make_mux(tmp_path, lean_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._in_lean is True
    # AirPlay joins; USB no longer the sole source.
    _stub_probes(patched_probes, usbsink=True, airplay=True)
    await m._tick()
    assert m._in_lean is False
    m._lean_restore_config.assert_awaited_once()
    # Last arm call disarmed back to aloop.
    assert arm.calls[-1][0] == "aloop"


@pytest.mark.asyncio
async def test_leave_lean_restore_failure_keeps_fifo_armed_and_retries(
    tmp_path, patched_probes, monkeypatch,
):
    # If the buffered restore raises (camilla hiccup), we must NOT disarm the
    # FIFO (that would point CamillaDSP at a dead pipe). Keep _in_lean + retry.
    arm = _patch_arm(monkeypatch, ok=True)
    m = _make_mux(tmp_path, lean_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._in_lean is True
    arm.calls.clear()
    # Restore fails on the first leave attempt, succeeds on the second.
    m._lean_restore_config = AsyncMock(side_effect=[RuntimeError("camilla down"), None])
    _stub_probes(patched_probes, usbsink=True, airplay=True)  # non-lean route
    await m._tick()
    # First leave: restore raised -> still in lean, FIFO NOT disarmed.
    assert m._in_lean is True
    assert all(mode != "aloop" for mode, _ in arm.calls)
    # Second leave attempt converges.
    await m._tick()
    assert m._in_lean is False
    assert arm.calls[-1][0] == "aloop"


# --------------------------------------------------------------------------
# Fail-loud -> buffered: arm failure and config-apply failure
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_arm_failure_falls_back_to_buffered(tmp_path, patched_probes, monkeypatch):
    _patch_arm(monkeypatch, ok=False)
    m = _make_mux(tmp_path, lean_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._in_lean is False
    # Config swap never attempted when the arm didn't take.
    m._lean_apply_config.assert_not_awaited()
    assert m._lean_enter_blocked is True


@pytest.mark.asyncio
async def test_config_apply_failure_disarms_and_blocks(tmp_path, patched_probes, monkeypatch):
    arm = _patch_arm(monkeypatch, ok=True)
    m = _make_mux(tmp_path, lean_enabled=True)
    m._lean_apply_config = AsyncMock(side_effect=RuntimeError("camilla down"))
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._in_lean is False
    assert m._lean_enter_blocked is True
    # Armed fifo, then rolled back to aloop on the apply failure.
    modes = [c[0] for c in arm.calls]
    assert modes == ["fifo", "aloop"]


@pytest.mark.asyncio
async def test_enter_block_prevents_restart_storm(tmp_path, patched_probes, monkeypatch):
    arm = _patch_arm(monkeypatch, ok=False)
    m = _make_mux(tmp_path, lean_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    await m._tick()
    await m._tick()
    # Only the first exclusive-USB tick attempts the arm; the block holds.
    assert len(arm.calls) == 1


@pytest.mark.asyncio
async def test_enter_block_clears_on_source_change(tmp_path, patched_probes, monkeypatch):
    _patch_arm(monkeypatch, ok=False)
    m = _make_mux(tmp_path, lean_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._lean_enter_blocked is True
    # AirPlay alone -> non-lean route clears the block.
    _stub_probes(patched_probes, airplay=True)
    await m._tick()
    assert m._lean_enter_blocked is False


# --------------------------------------------------------------------------
# Manual / test lanes never enter lean
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_pin_does_not_enter_lean(tmp_path, patched_probes, monkeypatch):
    _patch_arm(monkeypatch, ok=True)
    m = _make_mux(tmp_path, lean_enabled=True)
    m._manual_source = Source.USBSINK
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    # Manual path returns early via _reassert_manual_source; lean not entered.
    assert m._in_lean is False
    m._lean_apply_config.assert_not_awaited()


class _ArmRecorder:
    def __init__(self, ok):
        self._ok = ok
        self.calls: list[tuple[str, str]] = []

    def __call__(self, mode, *, reason, **kw):
        self.calls.append((mode, reason))
        from jasper.usbsink.output_mode_reconcile import ArmResult

        return ArmResult(
            ok=self._ok, changed=True, restarted=self._ok, mode=mode,
            detail="" if self._ok else "stub failure",
        )


def _patch_arm(monkeypatch, *, ok: bool) -> _ArmRecorder:
    rec = _ArmRecorder(ok)
    monkeypatch.setattr(
        "jasper.usbsink.output_mode_reconcile.set_output_mode", rec,
    )
    return rec


def test_repo_layout_marker():
    # Anchor the test file to the repo so a misconfigured collection fails loud.
    assert (Path(__file__).resolve().parents[1] / "jasper" / "mux.py").exists()
