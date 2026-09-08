# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""See `TurnTrace` and `set_active` below for what this module provides."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from jasper.voice.trace import set_sink


@dataclass
class TraceEvent:
    """One provider or harness evidence event."""
    ts: float                # time.monotonic() at emission
    kind: str
    payload: dict[str, Any]


@dataclass
class TurnTrace:
    """Evidence retained for inspection; result facts live in TurnResult."""
    turn_id: str
    session_id: str
    provider: str
    started_at: float = field(default_factory=time.monotonic)
    events: list[TraceEvent] = field(default_factory=list)

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append(TraceEvent(time.monotonic(), kind, dict(payload)))

    def tool_calls(self) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == "tool_call"]

    def tool_returns(self) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == "tool_return"]

# The receive task predates each turn. A global sink stays visible across tasks;
# a ContextVar set by ask() would remain absent in that receive task.
_active_trace: "TurnTrace | None" = None


def active() -> TurnTrace | None:
    return _active_trace


def set_active(trace: "TurnTrace | None"):
    """Install a single-turn evidence sink and return the previous trace."""
    global _active_trace
    assert not (trace is not None and _active_trace is not None), (
        "turn_trace.set_active: another trace is already active "
        f"({_active_trace!r}); concurrent turns are not supported"
    )
    prev = _active_trace
    _active_trace = trace
    set_sink(trace.append if trace is not None else None)
    return prev


def reset_active(token) -> None:
    """Restore the trace to a previous value returned by `set_active`."""
    global _active_trace
    _active_trace = token
    set_sink(token.append if token is not None else None)


__all__ = [
    "TraceEvent",
    "TurnTrace",
    "set_active",
    "reset_active",
]
