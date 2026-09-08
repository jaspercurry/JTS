# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncGenerator, Awaitable, Callable

from ..audio_io import TtsPlayout, confirmed_tts_flush
from ..log_event import log_event
from .session import AudioOutChunk, LiveTurn

logger = logging.getLogger("jasper.voice_daemon")

_WATCHDOG_POLL_SEC = 0.25


@dataclass
class PlaybackReport:
    accepted_audio: bool = False
    stop_reason: str | None = None


async def _turn_audio_chunks(turn: LiveTurn) -> AsyncGenerator[AudioOutChunk, None]:
    chunks = getattr(turn, "audio_out_chunks", None)
    source = chunks if callable(chunks) else turn.audio_out
    audio = source()
    try:
        async for chunk in audio:
            yield AudioOutChunk(pcm=chunk) if isinstance(chunk, bytes) else chunk
    finally:
        close = getattr(audio, "aclose", None)
        if callable(close):
            await close()


async def _flush_for_interrupt(turn: LiveTurn, tts: TtsPlayout) -> bool:
    ack = None
    try:
        ack = await tts.flush()
    except Exception as e:  # noqa: BLE001
        logger.warning("TTS interrupt flush failed: %s", e)
    confirmed = confirmed_tts_flush(ack)
    if confirmed:
        log_event(logger, "tts_flush.playout_ack", local_stop="confirmed")
    else:
        log_event(
            logger, "barge.flush_failed", local_stop="unconfirmed", level=logging.WARNING,
        )
    turn.clear_interrupted()
    dropped = turn.drop_pending_audio()
    if dropped:
        log_event(logger, "barge.dropped_pending_audio", chunks=dropped)
    await turn.cancel_response("barge_in")
    if confirmed and ack is not None:
        # Fan-in counts mix commits; outputd estimates drain. Neither proves
        # acoustic playback. Completed segments can be absent from this ledger.
        frames_by_item: dict[str, int] = {}
        for event in ack["events"]:
            item = event["provider_item_id"]
            if event["kind"] == "assistant" and item:
                frames_by_item[item] = frames_by_item.get(item, 0) + event["drained_frames"]
        for item, frames in frames_by_item.items():
            await turn.truncate_assistant_audio(item, frames * 1000 // 48000)
        log_event(logger, "barge.playback_boundary", items=len(frames_by_item))
    return confirmed


async def play_responses(
    turn: LiveTurn,
    tts: TtsPlayout,
    *,
    barge_in_enabled: bool = False,
    report: PlaybackReport | None = None,
    admission_refusal: Callable[[], str | None] | None = None,
    on_response_started: Callable[[], Awaitable[None]] | None = None,
    on_first_write: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Race provider gaps, output writes and the enabled drain tail against
    interruption. An interrupted response never resumes its audio iterator.
    """
    report = report if report is not None else PlaybackReport()

    async def first_write() -> None:
        if not report.accepted_audio:
            report.accepted_audio = True
            if on_first_write is not None:
                await on_first_write()

    async def play() -> None:
        response_started = False
        async with aclosing(_turn_audio_chunks(turn)) as chunks:
            async for chunk in chunks:
                if not chunk.pcm:
                    continue
                if not response_started:
                    response_started = True
                    if on_response_started is not None:
                        try:
                            await on_response_started()
                        except Exception as e:  # noqa: BLE001
                            logger.warning("turn response observer failed: %s", e)
                if interrupt.done():
                    return
                accepted = await tts.write_segment(
                    chunk.pcm,
                    provider_item_id=chunk.provider_item_id,
                    segment_kind=chunk.kind,
                    on_first_write=first_write,
                )
                if not accepted:
                    report.stop_reason = admission_refusal() if admission_refusal else None
                    if report.stop_reason is None:
                        raise OSError("assistant output refused nonempty audio")
                    return
        await tts.end_segment()
        if barge_in_enabled:
            await tts.wait_drained()

    interrupt = asyncio.create_task(turn.wait_for_interrupt())
    playback = asyncio.create_task(play())
    try:
        done, _ = await asyncio.wait(
            {playback, interrupt}, return_when=asyncio.FIRST_COMPLETED,
        )
        if interrupt not in done:
            await playback
    finally:
        playback.cancel()
        await asyncio.gather(playback, return_exceptions=True)
        try:
            if interrupt.done() and not interrupt.cancelled():
                await interrupt
                report.stop_reason = "barge_in"
                await _flush_for_interrupt(turn, tts)
        finally:
            interrupt.cancel()
            await asyncio.gather(interrupt, return_exceptions=True)
        if not playback.cancelled():
            playback.result()
    if not barge_in_enabled and report.stop_reason != "barge_in":
        await tts.wait_drained()



async def idle_watchdog(
    turn: LiveTurn,
    tts: TtsPlayout,
    timeout: float,
    response_stall_timeout: float,
) -> None:
    """Close the turn based on explicit server-side signals where
    possible, falling back to a timer when the server stays silent.

    Three cases:
      * `turn.server_turn_complete()` is True → server says "model is
        done speaking". Canonical clean close; the loop below holds the
        turn open while playout is still moving.
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

    Coordinates with ``play_responses``: the consumer awaits
    ``tts.wait_drained()`` after its final write, while this watchdog
    polls ``expected_drain_at()`` cooperatively. Both consult the same
    drain anchor, so whichever observes "drained" first completes its
    background task and lets WakeLoop schedule ``_end_turn``. The
    session-frame done-task check remains as a backup for always-on mic
    frames. End-of-turn drain timing is logged by ``_end_turn`` itself
    so observability is symmetric across whichever side wins the race."""
    playout_pending: int | None = None
    progressed_at = time.monotonic()
    while True:
        await asyncio.sleep(_WATCHDOG_POLL_SEC)
        if turn.turn_lost():
            logger.warning("idle watchdog: connection lost mid-turn, ending turn")
            return
        now = time.monotonic()
        idle_for = now - turn.last_activity_at()
        if turn.server_turn_complete():
            # Defer while the inter-task buffer is still MOVING, never on
            # depth alone. The progress signal is this turn's own pending
            # count, which falls exactly when the consumer dequeues;
            # `expected_drain_at` is shared TtsPlayout state a cue or a
            # flush can advance, so reading it as progress would mask a
            # wedged consumer. See ADR-0254.
            pending = turn.audio_chunks_pending()
            if pending != playout_pending:
                playout_pending = pending
                progressed_at = now
            if pending > 0:
                stalled_for = now - progressed_at
                if stalled_for <= response_stall_timeout:
                    continue
                log_event(
                    logger,
                    "turn.playout_stalled",
                    pending=pending,
                    stalled_s=round(stalled_for, 2),
                    level=logging.WARNING,
                )
                return
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
