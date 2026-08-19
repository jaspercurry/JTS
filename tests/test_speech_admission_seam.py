# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One utterance, one speech run, one gate — across the acquire boundary.

A turn's audio arrives in two halves: frames captured between the wake
firing and the provider turn being ready (replayed by
`drain_acquire_buffer`), and frames that arrive live afterwards. The
boundary between them falls wherever the provider happened to finish
opening the turn — nowhere near a word boundary.

Both halves used to accumulate their own run of continuous speech and
each judge it alone, so "Hey Jarvis, volume down" with no pause could
put two frames on one side and the rest on the other, fail both gates
while satisfying neither's intent, and abort at NO_SPEECH_ABORT_SEC with
the user standing there having plainly spoken. Speaking slowly worked
because slow speech starts after the cut. These tests pin the fix: the
drain hands its open run to the live path, and
`WakeLoop._arm_speech_if_admitted` judges it once.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from jasper.voice_daemon import (
    END_OF_UTTERANCE_SPEECH_THRESHOLD,
    SPEECH_RUN_MIN_FRAMES,
    SPEECH_RUN_PEAK_MIN,
    WakeLoop,
)

# A frame that clears both halves of the bar, and one that clears
# neither. Named so a test reads as speech/silence, not as numbers.
LOUD = 0.90
QUIET = 0.02


class _SpyTurn:
    def __init__(self) -> None:
        self.sends = 0

    async def send_audio(self, _data) -> None:
        self.sends += 1

    async def end_input(self) -> None:
        return None


class _ScriptedVad:
    """Returns the next scripted score per `predict` call. A score given
    as an exception instance is raised instead — that is how a VAD blowing
    up mid-drain is expressed."""

    def __init__(self, scores) -> None:
        self._scores = list(scores)
        self.calls = 0

    def predict(self, _frame) -> float:
        score = self._scores[self.calls] if self.calls < len(self._scores) else 0.0
        self.calls += 1
        if isinstance(score, BaseException):
            raise score
        return float(score)

    def reset(self) -> None:
        return None


def _frame() -> np.ndarray:
    return np.zeros(1280, dtype=np.int16)


async def _play(acquire, live) -> WakeLoop:
    """Run one turn's worth of audio through the real seam: `acquire`
    scores land in the acquire buffer and are replayed by the drain,
    `live` scores arrive as live mic frames."""
    wl = WakeLoop.for_tests()
    wl._turn = _SpyTurn()
    wl._vad = _ScriptedVad(list(acquire) + list(live))
    wl._turn_started_at_loop = asyncio.get_event_loop().time()
    wl._acquire_buffer.extend(_frame() for _ in acquire)

    # Funnel stages the turn recorded, hung off the loop so a test can
    # assert the gate fired once rather than once per frame.
    stages: list[str] = []

    async def _record(stage: str, **_kw) -> None:
        stages.append(stage)

    wl._telemetry_stage = _record  # type: ignore[method-assign]
    wl.stages = stages  # type: ignore[attr-defined]

    await wl._drain_acquire_audio()
    for _ in live:
        await wl._handle_session_frame(_frame())
    return wl


# --- the bug ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_utterance_split_by_the_boundary_arms_once():
    """The headline case: a continuous command whose first frames land in
    the acquire window and whose rest arrives live. Neither half is long
    enough on its own; the utterance is."""
    half = SPEECH_RUN_MIN_FRAMES // 2

    wl = await _play([LOUD] * half, [LOUD] * (SPEECH_RUN_MIN_FRAMES - half))

    assert wl._user_speech_seen is True
    # Armed once — the gate is not re-entered for every later frame.
    assert wl.stages.count("speech_detected") == 1


@pytest.mark.asyncio
async def test_neither_side_alone_would_have_cleared_the_bar():
    """Mutation of the test above: the same split, one frame short. If
    either half could arm on its own this would still pass — it must not."""
    half = SPEECH_RUN_MIN_FRAMES // 2

    wl = await _play([LOUD] * half, [LOUD] * (SPEECH_RUN_MIN_FRAMES - half - 1))

    assert wl._user_speech_seen is False


@pytest.mark.asyncio
@pytest.mark.parametrize("in_acquire", range(SPEECH_RUN_MIN_FRAMES + 1))
async def test_the_bar_is_the_same_wherever_the_boundary_falls(in_acquire):
    """The admission requirement is a property of the utterance, not of
    where the provider happened to finish opening the turn. Exactly
    SPEECH_RUN_MIN_FRAMES arms at every split; one fewer never does."""
    live = SPEECH_RUN_MIN_FRAMES - in_acquire

    armed = await _play([LOUD] * in_acquire, [LOUD] * live)
    assert armed._user_speech_seen is True

    if live:
        short = await _play([LOUD] * in_acquire, [LOUD] * (live - 1))
    else:
        short = await _play([LOUD] * (in_acquire - 1), [])
    assert short._user_speech_seen is False


@pytest.mark.asyncio
async def test_a_gap_at_the_boundary_starts_a_new_run():
    """Carrying the run is not the same as summing frames: silence at the
    seam ends the run exactly as silence anywhere else does."""
    wl = await _play(
        [LOUD] * (SPEECH_RUN_MIN_FRAMES - 1),
        [QUIET] + [LOUD] * (SPEECH_RUN_MIN_FRAMES - 1),
    )

    assert wl._user_speech_seen is False


# --- what must NOT get easier ----------------------------------------


@pytest.mark.asyncio
async def test_wake_tail_shaped_audio_still_does_not_arm():
    """Wake-word tail residual: long enough, never confident. The 2026-05-23
    corpus put it in the 0.15-0.52 band, below SPEECH_RUN_PEAK_MIN, and
    carrying the run across the boundary must not hand it a way in."""
    tail = [0.43, 0.52, 0.38, 0.31, 0.45, 0.52]
    assert max(tail) < SPEECH_RUN_PEAK_MIN
    assert min(tail) >= END_OF_UTTERANCE_SPEECH_THRESHOLD

    for split in range(len(tail) + 1):
        wl = await _play(tail[:split], tail[split:])
        assert wl._user_speech_seen is False, f"armed on wake tail at split {split}"


@pytest.mark.asyncio
async def test_a_late_loud_frame_vouches_only_for_its_own_run():
    """The peak travels with the run, not with the turn: a confident frame
    after the gap cannot retroactively admit the quiet run before it."""
    wl = await _play(
        [0.30] * SPEECH_RUN_MIN_FRAMES + [QUIET],
        [LOUD],
    )

    assert wl._user_speech_seen is False


# --- honest diagnostics ----------------------------------------------


@pytest.mark.asyncio
async def test_the_drains_peak_reaches_the_turn_telemetry():
    """`_max_silero_score_in_turn` is what the no-speech abort logs and
    what `wake_events.max_silero_aec` stores, and the comment on that log
    line says it exists to tell "the user never spoke" from "the user
    spoke but stayed under threshold". Scored only by the live path, it
    answered "never spoke" for exactly the fast talker the drain heard."""
    wl = await _play([0.94, QUIET], [0.05])

    assert wl._user_speech_seen is False  # too short to arm — still logged
    assert wl._max_silero_score_in_turn == pytest.approx(0.94)


@pytest.mark.asyncio
async def test_a_turn_armed_from_the_acquire_window_records_when():
    """`silero_aec_armed_at_ms` was left NULL on every pre-armed turn, so
    the corpus could not distinguish them from turns that never armed.
    0 is the honest answer: armed before the first live frame."""
    wl = await _play([LOUD] * SPEECH_RUN_MIN_FRAMES, [])

    assert wl._user_speech_seen is True
    assert wl._silero_aec_armed_at_ms == 0


# --- a VAD that explodes mid-drain -----------------------------------


@pytest.mark.asyncio
async def test_a_vad_failure_mid_drain_does_not_cost_the_command():
    """The drain's `vad_predict` was unguarded: a raise unwound it with
    the buffer part-drained, so the rest of the user's command was never
    sent and the caller reported zero frames. (The LIVE path's predict
    stays unguarded on purpose — a persistently broken VAD should crash
    loudly rather than degrade.)"""
    wl = WakeLoop.for_tests()
    wl._turn = _SpyTurn()
    wl._vad = _ScriptedVad([LOUD, LOUD, RuntimeError("onnxruntime exploded"), LOUD, LOUD])
    wl._acquire_buffer.extend(_frame() for _ in range(5))

    drained = await wl._drain_acquire_audio()

    assert drained == 5
    assert wl._turn.sends == 5
    assert not wl._acquire_buffer
    # Whatever was scored before the failure still counts.
    assert wl._speech_run_frames == 2


@pytest.mark.asyncio
async def test_a_failed_drain_does_not_leave_frames_for_the_next_turn():
    """`drain_acquire_buffer` propagates a `send_audio` failure with the
    buffer part-drained and documents that the caller logs and clears the
    remainder. The caller's handler never did, so those frames sat in the
    buffer and replayed into the NEXT turn as leading audio."""
    wl = WakeLoop.for_tests()
    wl._turn = _SpyTurn()
    wl._acquire_buffer.extend(_frame() for _ in range(6))

    async def _explode() -> int:
        raise RuntimeError("turn torn down mid-drain")

    async def _noop(*_a, **_k) -> None:
        return None

    wl._drain_acquire_audio = _explode  # type: ignore[method-assign]
    wl._begin_turn = _noop  # type: ignore[method-assign]
    wl._notify_peering_session_started = _noop  # type: ignore[method-assign]

    await wl._arbitrate_acquire_drain(
        score=0.9,
        rms_dbfs=-20.0,
        spend_allowed=True,
        conn_paused=False,
        can_serve=True,
    )

    assert not wl._acquire_buffer
