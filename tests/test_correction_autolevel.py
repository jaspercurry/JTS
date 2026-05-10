"""Auto-level state machine + stale-state race fix.

The autolevel function ramps CamillaDSP main_volume while a continuous
tone plays, until the iPhone client either signals lock (mic level
in target range) or the ramp tops out. We test the state machine
with stub set/get/play callbacks — no real CamillaDSP or aplay.

The `state_changed_from` helper is the load-bearing fix for the
"cannot advance to next position from state awaiting_capture" race
the user hit. We test that it returns promptly when state changes,
and times out cleanly when it doesn't.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jasper.correction.session import (
    AutolevelStatus,
    MeasurementSession,
    SessionConfig,
    SessionState,
)


def _make_session(tmp_path: Path, **kwargs) -> MeasurementSession:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = SessionConfig(
        sweep_dir=tmp_path / "sweeps",
        capture_dir=tmp_path / "captures",
        config_dir=tmp_path / "configs",
        base_config_path=tmp_path / "v1.yml",
        duration_s=1.0,
    )
    cfg.base_config_path.write_text("# stub\n")
    return MeasurementSession(cfg, **kwargs)


# ---------- state_changed_from --------------------------------------------


@pytest.mark.asyncio
async def test_state_changed_from_returns_true_when_state_changes(tmp_path):
    sess = _make_session(tmp_path)
    # Spawn a task that flips state after a short delay.
    async def _flip():
        await asyncio.sleep(0.05)
        sess.state = SessionState.PREPARING

    asyncio.create_task(_flip())
    result = await sess.state_changed_from(
        SessionState.IDLE, timeout_s=1.0,
    )
    assert result is True
    assert sess.state == SessionState.PREPARING


@pytest.mark.asyncio
async def test_state_changed_from_returns_false_on_timeout(tmp_path):
    sess = _make_session(tmp_path)
    # State never changes; helper should time out and return False.
    result = await sess.state_changed_from(
        SessionState.IDLE, timeout_s=0.15,
    )
    assert result is False
    assert sess.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_state_changed_from_accepts_set_of_states(tmp_path):
    """The helper accepts a single state OR a set, so callers can
    block on 'state changed out of {APPLIED, VERIFIED}'."""
    sess = _make_session(tmp_path)
    sess.state = SessionState.APPLIED

    async def _flip():
        await asyncio.sleep(0.05)
        sess.state = SessionState.VERIFYING

    asyncio.create_task(_flip())
    result = await sess.state_changed_from(
        {SessionState.APPLIED, SessionState.VERIFIED}, timeout_s=1.0,
    )
    assert result is True


# ---------- run_autolevel --------------------------------------------------


class _StubTonePlayer:
    """Stand-in for jasper.correction.playback.TonePlayer in tests —
    awaitable, cancellable, no actual audio."""

    def __init__(self):
        self.played = False
        self.cancelled = False
        self._cancel_event = asyncio.Event()

    async def play(self):
        self.played = True
        # Block until cancel() is called (mimics aplay holding the
        # ALSA device until killed). Or wait at most 20 s — safety
        # net so a buggy test can't hang forever.
        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

    def cancel(self):
        self.cancelled = True
        self._cancel_event.set()


@pytest.mark.asyncio
async def test_autolevel_locks_when_lock_event_set(tmp_path):
    """Drive a fake autolevel: start the ramp, immediately signal
    lock, expect status=LOCKED at the next ramp step."""
    sess = _make_session(tmp_path)
    set_history: list[float] = []

    async def fake_get_vol():
        return -10.0  # original main_volume

    async def fake_set_vol(db):
        set_history.append(db)

    player = _StubTonePlayer()

    # Run autolevel in a task, signal lock after one ramp step.
    async def _signal_lock_quickly():
        await asyncio.sleep(0.05)  # let one ramp step happen
        await sess.lock_autolevel()

    asyncio.create_task(_signal_lock_quickly())
    await sess.run_autolevel(
        get_main_volume_db=fake_get_vol,
        set_main_volume_db=fake_set_vol,
        play_continuous_tone=player.play,
        cancel_tone=player.cancel,
        start_db=-40.0,
        end_db=0.0,
        step_db=1.0,
        step_interval_s=0.05,  # fast for tests
    )

    assert sess.autolevel.status == AutolevelStatus.LOCKED
    assert sess.autolevel.original_main_volume_db == -10.0
    assert sess.autolevel.locked_main_volume_db is not None
    # Locked somewhere in the ramp band (started at -40, was rising).
    assert -40.0 <= sess.autolevel.locked_main_volume_db < 0.0
    # The locked value matches the final set in set_history.
    assert set_history[-1] == sess.autolevel.locked_main_volume_db
    # Tone player was started + cancelled.
    assert player.played
    assert player.cancelled


@pytest.mark.asyncio
async def test_autolevel_maxes_out_when_no_lock(tmp_path):
    """If lock is never signalled, the ramp reaches end_db and
    status becomes MAXED_OUT. The UI tells the user to turn up the
    amplifier."""
    sess = _make_session(tmp_path)
    set_history: list[float] = []

    async def fake_get_vol():
        return -10.0

    async def fake_set_vol(db):
        set_history.append(db)

    player = _StubTonePlayer()

    await sess.run_autolevel(
        get_main_volume_db=fake_get_vol,
        set_main_volume_db=fake_set_vol,
        play_continuous_tone=player.play,
        cancel_tone=player.cancel,
        start_db=-10.0,  # short range for fast test
        end_db=0.0,
        step_db=2.0,
        step_interval_s=0.01,  # very fast
    )

    assert sess.autolevel.status == AutolevelStatus.MAXED_OUT
    assert sess.autolevel.current_main_volume_db == 0.0
    assert sess.autolevel.locked_main_volume_db == 0.0


@pytest.mark.asyncio
async def test_autolevel_cancel_restores_main_volume(tmp_path):
    """When the user cancels mid-ramp, main_volume must be restored
    to the pre-autolevel value. Otherwise their music would be at a
    surprising volume next time they play something."""
    sess = _make_session(tmp_path)
    set_history: list[float] = []
    ORIG = -8.5

    async def fake_get_vol():
        return ORIG

    async def fake_set_vol(db):
        set_history.append(db)

    player = _StubTonePlayer()

    async def _cancel_quickly():
        await asyncio.sleep(0.05)
        await sess.cancel_autolevel()

    asyncio.create_task(_cancel_quickly())
    await sess.run_autolevel(
        get_main_volume_db=fake_get_vol,
        set_main_volume_db=fake_set_vol,
        play_continuous_tone=player.play,
        cancel_tone=player.cancel,
        start_db=-40.0,
        end_db=0.0,
        step_db=1.0,
        step_interval_s=0.05,
    )

    assert sess.autolevel.status == AutolevelStatus.CANCELLED
    # Last `set` call should be the restoration to ORIG.
    assert set_history[-1] == ORIG


@pytest.mark.asyncio
async def test_autolevel_lock_when_no_run_in_progress_returns_false(tmp_path):
    """Pre-condition guard: locking without a running autolevel is
    a no-op that returns False."""
    sess = _make_session(tmp_path)
    fired = await sess.lock_autolevel()
    assert fired is False


@pytest.mark.asyncio
async def test_autolevel_cancel_when_no_run_in_progress_returns_false(tmp_path):
    sess = _make_session(tmp_path)
    fired = await sess.cancel_autolevel()
    assert fired is False


@pytest.mark.asyncio
async def test_autolevel_safety_timeout_restores_main_volume(tmp_path):
    """If neither lock nor cancel arrives within safety_timeout_s,
    the autolevel auto-restores main_volume and reports CANCELLED.
    Prevents a crashed client from leaving the speaker stuck at
    ramp-end volume."""
    sess = _make_session(tmp_path)
    set_history: list[float] = []
    ORIG = -12.0

    async def fake_get_vol():
        return ORIG

    async def fake_set_vol(db):
        set_history.append(db)

    player = _StubTonePlayer()

    # Very short safety timeout. No lock / cancel signal arrives.
    await sess.run_autolevel(
        get_main_volume_db=fake_get_vol,
        set_main_volume_db=fake_set_vol,
        play_continuous_tone=player.play,
        cancel_tone=player.cancel,
        start_db=-40.0,
        end_db=0.0,
        step_db=0.5,
        step_interval_s=0.05,
        safety_timeout_s=0.2,
    )

    assert sess.autolevel.status == AutolevelStatus.CANCELLED
    assert sess.autolevel.error is not None and "safety" in sess.autolevel.error
    assert set_history[-1] == ORIG


# ---------- Session snapshot includes autolevel ----------------------------


def test_session_snapshot_includes_autolevel(tmp_path):
    sess = _make_session(tmp_path)
    snap = sess.snapshot()
    assert "autolevel" in snap
    assert snap["autolevel"]["status"] == "idle"
    assert snap["autolevel"]["original_main_volume_db"] is None
    assert snap["autolevel"]["locked_main_volume_db"] is None
