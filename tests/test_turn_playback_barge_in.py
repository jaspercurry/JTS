"""_play_responses barge-in flush behaviour (PR-2 spine).

Covers the two interrupt windows and the no-silent-failure contract:

  * chunk-loop window — interrupt while chunks are still being written
    (the path that already existed; pinned here against the local-barge
    trigger);
  * drain-tail window — interrupt after the last chunk, while
    ``wait_drained`` is pending. This is the new race, and the most
    common barge-in moment for burst-delivery providers (OpenAI/Grok
    stream every chunk before playout finishes). It only fires when
    ``barge_in_enabled`` is True; with it False the function is
    byte-identical to its pre-barge-in shape.
  * flush failure emits ``event=barge.flush_failed`` (WARN) and the turn
    still ends — never silently.

Plus the other half of the same "never silently" contract: ``idle_watchdog``
defers the post-turn_complete close on playout PROGRESS, so a wedged
consumer ends the turn instead of holding it (and the wake loop) open.
"""
from __future__ import annotations

import asyncio
import logging
import time

from jasper.voice import turn_playback
from jasper.voice._base import BaseLiveTurn
from jasper.voice.session import AudioOutChunk
from jasper.voice.turn_playback import idle_watchdog
from tests._log_events import event_fields, event_records


async def _play_responses(*args, **kwargs):
    from jasper.voice.turn_playback import play_responses as impl

    return await impl(*args, **kwargs)


class _FakeTurn:
    """Yields a fixed burst of audio chunks; carries the interrupt event
    the daemon would set via request_local_interrupt(). Generator-backed, so
    the `Interruptible` reconcile half is a no-op here (`_QueueTurn` and
    `_SeamTurn` below model the buffered and reconciling shapes)."""

    def __init__(self, n_chunks: int = 3) -> None:
        self._chunks = [bytes(8) for _ in range(n_chunks)]
        self._interrupt_event = asyncio.Event()

    async def audio_out_chunks(self):
        for c in self._chunks:
            yield c

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def request_local_interrupt(self) -> None:
        self._interrupt_event.set()

    def clear_interrupted(self) -> None:
        self._interrupt_event.clear()

    def drop_pending_audio(self) -> int:
        return 0

    def audio_chunks_pending(self) -> int:
        return 0

    async def cancel_response(self, reason: str) -> None:
        return None

    async def truncate_assistant_audio(
        self, provider_item_id, audio_played_ms,
    ) -> None:
        return None


class _QueueTurn:
    """Burst-delivery model: chunks sit in a queue (like the OpenAI/Grok
    adapter's ``_audio_q``) so ``drop_pending_audio`` can actually drain the
    backlog. A plain generator (``_FakeTurn``) cannot model the post-flush
    replay that A1 fixes, which is why the chunk-loop test above could pass
    while the assistant talked over the user."""

    def __init__(self, n_chunks: int = 5) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        for _ in range(n_chunks):
            self._q.put_nowait(AudioOutChunk(pcm=bytes(8)))
        self._q.put_nowait(None)  # terminal end-of-audio sentinel
        self._interrupt_event = asyncio.Event()
        self.cleared = 0
        self.dropped_calls = 0

    async def audio_out_chunks(self):
        while True:
            chunk = await self._q.get()
            if chunk is None:
                return
            yield chunk

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def request_local_interrupt(self) -> None:
        self._interrupt_event.set()

    def clear_interrupted(self) -> None:
        self.cleared += 1
        self._interrupt_event.clear()

    def audio_chunks_pending(self) -> int:
        return self._q.qsize()

    def drop_pending_audio(self) -> int:
        # Same shape as the real adapters: drain queued chunks, preserve the
        # terminal sentinel so audio_out_chunks still ends the turn.
        self.dropped_calls += 1
        dropped = 0
        while True:
            try:
                item = self._q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                self._q.put_nowait(None)
                break
            dropped += 1
        return dropped

    async def cancel_response(self, reason: str) -> None:
        pass

    async def truncate_assistant_audio(self, provider_item_id, audio_played_ms) -> None:
        pass

    async def release(self) -> None:
        pass


class _BaseTts:
    def __init__(self) -> None:
        self.flush_calls = 0
        self.write_calls = 0
        self.end_segment_calls = 0
        self.wait_drained_calls = 0

    async def write_segment(self, *_a, **_k) -> bool:
        self.write_calls += 1
        return True

    async def end_segment(self) -> None:
        self.end_segment_calls += 1

    async def wait_drained(self) -> None:
        self.wait_drained_calls += 1

    async def flush(self):
        self.flush_calls += 1
        return {"max_audio_played_ms": 0, "segments": 0, "flushed_frames": 0}

    def expected_drain_at(self) -> float:
        return 0.0


class _ChunkBargeTts(_BaseTts):
    """Trips a local barge-in during the first chunk write."""

    def __init__(self, turn: _FakeTurn) -> None:
        super().__init__()
        self._turn = turn

    async def write_segment(self, *_a, **_k) -> bool:
        if self.write_calls == 0:
            self._turn.request_local_interrupt()
        self.write_calls += 1
        return True


class _DrainBargeTts(_BaseTts):
    """Trips a local barge-in during the drain tail (after the last
    chunk) — the burst-delivery interrupt window."""

    def __init__(self, turn: _FakeTurn) -> None:
        super().__init__()
        self._turn = turn

    async def wait_drained(self) -> None:
        self.wait_drained_calls += 1
        self._turn.request_local_interrupt()
        await asyncio.sleep(0.02)


class _RefusedFirstWriteTts(_BaseTts):
    """The admission seam refuses the first chunk — an armed correction
    window drops the PCM rather than queueing it (see
    ``TtsPlayout.set_emission_admission``)."""

    async def write_segment(self, *_a, **_k) -> bool:
        self.write_calls += 1
        return self.write_calls > 1


class _FlushRaisesTts(_ChunkBargeTts):
    async def flush(self):
        self.flush_calls += 1
        raise RuntimeError("fan-in socket gone")


# --- response lifecycle observer --------------------------------------


def test_response_started_observed_once_before_first_playout_write():
    """The two per-turn latency boundaries straddle the first write: the
    model replied (before it), and the reply was handed to fan-in (after the
    write the socket accepted). Each fires once."""
    turn = _FakeTurn(n_chunks=3)
    tts = _BaseTts()
    write_counts: list[int] = []
    first_write_counts: list[int] = []

    async def response_started() -> None:
        write_counts.append(tts.write_calls)

    async def first_write() -> None:
        first_write_counts.append(tts.write_calls)

    asyncio.run(_play_responses(
        turn,
        tts,
        on_response_started=response_started,
        on_first_write=first_write,
    ))

    assert write_counts == [0]
    assert first_write_counts == [1]
    assert tts.write_calls == 3


def test_first_write_waits_for_a_write_the_playout_accepted():
    """`first_write` is the hand-off to fan-in, so a refused write must not
    stamp it: those bytes are dropped, not queued, and nobody heard them.
    Stamping on the attempt would time a chunk that never reached a
    speaker."""
    turn = _FakeTurn(n_chunks=3)
    tts = _RefusedFirstWriteTts()
    first_write_counts: list[int] = []

    async def first_write() -> None:
        first_write_counts.append(tts.write_calls)

    asyncio.run(_play_responses(turn, tts, on_first_write=first_write))

    assert first_write_counts == [2]
    assert tts.write_calls == 3


def test_response_observer_failure_does_not_block_playout():
    turn = _FakeTurn(n_chunks=2)
    tts = _BaseTts()

    async def broken_observer() -> None:
        raise OSError("telemetry disk unavailable")

    asyncio.run(_play_responses(
        turn,
        tts,
        on_response_started=broken_observer,
    ))

    assert tts.write_calls == 2


def test_response_observer_timeout_does_not_block_playout(monkeypatch):
    turn = _FakeTurn(n_chunks=2)
    tts = _BaseTts()

    async def stuck_observer() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(
        turn_playback,
        "_RESPONSE_OBSERVER_TIMEOUT_SEC",
        0.01,
    )
    asyncio.run(asyncio.wait_for(
        _play_responses(
            turn,
            tts,
            on_response_started=stuck_observer,
        ),
        timeout=0.2,
    ))

    assert tts.write_calls == 2


# --- chunk-loop window -------------------------------------------------


def test_local_barge_in_chunk_loop_flushes():
    turn = _FakeTurn(n_chunks=3)
    tts = _ChunkBargeTts(turn)

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.flush_calls == 1
    # _flush_for_interrupt cleared the interrupted state afterward.
    assert not turn._interrupt_event.is_set()


def test_local_barge_in_drops_buffered_audio_no_replay():
    """A1 regression: after a local-barge flush, the play loop must NOT
    resume writing the provider's already-buffered backlog.

    Burst-delivery providers (OpenAI/Grok) enqueue the whole response up
    front, so without ``drop_pending_audio`` the flush is cosmetic — the loop
    keeps writing the queued chunks and the assistant audibly talks over the
    user. With the bug every queued chunk reaches the speaker (write_calls ==
    5); the fix drains the backlog so only the pre-interrupt boundary chunk(s)
    land (write_calls <= 2 — the +1 is the single chunk that can race through
    before the interrupt branch is taken)."""
    turn = _QueueTurn(n_chunks=5)
    tts = _ChunkBargeTts(turn)  # trips the interrupt during the first write

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.flush_calls == 1
    assert turn.dropped_calls >= 1
    # The backlog behind the interrupt was dropped, not replayed.
    assert tts.write_calls <= 2
    assert not turn._interrupt_event.is_set()


# --- drain-tail window (the fix) ---------------------------------------


def test_local_barge_in_drain_tail_flushes_when_enabled():
    turn = _FakeTurn(n_chunks=2)
    tts = _DrainBargeTts(turn)

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.wait_drained_calls == 1
    assert tts.flush_calls == 1  # raced + flushed during the tail


def test_drain_tail_interrupt_ignored_when_disabled():
    """Byte-identical OFF path: the very same interrupt-during-drain does
    NOT flush when barge_in_enabled is False — it just drains and ends."""
    turn = _FakeTurn(n_chunks=2)
    tts = _DrainBargeTts(turn)

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=False))

    assert tts.wait_drained_calls == 1
    assert tts.flush_calls == 0


# --- no interrupt at all (the OpenAI/Grok steady state) ----------------


def test_no_interrupt_plays_through_and_drains():
    turn = _FakeTurn(n_chunks=3)
    tts = _BaseTts()

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.write_calls == 3
    assert tts.end_segment_calls == 1
    assert tts.flush_calls == 0


def test_flag_off_normal_turn_is_byte_identical():
    """Default OFF + no interrupt: plays every chunk, ends, drains once,
    never flushes — the unchanged pre-barge-in shape."""
    turn = _FakeTurn(n_chunks=3)
    tts = _BaseTts()

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=False))

    assert tts.write_calls == 3
    assert tts.end_segment_calls == 1
    assert tts.wait_drained_calls == 1
    assert tts.flush_calls == 0


# --- no silent failure -------------------------------------------------


def test_flush_failure_warns_and_ends_turn(caplog):
    turn = _FakeTurn(n_chunks=3)
    tts = _FlushRaisesTts(turn)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        # Must NOT raise — falls through to normal turn end.
        asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.flush_calls == 1
    assert tts.end_segment_calls == 1
    assert len(event_records(caplog, "barge.flush_failed")) == 1


# --- provider reconcile seam wiring (PR-4) -----------------------------
#
# After a successful local flush the spine drives the active provider's
# barge-in pack: cancel_response THEN truncate_assistant_audio, the latter
# with the flush ack's played-ms. Both are `Interruptible` members, so every
# turn that can reach the spine has them (pinned in
# tests/test_voice_barge_in_contract.py); a provider with nothing to
# reconcile ships them as no-ops.


class _SeamTurn(_FakeTurn):
    """Records the reconcile seam calls in order so the test can pin both
    sequence (cancel before truncate) and arguments."""

    def __init__(self, n_chunks: int = 3) -> None:
        super().__init__(n_chunks)
        self.seam_calls: list[tuple] = []

    async def cancel_response(self, reason: str) -> None:
        self.seam_calls.append(("cancel", reason))

    async def truncate_assistant_audio(
        self, provider_item_id, audio_played_ms,
    ) -> None:
        self.seam_calls.append(("truncate", provider_item_id, audio_played_ms))


class _LedgerTts(_BaseTts):
    """Trips a barge-in on the first chunk and reports a real played-ms in
    the flush ack — the production fan-in DAC-clock ledger value."""

    def __init__(self, turn: _FakeTurn, *, played_ms: int) -> None:
        super().__init__()
        self._turn = turn
        self._played_ms = played_ms

    async def write_segment(self, *_a, **_k) -> bool:
        if self.write_calls == 0:
            self._turn.request_local_interrupt()
        self.write_calls += 1
        return True

    async def flush(self):
        self.flush_calls += 1
        return {
            "max_audio_played_ms": self._played_ms,
            "segments": 1,
            "flushed_frames": 2,
        }


def test_flush_drives_cancel_then_truncate_with_ledger_ms():
    turn = _SeamTurn(n_chunks=3)
    tts = _LedgerTts(turn, played_ms=2750)

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.flush_calls == 1
    # cancel first (stop generation), then truncate with the ack's
    # played-ms as the heard boundary. The spine carries no provider id,
    # so it passes None (the OpenAI pack falls back to its own item id).
    assert turn.seam_calls == [
        ("cancel", "barge_in"),
        ("truncate", None, 2750),
    ]


class _SeamFlushRaisesTts(_BaseTts):
    """Trips a barge-in on the first chunk, then the flush itself errors."""

    def __init__(self, turn: _FakeTurn) -> None:
        super().__init__()
        self._turn = turn

    async def write_segment(self, *_a, **_k) -> bool:
        if self.write_calls == 0:
            self._turn.request_local_interrupt()
        self.write_calls += 1
        return True

    async def flush(self):
        self.flush_calls += 1
        raise RuntimeError("fan-in socket gone")


def test_flush_failure_skips_provider_reconcile(caplog):
    """A failed local flush has no trustworthy played boundary, so the spine
    must NOT cancel/truncate the provider — doing so could truncate against a
    guessed ms. The turn still ends, and the failure is logged (not silent)."""
    turn = _SeamTurn(n_chunks=3)
    tts = _SeamFlushRaisesTts(turn)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.flush_calls == 1
    assert turn.seam_calls == [], (
        "a failed flush must not drive the provider reconcile seam"
    )
    assert len(event_records(caplog, "barge.flush_failed")) == 1


# ---------------------------------------------------------------------------
# idle_watchdog: the post-turn_complete deferral is progress-based.
# ---------------------------------------------------------------------------


def _completed_turn(pending: int) -> BaseLiveTurn:
    """A real turn the server has finished, holding `pending` unplayed
    chunks and its terminal sentinel."""
    turn = BaseLiveTurn(conn=None, started_at=time.monotonic())  # type: ignore[arg-type]
    turn._server_turn_complete = True
    for _ in range(pending):
        turn._enqueue_audio(AudioOutChunk(pcm=bytes(8)))
    turn._audio_q.put_nowait(None)
    return turn


class _PollClock:
    """The monotonic clock `idle_watchdog` reads once per poll.

    Each read jumps `step` seconds, so every gap the watchdog measures is
    far past the `response_stall_timeout` the test passes it, and
    `on_poll` models what the playout consumer did in that gap. Real time
    then plays no part in the verdict — only the poll ORDER does."""

    def __init__(self, *, step: float, on_poll=None) -> None:
        self._now = 0.0
        self._step = step
        self._on_poll = on_poll
        self.reads = 0

    def monotonic(self) -> float:
        self.reads += 1
        # Read 1 is the loop's pre-entry anchor, not a poll.
        if self.reads > 1 and self._on_poll is not None:
            self._on_poll()
        self._now += self._step
        return self._now


def _run_watchdog(turn, clock, monkeypatch):
    monkeypatch.setattr(turn_playback, "time", clock)
    monkeypatch.setattr(turn_playback, "_WATCHDOG_POLL_SEC", 0.001)
    return asyncio.wait_for(
        idle_watchdog(turn, _BaseTts(), timeout=999.0, response_stall_timeout=1.0),
        timeout=5.0,
    )


async def test_idle_watchdog_ends_a_turn_whose_playout_stopped_moving(
    caplog, monkeypatch,
):
    """A consumer that wedges with audio queued behind it used to defer the
    watchdog forever: the turn never ended, so the wake loop never came
    back and the household lost the speaker until a restart."""
    turn = _completed_turn(pending=3)
    clock = _PollClock(step=5.0)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await _run_watchdog(turn, clock, monkeypatch)

    fields = event_fields(caplog, "turn.playout_stalled")
    # qsize(), so the 3 chunks plus the terminal sentinel behind them.
    assert int(fields["pending"]) == 4
    assert float(fields["stalled_s"]) > 1.0


async def test_idle_watchdog_keeps_deferring_while_playout_drains(
    caplog, monkeypatch,
):
    """Progress, not patience: a consumer still moving chunks holds the turn
    open across gaps well past `response_stall_timeout`, which a plain timer
    would cut off mid-sentence."""
    turn = _completed_turn(pending=3)
    # One chunk leaves the queue between polls — the whole backlog plus the
    # sentinel, one poll at a time.
    clock = _PollClock(step=5.0, on_poll=turn._audio_q.get_nowait)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await _run_watchdog(turn, clock, monkeypatch)

    assert turn.audio_chunks_pending() == 0
    assert clock.reads > 4, "the watchdog deferred across every drained chunk"
    assert event_records(caplog, "turn.playout_stalled") == []
