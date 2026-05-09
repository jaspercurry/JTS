"""Unit tests for jasper.timers.

Covers:
- human_duration formatting across boundary cases
- announcement_text labelled vs unlabelled selection
- TimerStore SQLite round-trip across new connections (the core
  restart-survival contract)
- TimerScheduler add / list / cancel CRUD
- TimerScheduler fire-and-callback within an asyncio event loop
- TimerScheduler.start() restoring future timers + dropping expired
- TimerScheduler.stop() cancelling in-flight tasks (no fire after stop)

No real ALSA, no real network. The Gemini TTS backend is never
touched — the announcement callback the daemon would hook in is
substituted with a plain fake.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid

import pytest

from jasper.timers import (
    Timer,
    TimerScheduler,
    TimerStore,
    announcement_text,
    human_duration,
)


# --- pure functions -----------------------------------------------


def test_human_duration_zero_seconds():
    assert human_duration(0) == "0 seconds"


def test_human_duration_one_second_singular():
    assert human_duration(1) == "1 second"


def test_human_duration_seconds_below_one_minute():
    assert human_duration(45) == "45 seconds"


def test_human_duration_one_minute_singular():
    assert human_duration(60) == "1 minute"


def test_human_duration_minutes_plural_no_seconds():
    assert human_duration(300) == "5 minutes"


def test_human_duration_minutes_with_seconds():
    assert human_duration(330) == "5 minutes and 30 seconds"


def test_human_duration_one_hour_exact():
    assert human_duration(3600) == "1 hour"


def test_human_duration_one_hour_thirty_minutes():
    assert human_duration(5400) == "1 hour and 30 minutes"


def test_human_duration_hours_minutes_seconds_three_part():
    # 1h 1m 1s — uses Oxford comma between minute and second parts.
    assert human_duration(3661) == "1 hour, 1 minute, and 1 second"


def test_human_duration_negative_floors_to_zero():
    assert human_duration(-5) == "0 seconds"


def test_announcement_text_labelled_uses_label():
    t = Timer(
        id="abc", label="pasta", fire_at=time.time() + 60,
        total_seconds=300, created_at=time.time(),
    )
    assert announcement_text(t) == "Your pasta timer is up."


def test_announcement_text_unlabelled_uses_duration():
    t = Timer(
        id="abc", label=None, fire_at=time.time() + 60,
        total_seconds=300, created_at=time.time(),
    )
    assert announcement_text(t) == "Your timer for 5 minutes is up."


def test_announcement_text_unlabelled_compound_duration():
    """Multi-unit durations (hour + minute) need the 'timer for X'
    phrasing — adjective form 'Your 1-hour-and-30-minute timer'
    reads worse aloud."""
    t = Timer(
        id="abc", label=None, fire_at=time.time() + 60,
        total_seconds=5400, created_at=time.time(),
    )
    assert announcement_text(t) == (
        "Your timer for 1 hour and 30 minutes is up."
    )


# --- Tool confirm strings (spoken verbatim by the model) --------


def test_set_confirm_unlabelled_uses_natural_noun_form():
    """Avoid the awkward 'Set a 30 seconds timer' construction —
    'Set a timer for 30 seconds' is what humans actually say."""
    from jasper.tools.timer import _set_confirm
    t = Timer(
        id="abc", label=None, fire_at=time.time() + 30,
        total_seconds=30, created_at=time.time(),
    )
    assert _set_confirm(t) == "Set a timer for 30 seconds."


def test_set_confirm_labelled_says_label_first():
    from jasper.tools.timer import _set_confirm
    t = Timer(
        id="abc", label="pasta", fire_at=time.time() + 600,
        total_seconds=600, created_at=time.time(),
    )
    assert _set_confirm(t) == "Set a pasta timer for 10 minutes."


def test_cancel_confirm_labelled():
    from jasper.tools.timer import _cancel_confirm
    t = Timer(
        id="abc", label="pasta", fire_at=time.time() + 60,
        total_seconds=600, created_at=time.time(),
    )
    assert _cancel_confirm(t) == "Cancelled the pasta timer."


def test_cancel_confirm_unlabelled_uses_duration():
    """Without a label, the duration is the only handle the user has
    to identify which timer was cancelled."""
    from jasper.tools.timer import _cancel_confirm
    t = Timer(
        id="abc", label=None, fire_at=time.time() + 60,
        total_seconds=60, created_at=time.time(),
    )
    assert _cancel_confirm(t) == "Cancelled the timer for 1 minute."


def test_timer_remaining_seconds_floors_at_zero():
    t = Timer(
        id="abc", label=None, fire_at=time.time() - 10,
        total_seconds=300, created_at=time.time(),
    )
    assert t.remaining_seconds == 0


# --- TimerStore ---------------------------------------------------


def _tmp_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def test_store_round_trip_across_new_connection():
    """Restart-survival contract: a timer added, then read back from
    a freshly opened connection, comes back unchanged."""
    path = _tmp_db_path()
    try:
        s1 = TimerStore(path)
        t = Timer(
            id="abc12345", label="pasta", fire_at=1234567890.5,
            total_seconds=300, created_at=1234567880.0,
        )
        s1.add(t)
        s1.close()

        s2 = TimerStore(path)
        rows = s2.all()
        assert len(rows) == 1
        assert rows[0].id == "abc12345"
        assert rows[0].label == "pasta"
        assert rows[0].fire_at == pytest.approx(1234567890.5)
        assert rows[0].total_seconds == 300
        s2.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_store_remove_drops_row():
    path = _tmp_db_path()
    try:
        s = TimerStore(path)
        t = Timer(
            id="abc", label=None, fire_at=99999.0,
            total_seconds=60, created_at=99000.0,
        )
        s.add(t)
        s.remove("abc")
        assert s.all() == []
        s.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_store_all_orders_by_fire_at():
    path = _tmp_db_path()
    try:
        s = TimerStore(path)
        s.add(Timer(id="b", label=None, fire_at=2000.0, total_seconds=10, created_at=1000.0))
        s.add(Timer(id="a", label=None, fire_at=1500.0, total_seconds=10, created_at=1000.0))
        s.add(Timer(id="c", label=None, fire_at=2500.0, total_seconds=10, created_at=1000.0))
        ids = [t.id for t in s.all()]
        assert ids == ["a", "b", "c"]
        s.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- TimerScheduler -----------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_add_creates_timer_and_persists():
    path = _tmp_db_path()
    try:
        sched = TimerScheduler(db_path=path)
        t = sched.add(60, label="laundry")
        assert t.label == "laundry"
        assert t.total_seconds == 60
        assert t.id  # uuid hex prefix
        # Read back via a fresh store on the same DB to confirm
        # persistence happened synchronously inside add().
        s2 = TimerStore(path)
        rows = s2.all()
        assert len(rows) == 1
        assert rows[0].id == t.id
        s2.close()
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_add_rejects_zero_and_negative():
    path = _tmp_db_path()
    try:
        sched = TimerScheduler(db_path=path)
        with pytest.raises(ValueError):
            sched.add(0)
        with pytest.raises(ValueError):
            sched.add(-30)
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_list_active_sorted_by_fire_at():
    path = _tmp_db_path()
    try:
        sched = TimerScheduler(db_path=path)
        sched.add(3600, label="long")
        sched.add(60, label="short")
        sched.add(600, label="medium")
        active = sched.list_active()
        assert [t.label for t in active] == ["short", "medium", "long"]
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_cancel_by_exact_label():
    path = _tmp_db_path()
    try:
        sched = TimerScheduler(db_path=path)
        sched.add(60, label="pasta")
        sched.add(120, label="laundry")
        cancelled, matches = sched.cancel("pasta")
        assert cancelled is True
        assert len(matches) == 1
        assert matches[0].label == "pasta"
        # After cancel, only laundry remains.
        remaining = sched.list_active()
        assert [t.label for t in remaining] == ["laundry"]
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_cancel_ambiguous_returns_matches_without_cancelling():
    path = _tmp_db_path()
    try:
        sched = TimerScheduler(db_path=path)
        sched.add(60, label="pasta")
        sched.add(300, label="pasta")  # same label, multiple timers
        cancelled, matches = sched.cancel("pasta")
        assert cancelled is False
        assert len(matches) == 2
        # Both still active — caller must disambiguate.
        assert len(sched.list_active()) == 2
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_cancel_by_id_prefix():
    path = _tmp_db_path()
    try:
        sched = TimerScheduler(db_path=path)
        t = sched.add(60, label="pasta")
        # Use a 3-char prefix of the 8-char id — unique by construction.
        cancelled, matches = sched.cancel(t.id[:3])
        assert cancelled is True
        assert matches[0].id == t.id
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_cancel_not_found():
    path = _tmp_db_path()
    try:
        sched = TimerScheduler(db_path=path)
        sched.add(60, label="pasta")
        cancelled, matches = sched.cancel("nonexistent")
        assert cancelled is False
        assert matches == []
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_fires_callback_with_timer_record():
    """Core fire path: a 50ms timer triggers on_fire with the matching
    Timer record. Persistence row is removed after fire so the next
    start() doesn't replay it."""
    path = _tmp_db_path()
    fired: list[Timer] = []

    async def on_fire(t: Timer) -> None:
        fired.append(t)

    try:
        sched = TimerScheduler(on_fire=on_fire, db_path=path)
        t = sched.add(1, label="quick")  # 1-second timer
        # Wait a touch over 1s for the asyncio.sleep + callback.
        await asyncio.wait_for(_wait_for(lambda: fired), timeout=2.5)
        assert len(fired) == 1
        assert fired[0].id == t.id
        assert fired[0].label == "quick"
        # Active list is empty after fire.
        assert sched.list_active() == []
        # And the persistence row was removed.
        s2 = TimerStore(path)
        assert s2.all() == []
        s2.close()
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_start_drops_expired_timers():
    """A timer with fire_at in the past (daemon was down at fire time)
    must NOT fire on restore — drop it cleanly."""
    path = _tmp_db_path()
    fired: list[Timer] = []

    async def on_fire(t: Timer) -> None:
        fired.append(t)

    try:
        # Hand-write an expired row directly into the store.
        store = TimerStore(path)
        store.add(Timer(
            id="expired1", label="stale", fire_at=time.time() - 100,
            total_seconds=60, created_at=time.time() - 200,
        ))
        store.close()

        sched = TimerScheduler(on_fire=on_fire, db_path=path)
        await sched.start()
        # Give any (incorrectly) scheduled task room to run.
        await asyncio.sleep(0.1)
        assert fired == []
        assert sched.list_active() == []
        # Row must be gone from the store too.
        s2 = TimerStore(path)
        assert s2.all() == []
        s2.close()
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_start_restores_future_timers():
    """Future timers in the store must be restored to in-memory
    state and scheduled, so the daemon picks up where it left off."""
    path = _tmp_db_path()
    try:
        store = TimerStore(path)
        store.add(Timer(
            id="future1", label="future", fire_at=time.time() + 3600,
            total_seconds=3600, created_at=time.time(),
        ))
        store.close()

        sched = TimerScheduler(db_path=path)
        await sched.start()
        active = sched.list_active()
        assert len(active) == 1
        assert active[0].id == "future1"
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_stop_prevents_fire():
    """After stop(), a previously-scheduled timer must NOT fire even
    if its deadline elapses."""
    path = _tmp_db_path()
    fired: list[Timer] = []

    async def on_fire(t: Timer) -> None:
        fired.append(t)

    try:
        sched = TimerScheduler(on_fire=on_fire, db_path=path)
        sched.add(1, label="quick")  # 1-second timer
        await sched.stop()  # cancel before it can fire
        await asyncio.sleep(1.5)
        assert fired == []
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_pre_render_fires_on_add():
    """Pre-render hook is called as a background task whenever a
    timer is added — the daemon uses this to render+cache the
    fire-time announcement WAV before fire_at, so the actual fire
    is a cache hit and audio plays instantly."""
    path = _tmp_db_path()
    rendered: list[Timer] = []

    async def pre_render(t: Timer) -> None:
        rendered.append(t)

    try:
        sched = TimerScheduler(pre_render=pre_render, db_path=path)
        sched.add(60, label="laundry")
        # Pre-render task is fire-and-forget; give the loop a tick to
        # run it before asserting.
        await asyncio.sleep(0.02)
        assert len(rendered) == 1
        assert rendered[0].label == "laundry"
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_pre_render_fires_on_restored_timers():
    """When the daemon restarts and start() restores persisted
    timers, each restored timer also gets a pre-render — handles the
    case where the user switched providers between runs (cached
    cue's hash is now stale, must re-render in new voice)."""
    path = _tmp_db_path()
    rendered: list[Timer] = []

    async def pre_render(t: Timer) -> None:
        rendered.append(t)

    try:
        # Pre-seed a future timer in the store directly.
        store = TimerStore(path)
        store.add(Timer(
            id="restored1", label="dinner",
            fire_at=time.time() + 3600, total_seconds=3600,
            created_at=time.time(),
        ))
        store.close()

        sched = TimerScheduler(pre_render=pre_render, db_path=path)
        await sched.start()
        await asyncio.sleep(0.02)
        assert len(rendered) == 1
        assert rendered[0].id == "restored1"
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_pre_render_failure_doesnt_abort_add():
    """A failing pre_render must not break timer scheduling — the
    fire-time fallback (synthesise on demand) still works."""
    path = _tmp_db_path()
    fired: list[Timer] = []

    async def bad_pre_render(t: Timer) -> None:
        raise RuntimeError("simulated TTS outage")

    async def on_fire(t: Timer) -> None:
        fired.append(t)

    try:
        sched = TimerScheduler(
            on_fire=on_fire,
            pre_render=bad_pre_render,
            db_path=path,
        )
        sched.add(1, label="quick")
        # Pre-render task runs and explodes silently; timer still
        # fires its on_fire hook normally.
        await asyncio.wait_for(_wait_for(lambda: fired), timeout=2.5)
        assert len(fired) == 1
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_scheduler_set_on_fire_after_construction():
    """Late-bound on_fire works — daemon constructs scheduler before
    WakeLoop, then wires the callback once WakeLoop exists."""
    path = _tmp_db_path()
    fired: list[Timer] = []

    async def on_fire(t: Timer) -> None:
        fired.append(t)

    try:
        sched = TimerScheduler(db_path=path)  # no on_fire yet
        sched.add(1, label="quick")
        sched.set_on_fire(on_fire)
        await asyncio.wait_for(_wait_for(lambda: fired), timeout=2.5)
        assert len(fired) == 1
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- helpers ------------------------------------------------------


async def _wait_for(predicate, *, interval: float = 0.05) -> None:
    """Poll `predicate()` until truthy. Outer asyncio.wait_for caps
    overall wait time."""
    while not predicate():
        await asyncio.sleep(interval)
