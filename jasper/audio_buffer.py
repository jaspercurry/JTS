# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for cross-reset audio buffering in the wake → turn flow.

When the daemon receives a wake event during a long-running idle
period, the underlying voice provider may need to reopen its
session before it can accept audio (Gemini Live: ~3 s context
reset; OpenAI Realtime: similar; xAI Grok: similar). The mic
loop can't block on that or it drops audio frames into either
sounddevice's OS-level queue (where they later arrive in a burst,
long after the audio they carry) or off the floor entirely —
either way the user's command gets clipped.

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

import logging
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

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


logger = logging.getLogger(__name__)


class DrainResult(NamedTuple):
    """What the drain sent, and what its VAD saw while sending it.

    ``run_frames`` / ``run_peak`` are the speech run STILL OPEN when the
    buffer ran dry — the caller carries them into the live mic path so one
    utterance cut by the acquire boundary keeps accumulating instead of
    restarting. ``max_score`` is the turn-so-far maximum (never reset by a
    silence gap) and feeds the abort log + ``wake_events.max_silero_aec``.
    """

    frames: int
    run_frames: int
    run_peak: float
    max_score: float


async def drain_acquire_buffer(
    buffer: deque, turn: "LiveTurn",
    *,
    vad_predict: Callable[[Any], float] | None = None,
    speech_threshold: float,
) -> DrainResult:
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

    If ``vad_predict`` is provided, score each frame and accumulate the
    run of consecutive frames at or above ``speech_threshold``. This
    helper does NOT decide whether that run admits speech: the acquire
    window and the live mic stream are two halves of one utterance, so
    the gate is evaluated once, by the caller, over the combined run
    (``WakeLoop._arm_speech_if_admitted``). Stateful VADs (Silero) also
    benefit from getting the acquire frames in order — the LSTM is warm
    when live frames start.

    A ``vad_predict`` failure disables scoring for the rest of the drain
    but never stops the drain: the frames are the user's command, and
    dropping them costs a turn. ``send_audio`` failures still propagate
    unchanged so the caller can log + clear the remaining frames;
    partially-drained buffer state stays as-is on raise.
    """
    count = 0
    run_frames = 0
    run_peak = 0.0
    max_score = 0.0
    while buffer:
        frame = buffer.popleft()
        await turn.send_audio(frame.tobytes())
        count += 1
        if vad_predict is None:
            continue
        try:
            prob = vad_predict(frame)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "acquire-buffer drain: VAD scoring failed on frame %d "
                "(%s: %s); forwarding the rest unscored",
                count, type(e).__name__, e,
            )
            vad_predict = None
            continue
        max_score = max(max_score, prob)
        if prob >= speech_threshold:
            run_frames += 1
            run_peak = max(run_peak, prob)
        else:
            run_frames = 0
            run_peak = 0.0
    return DrainResult(count, run_frames, run_peak, max_score)
