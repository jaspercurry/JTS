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
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types as _types

if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = _types.ModuleType("sounddevice")


async def _play_responses(*args, **kwargs):
    # Imported lazily so the sounddevice stub above is in place before
    # jasper.voice.turn_playback -> audio_io -> sounddevice resolves
    # (keeps the import ordering without a module-top lint suppression).
    from jasper.voice.turn_playback import _play_responses as impl

    return await impl(*args, **kwargs)


class _FakeTurn:
    """Yields a fixed burst of audio chunks; carries the interrupt event
    the daemon would set via request_local_interrupt()."""

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


class _BaseTts:
    def __init__(self) -> None:
        self.flush_calls = 0
        self.write_calls = 0
        self.end_segment_calls = 0
        self.wait_drained_calls = 0

    async def write_segment(self, *_a, **_k) -> None:
        self.write_calls += 1

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

    async def write_segment(self, *_a, **_k) -> None:
        if self.write_calls == 0:
            self._turn.request_local_interrupt()
        self.write_calls += 1


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


# --- chunk-loop window -------------------------------------------------


def test_local_barge_in_chunk_loop_flushes():
    turn = _FakeTurn(n_chunks=3)
    tts = _ChunkBargeTts(turn)

    asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))

    assert tts.flush_calls == 1
    # _flush_for_interrupt cleared the interrupted state afterward.
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
    failed = [r for r in caplog.records if "barge.flush_failed" in r.getMessage()]
    assert len(failed) == 1


# --- provider reconcile seam wiring (PR-4) -----------------------------
#
# After a successful local flush the spine drives the active provider's
# barge-in pack: cancel_response THEN truncate_assistant_audio, the latter
# with the flush ack's played-ms. The pack methods are getattr-probed so a
# turn predating the seam degrades to local-flush-only.


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

    async def write_segment(self, *_a, **_k) -> None:
        if self.write_calls == 0:
            self._turn.request_local_interrupt()
        self.write_calls += 1

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


def test_flush_seam_skipped_for_turn_without_capability():
    """A turn predating the seam (no cancel/truncate methods) still flushes
    cleanly — the spine getattr-probes and degrades to local-flush-only
    rather than crashing. _FakeTurn has neither method."""
    turn = _FakeTurn(n_chunks=2)
    tts = _ChunkBargeTts(turn)

    # Must not raise despite the turn lacking the seam methods.
    asyncio.run(_play_responses(turn, tts, barge_in_enabled=True))
    assert tts.flush_calls == 1


class _SeamFlushRaisesTts(_BaseTts):
    """Trips a barge-in on the first chunk, then the flush itself errors."""

    def __init__(self, turn: _FakeTurn) -> None:
        super().__init__()
        self._turn = turn

    async def write_segment(self, *_a, **_k) -> None:
        if self.write_calls == 0:
            self._turn.request_local_interrupt()
        self.write_calls += 1

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
    failed = [r for r in caplog.records if "barge.flush_failed" in r.getMessage()]
    assert len(failed) == 1
