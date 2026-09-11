# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the cross-provider tool-dispatch contract.

`jasper.tools.dispatch_tool` is the single home for what every
voice-provider adapter (Gemini, OpenAI, and Grok via the OpenAI
subclass) does when the model calls a tool: enforce the per-tool
timeout, wrap scalar results, and shape failures into a speakable
``{"error": …}`` payload. Pinning that contract directly — hardware-free,
no live session — means a refactor of any provider seam can't silently
change what the model sees back.
"""
from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from tests._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_signalled, wait_until
from tests._log_events import event_fields
from jasper.tools import (
    DEFAULT_TOOL_TIMEOUT_SEC,
    Tool,
    ToolDefinition,
    ToolRegistry,
    build_tool,
    dispatch_tool,
    tool,
)


def _registry(*fns) -> ToolRegistry:
    reg = ToolRegistry()
    for fn in fns:
        reg.register(fn)
    return reg


async def test_dict_result_passes_through():
    async def echo(x: str) -> dict:
        """echo back the argument."""
        return {"got": x}

    reg = _registry(echo)
    assert await dispatch_tool(reg, "echo", {"x": "hi"}) == {"got": "hi"}


async def test_dispatch_runs_tool_executor_boundary():
    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        async def execute(self, args):
            self.calls.append(dict(args))
            return {"got": args["x"]}

    executor = RecordingExecutor()
    reg = ToolRegistry()
    built = Tool(
        definition=ToolDefinition(
            name="echo",
            description="echo back the argument.",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        ),
        executor=executor,
    )
    reg.register_tool(built)

    assert await dispatch_tool(reg, "echo", {"x": "hi"}) == {"got": "hi"}
    assert executor.calls == [{"x": "hi"}]


async def test_scalar_result_is_wrapped():
    async def answer() -> int:
        """return a scalar."""
        return 42

    reg = _registry(answer)
    # Scalars are wrapped so the model never sees a bare value.
    assert await dispatch_tool(reg, "answer", {}) == {"value": 42}


async def test_sync_tool_is_supported():
    def now() -> str:
        """a non-coroutine tool."""
        return "noon"

    reg = _registry(now)
    assert await dispatch_tool(reg, "now", {}) == {"value": "noon"}


async def test_unknown_tool_returns_error():
    assert await dispatch_tool(ToolRegistry(), "nope", {}) == {
        "error": "unknown tool nope",
    }


async def test_dispatch_observer_sees_registered_call_start_and_completion():
    async def echo(x: str) -> dict:
        """echo back the argument."""
        return {"got": x}

    events: list[tuple[str, str]] = []

    async def observe(stage: str, name: str) -> None:
        events.append((stage, name))

    reg = _registry(echo)
    reg.set_dispatch_observer(lambda: observe)

    assert await dispatch_tool(reg, "echo", {"x": "hi"}) == {"got": "hi"}
    assert events == [("called", "echo"), ("completed", "echo")]


async def test_dispatch_observer_completion_includes_tool_error_payloads():
    async def boom() -> dict:
        """always raises."""
        raise RuntimeError("kaboom")

    events: list[tuple[str, str]] = []

    async def observe(stage: str, name: str) -> None:
        events.append((stage, name))

    reg = _registry(boom)
    reg.set_dispatch_observer(lambda: observe)

    assert await dispatch_tool(reg, "boom", {}) == {"error": "kaboom"}
    assert events == [("called", "boom"), ("completed", "boom")]


async def test_dispatch_observer_ignores_unknown_tool_names():
    events: list[tuple[str, str]] = []

    async def observe(stage: str, name: str) -> None:
        events.append((stage, name))

    reg = ToolRegistry()
    reg.set_dispatch_observer(lambda: observe)

    assert await dispatch_tool(reg, "nope", {}) == {"error": "unknown tool nope"}
    assert events == []


async def test_dispatch_observer_failure_does_not_block_tool():
    async def echo() -> dict:
        """return a normal payload."""
        return {"ok": True}

    async def broken_observer(_stage: str, _name: str) -> None:
        raise OSError("telemetry disk unavailable")

    reg = _registry(echo)
    reg.set_dispatch_observer(lambda: broken_observer)

    assert await dispatch_tool(reg, "echo", {}) == {"ok": True}


async def test_dispatch_observer_timeout_does_not_block_tool(monkeypatch):
    import jasper.tools as tools_module

    async def echo() -> dict:
        """return a normal payload."""
        return {"ok": True}

    async def stuck_observer(_stage: str, _name: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(tools_module, "_DISPATCH_OBSERVER_TIMEOUT_SEC", 0.01)
    reg = _registry(echo)
    reg.set_dispatch_observer(lambda: stuck_observer)

    result = await asyncio.wait_for(
        dispatch_tool(reg, "echo", {}),
        timeout=0.2,
    )

    assert result == {"ok": True}


async def test_exception_becomes_error_payload():
    async def boom() -> dict:
        """always raises."""
        raise RuntimeError("kaboom")

    reg = _registry(boom)
    assert await dispatch_tool(reg, "boom", {}) == {"error": "kaboom"}


async def test_timeout_returns_error_and_respects_per_tool_budget():
    @tool(timeout=0.01)
    async def slow() -> dict:
        """sleeps far past its 10ms budget."""
        await asyncio.sleep(5)
        return {"never": True}

    reg = _registry(slow)
    events: list[tuple[str, str]] = []

    async def observe(stage: str, name: str) -> None:
        events.append((stage, name))

    reg.set_dispatch_observer(lambda: observe)
    # The tool's own 10ms budget must apply (not the 12s default), so this
    # resolves promptly into the speakable timeout error rather than
    # hanging the session.
    out = await asyncio.wait_for(dispatch_tool(reg, "slow", {}), timeout=2)
    assert out == {"error": "slow timed out"}
    assert events == [("called", "slow"), ("completed", "slow")]


@pytest.mark.parametrize("survives", [True, False])
async def test_survives_cancellation_decides_whether_a_cancelled_caller_abandons_a_tool(
    survives, monkeypatch,
):
    """A caller cancelled mid-dispatch — here before the executor is even
    reached — drops the tool's effect, unless the tool declares that
    cancellation never abandons it. The caller is cancelled either way."""
    import jasper.tools as tools_module

    # The observer notify is this test's coordination point; leave it parked
    # long enough to drive the race rather than racing its own budget.
    monkeypatch.setattr(
        tools_module, "_DISPATCH_OBSERVER_TIMEOUT_SEC", DEFAULT_SIGNAL_TIMEOUT_S,
    )
    ran, stages = [], []
    dispatching, resume = asyncio.Event(), asyncio.Event()

    @tool(survives_cancellation=survives)
    async def dismiss() -> dict:
        """records that it reached the executor."""
        ran.append("dismiss")
        return {"status": "conversation_ended"}

    async def observe(stage: str, _name: str) -> None:
        stages.append(stage)
        if stage == "called":
            dispatching.set()
            await resume.wait()

    reg = _registry(dismiss)
    reg.set_dispatch_observer(lambda: observe)

    call = asyncio.create_task(dispatch_tool(reg, "dismiss", {}))
    await wait_signalled(dispatching, "the dispatch reaching its observer", producer=call)
    call.cancel()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await call

    if survives:
        await wait_until(lambda: stages == ["called", "completed"])
        assert ran == ["dismiss"]
    else:
        assert (ran, stages) == ([], ["called"])


@pytest.mark.parametrize("retirement", ["cancel", "timeout"])
async def test_retired_executor_consumes_late_failure_without_repeating_observer(retirement):
    entered = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    failures, events = [], []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: failures.append(context))

    def fail_later():
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(DEFAULT_SIGNAL_TIMEOUT_S)
        raise RuntimeError("late executor failure")

    @tool(timeout=0.01 if retirement == "timeout" else DEFAULT_SIGNAL_TIMEOUT_S)
    async def slow() -> dict:
        """Fail after the caller retires."""
        return await asyncio.to_thread(fail_later)

    @tool(timeout=0.01)
    async def fast() -> dict:
        """Never run after its queued deadline expires."""
        raise AssertionError("expired queued executor started")

    async def observe(stage, name):
        events.append((stage, name))

    registry = _registry(slow, fast)
    registry.set_dispatch_observer(lambda: observe)
    caller = asyncio.create_task(dispatch_tool(registry, "slow", {}))
    try:
        await wait_signalled(entered, "executor thread entered", producer=caller)
        executor = registry._execution_task
        if retirement == "cancel":
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        else:
            assert await caller == {"error": "slow timed out"}
        expected = [("called", "slow")] + ([("completed", "slow")] if retirement == "timeout" else [])
        assert events == expected
        assert registry._execution_task is executor and not executor.done()
        assert await asyncio.wait_for(
            dispatch_tool(registry, "fast", {}), timeout=DEFAULT_SIGNAL_TIMEOUT_S,
        ) == {"error": "fast timed out"}
        expected += [("called", "fast"), ("completed", "fast")]
        assert events == expected
        assert registry._execution_task is executor and not executor.done()
        finish.set()
        await wait_until(lambda: registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        del executor
        assert failures == []
        assert events == expected
    finally:
        finish.set()
        await wait_until(lambda: registry._execution_task is None, timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        loop.set_exception_handler(previous_handler)


async def test_redacted_tool_payload_omits_body_text_from_info_logs(caplog):
    @tool(log_payload=False)
    async def read_private_message() -> dict:
        """Return sensitive content."""
        return {
            "ok": True,
            "subject": "dentist appointment",
            "body": "Your appointment is Tuesday at 9.",
        }

    reg = _registry(read_private_message)
    with caplog.at_level(logging.INFO, logger="jasper.tools"):
        out = await dispatch_tool(reg, "read_private_message", {})

    assert out["body"] == "Your appointment is Tuesday at 9."
    assert event_fields(caplog, "tool.dispatch_done")["payload"].startswith(
        "<redacted len="
    )
    assert "dentist appointment" not in caplog.text
    assert "Your appointment is Tuesday" not in caplog.text


async def test_redacted_tool_args_omit_user_text_from_info_logs(caplog):
    @tool(log_args=False)
    async def relay_user_phrase(query: str) -> dict:
        """Forward a private user phrase."""
        return {"ok": True}

    reg = _registry(relay_user_phrase)
    with caplog.at_level(logging.INFO, logger="jasper.tools"):
        out = await dispatch_tool(
            reg,
            "relay_user_phrase",
            {"query": "turn on the bedroom lights"},
        )

    assert out == {"ok": True}
    assert event_fields(caplog, "tool.dispatch_start")["args"].startswith(
        "<redacted keys=query len="
    )
    assert "turn on the bedroom lights" not in caplog.text


async def test_unknown_tool_args_are_value_redacted(caplog):
    with caplog.at_level(logging.WARNING, logger="jasper.tools"):
        out = await dispatch_tool(
            ToolRegistry(),
            "missing_tool",
            {"query": "unlock the front door"},
        )

    assert out == {"error": "unknown tool missing_tool"}
    assert event_fields(caplog, "tool.dispatch_unknown")["args"].startswith(
        "<redacted keys=query len="
    )
    assert "unlock the front door" not in caplog.text


def test_default_timeout_is_single_sourced():
    """A tool that doesn't override `timeout` inherits the one constant."""
    def plain() -> str:
        """no timeout override."""
        return "x"

    assert build_tool(plain).timeout == DEFAULT_TOOL_TIMEOUT_SEC
