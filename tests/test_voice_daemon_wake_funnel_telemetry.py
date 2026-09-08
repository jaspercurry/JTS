# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free integration coverage for the wake response/tool funnel."""

from __future__ import annotations

import asyncio


from jasper.tools import ToolRegistry, dispatch_tool
from jasper.wake_events import WakeEventStore
from tests._async_wait import wait_signalled
from tests._wake_loop import wake_loop_for_tests


async def test_shared_dispatch_observer_populates_active_wake_event(tmp_path):
    store = WakeEventStore(tmp_path)
    store.open()
    try:
        await store.begin_event(
            event_id="evt-funnel",
            trigger_kind="fire_aec_on",
            peak_score_aec_on=0.9,
            peak_score_aec_off=None,
            threshold=0.5,
            wake_model="jarvis_v2.onnx",
        )
        wake_loop = wake_loop_for_tests(
            wake_event_store=store,
            current_event_id="evt-funnel",
        )

        async def get_weather() -> dict:
            """Return a test forecast."""
            return {"temperature": 72}

        registry = ToolRegistry()
        registry.register(get_weather)
        registry.set_dispatch_observer(
            wake_loop.bind_tool_dispatch,
        )

        wake_loop._anchor_turn_timeline(1.0)
        await wake_loop._turn_observer("first_response", event_stage="response_started")()
        assert await dispatch_tool(registry, "get_weather", {}) == {
            "temperature": 72,
        }

        row = await store.get_event("evt-funnel")
        assert row["ts_response_started"] is not None
        assert row["ts_tool_called"] is not None
        assert row["ts_tool_completed"] is not None
        assert row["tool_name"] == "get_weather"
    finally:
        store.close()


async def test_queued_tools_preserve_first_call_and_completion_milestones(
    tmp_path,
):
    store = WakeEventStore(tmp_path)
    store.open()
    release_first, release_second = asyncio.Event(), asyncio.Event()
    first_running = asyncio.Event()
    tasks: list[asyncio.Task] = []
    try:
        await store.begin_event(
            event_id="evt-concurrent",
            trigger_kind="fire_aec_on",
            peak_score_aec_on=0.9,
            peak_score_aec_off=None,
            threshold=0.5,
            wake_model="jarvis_v2.onnx",
        )
        wake_loop = wake_loop_for_tests(
            wake_event_store=store,
            current_event_id="evt-concurrent",
        )

        async def slow_first() -> dict:
            """Return the first result after release."""
            first_running.set()
            await release_first.wait()
            return {"first": True}

        async def fast_second() -> dict:
            """Return the second result after release."""
            await release_second.wait()
            return {"second": True}

        registry = ToolRegistry()
        registry.register(slow_first)
        registry.register(fast_second)
        registry.set_dispatch_observer(
            wake_loop.bind_tool_dispatch,
        )

        first_task = asyncio.create_task(
            dispatch_tool(registry, "slow_first", {}),
        )
        tasks.append(first_task)
        await wait_signalled(
            first_running,
            "first tool entering its body",
            producer=first_task,
        )
        second_task = asyncio.create_task(dispatch_tool(registry, "fast_second", {}))
        tasks.append(second_task)
        release_first.set()
        assert await first_task == {"first": True}

        row = await store.get_event("evt-concurrent")
        assert row["tool_name"] == "slow_first"
        first_completion = row["ts_tool_completed"]
        assert first_completion is not None

        release_second.set()
        assert await second_task == {"second": True}
        row = await store.get_event("evt-concurrent")
        assert row["tool_name"] == "slow_first"
        assert row["ts_tool_completed"] == first_completion
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


async def test_actual_fire_ids_and_delayed_observers_stay_with_their_turn(tmp_path, monkeypatch):
    import sqlite3
    import time
    from contextlib import closing
    from datetime import datetime, timezone
    from jasper import wake_events
    from jasper.wake_condition_context import classify_condition

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(wake_events, "datetime", FrozenDateTime)
    store = WakeEventStore(tmp_path)
    store.open()
    wl = wake_loop_for_tests(wake_event_store=store)
    fire = dict(leg="on", score=0.9, now_loop=1.0, legs={}, firing_threshold=0.5,
                fired_legs="on", condition=classify_condition(None, None), mic_muted=False)
    try:
        await store._result(store._execute, "PRAGMA busy_timeout=5000", ())
        with closing(sqlite3.connect(store._db_path, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            first = await wl._wake_telemetry.on_fire(**fire)
            wl._anchor_turn_timeline(time.monotonic())
            response = wl._turn_observer("first_response", event_stage="response_started")
            write = wl._turn_observer("first_write")
            chirp = wl._play_listening_chirp(going_on=True)
            await wl._wake_telemetry.outcome("completed")
            second = await wl._wake_telemetry.on_fire(**fire)
            wl._anchor_turn_timeline(time.monotonic())
            assert first != second
            assert wl.session_status()["turn_event_id"] == second
            await response()
            await write()
            await chirp
            assert wl._turn_timeline_ms().keys() == {"total_ms"}
            assert wl.session_status()["wake_event_store"]["pending_work"] > 0
            lock.execute("ROLLBACK")
        assert (await store.get_event(first))["outcome"] == "completed"
        assert (await store.get_event(second))["ts_response_started"] is None
        await wl._turn_observer("first_response", event_stage="response_started")()
        assert (await store.get_event(second))["ts_response_started"] is not None
        wl._emit_turn_timeline("complete")
        assert wl.session_status()["last_turn_ms"]["event_id"] == second
    finally:
        await store.aclose()


async def test_tool_completion_from_old_event_cannot_stamp_new_event(tmp_path):
    store = WakeEventStore(tmp_path)
    store.open()
    release, started = asyncio.Event(), asyncio.Event()
    task = None
    try:
        for event_id in ("A", "B"):
            await store.begin_event(
                event_id=event_id, trigger_kind="fire_aec_on", peak_score_aec_on=0.9,
                peak_score_aec_off=None, threshold=0.5, wake_model="test",
            )
        wl = wake_loop_for_tests(wake_event_store=store, current_event_id="A")
        async def slow() -> dict:
            """Wait for another turn."""
            started.set()
            await release.wait()
            return {"ok": True}
        registry = ToolRegistry()
        registry.register(slow)
        registry.set_dispatch_observer(wl.bind_tool_dispatch)
        task = asyncio.create_task(dispatch_tool(registry, "slow", {}))
        await wait_signalled(started, "tool A started", producer=task)
        await wl._wake_telemetry.outcome("completed")
        wl._wake_telemetry._current_event_id = "B"
        release.set()
        assert await task == {"ok": True}
        old, new = await store.get_event("A"), await store.get_event("B")
        assert old["ts_tool_called"] is not None
        assert old["ts_tool_completed"] is new["ts_tool_called"] is new["ts_tool_completed"] is None
    finally:
        release.set()
        if task is not None:
            await task
        await store.aclose()


async def test_wake_admission_and_arbitration_continue_with_sqlite_locked(tmp_path):
    import sqlite3
    from contextlib import closing
    from tests._live_turn_fake import silent_frame

    store = WakeEventStore(tmp_path)
    store.open()
    wl = wake_loop_for_tests(wake_event_store=store)
    wl._legs["on"].detector.score_frame = lambda _frame: 0.95
    arbitrated = asyncio.Event()
    event_id = None
    async def lose(**_kwargs):
        nonlocal event_id
        event_id = wl._wake_telemetry.current_event_id
        arbitrated.set()
        return "LOSE"
    wl._peering.arbitrate = lose
    try:
        await store._result(store._execute, "PRAGMA busy_timeout=5000", ())
        with closing(sqlite3.connect(store._db_path, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            await wl._handle_wake_frame(silent_frame(), leg="on")
            await wait_signalled(arbitrated, "wake arbitration with SQLite held")
            assert event_id is not None
            lock.execute("ROLLBACK")
        await wl._cancel_fire_and_forget_tasks()
        assert (await store.get_event(event_id))["outcome"] == "peer_lost"
    finally:
        await wl._cancel_fire_and_forget_tasks()
        await store.aclose()
