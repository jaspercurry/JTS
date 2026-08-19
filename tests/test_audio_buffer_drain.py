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

The drain scores frames but does NOT decide whether the turn heard
speech: it reports the run still open when the buffer ran dry, and the
caller judges that run once, together with the live frames that follow
(`WakeLoop._arm_speech_if_admitted`). The cross-boundary behaviour that
buys is pinned in `tests/test_speech_admission_seam.py`.
"""
from __future__ import annotations

import asyncio
from collections import deque

import pytest

from jasper.audio_buffer import drain_acquire_buffer

# The drain never defaults this — the daemon owns the value. Tests that
# do not care about scoring still have to pass one.
THRESHOLD = 0.15


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


def _scored(scores: dict[int, float]):
    return lambda f: scores[f.tag]


@pytest.mark.asyncio
async def test_drain_sends_all_frames_in_fifo_order():
    """Core contract: frames sent to the turn in the exact order
    they were appended. A reordered drain would garble the user's
    utterance — this is the load-bearing property."""

    buf: deque = deque()
    for i in range(10):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    result = await drain_acquire_buffer(buf, turn, speech_threshold=THRESHOLD)

    assert result.frames == 10
    # No vad_predict provided -> nothing scored.
    assert (result.run_frames, result.run_peak, result.max_score) == (0, 0.0, 0.0)
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

    drain_task = asyncio.create_task(
        drain_acquire_buffer(buf, turn, speech_threshold=THRESHOLD),
    )
    # Yield once so the drain task starts and processes its first
    # frame (an `await send_audio` will yield back here).
    for j in range(5, 10):
        await asyncio.sleep(0)
        buf.append(_FakeFrame(j))

    result = await drain_task
    assert result.frames == 10
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
        await drain_acquire_buffer(buf, turn, speech_threshold=THRESHOLD)

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

    result = await drain_acquire_buffer(buf, turn, speech_threshold=THRESHOLD)
    assert result.frames == 0
    assert result.run_frames == 0
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

    result = await drain_acquire_buffer(buf, turn, speech_threshold=THRESHOLD)
    assert result.frames == 3
    # Last 3 frames retained (deque semantics): tags 4, 5, 6.
    assert turn.sends == [b"frame-4", b"frame-5", b"frame-6"]


# --- what the VAD saw -------------------------------------------------


@pytest.mark.asyncio
async def test_drain_reports_the_run_still_open_at_the_boundary():
    """The whole point of returning run state: the utterance that was
    still going when the buffer ran dry continues into the live path.
    Only the trailing run is reported — the two frames before the gap
    belong to a run that already ended."""

    buf: deque = deque()
    for i in range(5):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    scores = {0: 0.91, 1: 0.88, 2: 0.02, 3: 0.31, 4: 0.88}

    result = await drain_acquire_buffer(
        buf, turn, vad_predict=_scored(scores), speech_threshold=THRESHOLD,
    )

    assert result.frames == 5
    assert result.run_frames == 2
    assert result.run_peak == 0.88


@pytest.mark.asyncio
async def test_drain_run_resets_on_a_sub_threshold_frame():
    """A silence gap ends the run — frames and peak reset together, so
    a loud frame before the gap cannot vouch for the run after it."""

    buf: deque = deque()
    for i in range(4):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    scores = {0: 0.91, 1: 0.88, 2: 0.02, 3: 0.20}

    result = await drain_acquire_buffer(
        buf, turn, vad_predict=_scored(scores), speech_threshold=THRESHOLD,
    )

    assert result.run_frames == 1
    assert result.run_peak == 0.20


@pytest.mark.asyncio
async def test_drain_max_score_survives_the_silence_gap():
    """`max_score` is turn telemetry, not run state: it is the honest
    answer to "did Silero ever see anything?" that the no-speech abort
    log and `wake_events.max_silero_aec` report. It must NOT reset with
    the run, or a fast talker whose speech landed in the acquire window
    is recorded as silence."""

    buf: deque = deque()
    for i in range(3):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()
    scores = {0: 0.94, 1: 0.02, 2: 0.03}

    result = await drain_acquire_buffer(
        buf, turn, vad_predict=_scored(scores), speech_threshold=THRESHOLD,
    )

    assert result.run_frames == 0
    assert result.max_score == 0.94


@pytest.mark.asyncio
async def test_drain_without_vad_scores_nothing():
    """`vad_predict=None` (a push-to-talk turn) must not cost a Silero
    pass per frame; the frames are still forwarded."""

    buf: deque = deque()
    for i in range(4):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    result = await drain_acquire_buffer(buf, turn, speech_threshold=THRESHOLD)

    assert result.frames == 4
    assert result.max_score == 0.0


# --- a VAD that raises mid-drain --------------------------------------


@pytest.mark.asyncio
async def test_a_vad_failure_does_not_cost_the_rest_of_the_command():
    """The frames are the user's command. A `vad_predict` that raises
    used to unwind the drain with the buffer part-drained: the rest of
    the utterance was never sent, and the caller reported zero frames.
    Scoring is what degrades now — never the forwarding."""

    buf: deque = deque()
    for i in range(5):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    def predict(frame):
        if frame.tag == 2:
            raise RuntimeError("onnxruntime exploded")
        return 0.9

    result = await drain_acquire_buffer(
        buf, turn, vad_predict=predict, speech_threshold=THRESHOLD,
    )

    assert result.frames == 5
    assert turn.sends == [f"frame-{i}".encode() for i in range(5)]
    assert not buf
    # Scoring stops at the failure rather than being retried per frame,
    # so the run is what had accumulated before it.
    assert result.run_frames == 2
    assert result.max_score == 0.9


@pytest.mark.asyncio
async def test_a_vad_failure_is_logged_once(caplog):
    """Silent degradation is the failure mode this guard could
    introduce, so the WARN naming the frame is part of the contract —
    and it is logged once, not once per remaining frame."""

    buf: deque = deque()
    for i in range(6):
        buf.append(_FakeFrame(i))
    turn = _FakeTurn()

    def predict(_frame):
        raise RuntimeError("onnxruntime exploded")

    with caplog.at_level("WARNING", logger="jasper.audio_buffer"):
        result = await drain_acquire_buffer(
            buf, turn, vad_predict=predict, speech_threshold=THRESHOLD,
        )

    assert result.frames == 6
    warnings = [r for r in caplog.records if "VAD scoring failed" in r.message]
    assert len(warnings) == 1
