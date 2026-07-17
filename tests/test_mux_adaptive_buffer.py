# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the adaptive fan-in output-buffer wiring in jasper.mux.

Covers: default-OFF byte-identical _tick (spy proves the shared route policy is
never consulted), shrink on the exclusive-USB edge, restore on a networked join,
restart-fail -> shrink-block fail-safe, below-floor rejection surfaced as
fail-safe-to-full, idempotency across ticks, and that manual/test lanes never
shrink. The fan-in restart + env write are stubbed — this is the decision +
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

    async def publish_volume_context(self):
        pass

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

    async def usb_probe(_mux):
        return await usbsink()

    monkeypatch.setattr(Mux, "_usbsink_playing", usb_probe)
    return SimpleNamespace(
        spotify=spotify, airplay=airplay, bluetooth=bluetooth, usbsink=usbsink,
    )


def _stub_probes(probes, *, spotify=False, airplay=False, bluetooth=False, usbsink=False):
    probes.spotify.return_value = spotify
    probes.airplay.return_value = airplay
    probes.bluetooth.return_value = bluetooth
    probes.usbsink.return_value = usbsink


def _make_mux(tmp_path, *, adaptive_enabled: bool) -> Mux:
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
    # Force the parsed-once adaptive flag regardless of ambient env.
    m._adaptive_buffer_enabled = adaptive_enabled
    # Stub the two reconciler seams so nothing touches the env file / broker.
    m._shrink_output_buffer = AsyncMock(wraps=m._shrink_output_buffer)
    m._restore_output_buffer = AsyncMock(wraps=m._restore_output_buffer)
    return m


# --------------------------------------------------------------------------
# Default-OFF: byte-identical (no adaptive calls at all)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_off_never_shrinks(tmp_path, patched_probes, monkeypatch):
    monkeypatch.delenv("JASPER_FANIN_ADAPTIVE_BUFFER", raising=False)
    m = _make_mux(tmp_path, adaptive_enabled=False)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()  # USB sole source + winner
    assert m._winner is Source.USBSINK
    m._shrink_output_buffer.assert_not_awaited()
    m._restore_output_buffer.assert_not_awaited()
    assert m._buffer_shrunk is False


@pytest.mark.asyncio
async def test_default_off_does_not_call_source_route_policy(
    tmp_path, patched_probes, monkeypatch,
):
    # Byte-identical proof: with the flag off, _settle_adaptive_buffer returns
    # before the shared route policy is ever consulted — the policy function
    # sees ZERO calls, so the adaptive wiring adds nothing to the hot path when
    # disabled.
    called = {"n": 0}
    real = __import__(
        "jasper.mux", fromlist=["decide_source_low_latency_route"]
    ).decide_source_low_latency_route

    def spy(**kw):
        called["n"] += 1
        return real(**kw)

    monkeypatch.setattr("jasper.mux.decide_source_low_latency_route", spy)
    m = _make_mux(tmp_path, adaptive_enabled=False)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_adaptive_consumes_one_source_route_decision(
    tmp_path,
    patched_probes,
    monkeypatch,
):
    called = {"n": 0}
    real = __import__(
        "jasper.mux", fromlist=["decide_source_low_latency_route"]
    ).decide_source_low_latency_route

    def spy(**kw):
        called["n"] += 1
        return real(**kw)

    monkeypatch.setattr("jasper.mux.decide_source_low_latency_route", spy)
    _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)

    await m._tick()

    assert called["n"] == 1
    assert m._buffer_shrunk is True


# --------------------------------------------------------------------------
# Enabled: shrink on the exclusive-USB edge
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shrink_on_exclusive_usb(tmp_path, patched_probes, monkeypatch):
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._winner is Source.USBSINK
    assert m._buffer_shrunk is True
    # Shrunk to the floor target (default 1024).
    assert rec.set_calls and rec.set_calls[0][0] == 1024


@pytest.mark.asyncio
async def test_shrink_honors_sweep_override(tmp_path, patched_probes, monkeypatch):
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    monkeypatch.setenv("JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES", "2048")
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert rec.set_calls[0][0] == 2048


@pytest.mark.asyncio
async def test_shrink_idempotent_across_ticks(tmp_path, patched_probes, monkeypatch):
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    await m._tick()
    await m._tick()
    # Shrunk exactly once; subsequent ticks short-circuit on _buffer_shrunk.
    assert len(rec.set_calls) == 1


# --------------------------------------------------------------------------
# Restore: a networked source joins -> not exclusive -> full buffer
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_when_networked_source_joins(tmp_path, patched_probes, monkeypatch):
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._buffer_shrunk is True
    # AirPlay (networked) joins; USB no longer sole -> restore full.
    _stub_probes(patched_probes, usbsink=True, airplay=True)
    await m._tick()
    assert m._buffer_shrunk is False
    assert rec.restore_calls  # restore was invoked


@pytest.mark.asyncio
async def test_restore_when_idle(tmp_path, patched_probes, monkeypatch):
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._buffer_shrunk is True
    _stub_probes(patched_probes)  # everything idle
    await m._tick()
    assert m._buffer_shrunk is False
    assert rec.restore_calls


# --------------------------------------------------------------------------
# Fail-safe: shrink (env/restart) failure keeps the FULL buffer + blocks retry
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shrink_failure_stays_full_and_blocks(tmp_path, patched_probes, monkeypatch):
    _patch_reconcile(monkeypatch, set_ok=False, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    # Shrink failed -> stay full (fail-safe), and the block is armed.
    assert m._buffer_shrunk is False
    assert m._buffer_shrink_blocked is True


@pytest.mark.asyncio
async def test_below_floor_override_rejected_stays_full(
    tmp_path, patched_probes, monkeypatch,
):
    # A genuinely below-floor sweep value is resolved up to the floor by
    # shrunk_target_frames, so the reconciler is asked for 1024, not 512 — the
    # shrink succeeds at the floor rather than no-opping. This pins that the
    # mux never asks the reconciler for an unstartable value.
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    monkeypatch.setenv("JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES", "512")
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert rec.set_calls[0][0] == 1024
    assert m._buffer_shrunk is True


@pytest.mark.asyncio
async def test_shrink_block_prevents_restart_storm(tmp_path, patched_probes, monkeypatch):
    rec = _patch_reconcile(monkeypatch, set_ok=False, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    await m._tick()
    await m._tick()
    # Only the first exclusive-USB tick attempts the shrink; the block holds.
    assert len(rec.set_calls) == 1


@pytest.mark.asyncio
async def test_shrink_block_clears_on_source_change(tmp_path, patched_probes, monkeypatch):
    _patch_reconcile(monkeypatch, set_ok=False, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._buffer_shrink_blocked is True
    # AirPlay alone -> non-exclusive route clears the block.
    _stub_probes(patched_probes, airplay=True)
    await m._tick()
    assert m._buffer_shrink_blocked is False


@pytest.mark.asyncio
async def test_restore_failure_keeps_shrunk_and_retries(
    tmp_path, patched_probes, monkeypatch,
):
    # If restore fails (broker hiccup), keep _buffer_shrunk=True so the next
    # non-exclusive tick retries — convergent, never stuck shrunk silently.
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._buffer_shrunk is True
    rec.restore_ok = False  # first restore fails
    _stub_probes(patched_probes, airplay=True)  # non-exclusive route
    await m._tick()
    assert m._buffer_shrunk is True  # stayed shrunk, will retry
    rec.restore_ok = True  # second restore succeeds
    await m._tick()
    assert m._buffer_shrunk is False


# --------------------------------------------------------------------------
# Manual / test lanes never shrink (they restore to full)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_pin_does_not_shrink(tmp_path, patched_probes, monkeypatch):
    _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    m._manual_source = Source.USBSINK
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    # Manual path returns early; never shrinks even with USB the sole source.
    assert m._buffer_shrunk is False
    m._shrink_output_buffer.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_pin_restores_a_shrunk_buffer(tmp_path, patched_probes, monkeypatch):
    rec = _patch_reconcile(monkeypatch, set_ok=True, restore_ok=True)
    m = _make_mux(tmp_path, adaptive_enabled=True)
    _stub_probes(patched_probes, usbsink=True)
    await m._tick()
    assert m._buffer_shrunk is True
    # Operator pins USB manually while shrunk -> the manual/test branch restores.
    m._manual_source = Source.USBSINK
    await m._tick()
    assert m._buffer_shrunk is False
    assert rec.restore_calls


# --------------------------------------------------------------------------
# Reconciler stub
# --------------------------------------------------------------------------

class _ReconcileRecorder:
    def __init__(self, *, set_ok: bool, restore_ok: bool):
        self.set_ok = set_ok
        self.restore_ok = restore_ok
        self.set_calls: list[tuple[int, str]] = []
        self.restore_calls: list[str] = []

    def set_fanin_output_buffer(self, frames, *, reason, **kw):
        self.set_calls.append((frames, reason))
        from jasper.fanin.buffer_reconcile import BufferResult

        return BufferResult(
            ok=self.set_ok, changed=True, restarted=self.set_ok, frames=frames,
            detail="" if self.set_ok else "stub failure",
        )

    def restore_fanin_output_buffer(self, *, reason, **kw):
        self.restore_calls.append(reason)
        from jasper.fanin.buffer_reconcile import (
            DEFAULT_OUTPUT_BUFFER_FRAMES,
            BufferResult,
        )

        return BufferResult(
            ok=self.restore_ok, changed=True, restarted=self.restore_ok,
            frames=DEFAULT_OUTPUT_BUFFER_FRAMES,
            detail="" if self.restore_ok else "stub failure",
        )


def _patch_reconcile(monkeypatch, *, set_ok: bool, restore_ok: bool) -> _ReconcileRecorder:
    rec = _ReconcileRecorder(set_ok=set_ok, restore_ok=restore_ok)
    monkeypatch.setattr(
        "jasper.fanin.buffer_reconcile.set_fanin_output_buffer",
        rec.set_fanin_output_buffer,
    )
    monkeypatch.setattr(
        "jasper.fanin.buffer_reconcile.restore_fanin_output_buffer",
        rec.restore_fanin_output_buffer,
    )
    return rec


def test_repo_layout_marker():
    assert (Path(__file__).resolve().parents[1] / "jasper" / "mux.py").exists()
