# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time

from ..audio_io import TtsPlayout
from ..log_event import log_event
from .session import AudioOutChunk, LiveTurn

logger = logging.getLogger("jasper.voice_daemon")


async def _turn_audio_chunks(turn: LiveTurn):
    chunks = getattr(turn, "audio_out_chunks", None)
    if callable(chunks):
        async for chunk in chunks():
            if isinstance(chunk, bytes):
                chunk = AudioOutChunk(pcm=chunk)
            yield chunk
        return
    async for pcm in turn.audio_out():
        yield AudioOutChunk(pcm=pcm)


async def _play_responses(
    turn: LiveTurn,
    tts: TtsPlayout,
) -> None:
    """Drain turn.audio_out() to the speaker. Barge-in handling: race
    each write against an interrupt signal so a user-interrupted-the-model
    event immediately cancels in-flight playback and flushes the audio
    buffer. Without this, ALSA/sounddevice buffering causes 100-300ms of
    overrun where the model talks over the user.

    Cleanup contract: both per-iteration helpers (the interrupt waiter
    and the in-flight write) MUST be cancelled and awaited before this
    function returns, otherwise they leak as `Task destroyed but it is
    pending` warnings. The waiter is held alive by a reference cycle
    through `turn._interrupt_event`, so dropping the local without
    explicit cleanup means GC eventually breaks the cycle and Task.__del__
    fires. The OpenAI / Grok adapters never set `_interrupt_event` (no
    barge-in implemented), so the waiter is always pending at turn end
    and the leak would fire every turn without this try/finally."""
    interrupt_task: asyncio.Task | None = None
    write_task: asyncio.Task | None = None
    try:
        async for chunk in _turn_audio_chunks(turn):
            if interrupt_task is None or interrupt_task.done():
                interrupt_task = asyncio.create_task(turn.wait_for_interrupt())
            write_task = asyncio.create_task(
                tts.write_segment(
                    chunk.pcm,
                    provider_item_id=chunk.provider_item_id,
                    segment_kind=chunk.kind,
                )
            )
            done, _ = await asyncio.wait(
                {write_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupt_task in done:
                write_task.cancel()
                try:
                    await write_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                ack = await tts.flush()
                flush_handler = getattr(turn, "on_tts_flush", None)
                if callable(flush_handler):
                    await flush_handler(ack)
                if ack is not None:
                    log_event(
                        logger,
                        "tts_flush.playout_ack",
                        max_audio_played_ms=ack.get("max_audio_played_ms"),
                        segments=ack.get("segments"),
                        flushed_frames=ack.get("flushed_frames"),
                    )
                turn.clear_interrupted()
                interrupt_task = None
            elif write_task in done:
                try:
                    await write_task
                except Exception as e:  # noqa: BLE001
                    log_event(
                        logger,
                        "tts_write.failed",
                        error=type(e).__name__,
                        detail=str(e),
                        level=logging.WARNING,
                    )
                    raise
                finally:
                    write_task = None
            if write_task is not None and write_task.done():
                write_task = None
        await tts.end_segment()
        # Block until the last sample we wrote has cleared the OS
        # audio stack — see TtsPlayout.wait_drained. Cheap if the ring
        # is already empty; otherwise a single sleep for the residual.
        # Anchors on samples queued (not network arrivals), so an
        # OpenAI-style burst delivery and a Gemini-style real-time
        # pacing both end the turn at the right moment.
        await tts.wait_drained()
    finally:
        for t in (interrupt_task, write_task):
            if t is None or t.done():
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _idle_watchdog(
    turn: LiveTurn,
    tts: TtsPlayout,
    timeout: float,
    response_stall_timeout: float,
) -> None:
    """Close the turn based on explicit server-side signals where
    possible, falling back to a timer when the server stays silent.

    Three cases:
      * `turn.server_turn_complete()` is True → server says "model is
        done speaking". Defer while audio remains in flight, anchored
        on TtsPlayout's sample-counted drain deadline (see
        ``expected_drain_at``). Canonical clean close.
      * No chunks received yet → model hasn't started speaking;
        wait the full `timeout` for the first chunk to arrive (Live
        API can take 3-5 s, sometimes longer).
      * Chunks arriving but turn_complete hasn't fired → mid-response
        chunk gaps can be > 1.5 s during normal speech pauses, so a
        short timer here would race with real output. A separate,
        generous last-resort cap handles the wedged-provider case:
        if no new output chunk arrives for `response_stall_timeout`
        seconds and the server never sends turn_complete, end the turn
        through the normal teardown path.

    Coordinates with ``_play_responses``: the consumer awaits
    ``tts.wait_drained()`` after its final write, while this watchdog
    polls ``expected_drain_at()`` cooperatively. Both consult the same
    drain anchor, so whichever observes "drained" first triggers
    ``_end_turn`` (via the bg-task done check at
    ``_handle_session_frame``). End-of-turn drain timing is logged
    by ``_end_turn`` itself so observability is symmetric across
    whichever side wins the race."""
    while True:
        await asyncio.sleep(0.25)
        if turn.turn_lost():
            logger.warning("idle watchdog: connection lost mid-turn, ending turn")
            return
        now = time.monotonic()
        idle_for = now - turn.last_activity_at()
        if turn.server_turn_complete():
            # Defer while chunks are still queued in the inter-task
            # buffer — the consumer hasn't yet pushed them to TtsPlayout.
            pending_getter = getattr(turn, "audio_chunks_pending", None)
            if callable(pending_getter) and pending_getter() > 0:
                continue
            if tts.expected_drain_at() > now:
                continue
            return
        any_chunk_received = turn.last_chunk_at() > 0
        if not any_chunk_received and idle_for > timeout:
            logger.info(
                "idle timeout (pre-response phase, %.1fs); no chunks, ending turn",
                float(timeout),
            )
            return
        if any_chunk_received:
            stalled_for = now - turn.last_chunk_at()
            if stalled_for > response_stall_timeout:
                logger.warning(
                    "idle timeout (response stalled, %.1fs since last chunk); "
                    "no turn_complete, ending turn",
                    stalled_for,
                )
                return
