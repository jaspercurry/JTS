# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tool-call tracing for the eval harness — see `traced_registry`."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from jasper.tools import ToolExecutor, ToolRegistry
from .turn_trace import active


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    result: Any = None
    elapsed_ms: int = 0
    error: str | None = None


@dataclass(frozen=True)
class _TracingExecutor:
    """ToolExecutor wrapper that records calls without changing execution."""

    name: str
    executor: ToolExecutor
    records: Callable[[], list[ToolCallRecord]] | None = None

    @property
    def fn(self) -> Callable[..., Any]:
        """Expose Python-callable compatibility when the wrapped executor has it."""
        fn = getattr(self.executor, "fn", None)
        if fn is None:
            raise AttributeError("tool executor has no Python function")
        return fn

    async def execute(self, args: dict[str, Any]) -> Any:
        started = time.monotonic()
        trace = active()

        def emit(kind: str, payload: dict) -> None:
            if trace is not None:
                trace.append(kind, payload)

        record = ToolCallRecord(self.name, dict(args))
        if self.records is not None:
            self.records().append(record)
        emit("tool_call", {"name": self.name, "args": dict(args)})
        try:
            result = await self.executor.execute(args)
        except Exception as e:  # noqa: BLE001
            record.error = repr(e)
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            emit("tool_return", {
                "name": self.name,
                "result": None,
                "elapsed_ms": record.elapsed_ms,
                "error": record.error,
            })
            raise
        record.result = result
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        emit("tool_return", {
            "name": self.name,
            "result": result,
            "elapsed_ms": record.elapsed_ms,
        })
        return result


def traced_registry(
    registry: ToolRegistry, *, records: Callable[[], list[ToolCallRecord]] | None = None,
) -> ToolRegistry:
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
            executor=_TracingExecutor(tool.name, tool.executor, records),
        )
    return new
