# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.tools import Tool, ToolDefinition, ToolRegistry, dispatch_tool, tool
from jasper.voice import trace

from tests.voice_eval.trace_registry import traced_registry
from tests.voice_eval.turn_trace import TurnTrace, reset_active, set_active


async def test_traced_registry_wraps_explicit_executor_boundary():
    class ExplicitExecutor:
        async def execute(self, args):
            return {"got": args["x"]}

    original = ToolRegistry()
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
        executor=ExplicitExecutor(),
    )
    original.register_tool(built)

    wrapped = traced_registry(original)
    turn_trace = TurnTrace(turn_id="turn-1", session_id="session-1", provider="test")
    token = set_active(turn_trace)
    try:
        assert await dispatch_tool(wrapped, "echo", {"x": "hi"}) == {"got": "hi"}
    finally:
        reset_active(token)

    assert turn_trace.tool_calls()[0].payload == {"name": "echo", "args": {"x": "hi"}}
    assert turn_trace.tool_returns()[0].payload["name"] == "echo"
    assert turn_trace.tool_returns()[0].payload["result"] == {"got": "hi"}
    assert original.get("echo") is not wrapped.get("echo")


def test_traced_registry_preserves_python_fn_compatibility():
    @tool()
    async def echo(x: str) -> dict:
        """echo back the argument."""
        return {"got": x}

    original = ToolRegistry()
    original.register(echo)

    wrapped = traced_registry(original)

    assert wrapped.get("echo").fn is echo


def test_emit_is_noop_without_sink_then_forwards_once_sink_installed():
    received: list[tuple[str, dict]] = []
    trace.set_sink(None)
    try:
        trace.emit("no_listener", {"x": 1})  # must not raise; nothing to observe

        trace.set_sink(lambda kind, payload: received.append((kind, payload)))
        trace.emit("tool_call", {"name": "echo"})
    finally:
        trace.set_sink(None)

    assert received == [("tool_call", {"name": "echo"})]
