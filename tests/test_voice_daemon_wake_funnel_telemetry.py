# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free integration coverage for the wake response/tool funnel."""

from __future__ import annotations

import asyncio


from jasper.tools import ToolRegistry, dispatch_tool
from jasper.voice_daemon import WakeLoop
from jasper.wake_events import WakeEventStore
from tests._async_wait import wait_signalled


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
        wake_loop = WakeLoop.for_tests(
            wake_event_store=store,
            current_event_id="evt-funnel",
        )

        async def get_weather() -> dict:
            """Return a test forecast."""
            return {"temperature": 72}

        registry = ToolRegistry()
        registry.register(get_weather)
        registry.set_dispatch_observer(
            wake_loop.record_tool_dispatch_stage,
        )

        await wake_loop._record_response_started()
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


async def test_concurrent_tools_preserve_first_call_and_completion_milestones(
    tmp_path,
):
    store = WakeEventStore(tmp_path)
    store.open()
    release_first = asyncio.Event()
    first_running = asyncio.Event()
    first_task: asyncio.Task | None = None
    try:
        await store.begin_event(
            event_id="evt-concurrent",
            trigger_kind="fire_aec_on",
            peak_score_aec_on=0.9,
            peak_score_aec_off=None,
            threshold=0.5,
            wake_model="jarvis_v2.onnx",
        )
        wake_loop = WakeLoop.for_tests(
            wake_event_store=store,
            current_event_id="evt-concurrent",
        )

        async def slow_first() -> dict:
            """Wait until the later tool has completed."""
            first_running.set()
            await release_first.wait()
            return {"first": True}

        async def fast_second() -> dict:
            """Complete while the first tool is still running."""
            return {"second": True}

        registry = ToolRegistry()
        registry.register(slow_first)
        registry.register(fast_second)
        registry.set_dispatch_observer(
            wake_loop.record_tool_dispatch_stage,
        )

        first_task = asyncio.create_task(
            dispatch_tool(registry, "slow_first", {}),
        )
        await wait_signalled(
            first_running,
            "first tool entering its body",
            producer=first_task,
        )
        assert await dispatch_tool(registry, "fast_second", {}) == {
            "second": True,
        }

        row = await store.get_event("evt-concurrent")
        assert row["tool_name"] == "slow_first"
        first_completion = row["ts_tool_completed"]
        assert first_completion is not None

        release_first.set()
        assert await first_task == {"first": True}
        row = await store.get_event("evt-concurrent")
        assert row["tool_name"] == "slow_first"
        assert row["ts_tool_completed"] == first_completion
    finally:
        release_first.set()
        if first_task is not None and not first_task.done():
            await asyncio.gather(first_task, return_exceptions=True)
        store.close()
