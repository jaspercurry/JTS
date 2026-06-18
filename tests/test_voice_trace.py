from __future__ import annotations

import pytest

from jasper.tools import Tool, ToolDefinition, ToolRegistry, dispatch_tool, tool
from jasper.voice.trace import TurnTrace, reset_active, set_active, traced_registry


@pytest.mark.asyncio
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
    trace = TurnTrace(turn_id="turn-1", session_id="session-1", provider="test")
    token = set_active(trace)
    try:
        assert await dispatch_tool(wrapped, "echo", {"x": "hi"}) == {"got": "hi"}
    finally:
        reset_active(token)

    assert trace.tool_calls()[0].payload == {"name": "echo", "args": {"x": "hi"}}
    assert trace.tool_returns()[0].payload["name"] == "echo"
    assert trace.tool_returns()[0].payload["result"] == {"got": "hi"}
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
