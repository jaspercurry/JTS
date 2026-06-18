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

import pytest

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


@pytest.mark.asyncio
async def test_dict_result_passes_through():
    async def echo(x: str) -> dict:
        """echo back the argument."""
        return {"got": x}

    reg = _registry(echo)
    assert await dispatch_tool(reg, "echo", {"x": "hi"}) == {"got": "hi"}


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_scalar_result_is_wrapped():
    async def answer() -> int:
        """return a scalar."""
        return 42

    reg = _registry(answer)
    # Scalars are wrapped so the model never sees a bare value.
    assert await dispatch_tool(reg, "answer", {}) == {"value": 42}


@pytest.mark.asyncio
async def test_sync_tool_is_supported():
    def now() -> str:
        """a non-coroutine tool."""
        return "noon"

    reg = _registry(now)
    assert await dispatch_tool(reg, "now", {}) == {"value": "noon"}


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    assert await dispatch_tool(ToolRegistry(), "nope", {}) == {
        "error": "unknown tool nope",
    }


@pytest.mark.asyncio
async def test_exception_becomes_error_payload():
    async def boom() -> dict:
        """always raises."""
        raise RuntimeError("kaboom")

    reg = _registry(boom)
    assert await dispatch_tool(reg, "boom", {}) == {"error": "kaboom"}


@pytest.mark.asyncio
async def test_timeout_returns_error_and_respects_per_tool_budget():
    @tool(timeout=0.01)
    async def slow() -> dict:
        """sleeps far past its 10ms budget."""
        await asyncio.sleep(5)
        return {"never": True}

    reg = _registry(slow)
    # The tool's own 10ms budget must apply (not the 12s default), so this
    # resolves promptly into the speakable timeout error rather than
    # hanging the session.
    out = await asyncio.wait_for(dispatch_tool(reg, "slow", {}), timeout=2)
    assert out == {"error": "slow timed out"}


@pytest.mark.asyncio
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
    assert "payload=<redacted len=" in caplog.text
    assert "dentist appointment" not in caplog.text
    assert "Your appointment is Tuesday" not in caplog.text


@pytest.mark.asyncio
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
    assert "args=<redacted keys=query len=" in caplog.text
    assert "turn on the bedroom lights" not in caplog.text


@pytest.mark.asyncio
async def test_unknown_tool_args_are_value_redacted(caplog):
    with caplog.at_level(logging.WARNING, logger="jasper.tools"):
        out = await dispatch_tool(
            ToolRegistry(),
            "missing_tool",
            {"query": "unlock the front door"},
        )

    assert out == {"error": "unknown tool missing_tool"}
    assert "args=<redacted keys=query len=" in caplog.text
    assert "unlock the front door" not in caplog.text


def test_default_timeout_is_single_sourced():
    """A tool that doesn't override `timeout` inherits the one constant."""
    def plain() -> str:
        """no timeout override."""
        return "x"

    assert build_tool(plain).timeout == DEFAULT_TOOL_TIMEOUT_SEC
