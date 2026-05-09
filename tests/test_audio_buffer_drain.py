"""Unit tests for `jasper.audio_buffer.drain_acquire_buffer`.

Behavior under test (the regression fix from the 2026-05-09 timer
incident): when wake fires and `connection.acquire_turn()` takes
multi-second to resolve (context reset, network blip), the daemon
captures mic frames into `_acquire_buffer` rather than letting them
pile up in sounddevice's OS-level queue. Once the turn is ready,
this helper drains the buffer into the turn in FIFO order, picking
up any frames appended by the mic loop *during* the drain.

Pattern modelled on LiveKit's pre-connect audio buffer + Pipecat's
reconnection frame buffer.
"""
from __future__ import annotations

import asyncio
from collections import deque

import pytest

from jasper.audio_buffer import drain_acquire_buffer


class _FakeFrame:
    """Stand-in for the numpy mic frames the real daemon handles.
    The drain helper only calls `.tobytes()` so this is enough."""

    def __init__(self, tag: int) -> None:
        self.tag = tag

    def tobytes(self) -> bytes:
        return f"frame-{self.tag}".encode()


class _FakeTurn:
    """Records every send_audio call so tests can assert on order
    + count. Optionally fails on a configured frame index to
    exercise the early-stop path."""

    def __init__(self, fail_on_index: int | None = None) -> None:
        self.sends: list[bytes] = []
        self._fail_on = fail_on_index

    async def send_audio(self, data: bytes) -> None:
        # Yield to the loop so concurrent appends from another
        # coroutine actually interleave with the drain — mirrors
        # the real network-bound behavior of the provider adapters'
        # `send_audio` (each call awaits a WebSocket write).
        await asyncio.sleep(0)
        if (
            self._fail_on is not None
            and len(self.sends) == self._fail_on
        ):
            raise RuntimeError("simulated send_audio failure")
        self.sends.append(data)


@pytest.mark.asyncio
async def test_drain_sends_all_frames_in_fifo_order():
    """Core contract: frames sent to the turn in the exact order
    they were appended. A reordered drain would garble the user's
    utterance — this is the load-bearing property."""
    
    buf: deque = deque()
    for i in range(10):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    count = await drain_acquire_buffer(buf, turn)

    assert count == 10
    assert len(buf) == 0
    assert turn.sends == [f"frame-{i}".encode() for i in range(10)]


@pytest.mark.asyncio
async def test_drain_picks_up_concurrent_appends():
    """The mic loop continues to append frames during the drain.
    The sync `while buffer:` check after each `await send_audio`
    must catch newly-appended frames, not exit prematurely.
    Without this, frames captured in the tail of the acquire window
    get dropped."""
    
    buf: deque = deque()
    # Seed with the frames captured during acquire (acquire_buffer
    # at drain start).
    for i in range(5):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    # Drive the drain task, and concurrently append more frames.
    drain_task = asyncio.create_task(drain_acquire_buffer(buf, turn))
    # Yield once so the drain task starts and processes its first
    # frame (an `await send_audio` will yield back here).
    for j in range(5, 10):
        await asyncio.sleep(0)
        buf.append(_FakeFrame(j))

    count = await drain_task
    assert count == 10
    # FIFO is preserved across concurrent appends — the mic loop
    # appends in real-time order, drain pops from the left.
    assert turn.sends == [f"frame-{i}".encode() for i in range(10)]


@pytest.mark.asyncio
async def test_drain_stops_at_first_send_audio_failure():
    """If `send_audio` raises (turn was torn down mid-drain, network
    blip), the helper must propagate so the caller can log + clear.
    Frames after the failure stay in the buffer — caller's
    responsibility to clear."""
    
    buf: deque = deque()
    for i in range(5):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn(fail_on_index=2)

    with pytest.raises(RuntimeError, match="simulated"):
        await drain_acquire_buffer(buf, turn)

    # First two frames sent before failure on index 2.
    assert turn.sends == [b"frame-0", b"frame-1"]
    # Two frames remain in the buffer (the failed one was already
    # popped; frames 3, 4 were never reached).
    assert len(buf) == 2


@pytest.mark.asyncio
async def test_drain_on_empty_buffer_is_noop():
    """The fast-path: a wake event that opens a turn instantly
    (warm session, no context reset) leaves the acquire_buffer
    empty. Drain must be a no-op, not an error or a wasted
    round-trip."""
    
    buf: deque = deque()
    turn = _FakeTurn()

    count = await drain_acquire_buffer(buf, turn)
    assert count == 0
    assert turn.sends == []


@pytest.mark.asyncio
async def test_drain_handles_bounded_deque():
    """`WakeLoop._acquire_buffer` is a bounded deque
    (`maxlen=ACQUIRE_BUFFER_MAX_FRAMES`). On a wedged connection
    where the buffer wraps, drain still operates on whatever's
    there — losing the leading frames is the explicit trade-off
    versus unbounded memory growth."""
    
    buf: deque = deque(maxlen=3)
    for i in range(7):  # 4 frames pushed off the front
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    count = await drain_acquire_buffer(buf, turn)
    assert count == 3
    # Last 3 frames retained (deque semantics): tags 4, 5, 6.
    assert turn.sends == [b"frame-4", b"frame-5", b"frame-6"]
