# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-turn structured tracing for the voice loop.

The voice-eval harness records what happened during a turn — every tool
call (with args and result), every audio chunk in/out, session
open/close — by setting a `TurnTrace` on a module-level "active trace"
global for the duration of the turn, and reading it back when the turn
ends. (See the comment above `_active_trace` for why this is a module
global rather than a `ContextVar`.)

The schema is intentionally simple and provider-agnostic so the same
shape can be emitted by either the synthetic test path or the live
daemon path. Production trace ingestion (taking real user sessions
and converting them into regression scenarios) is V2; the schema is
ready for it today.

Importing this module adds zero overhead and zero behaviour change to
the daemon when no trace is active — the module global defaults to
`None`, the emit helpers no-op when nothing is listening, and the
`traced_registry` wrapper is opt-in. In production, `emit()` is called
directly from `jasper/voice/openai_session.py`'s `_dispatch_event` on
every `response.audio_transcript.delta` /
`response.output_audio_transcript.delta` / `response.output_text.delta`
event (inherited by the Grok adapter too) — it is not test-only, though
it stays a no-op there unless a trace has been set active.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from ..tools import ToolExecutor, ToolRegistry


@dataclass
class TraceEvent:
    """One structured event in a turn.

    `kind` is the event type — current vocabulary:

      session_open   payload={provider, model, system_instruction_hash}
      turn_start     payload={turn_id}
      audio_in       payload={n_bytes, sample_rate}
      tool_call      payload={name, args}
      tool_return    payload={name, result, elapsed_ms, error?}
      audio_out      payload={n_bytes}            (per chunk)
      turn_complete  payload={tokens, usage_breakdown?}
      turn_end       payload={reason}             (release/lost/error)
      session_close  payload={reason}

    Add new kinds as needed — consumers should ignore unknown kinds
    so we can extend without coordinated changes."""
    ts: float                # time.monotonic() at emission
    kind: str
    payload: dict[str, Any]


@dataclass
class TurnTrace:
    """A complete record of one voice turn.

    Mutable during the turn, snapshotted after. The harness creates a
    fresh `TurnTrace` per scenario invocation, sets it as the active
    trace via `set_active`, runs the turn, then reads `.events` for
    assertions and writes the transcript."""
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

    def spoken_text(self) -> str:
        """Concatenated assistant-spoken text across the turn.

        Built from `text_out` events that each provider's adapter
        emits when the server sends transcript deltas alongside
        audio. All three current providers (Gemini Live, OpenAI
        Realtime, xAI Grok Voice Agent) stream these natively —
        no STT pass needed, no Whisper dependency, 100% accurate
        (this IS the text the model emitted, not a transcription).

        Empty string when no text deltas arrived — meaningful in
        its own right (model produced audio without transcripts,
        or this provider's text channel isn't wired). The harness
        falls back to "skip text assertion" when this happens."""
        return "".join(
            e.payload.get("delta") or ""
            for e in self.events
            if e.kind == "text_out"
        )

    def tool_pairs(self) -> list[tuple[TraceEvent, TraceEvent | None]]:
        """Pair each tool_call with its matching tool_return (by name and
        order). Returns are matched FIFO per name — handles the case
        where the model calls the same tool twice in one turn."""
        pending: dict[str, list[TraceEvent]] = {}
        pairs: list[tuple[TraceEvent, TraceEvent | None]] = []
        for ev in self.events:
            if ev.kind == "tool_call":
                pairs.append((ev, None))
                pending.setdefault(ev.payload["name"], []).append(ev)
            elif ev.kind == "tool_return":
                callers = pending.get(ev.payload["name"]) or []
                if not callers:
                    continue
                call = callers.pop(0)
                for i, (c, r) in enumerate(pairs):
                    if c is call:
                        pairs[i] = (c, ev)
                        break
        return pairs


# Module-level "active trace" — deliberately NOT a ContextVar.
#
# Originally this was `ContextVar`, intended to keep trace state
# task-local. That choice silently broke in practice: when the
# OpenAI adapter's `_receive_loop` task is spawned by
# `connection.start()`, it captures a snapshot of the current
# context at spawn time. The harness opens the connection BEFORE
# setting an active trace per turn, so the receive-loop task sees
# `None` forever — even when the harness later calls
# `set_active(trace)` from its own task. The wrapper functions in
# `traced_registry` run inside the receive-loop's task (the adapter
# dispatches tool calls there), so their `emit` calls would no-op,
# and tool calls never reached the trace.
#
# Confirmed 2026-05-21 by logging server events: OpenAI emitted
# `response.output_item.added` with `type: function_call,
# name: get_subway_arrivals` — i.e. the model called the tool —
# yet `_active.get()` returned `None` inside the wrapper, so the
# trace had `tool_call_records == []`.
#
# Switching to a module-level global trades ContextVar's task-isolation
# guarantee for cross-task visibility. The harness is single-process,
# single-event-loop, single-turn-at-a-time, so the isolation was
# never needed; the visibility absolutely was.
_active_trace: "TurnTrace | None" = None


def active() -> "TurnTrace | None":
    """Return the currently-active trace, or None if no tracing is on."""
    return _active_trace


def set_active(trace: "TurnTrace | None"):
    """Set the active trace. Returns the previous value so the caller
    can `reset_active(token)` to restore — same set/reset shape as the
    old ContextVar API.

    Asserts no two non-None traces are active at once. Today's voice-eval
    harness runs scenarios serially against one connection (the
    ``_connection_lock`` enforces single-flight), so this assertion is
    defensive only — but if a future maintainer adds concurrent
    `ask()` calls, the existing module-global pattern would silently
    interleave events into whichever trace happened to be set last.
    The assertion turns that subtle data-corruption bug into a loud
    AssertionError at the source."""
    global _active_trace
    assert not (trace is not None and _active_trace is not None), (
        "trace.set_active: another trace is already active "
        f"({_active_trace!r}); concurrent turns are not supported"
    )
    prev = _active_trace
    _active_trace = trace
    return prev


def reset_active(token) -> None:
    """Restore the trace to a previous value returned by `set_active`."""
    global _active_trace
    _active_trace = token


def emit(kind: str, payload: dict[str, Any] | None = None) -> None:
    """Append an event to the active trace. No-op when nothing is
    listening (the common production case)."""
    trace = _active_trace
    if trace is None:
        return
    trace.append(kind, payload or {})


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
    )
    for name, tool in registry.tools.items():
        # Preserve the ToolDefinition unchanged — parameters, description,
        # providers — only the executor is wrapped for trace emission.
        new.tools[name] = replace(
            tool,
            executor=_TracingExecutor(tool.name, tool.executor),
        )
    return new


__all__ = [
    "TraceEvent",
    "TurnTrace",
    "active",
    "set_active",
    "reset_active",
    "emit",
    "traced_registry",
]
