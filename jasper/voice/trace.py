# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Production seam for per-turn tracing: `emit()` plus an installable sink.

Zero overhead, zero behaviour change when no sink is installed —
`emit()` no-ops. Called from `openai_session.py`'s `_dispatch_event`
on every transcript-delta event (inherited by Grok); never test-only.

`TurnTrace`, the active-trace bookkeeping, and the tool-registry
wrapper that calls `emit` live with the eval harness instead, in
`tests/voice_eval/turn_trace.py` and `tests/voice_eval/trace_registry.py`."""
from __future__ import annotations

from typing import Any, Callable

_sink: "Callable[[str, dict[str, Any]], None] | None" = None


def set_sink(sink: "Callable[[str, dict[str, Any]], None] | None") -> None:
    """Install (or, with `None`, clear) the module-level trace sink."""
    global _sink
    _sink = sink


def emit(kind: str, payload: dict[str, Any] | None = None) -> None:
    """Forward one event to the installed sink. No-op when nothing is
    listening (the common production case)."""
    if _sink is not None:
        _sink(kind, payload or {})


__all__ = ["emit", "set_sink"]
