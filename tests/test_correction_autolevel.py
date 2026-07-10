# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

from jasper.correction.autolevel import AutolevelController, AutolevelData
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


@pytest.mark.asyncio
async def test_autolevel_controller_restores_locked_level_once():
    controller = AutolevelController(session_id="restore-once")
    restored: list[float] = []

    async def fake_set_vol(db):
        restored.append(db)

    controller.main_volume_setter = fake_set_vol
    controller.data = AutolevelData(
        status=AutolevelStatus.LOCKED,
        original_main_volume_db=-18.0,
    )

    await controller.restore_listening_volume_if_ramped()
    await controller.restore_listening_volume_if_ramped()

    assert restored == [-18.0]
    assert controller.data.restored is True


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
    assert sess.autolevel.current_main_volume_db == -10.0
    assert sess.autolevel.locked_main_volume_db is None
    assert sess.autolevel.error is not None
    assert "external amplifier" in sess.autolevel.error


@pytest.mark.asyncio
async def test_autolevel_quiet_start_never_jumps_above_dynamic_cap(tmp_path):
    """A very quiet listener must not be raised to the nominal start_db."""
    sess = _make_session(tmp_path)
    set_history: list[float] = []

    async def fake_get_vol():
        return -80.0

    async def fake_set_vol(db):
        set_history.append(db)

    player = _StubTonePlayer()
    await sess.run_autolevel(
        get_main_volume_db=fake_get_vol,
        set_main_volume_db=fake_set_vol,
        play_continuous_tone=player.play,
        cancel_tone=player.cancel,
        start_db=-40.0,
        step_interval_s=0.01,
        fade_step_s=0.001,
    )

    assert sess.autolevel.cap_db == -74.0
    assert max(set_history) <= -74.0
    assert sess.autolevel.status == AutolevelStatus.MAXED_OUT
    assert sess.autolevel.locked_main_volume_db is None


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


# ---------- Order-of-operations + safety -----------------------------------


@pytest.mark.asyncio
async def test_autolevel_sets_quiet_start_volume_before_tone(tmp_path):
    """Bug fix from the first user run: the tone was starting at
    the user's previous main_volume (often loud) BEFORE the ramp
    dropped it to start_db. Now we set start_db FIRST, then start
    the tone. Pin the order with a recorder.
    """
    sess = _make_session(tmp_path)
    events: list[tuple[str, float | None]] = []

    async def fake_get_vol():
        return -10.0

    async def fake_set_vol(db):
        events.append(("set_vol", float(db)))

    class _RecordingPlayer:
        def __init__(self):
            self._cancel = asyncio.Event()
        async def play(self):
            events.append(("tone_start", None))
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=20.0)
            except asyncio.TimeoutError:
                pass
            events.append(("tone_end", None))
        def cancel(self):
            self._cancel.set()

    player = _RecordingPlayer()

    async def _cancel_after_step():
        await asyncio.sleep(0.05)
        await sess.cancel_autolevel()

    asyncio.create_task(_cancel_after_step())
    await sess.run_autolevel(
        get_main_volume_db=fake_get_vol,
        set_main_volume_db=fake_set_vol,
        play_continuous_tone=player.play,
        cancel_tone=player.cancel,
        start_db=-40.0,
        end_db=-6.0,
        step_db=2.0,
        step_interval_s=0.05,
        fade_step_s=0.01,
    )

    # First two events should be: set_vol(-40), tone_start.
    # NEVER tone_start before set_vol(-40).
    first_set = next(i for i, e in enumerate(events) if e[0] == "set_vol")
    first_tone = next(i for i, e in enumerate(events) if e[0] == "tone_start")
    assert first_set < first_tone, (
        f"tone started before main_volume was set to start_db. "
        f"events={events[:6]}"
    )
    assert events[first_set] == ("set_vol", -40.0), (
        f"first set_vol should be -40 dB (start_db), got {events[first_set]}"
    )


@pytest.mark.asyncio
async def test_autolevel_end_db_computed_relative_to_original(tmp_path):
    """end_db now defaults to None and is computed from the user's
        actual listening volume at the start of the run — NOT a fixed
        cap. This is the "stop being a menace about volume" fix: cap
        is min(original + 6 dB, -6 dB) with no unsafe upward floor. Verified via
    sess.autolevel.cap_db which now exposes the computed value.
    """
    cases = [
        # (original main_volume, expected cap)
        (-20.0, -14.0),  # +6 bump
        (-10.0, -6.0),   # bump would give -4; clamped at absolute max
        (-5.0,  -6.0),   # already above; clamped
        (-45.0, -39.0),  # quiet listeners still rise by at most the +6 bump
        (-25.0, -19.0),  # +6 bump
    ]
    for original, expected_cap in cases:
        sess = _make_session(tmp_path)

        async def fake_get_vol(_o=original):
            return _o

        async def fake_set_vol(db):
            pass

        player = _StubTonePlayer()

        async def _cancel_quickly():
            # Fire cancel almost immediately so we don't sit through
            # a multi-second ramp — we just need the cap to land in
            # autolevel.cap_db before exit.
            await asyncio.sleep(0.02)
            await sess.cancel_autolevel()

        asyncio.create_task(_cancel_quickly())
        await sess.run_autolevel(
            get_main_volume_db=fake_get_vol,
            set_main_volume_db=fake_set_vol,
            play_continuous_tone=player.play,
            cancel_tone=player.cancel,
            # end_db left at default (None) — must auto-compute
            start_db=-40.0,
            step_db=2.0,
            step_interval_s=0.05,
            fade_step_s=0.005,
        )
        assert sess.autolevel.original_main_volume_db == original
        assert sess.autolevel.cap_db == expected_cap, (
            f"original={original}: expected cap {expected_cap}, "
            f"got {sess.autolevel.cap_db}"
        )


@pytest.mark.asyncio
async def test_autolevel_lock_fades_down_before_tone_cancel(tmp_path):
    """When the user locks, we must fade main_volume DOWN to a
    quiet value before killing the tone. Otherwise the abrupt
    aplay kill produces an audible click at whatever loud level
    was last set. Verify the sequence: set_vol going down, then
    tone_cancel, then set_vol to lock_value."""
    sess = _make_session(tmp_path)
    events: list[tuple[str, float | None]] = []

    async def fake_get_vol():
        return -10.0

    async def fake_set_vol(db):
        events.append(("set_vol", float(db)))

    class _RecordingPlayer:
        def __init__(self):
            self._cancel = asyncio.Event()
        async def play(self):
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=20.0)
            except asyncio.TimeoutError:
                pass
        def cancel(self):
            events.append(("tone_cancel", None))
            self._cancel.set()

    player = _RecordingPlayer()

    async def _lock_after_few_steps():
        await asyncio.sleep(0.12)  # let ramp climb a bit
        await sess.lock_autolevel()

    asyncio.create_task(_lock_after_few_steps())
    await sess.run_autolevel(
        get_main_volume_db=fake_get_vol,
        set_main_volume_db=fake_set_vol,
        play_continuous_tone=player.play,
        cancel_tone=player.cancel,
        start_db=-30.0,
        end_db=-6.0,
        step_db=2.0,
        step_interval_s=0.05,
        fade_down_to_db=-40.0,
        fade_step_s=0.01,
    )

    assert sess.autolevel.status == AutolevelStatus.LOCKED
    lock_db = sess.autolevel.locked_main_volume_db

    # After lock fires, the sequence should be:
    #   1. Some `set_vol` calls going DOWN toward fade_down_to_db
    #   2. One `tone_cancel`
    #   3. One `set_vol(lock_db)` to set the final value
    cancel_idx = next(i for i, e in enumerate(events) if e[0] == "tone_cancel")
    # Just before cancel, the volume should be near fade_down_to_db.
    prev_set_vols = [
        e[1] for e in events[:cancel_idx] if e[0] == "set_vol"
    ]
    assert prev_set_vols[-1] <= -38.0, (
        f"expected fade-down before cancel; last set_vol pre-cancel="
        f"{prev_set_vols[-1]}"
    )
    # And after cancel, the final set_vol is lock_db.
    post_set_vols = [
        e[1] for e in events[cancel_idx:] if e[0] == "set_vol"
    ]
    assert post_set_vols[-1] == lock_db, (
        f"expected final set_vol to lock value {lock_db}, "
        f"got {post_set_vols[-1]}"
    )


# ---------- Session snapshot includes autolevel ----------------------------


def test_session_snapshot_includes_autolevel(tmp_path):
    sess = _make_session(tmp_path)
    snap = sess.snapshot()
    assert "autolevel" in snap
    assert snap["autolevel"]["status"] == "idle"
    assert snap["autolevel"]["original_main_volume_db"] is None
    assert snap["autolevel"]["locked_main_volume_db"] is None
