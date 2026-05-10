"""Helpers for cross-reset audio buffering in the wake → turn flow.

When the daemon receives a wake event during a long-running idle
period, the underlying voice provider may need to reopen its
session before it can accept audio (Gemini Live: ~3 s context
reset; OpenAI Realtime: similar; xAI Grok: similar). The mic
loop can't block on that or it drops audio frames into either
sounddevice's OS-level queue (where they later arrive as a burst
that confuses our wall-clock-based VAD) or off the floor entirely
— either way the user's command gets clipped.

The fix is the canonical voice-agent pattern: capture frames into
a bounded buffer during the wake → turn-acquired window, then
replay them into the turn in FIFO order before live frames take
over. LiveKit's "instant connect" pre-connect audio buffer and
Pipecat's `CartesiaSTTService` / `DeepgramSTTService` reconnection
buffers are the reference implementations.

This module hosts the small drain primitive that the wake loop
spawns as part of its background acquire task. Kept separate from
`voice_daemon` so it's unit-testable without dragging in
sounddevice / openwakeword / camilladsp.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .voice.session import LiveTurn


# 250 frames @ 80 ms/frame = 20 s. The vast majority of acquire
# windows are <100 ms (warm session); the slow path is a context
# reset (~3 s) or a network blip mid-reconnect (~1 s); even at 5 s
# we'd only need ~63 frames. 20 s is a generous upper bound that
# still caps memory at ~80 KB (250 × 320 bytes per frame at 16 kHz
# mono int16 with 80 ms duration). If acquire takes longer than
# 20 s we accept losing the leading audio — the alternative would
# be unbounded memory growth on a wedged connection.
ACQUIRE_BUFFER_MAX_FRAMES = 250


async def drain_acquire_buffer(
    buffer: deque, turn: "LiveTurn",
) -> int:
    """Pop frames from `buffer` and forward each via
    ``turn.send_audio`` in FIFO order. Loops until the buffer is
    briefly empty.

    Concurrent appends from another coroutine (the mic loop adds
    frames while this drains) are picked up: the ``while`` check
    is synchronous, so any frame appended during a ``send_audio``
    await is visible on the next iteration. The sync exit + the
    caller's state-flag clear (in
    ``WakeLoop._acquire_and_drain``) happen without yielding, so
    no frame is appended after the drain decides it's done.

    Returns the count drained. Propagates any ``send_audio``
    exception unchanged so the caller can log + clear remaining
    frames; partially-drained buffer state stays as-is on raise.
    """
    count = 0
    while buffer:
        frame = buffer.popleft()
        await turn.send_audio(frame.tobytes())
        count += 1
    return count
