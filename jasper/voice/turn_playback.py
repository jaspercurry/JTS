# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from ..audio_io import TtsPlayout
from ..log_event import log_event
from .session import AudioOutChunk, LiveTurn

logger = logging.getLogger("jasper.voice_daemon")

# The response observer awaits a SQLite write until Wave 3.1 moves that off
# the loop; first-audio telemetry cannot be allowed to hold up speech.
_RESPONSE_OBSERVER_TIMEOUT_SEC = 0.1


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


async def _flush_for_interrupt(turn: LiveTurn, tts: TtsPlayout) -> bool:
    """Flush local TTS playout in response to an interrupt, clear the turn's
    interrupted state, then reconcile the active provider's conversation
    state to the heard boundary.

    The interrupt can come from a provider server-interrupt (Gemini) or
    from local barge-in detection (``WakeLoop._handle_playback_frame`` ->
    ``turn.request_local_interrupt()``). The local TTS flush always happens
    first (the latency-critical action). After a *clean* flush this drives
    the ``session.Interruptible`` seam — ``cancel_response`` then
    ``truncate_assistant_audio`` — with the playout ledger's played-ms, so
    provider history matches what the listener actually heard. Both are
    no-ops for a server-self-truncating provider (Gemini); the OpenAI/Grok
    pack sends ``response.cancel`` + ``conversation.item.truncate``.

    Returns ``True`` on a clean flush, ``False`` if the flush errored. On a
    flush error it emits
    ``event=barge.flush_failed`` at WARNING — never silently — skips the
    provider reconcile (a failed local flush has no trustworthy played
    boundary to truncate against), and the caller stops racing the interrupt
    and lets the turn end through its normal teardown."""
    try:
        ack = await tts.flush()
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "barge.flush_failed",
            error=type(e).__name__,
            detail=str(e),
            level=logging.WARNING,
        )
        return False
    if ack is not None:
        log_event(
            logger,
            "tts_flush.playout_ack",
            max_audio_played_ms=ack.get("max_audio_played_ms"),
            segments=ack.get("segments"),
            flushed_frames=ack.get("flushed_frames"),
        )
    turn.clear_interrupted()
    dropped = turn.drop_pending_audio()
    if dropped:
        log_event(logger, "barge.dropped_pending_audio", chunks=dropped)
    # Local playout is now fully stopped; reconcile the active provider's
    # conversation state to the heard boundary using the playout ledger's
    # played-ms from the flush ack. Order matters: cancel stops generation
    # first, then truncate trims history to what actually played. The
    # truncate's own no-op-if-0 / missing-id guards live in the pack, so
    # passing the raw ledger value (0 when unwired/unavailable) is safe here.
    played_ms = ack.get("max_audio_played_ms") if isinstance(ack, dict) else None
    try:
        played_ms_int = int(played_ms) if played_ms is not None else 0
    except (TypeError, ValueError):
        # Defensive: a non-numeric value from a foreign/corrupted producer
        # must not raise out of the flush path. The pack's own no-op-if-<=0
        # guard then skips the truncate.
        played_ms_int = 0
    await turn.cancel_response("barge_in")
    await turn.truncate_assistant_audio(None, played_ms_int)
    return True


async def _play_responses(
    turn: LiveTurn,
    tts: TtsPlayout,
    *,
    barge_in_enabled: bool = False,
    on_response_started: Callable[[], Awaitable[None]] | None = None,
    on_first_write: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Drain turn.audio_out() to the speaker. Barge-in handling: race
    each write against an interrupt signal so a user-interrupted-the-model
    event immediately cancels in-flight playback and flushes the audio
    buffer. Without this, ALSA/sounddevice buffering causes 100-300ms of
    overrun where the model talks over the user.

    ``barge_in_enabled`` (default False) extends the interrupt race to the
    drain-tail window below. With it False the function is byte-identical
    to its pre-barge-in shape: the chunk-loop race is unchanged (it still
    handles Gemini's server-interrupt), and the drain tail is a plain
    ``wait_drained()``. With it True a barge-in arriving *after* the last
    chunk was written — the common case for burst-delivery providers that
    stream every chunk before playout finishes — still flushes, instead of
    being swallowed until the tail drains on its own.

    Cleanup contract: the per-iteration helpers (the interrupt waiter, the
    in-flight write, and the drain waiter) MUST be cancelled and awaited
    before this function returns, otherwise they leak as `Task destroyed
    but it is pending` warnings. The waiter is held alive by a reference
    cycle through `turn._interrupt_event`, so dropping the local without
    explicit cleanup means GC eventually breaks the cycle and Task.__del__
    fires. Providers that implement no interrupt at all leave the waiter
    pending at turn end, so the leak would fire every turn without this
    try/finally."""
    interrupt_task: asyncio.Task | None = None
    write_task: asyncio.Task | None = None
    drain_task: asyncio.Task | None = None
    flush_failed = False
    response_started = False
    first_write_seen = False
    try:
        async for chunk in _turn_audio_chunks(turn):
            # This is the one provider-neutral response boundary: the first
            # non-empty assistant PCM chunk has left the adapter and reached
            # the daemon. Observe it before local playout so a TTS write
            # failure does not falsely erase the fact that the model replied.
            if not response_started and chunk.pcm:
                response_started = True
                if on_response_started is not None:
                    try:
                        await asyncio.wait_for(
                            on_response_started(),
                            timeout=_RESPONSE_OBSERVER_TIMEOUT_SEC,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("turn response observer timed out")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("turn response observer failed: %s", e)
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
                if not await _flush_for_interrupt(turn, tts):
                    # Flush errored (already WARNed). Stop racing the
                    # interrupt and fall through to the normal turn end.
                    flush_failed = True
                    break
                interrupt_task = None
            elif write_task in done:
                try:
                    accepted = await write_task
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
                # Accepted by the admission seam, not merely attempted: a
                # refused write drops the PCM, so nothing reached fan-in.
                if not first_write_seen and chunk.pcm and accepted:
                    first_write_seen = True
                    if on_first_write is not None:
                        try:
                            await on_first_write()
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "turn first_write observer failed: %s", e,
                            )
            if write_task is not None and write_task.done():
                write_task = None
        await tts.end_segment()
        # Block until the last sample we wrote has cleared the OS
        # audio stack — see TtsPlayout.wait_drained. Cheap if the ring
        # is already empty; otherwise a single sleep for the residual.
        # Anchors on samples queued (not network arrivals), so an
        # OpenAI-style burst delivery and a Gemini-style real-time
        # pacing both end the turn at the right moment.
        if barge_in_enabled and not flush_failed:
            # Race a late barge-in against the drain. wait_drained is a
            # single timed sleep, so cancelling it mid-wait is free.
            if interrupt_task is None or interrupt_task.done():
                interrupt_task = asyncio.create_task(turn.wait_for_interrupt())
            drain_task = asyncio.create_task(tts.wait_drained())
            done, _ = await asyncio.wait(
                {drain_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupt_task in done:
                # drain_task is still pending; the finally block below
                # cancels and awaits it (a single timed sleep, free to
                # cancel). Flush the residual tail, then end the turn. The
                # return is intentionally not captured: unlike the chunk-loop
                # caller there is nothing left to gate (the turn ends here
                # either way), and a failed flush already WARNed inside
                # _flush_for_interrupt.
                await _flush_for_interrupt(turn, tts)
                interrupt_task = None
        else:
            await tts.wait_drained()
    finally:
        for t in (interrupt_task, write_task, drain_task):
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
    drain anchor, so whichever observes "drained" first completes its
    background task and lets WakeLoop schedule ``_end_turn``. The
    session-frame done-task check remains as a backup for always-on mic
    frames. End-of-turn drain timing is logged by ``_end_turn`` itself
    so observability is symmetric across whichever side wins the race."""
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
            if turn.audio_chunks_pending() > 0:
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
