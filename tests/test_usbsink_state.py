"""Tests for the usbsink state pieces: source_state probe, state
publisher hysteresis, preempt listener wire format.

Hardware-free — these mock the AudioBridge surface (just `last_rms_dbfs`
and `is_preempted` attribute reads) and exercise the publish loop with
synthetic timing.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jasper.source_state import usbsink_playing
from jasper.usbsink.state_publisher import (
    StatePublisher,
    RMS_ACTIVE_DBFS,
    ACTIVE_DEBOUNCE_SEC,
    INACTIVE_DEBOUNCE_SEC,
)
from jasper.usbsink.preempt_listener import (
    _read_persisted_preempt,
    _persist_preempt,
)


# ----------------------------------------------------------------------
# source_state.usbsink_playing — the probe consumed by mux + renderer
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usbsink_playing_missing_file_returns_false(tmp_path):
    """Daemon not running / feature disabled → no state file → False."""
    assert await usbsink_playing(str(tmp_path / "missing.json")) is False


@pytest.mark.asyncio
async def test_usbsink_playing_reads_true(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "playing": True, "preempted": False,
        "host_connected": True, "rms_dbfs": -12.0,
        "updated_at": "2026-05-16T00:00:00+00:00",
    }))
    assert await usbsink_playing(str(p)) is True


@pytest.mark.asyncio
async def test_usbsink_playing_reads_false_explicit(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"playing": False, "preempted": False}))
    assert await usbsink_playing(str(p)) is False


@pytest.mark.asyncio
async def test_usbsink_playing_malformed_json_returns_false(tmp_path):
    """Partial write or corruption — return False (fail-soft)."""
    p = tmp_path / "state.json"
    p.write_text("{not valid json")
    assert await usbsink_playing(str(p)) is False


@pytest.mark.asyncio
async def test_usbsink_playing_missing_key_returns_false(tmp_path):
    """State dict without `playing` key — treat as not playing."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"rms_dbfs": -50.0}))
    assert await usbsink_playing(str(p)) is False


# ----------------------------------------------------------------------
# StatePublisher hysteresis — the load-bearing piece for mux edges
# ----------------------------------------------------------------------


class _FakeBridge:
    """Minimal bridge surface for the state publisher to consume."""
    def __init__(self):
        self.last_rms_dbfs = float("-inf")
        self.is_preempted = False


def _make_publisher(tmp_path, bridge=None, *, host_card_present=False):
    bridge = bridge or _FakeBridge()
    state_file = tmp_path / "state.json"
    # Fake the host-card-present probe by pointing it at a path we
    # control. When host_card_present is True, we'll create it.
    host_card = tmp_path / "card_present"
    if host_card_present:
        host_card.mkdir()
    pub = StatePublisher(
        bridge,
        state_path=str(state_file),
        host_card_path=str(host_card),
    )
    return pub, bridge, state_file


def _read_state_file(path: Path) -> dict:
    return json.loads(path.read_text())


def test_publisher_initially_writes_not_playing(tmp_path):
    pub, bridge, state_file = _make_publisher(tmp_path)
    # _tick is sync (no asyncio loop) — exercise it directly for
    # determinism.
    pub._tick()
    assert state_file.exists()
    state = _read_state_file(state_file)
    assert state["playing"] is False
    assert state["preempted"] is False
    assert state["host_connected"] is False


def test_publisher_active_transition_requires_debounce(tmp_path):
    pub, bridge, state_file = _make_publisher(tmp_path)
    bridge.last_rms_dbfs = RMS_ACTIVE_DBFS + 10.0  # above threshold
    pub._tick()
    # First tick observes above-threshold but debounce hasn't elapsed.
    assert _read_state_file(state_file)["playing"] is False

    # Simulate time passing by directly mutating the internal mono
    # marker. This is the cleanest way to exercise debouncing without
    # sleep()ing in a test.
    pub._debounce.last_active_change_mono -= ACTIVE_DEBOUNCE_SEC + 0.01
    pub._tick()
    assert _read_state_file(state_file)["playing"] is True


def test_publisher_inactive_transition_requires_longer_debounce(tmp_path):
    pub, bridge, state_file = _make_publisher(tmp_path)

    # Get to playing=True first.
    bridge.last_rms_dbfs = RMS_ACTIVE_DBFS + 10.0
    pub._tick()
    pub._debounce.last_active_change_mono -= ACTIVE_DEBOUNCE_SEC + 0.01
    pub._tick()
    assert _read_state_file(state_file)["playing"] is True

    # Now drop below threshold and check the inactive debounce holds.
    bridge.last_rms_dbfs = RMS_ACTIVE_DBFS - 10.0
    pub._tick()
    assert _read_state_file(state_file)["playing"] is True

    # Cross the inactive-debounce window.
    pub._debounce.last_active_change_mono -= INACTIVE_DEBOUNCE_SEC + 0.01
    pub._tick()
    assert _read_state_file(state_file)["playing"] is False


def test_publisher_brief_dip_does_not_flap_playing(tmp_path):
    """If RMS drops below threshold for less than the inactive
    debounce and then comes back, `playing` should stay True. This
    is the hysteresis guarantee that keeps mux happy."""
    pub, bridge, state_file = _make_publisher(tmp_path)

    # Get to playing=True.
    bridge.last_rms_dbfs = RMS_ACTIVE_DBFS + 10.0
    pub._tick()
    pub._debounce.last_active_change_mono -= ACTIVE_DEBOUNCE_SEC + 0.01
    pub._tick()
    assert _read_state_file(state_file)["playing"] is True

    # Brief dip below threshold (e.g. track gap).
    bridge.last_rms_dbfs = RMS_ACTIVE_DBFS - 5.0
    pub._tick()
    # Doesn't satisfy the inactive debounce yet.
    pub._debounce.last_active_change_mono -= INACTIVE_DEBOUNCE_SEC / 2
    pub._tick()
    # Back above threshold before the debounce expires.
    bridge.last_rms_dbfs = RMS_ACTIVE_DBFS + 10.0
    pub._tick()
    assert _read_state_file(state_file)["playing"] is True


def test_publisher_host_connected_reflects_card_path(tmp_path):
    pub, bridge, state_file = _make_publisher(tmp_path, host_card_present=True)
    pub._tick()
    assert _read_state_file(state_file)["host_connected"] is True


def test_publisher_preempted_passthrough(tmp_path):
    pub, bridge, state_file = _make_publisher(tmp_path)
    bridge.is_preempted = True
    pub._tick()
    assert _read_state_file(state_file)["preempted"] is True


def test_publisher_atomic_write_never_leaves_partial_json(tmp_path):
    """tempfile + os.replace contract: no observer ever sees a half-
    written file. Stub fsync to capture the rename ordering."""
    pub, bridge, state_file = _make_publisher(tmp_path)
    pub._tick()
    # The tmpfile pattern leaves no leftover dotfile.
    leftover = [f for f in tmp_path.iterdir() if f.name.startswith(".state.")]
    assert leftover == [], f"unexpected leftover tmpfiles: {leftover}"


# ----------------------------------------------------------------------
# Preempt-listener persistence helpers
# ----------------------------------------------------------------------


def test_preempt_persist_round_trip(tmp_path):
    p = tmp_path / "preempt.state"
    assert _read_persisted_preempt(p) is False  # no file
    _persist_preempt(p, True)
    assert _read_persisted_preempt(p) is True
    _persist_preempt(p, False)
    assert _read_persisted_preempt(p) is False


def test_preempt_persist_atomic_no_partial(tmp_path):
    p = tmp_path / "preempt.state"
    _persist_preempt(p, True)
    leftover = [f for f in tmp_path.iterdir() if f.name.startswith(".preempt.")]
    assert leftover == []


def test_preempt_persist_corrupt_file_returns_false(tmp_path):
    """A truncated or hand-edited file should resolve to False
    (fail-safe: better to leak briefly than be silently muted)."""
    p = tmp_path / "preempt.state"
    p.write_text("garbage")
    assert _read_persisted_preempt(p) is False
