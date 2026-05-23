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

    count, speech_seen = await drain_acquire_buffer(buf, turn)

    assert count == 10
    assert speech_seen is False  # no vad_predict provided
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

    count, _speech = await drain_task
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

    count, speech_seen = await drain_acquire_buffer(buf, turn)
    assert count == 0
    assert speech_seen is False
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

    count, _speech = await drain_acquire_buffer(buf, turn)
    assert count == 3
    # Last 3 frames retained (deque semantics): tags 4, 5, 6.
    assert turn.sends == [b"frame-4", b"frame-5", b"frame-6"]


@pytest.mark.asyncio
async def test_drain_with_vad_flags_sustained_speech():
    """Fast-talker compensation: a run of ≥``min_consecutive_speech``
    consecutive frames above the speech threshold should set
    sustained_speech_detected=True. Caller uses this to pre-arm
    its end-of-utterance silence detector so live frames see
    "watch for silence" rather than "wait for speech to arm"
    (which never happens if the user's whole question is in the
    acquire window)."""

    buf: deque = deque()
    for i in range(4):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    # First frame is silence (wake-word tail) then 3 frames of
    # speech — typical pattern when fast talker starts the question
    # immediately after the wake word. 3 consecutive speech frames
    # clears the default ``min_consecutive_speech=3`` gate.
    scores = {0: 0.02, 1: 0.91, 2: 0.88, 3: 0.95}
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn, vad_predict=predict, speech_threshold=0.15,
    )

    assert count == 4
    assert speech_seen is True


@pytest.mark.asyncio
async def test_drain_with_vad_below_threshold_stays_unarmed():
    """Acquire window with no speech (user wake-fired but walked
    away, or wake fired on background TV). Caller should NOT pre-arm
    so the existing 5 s no-speech-abort still applies."""

    buf: deque = deque()
    for i in range(5):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    predict = lambda f: 0.05  # all sub-threshold

    count, speech_seen = await drain_acquire_buffer(
        buf, turn, vad_predict=predict, speech_threshold=0.15,
    )

    assert count == 5
    assert speech_seen is False


@pytest.mark.asyncio
async def test_drain_with_vad_requires_consecutive_frames():
    """A single high-score frame (e.g. a transient click registered
    as speech by Silero) must NOT pre-arm. Only sustained
    speech-above-threshold across `min_consecutive_speech` frames
    counts. Defaults to 3 frames ≈ 240 ms which mirrors the
    SUSTAINED_SPEECH_TO_ARM_SEC = 0.20 s threshold used on live
    frames."""

    buf: deque = deque()
    for i in range(4):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    # Alternating: speech, silence, speech, silence — never 2 in a row.
    scores = {0: 0.91, 1: 0.02, 2: 0.88, 3: 0.04}
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn, vad_predict=predict, speech_threshold=0.15,
    )

    assert count == 4
    assert speech_seen is False


@pytest.mark.asyncio
async def test_drain_with_peak_min_rejects_wake_tail_residual():
    """Regression for the 2026-05-23 broken-event bug.

    The live VAD's duration gate (200 ms continuous at silero ≥
    0.15) was tripped by wake-word tail residual peaking at silero
    ≈ 0.52 — high enough to look "speech-like" to Silero VAD, low
    enough to NOT be real user speech (real speech reliably peaks
    above 0.7). The 800 ms silence detector then fired and ended
    the turn before the user started their actual question. Adding
    a peak-confidence requirement (max silero in the arming run
    must be ≥ ``peak_min``) discriminates real speech from
    wake-tail residual cleanly.

    Scenario here mirrors the broken event's cold-replay scores:
    3 consecutive frames at silero 0.43 / 0.52 / 0.38. Duration
    alone clears 3 frames at threshold 0.15, but max in run is
    0.52 — below ``peak_min=0.60``. Must stay unarmed."""

    buf: deque = deque()
    for i in range(3):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    # The broken event's actual silero scores from cold-replay.
    scores = {0: 0.43, 1: 0.52, 2: 0.38}
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn,
        vad_predict=predict,
        speech_threshold=0.15,
        peak_min=0.60,
    )

    assert count == 3
    assert speech_seen is False  # peak (0.52) < peak_min (0.60)


@pytest.mark.asyncio
async def test_drain_with_peak_min_passes_real_speech():
    """Counterpart to the wake-tail rejection test: real user
    speech reliably peaks well above 0.6 within the first few
    frames. The gate must still arm on those, otherwise we've
    traded one silent-failure mode for another (gate never arms →
    5 s no-speech abort).

    Scenario: 3 consecutive frames at silero 0.30 / 0.85 / 0.92.
    Sustained ≥ 3 frames AND peak (0.92) >= peak_min (0.60)."""

    buf: deque = deque()
    for i in range(3):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    scores = {0: 0.30, 1: 0.85, 2: 0.92}
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn,
        vad_predict=predict,
        speech_threshold=0.15,
        peak_min=0.60,
    )

    assert count == 3
    assert speech_seen is True


@pytest.mark.asyncio
async def test_drain_with_peak_min_resets_across_silence_gap():
    """Peak tracker must reset on a sub-threshold frame, not
    accumulate across silence gaps. Otherwise a sequence like
    [0.91 silence 0.91 silence 0.91 0.20 0.20] could falsely
    "remember" the 0.91 peak from the earlier broken runs and arm
    on the new run.

    Scenario: high-peak run, then silence, then a low-peak run
    that's long enough sustained-wise but doesn't reach
    ``peak_min``. Must NOT arm."""

    buf: deque = deque()
    for i in range(7):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    # Frame 0: high peak but isolated (no sustain). Frames 1-2:
    # silence. Frames 3-5: 3-frame run, but all low silero (max
    # 0.30, below peak_min=0.60). Frame 6: silence.
    scores = {
        0: 0.91, 1: 0.04, 2: 0.04,
        3: 0.20, 4: 0.30, 5: 0.18,
        6: 0.04,
    }
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn,
        vad_predict=predict,
        speech_threshold=0.15,
        peak_min=0.60,
    )

    assert count == 7
    assert speech_seen is False  # peak in 2nd run (0.30) < peak_min


@pytest.mark.asyncio
async def test_drain_peak_min_default_is_off():
    """``peak_min`` defaults to 0.0 (off) — backward-compatible.
    Existing tests using the duration-only gate continue to pass."""

    buf: deque = deque()
    for i in range(3):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    # All three frames clear threshold but max is only 0.30 (below
    # the typical peak_min=0.6 a strict caller would pass). With
    # peak_min defaulting to 0.0, the gate arms purely on duration.
    scores = {0: 0.20, 1: 0.25, 2: 0.30}
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn, vad_predict=predict, speech_threshold=0.15,
    )

    assert count == 3
    assert speech_seen is True


@pytest.mark.asyncio
async def test_drain_with_two_consecutive_speech_frames_stays_unarmed():
    """Regression: 2 consecutive speech frames (~160 ms) must NOT
    arm. That length is the natural signature of the wake-word
    tail + quiet music vocals when the user wakes the speaker
    while music is playing — pre-arming on it ends the turn after
    END_OF_UTTERANCE_SILENCE_SEC of "user thinking" silence,
    before the user has time to start speaking. The model then
    receives ~1 s of pre-roll + wake-tail audio and fabricates a
    follow-up question from the prior turn's cached tool result.

    The fix matches the acquire path's gate to the live path's
    SUSTAINED_SPEECH_TO_ARM_SEC = 0.20 s; with 80 ms frames that's
    a ≥3-frame run."""

    buf: deque = deque()
    for i in range(4):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    # Silence + 2 consecutive speech (wake-word tail signature) + silence.
    scores = {0: 0.02, 1: 0.91, 2: 0.88, 3: 0.04}
    predict = lambda f: scores[f.tag]

    count, speech_seen = await drain_acquire_buffer(
        buf, turn, vad_predict=predict, speech_threshold=0.15,
    )

    assert count == 4
    assert speech_seen is False
