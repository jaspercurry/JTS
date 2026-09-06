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
    """One structured event in a turn.

    `kind` is the event type — current vocabulary:

      session_open   payload={provider, model, system_instruction_hash}
      turn_start     payload={turn_id}
      audio_in       payload={n_bytes, sample_rate}
      tool_call      payload={name, args}
      tool_return    payload={name, result, elapsed_ms, error?}
      audio_out      payload={n_bytes}            (per chunk)
      turn_complete  payload={tokens}
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
    fresh `TurnTrace` per scenario invocation, installs it as the
    active trace via `set_active`, runs the turn, then reads `.events`
    for assertions and writes the transcript."""
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
        """Concatenated assistant-spoken text across the turn, built
        from `text_out` events.

        OpenAI Realtime emits these from `_dispatch_event` on every
        transcript delta; Grok inherits that dispatcher and gets them
        for free. Gemini Live exposes no final text transcript through
        this adapter (see `GeminiLiveTurn`), so this is always "" for
        Gemini turns — callers must treat empty as "not available for
        this provider," not "the model said nothing." For OpenAI and
        Grok, empty is also possible (audio without transcript
        deltas). The harness falls back to "skip text assertion" in
        both cases."""
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


# Module-level "active trace" — deliberately not surfaced to
# `jasper.voice.trace` as a ContextVar.
#
# Originally the production module held this state directly, as a
# `ContextVar`, intended to keep trace state task-local. That choice
# silently broke in practice: when the OpenAI adapter's `_receive_loop`
# task is spawned by `connection.start()`, it captures a snapshot of
# the current context at spawn time. The harness opens the connection
# BEFORE setting an active trace per turn, so the receive-loop task
# saw `None` forever — even after `set_active(trace)` ran later from
# the harness's own task. `traced_registry`'s wrappers run inside the
# receive-loop's task (the adapter dispatches tool calls there), so
# their `emit` calls would no-op, and tool calls never reached the
# trace.
#
# Confirmed 2026-05-21 by logging server events: OpenAI emitted
# `response.output_item.added` with `type: function_call,
# name: get_subway_arrivals` — i.e. the model called the tool — yet
# the ContextVar read `None` inside the wrapper, so the trace had
# `tool_call_records == []`.
#
# The fix carries over to this sink-based design: `set_active` installs
# `trace.append` itself as `jasper.voice.trace`'s sink — a plain
# module-level global, not a ContextVar-backed lookup. A closure over
# the `TurnTrace` object is visible to every task the instant it's
# installed, regardless of which task's context existed at spawn time;
# a ContextVar read from inside the sink would reproduce the exact
# bug above, since the receive-loop task's context predates the
# `set_active` call. The harness is single-process, single-event-loop,
# single-turn-at-a-time, so the ContextVar's task-isolation guarantee
# was never needed; the cross-task visibility absolutely was.
_active_trace: "TurnTrace | None" = None


def active() -> "TurnTrace | None":
    """Return the currently-active trace, or None if no tracing is on."""
    return _active_trace


def set_active(trace: "TurnTrace | None"):
    """Set the active trace and install its sink. Returns the previous
    value so the caller can `reset_active(token)` to restore.

    Asserts no two non-None traces are active at once. Today's voice-eval
    harness runs scenarios serially against one connection (the
    ``_connection_lock`` enforces single-flight), so this assertion is
    defensive only — but if a future maintainer adds concurrent
    `ask()` calls, the module-global sink would silently interleave
    events into whichever trace happened to be set last. The assertion
    turns that subtle data-corruption bug into a loud AssertionError at
    the source."""
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
    "active",
    "set_active",
    "reset_active",
]
