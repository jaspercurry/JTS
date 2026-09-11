from __future__ import annotations

import asyncio
import logging
import time

import pytest

from jasper.voice import turn_playback
from jasper.voice._base import BaseLiveTurn
from jasper.voice.session import AudioOutChunk
from jasper.voice.turn_playback import PlaybackReport, idle_watchdog
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
        callback = _k.get("on_first_write")
        if callback is not None:
            await callback()
        await asyncio.sleep(0)
        return True

    async def end_segment(self) -> None:
        self.end_segment_calls += 1

    async def wait_drained(self) -> None:
        self.wait_drained_calls += 1

    async def flush(self):
        self.flush_calls += 1
        return _flush_ack()

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
        return await super().write_segment(*_a, **_k)


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


@pytest.mark.parametrize("mode", ["refused", "error", "partial_error", "measurement", "empty"])
async def test_playback_reports_acceptance_and_stops_after_a_failed_write(mode):
    turn, tts, report = _FakeTurn(), _BaseTts(), PlaybackReport()
    if mode == "empty":
        turn._chunks = [b""]
    first_write = []

    async def write(*args, on_first_write, **kwargs):
        tts.write_calls += 1
        if mode == "partial_error":
            await on_first_write()
        if mode in {"error", "partial_error"}:
            raise OSError("output unavailable")
        return False

    async def accepted():
        first_write.append(tts.write_calls)

    tts.write_segment = write
    playback = _play_responses(
        turn, tts, report=report, on_first_write=accepted,
        admission_refusal=lambda: "measurement_active" if mode == "measurement" else None,
    )
    if mode in {"empty", "measurement"}:
        await playback
    else:
        with pytest.raises(OSError):
            await playback
    assert report.accepted_audio == (mode == "partial_error")
    assert first_write == ([1] if report.accepted_audio else [])
    assert report.stop_reason == ("measurement_active" if mode == "measurement" else None)
    assert tts.write_calls == (0 if mode == "empty" else 1)


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


async def test_received_response_is_observed_even_when_interrupt_prevents_its_write():
    turn, tts, report = _FakeTurn(), _BaseTts(), PlaybackReport()
    turn.request_local_interrupt()
    events = []

    async def response():
        events.append("response")

    await _play_responses(turn, tts, report=report, on_response_started=response)
    assert report.stop_reason == "barge_in"
    assert not report.accepted_audio
    assert events == ["response"]
    assert tts.write_calls == 0


@pytest.mark.parametrize("chunk_api", [False, True])
async def test_playback_accepts_audio_iterators_without_a_close_method(chunk_api):
    class Audio:
        def __init__(self):
            self.frames = iter([b"first", b"second"])

        def __aiter__(self):
            return self

        async def __anext__(self):
            pcm = next(self.frames, None)
            if pcm is None:
                raise StopAsyncIteration
            return AudioOutChunk(pcm) if chunk_api else pcm

    turn, tts = _FakeTurn(), _BaseTts()
    turn.audio_out_chunks = Audio if chunk_api else None
    turn.audio_out = Audio
    await _play_responses(turn, tts)
    assert tts.write_calls == 2
    assert tts.end_segment_calls == tts.wait_drained_calls == 1


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
    assert tts.end_segment_calls == 0
    assert len(event_records(caplog, "barge.flush_failed")) == 1


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


def _flush_ack(events=()):
    return {
        "ok": True, "requests": 1, "pending_frames": 0,
        "segments": len(events), "flushed_frames": 0,
        "max_audio_played_ms": 9999, "events": list(events),
    }


def _segment(item="a", frames=0, segment=1, kind="assistant"):
    return {
        "segment": segment, "kind": kind, "provider_item_id": item,
        "queued_frames": 480000, "written_frames": frames,
        "drained_frames": frames, "flushed_frames": 0,
    }


@pytest.mark.parametrize("ack, confirmed, boundaries", [
    (None, False, []),
    ({"ok": False}, False, []),
    ({"ok": True}, False, []),
    ([], False, []),
    (_flush_ack([dict(_segment(), drained_frames="480")]), False, []),
    (_flush_ack([dict(_segment(), drained_frames=True)]), False, []),
    (_flush_ack([dict(_segment(), drained_frames=-1)]), False, []),
    (_flush_ack(), True, []),
    (_flush_ack([_segment()]), True, [("a", 0)]),
    (_flush_ack([_segment("a", 96000), _segment("b", 24000, 2)]),
     True, [("a", 2000), ("b", 500)]),
    (_flush_ack([_segment("a", 480, 1), _segment("a", 960, 2),
                 _segment("cue", 24000, 3, "cue")]), True, [("a", 30)]),
])
async def test_interrupt_stop_and_item_boundary_are_distinct(ack, confirmed, boundaries):
    turn = _SeamTurn()
    tts = _BaseTts()

    async def flush():
        return ack

    tts.flush = flush
    assert await turn_playback._flush_for_interrupt(turn, tts) is confirmed
    assert turn.seam_calls == [("cancel", "barge_in")] + [
        ("truncate", item, ms) for item, ms in boundaries
    ]


@pytest.mark.parametrize("phase", ["gap", "write", "drain"])
async def test_interrupt_owns_gap_write_and_drain_helpers(phase):
    entered = asyncio.Event()
    stopped = asyncio.Event()
    source_closed = asyncio.Event()
    live_helpers = set()

    async def block():
        task = asyncio.current_task()
        live_helpers.add(task)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            live_helpers.remove(task)
            stopped.set()

    class Turn(_SeamTurn):
        async def audio_out_chunks(self):
            try:
                yield AudioOutChunk(b"first", "a")
                if phase == "gap":
                    await block()
                yield AudioOutChunk(b"stale", "a")
            finally:
                source_closed.set()

    class Tts(_BaseTts):
        async def write_segment(self, *args, **kwargs):
            await super().write_segment(*args, **kwargs)
            if phase == "write":
                await block()
            return True

        async def wait_drained(self):
            if phase == "drain":
                await block()

    turn, tts = Turn(), Tts()
    playing = asyncio.create_task(_play_responses(turn, tts, barge_in_enabled=True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        turn.request_local_interrupt()
        await asyncio.wait_for(playing, 1)
        assert stopped.is_set()
        assert not live_helpers
        assert source_closed.is_set()
        assert tts.write_calls == (2 if phase == "drain" else 1)
        assert tts.flush_calls == 1
        assert turn.seam_calls == [("cancel", "barge_in")]
    finally:
        playing.cancel()
        await asyncio.gather(playing, return_exceptions=True)


async def test_flush_exception_still_cancels_generation():
    turn = _SeamTurn()
    tts = _FlushRaisesTts(turn)
    assert not await turn_playback._flush_for_interrupt(turn, tts)
    assert turn.seam_calls == [("cancel", "barge_in")]


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
    monkeypatch.setattr(turn_playback, "WATCHDOG_POLL_SEC", 0.001)
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
