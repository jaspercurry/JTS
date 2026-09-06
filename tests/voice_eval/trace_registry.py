# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tool-registry wrapper that records calls onto the active `TurnTrace`.

`traced_registry` is eval-harness-only: it wraps every tool executor in
a registry so calls emit `tool_call` / `tool_return` events via
`jasper.voice.trace.emit`. Production registries are never wrapped —
`emit()` itself lives in `jasper.voice.trace` and stays a no-op there
when no trace is active."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from jasper.tools import ToolExecutor, ToolRegistry
from jasper.voice.trace import emit


@dataclass(frozen=True)
class _TracingExecutor:
    """ToolExecutor wrapper that records calls without changing execution."""

    name: str
    executor: ToolExecutor

    @property
    def fn(self) -> Callable[..., Any]:
        """Expose Python-callable compatibility when the wrapped executor has it."""
        fn = getattr(self.executor, "fn", None)
        if fn is None:
            raise AttributeError("tool executor has no Python function")
        return fn

    async def execute(self, args: dict[str, Any]) -> Any:
        started = time.monotonic()
        emit("tool_call", {"name": self.name, "args": dict(args)})
        try:
            result = await self.executor.execute(args)
        except Exception as e:  # noqa: BLE001
            emit("tool_return", {
                "name": self.name,
                "result": None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error": repr(e),
            })
            raise
        emit("tool_return", {
            "name": self.name,
            "result": result,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
        return result


def traced_registry(registry: ToolRegistry) -> ToolRegistry:
    """Return a new `ToolRegistry` with every tool executor wrapped
    to emit `tool_call` and `tool_return` events on the active trace.

    The original registry is unchanged. Production code receives the
    original; the harness receives the wrapped version. Adapter-side
    dispatch (`tool.executor.execute(args)`) is unchanged — the wrapping
    happens transparently inside the same call.

    Safe to call when no trace is active — the wrapper's emit calls
    are no-ops in that case. So the wrapped registry can be used in
    contexts where tracing is sometimes on and sometimes off."""
    new = ToolRegistry(
        tool_packs=dict(registry.tool_packs),
        pack_outcomes=list(registry.pack_outcomes),
        dispatch_observer=registry.dispatch_observer,
    )
    for name, tool in registry.tools.items():
        # Preserve the ToolDefinition unchanged — parameters, description,
        # providers — only the executor is wrapped for trace emission.
        new.tools[name] = replace(
            tool,
            executor=_TracingExecutor(tool.name, tool.executor),
        )
    return new
