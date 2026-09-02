# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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


async def test_drain_sends_all_frames_in_fifo_order():
    """Core contract: frames sent to the turn in the exact order
    they were appended. A reordered drain would garble the user's
    utterance — this is the load-bearing property."""
    
    buf: deque = deque()
    for i in range(10):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    count, speech_seen = await drain_acquire_buffer(buf, turn)

    assert count == 10
    assert speech_seen is False  # no vad_predict provided
    assert len(buf) == 0
    assert turn.sends == [f"frame-{i}".encode() for i in range(10)]


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

    count, _speech = await drain_task
    assert count == 10
    # FIFO is preserved across concurrent appends — the mic loop
    # appends in real-time order, drain pops from the left.
    assert turn.sends == [f"frame-{i}".encode() for i in range(10)]


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


async def test_drain_on_empty_buffer_is_noop():
    """The fast-path: a wake event that opens a turn instantly
    (warm session, no context reset) leaves the acquire_buffer
    empty. Drain must be a no-op, not an error or a wasted
    round-trip."""
    
    buf: deque = deque()
    turn = _FakeTurn()

    count, speech_seen = await drain_acquire_buffer(buf, turn)
    assert count == 0
    assert speech_seen is False
    assert turn.sends == []


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

    count, _speech = await drain_acquire_buffer(buf, turn)
    assert count == 3
    # Last 3 frames retained (deque semantics): tags 4, 5, 6.
    assert turn.sends == [b"frame-4", b"frame-5", b"frame-6"]


@pytest.mark.parametrize(
    ("scores", "peak_min", "expected_speech_seen"),
    [
        # Fast-talker: wake-tail silence then 3 consecutive speech
        # frames clears the default min_consecutive_speech=3 gate.
        pytest.param(
            {0: 0.02, 1: 0.91, 2: 0.88, 3: 0.95}, 0.0, True,
            id="drain_with_vad_flags_sustained_speech",
        ),
        # No speech at all (walked away, or wake fired on background TV).
        pytest.param(
            {0: 0.05, 1: 0.05, 2: 0.05, 3: 0.05, 4: 0.05}, 0.0, False,
            id="drain_with_vad_below_threshold_stays_unarmed",
        ),
        # Alternating speech/silence — never 2 consecutive frames.
        pytest.param(
            {0: 0.91, 1: 0.02, 2: 0.88, 3: 0.04}, 0.0, False,
            id="drain_with_vad_requires_consecutive_frames",
        ),
        # 2026-05-23 broken-event regression: wake-word tail residual
        # (cold-replay scores 0.43/0.52/0.38) clears the duration gate
        # but peaks below peak_min=0.60 — must stay unarmed.
        pytest.param(
            {0: 0.43, 1: 0.52, 2: 0.38}, 0.60, False,
            id="drain_with_peak_min_rejects_wake_tail_residual",
        ),
        # Real speech reliably peaks well above peak_min — must still arm.
        pytest.param(
            {0: 0.30, 1: 0.85, 2: 0.92}, 0.60, True,
            id="drain_with_peak_min_passes_real_speech",
        ),
        # Peak tracker must reset on a sub-threshold frame: an early
        # isolated high peak (0.91) must not carry into a later 3-frame
        # run whose own max (0.30) is below peak_min.
        pytest.param(
            {0: 0.91, 1: 0.04, 2: 0.04, 3: 0.20, 4: 0.30, 5: 0.18, 6: 0.04},
            0.60, False,
            id="drain_with_peak_min_resets_across_silence_gap",
        ),
        # peak_min defaults to 0.0 (off): duration-only gate still arms.
        pytest.param(
            {0: 0.20, 1: 0.25, 2: 0.30}, 0.0, True,
            id="drain_peak_min_default_is_off",
        ),
        # Regression: 2 consecutive speech frames (~160ms, the
        # wake-tail + quiet music vocals signature) must NOT arm --
        # only a >=3-frame run (>=240ms) does.
        pytest.param(
            {0: 0.02, 1: 0.91, 2: 0.88, 3: 0.04}, 0.0, False,
            id="drain_with_two_consecutive_speech_frames_stays_unarmed",
        ),
    ],
)
async def test_drain_with_vad(scores, peak_min, expected_speech_seen):
    """VAD pre-arm gate: a sustained run of >=min_consecutive_speech
    frames above speech_threshold sets sustained_speech_detected=True,
    so the caller can pre-arm its end-of-utterance silence detector.
    An optional peak_min floor additionally discriminates real speech
    from wake-word tail residual, which can clear the duration gate
    without ever peaking as high as real speech does."""

    buf: deque = deque()
    for i in range(len(scores)):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn,
        vad_predict=predict,
        speech_threshold=0.15,
        peak_min=peak_min,
    )

    assert count == len(scores)
    assert speech_seen is expected_speech_seen
