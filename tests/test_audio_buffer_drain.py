# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from jasper.audio_buffer import AudioBuffer
from jasper.voice.turn_lifecycle import State
from jasper.voice_daemon import PRE_ROLL_FRAMES
from tests._wake_loop import wake_loop_for_tests


def _frame(tag):
    return np.full(1280, tag, dtype=np.int16)


def _stub_turn(**members) -> SimpleNamespace:
    """A turn stub plus the `LiveTurn` members the daemon reads on every frame."""
    return SimpleNamespace(continuous_input=False, discard_input=lambda: None, **members)


@pytest.mark.parametrize("overflow", [False, True])
def test_buffer_drops_oldest_and_marks_next_frame(monkeypatch, overflow):
    monkeypatch.setattr("jasper.audio_buffer.time.monotonic", lambda: 10.0)
    buffer = AudioBuffer(max_frames=2, max_age_sec=1.0)
    for tag, captured_at in enumerate([8.0, 9.5, 10.0] if overflow else [8.0, 10.0]):
        buffer.append(_frame(tag), captured_at)
    first = buffer.pop()
    assert first.pcm[0] == 1
    assert first.discontinuity
    assert buffer.dropped_frames == 1
    if overflow:
        assert buffer.pop().pcm[0] == 2
    assert buffer.pop() is None


async def _park():
    await asyncio.Event().wait()


def _acquire_loop(monkeypatch):
    wl = wake_loop_for_tests()
    wl._prepare_assistant_loudness_context = AsyncMock()
    wl._content_activity.refresh_now = AsyncMock()
    wl._turns.arm_background_end = lambda: None
    monkeypatch.setattr("jasper.voice.turn_lifecycle.play_responses", lambda *a, **k: _park())
    monkeypatch.setattr("jasper.voice.turn_lifecycle.idle_watchdog", lambda *a, **k: _park())
    return wl


async def _stop_playback(wl):
    for task in wl._turns.bg_tasks:
        task.cancel()
    await asyncio.gather(*wl._turns.bg_tasks, return_exceptions=True)


@pytest.mark.parametrize("trigger", ["wake", "manual", "remote"])
async def test_delayed_acquire_freezes_prefix_and_drains_concurrent_input(monkeypatch, trigger):
    wl = _acquire_loop(monkeypatch)
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent = []

    class Mic:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def frames(self):
            while True:
                yield await self.queue.get()
                self.queue.task_done()

    mic = Mic()
    wl._mic = wl._wake_legs.legs["on"].mic = mic
    remote = Mic()
    if trigger == "remote":
        wl._push_to_talk.sources["remote"] = SimpleNamespace(mic=remote)
    source = remote if trigger == "remote" else mic
    wl._wake_legs.legs["on"].detector.score_frame = lambda frame: float(trigger == "wake" and frame[0] == 0)
    wl._play_listening_chirp = AsyncMock()

    async def send(pcm):
        tag = int(np.frombuffer(pcm, dtype=np.int16)[0])
        sent.append(tag)
        if tag == 3:
            source.queue.put_nowait(_frame(11))
        if tag == 11:
            delivered.set()
        await asyncio.sleep(0)

    async def acquire():
        entered.set()
        await release.wait()
        return _stub_turn(send_audio=send)

    wl._connection.acquire_turn = acquire
    runner = asyncio.create_task(wl.run())
    starter = None
    try:
        for tag in range(1 - PRE_ROLL_FRAMES, 1):
            mic.queue.put_nowait(_frame(tag))
        await asyncio.wait_for(mic.queue.join(), 1.0)
        if trigger != "wake":
            starter = asyncio.create_task(wl.manual_session_start(
                "remote" if trigger == "remote" else None,
            ))
        await asyncio.wait_for(entered.wait(), 1.0)
        for tag in range(1, 11):
            source.queue.put_nowait(_frame(tag))
        await asyncio.wait_for(source.queue.join(), 1.0)
        release.set()
        await asyncio.wait_for(delivered.wait(), 1.0)
        if starter is not None:
            assert await starter == "OK"
        assert sent == list(range(1 if trigger == "remote" else 1 - PRE_ROLL_FRAMES, 12))
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        if starter is not None:
            starter.cancel()
            await asyncio.gather(starter, return_exceptions=True)
        await _stop_playback(wl)


@pytest.mark.parametrize("split", [0, 2, 5, 10, 16])
async def test_endpoint_is_independent_of_acquire_split(split):
    wl = wake_loop_for_tests()
    wl._turns.state = State.SESSION
    wl._turns.started_at_loop = time.monotonic() - 2.0
    wl._turns.turn = _stub_turn(send_audio=AsyncMock(), end_input=AsyncMock())
    wl._vad.predict = lambda frame: float(frame[0])
    frames = [1] * 4 + [0] * 12
    for index, score in enumerate(frames):
        at = wl._turns.started_at_loop + (index + 1) * 0.081
        if index < split:
            wl._acquire_buffer.append(_frame(score), at)
        else:
            if index == split:
                await wl._drain_acquire_audio()
            await wl._handle_session_frame(_frame(score), captured_at=at)
    if split == len(frames):
        await wl._drain_acquire_audio()
    assert wl._turns.user_speech_seen
    assert wl._turns.input_ended
    assert wl._turns.turn.end_input.await_count == 1
    assert wl._turns.turn.send_audio.await_count == 14


@pytest.mark.parametrize("armed", [False, True])
async def test_acquire_gap_resets_speech_and_silence_runs(armed):
    wl = wake_loop_for_tests()
    wl._turns.state = State.SESSION
    wl._turns.turn = _stub_turn(send_audio=AsyncMock(), end_input=AsyncMock())
    wl._turns.started_at_loop = time.monotonic() - 2.0
    wl._turns.user_speech_seen = armed
    wl._speech_run_started_at = wl._silence_started_at = time.monotonic() - 1.0
    wl._speech_run_max_silero = 1.0
    wl._barge_in_run_started_at = time.monotonic() - 1.0
    reset = []
    wl._vad.reset = lambda: reset.append(True)
    wl._vad.predict = lambda frame: float(frame[0])
    wl._acquire_buffer.append(_frame(0 if armed else 1), discontinuity=True)
    await wl._drain_acquire_audio()
    assert wl._turns.user_speech_seen is armed
    assert not wl._turns.input_ended
    assert reset == [True]
    assert wl._barge_in_run_started_at == 0.0


async def test_no_speech_abort_then_fresh_command(monkeypatch):
    wl = _acquire_loop(monkeypatch)
    turns = []

    async def acquire():
        turn = _stub_turn(send_audio=AsyncMock(), end_input=AsyncMock())
        turns.append(turn)
        return turn

    async def end():
        await _stop_playback(wl)
        wl._turns.turn = None
        wl._turns.state = State.WAKE

    wl._connection.acquire_turn = acquire
    wl._turns.end = end
    wl._vad.predict = lambda frame: float(frame[0])
    await wl._turns.begin_inner(pre_roll=False)
    await wl._handle_session_frame(_frame(0), captured_at=wl._turns.started_at_loop + 5.1)
    assert wl._turns.state is State.WAKE
    await wl._turns.begin_inner(pre_roll=False)
    try:
        for index, score in enumerate([1] * 4 + [0] * 12):
            await wl._handle_session_frame(
                _frame(score), captured_at=wl._turns.started_at_loop + (index + 1) * 0.081,
            )
        assert wl._turns.input_ended
        assert turns[0].send_audio.await_count == 0
        assert turns[1].end_input.await_count == 1
    finally:
        await _stop_playback(wl)


async def test_endpoint_discarded_tail_does_not_keep_pre_gap_vad_state():
    wl = wake_loop_for_tests()
    wl._turns.state = State.SESSION
    wl._turns.turn = _stub_turn(send_audio=AsyncMock(), end_input=AsyncMock())
    wl._turns.started_at_loop = time.monotonic() - 2.0
    wl._turns.user_speech_seen = True
    wl._silence_started_at = time.monotonic() - 1.0
    reset = []
    wl._vad.reset = lambda: reset.append(True)
    wl._acquire_buffer.append(_frame(0))
    wl._acquire_buffer.append(_frame(1), discontinuity=True)
    await wl._drain_acquire_audio()
    assert wl._turns.input_ended
    assert len(wl._acquire_buffer) == 0
    assert reset == [True]


@pytest.mark.parametrize("gate", ["mute", "measurement"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("wait_at", ["prepare", "acquire", "prefix"])
async def test_input_pause_during_acquire_never_uploads_frozen_prefix(
    monkeypatch, tmp_path, gate, resume, wait_at,
):
    wl = _acquire_loop(monkeypatch)
    monkeypatch.setattr("jasper.voice.measurement_hold.MEASUREMENT_INFLIGHT_DRAIN_SEC", 0.0)
    wl._cfg.mic_mute_state_path = str(tmp_path / "mute.env")
    wl._play_mute_click = AsyncMock()
    wl._play_listening_chirp = AsyncMock()
    wl._pre_roll.append(_frame(99))
    entered, release = asyncio.Event(), asyncio.Event()
    turn = _stub_turn(send_audio=AsyncMock(), release=AsyncMock())

    async def hold(*_args):
        entered.set()
        await release.wait()

    async def acquire():
        if wait_at == "acquire":
            await hold()
        return turn

    if wait_at == "prepare":
        wl._prepare_assistant_loudness_context = hold
    elif wait_at == "prefix":
        turn.send_audio.side_effect = hold
    wl._connection.acquire_turn = acquire
    task = asyncio.create_task(wl.manual_session_start())
    await asyncio.wait_for(entered.wait(), 1.0)
    try:
        if gate == "mute":
            await wl.mute_mic()
            if resume:
                await wl.unmute_mic()
        else:
            assert await wl.measurement_hold.pause_response() == {"result": "ok", "drained": False}
            if resume:
                await wl.measurement_hold.resume()
        release.set()
        assert (await task, turn.send_audio.await_count) == (
            "MUTED" if gate == "mute" else "MEASURING", int(wait_at == "prefix"),
        )
        assert turn.release.await_count == int(wait_at != "prepare")
        assert not wl._acquiring
        assert wl._turns.state is State.WAKE
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _stop_playback(wl)
        await wl.measurement_hold.resume()
        await wl._cancel_fire_and_forget_tasks()


@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize("scores,armed", [
    ([0.5] * 8, False),
    ([0.6, 0.2, 0.2, 0.2], True),
    ([0.6, 0.2, 0.0, 0.2, 0.2, 0.2, 0.2], False),
    ([0.6, 0.2, 0.2], False),
])
async def test_endpoint_preserves_sustained_speech_and_peak_rules(buffered, scores, armed):
    wl = wake_loop_for_tests()
    wl._turns.state = State.SESSION
    wl._turns.started_at_loop = time.monotonic() - 3.0
    wl._turns.turn = _stub_turn(send_audio=AsyncMock(), end_input=AsyncMock())
    wl._vad.predict = lambda frame: float(frame[0]) / 100
    for index, score in enumerate(scores + [0.0] * 12):
        frame = _frame(round(score * 100))
        at = wl._turns.started_at_loop + (index + 1) * 0.081
        if buffered:
            wl._acquire_buffer.append(frame, at)
        else:
            await wl._handle_session_frame(frame, captured_at=at)
    if buffered:
        await wl._drain_acquire_audio()
    assert wl._turns.user_speech_seen is armed
    assert wl._turns.input_ended is armed
    assert wl._turns.turn.end_input.await_count == int(armed)
