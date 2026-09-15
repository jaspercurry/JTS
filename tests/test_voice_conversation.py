# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jasper.tools import ToolRegistry, dispatch_tool
from jasper.voice.conversation import (
    WATCHDOG_POLL_SEC,
    continuous_watchdog,
    register_conversation_tools,
)
from jasper.voice.openai_live_session import OpenAILiveConnection, OpenAILiveTurn
from jasper.voice.turn_lifecycle import State
from jasper.voice.turn_playback import PlaybackReport, play_responses
from jasper.voice.session import AudioOutChunk
from jasper.voice import conversation, openai_live_session, turn_playback
from tests._async_wait import wait_signalled, wait_until
from tests._live_turn_fake import FakeLiveTurn
from tests._playout import FakeTts
from tests._wake_loop import wake_loop_for_tests
from tests.usage_store_fixtures import FakeUsageStore


def answered_loop(tts=None):
    loop = wake_loop_for_tests(usage_store=FakeUsageStore(), tts=tts)
    loop._turns.state = State.SESSION
    loop._turns.turn = FakeLiveTurn(chunks_received=1)
    loop._turns.session_id = 7
    loop._turns.user_speech_seen = True
    loop._turns.playback_report.accepted_audio = True
    return loop


async def test_endpointed_answer_closes_the_turn_once_playout_drains():
    """No host follow-up window survives: see ADR-0292."""
    loop = answered_loop()
    old = loop._turns.turn
    finish = AsyncMock()
    loop._assistant_output.finish_turn_episode = finish
    ended = AsyncMock()
    loop._peering.session_ended = ended
    await loop._turns.finish_response("ended")
    assert loop._turns.state is State.WAKE
    assert old.release_calls == 1
    assert loop._usage_store.close_calls == 1
    finish.assert_awaited_once()
    ended.assert_awaited_once_with("ended")
    await loop._cancel_fire_and_forget_tasks()


async def test_end_conversation_tool_closes_even_without_more_mic_frames():
    loop = answered_loop()
    registry = ToolRegistry()
    register_conversation_tools(registry, loop._turns.request_conversation_end)
    result = await dispatch_tool(registry, "end_conversation", {})
    await wait_until(lambda: loop._turns.state is State.WAKE)
    assert result == {"status": "conversation_ended"}
    assert loop._usage_store.close_calls == 1
    await loop._cancel_fire_and_forget_tasks()


@pytest.mark.parametrize(
    "busy, followup_seconds",
    [("speaker", 5), ("user", 5), ("tool", 5), ("quiet", 5), ("quiet", 0)],
)
async def test_live_followup_waits_for_playout_speech_and_tools(busy, followup_seconds):
    now = time.monotonic()
    turn = FakeLiveTurn(chunks_received=1)
    turn.last_chunk_at = lambda: now - 8
    turn.audio_chunks_pending = lambda: 0
    turn.backend_pending = busy == "tool"
    turn.last_activity_at = lambda: now if busy == "tool" else now - 8
    tts = FakeTts()
    tts.expected_drain_at = lambda: now + 10 if busy == "speaker" else now - 8
    task = asyncio.create_task(continuous_watchdog(
        turn, tts, followup_seconds=followup_seconds, stall_seconds=120,
        user_activity=lambda: (now - 10, now if busy == "user" else now - 9),
        last_accepted_at=lambda: now - 8,
    ))
    try:
        # Long enough for one full watchdog poll to reach its verdict.
        await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
        assert task.done() == (busy == "quiet")
        if task.done():
            assert task.result() == "followup_timeout"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("acknowledged, backend, elapsed, ended", [
    (False, False, 7, False),
    (False, False, 9, True),
    (False, True, 9, True),
    (True, True, 9, False),
    (True, True, 31, True),
    (True, False, 9, True),
])
async def test_live_deadlines_ignore_provider_chatter(acknowledged, backend, elapsed, ended):
    now = time.monotonic()
    turn = FakeLiveTurn(chunks_received=1)
    turn.backend_pending = backend
    turn.last_chunk_at = lambda: now
    turn.last_activity_at = lambda: time.monotonic()
    task = asyncio.create_task(continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (now - elapsed - 1, now - elapsed),
        last_accepted_at=lambda: now - elapsed + 1 if acknowledged else 0,
    ))
    try:
        await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
        assert task.done() is ended
        if ended:
            assert task.result() == ("response_stalled" if backend or not acknowledged else "followup_timeout")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_partial_transcripts_do_not_end_the_conversation():
    now = time.monotonic()
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), now)
    task = asyncio.create_task(continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (now - 2, now - 1), last_accepted_at=lambda: 0,
    ))
    try:
        for delta in ("stop", " the timer"):
            await turn.on_event({
                "type": "session.input_transcript.delta", "delta": delta,
                "start_ms": 0, "end_ms": 1000,
            })
            await asyncio.sleep(WATCHDOG_POLL_SEC * 2)
            assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("second_utterance", [False, True])
@pytest.mark.parametrize("nudge_result", [False, True])
async def test_unanswered_speech_gets_one_nudge_per_utterance(monkeypatch, second_utterance, nudge_result):
    now = 100.0
    speech_started = now
    turn = FakeLiveTurn()
    turn.nudge_backend_result = nudge_result

    async def tick(seconds):
        nonlocal now, speech_started
        now += seconds
        if second_utterance and now == 104:
            speech_started = now

    monkeypatch.setattr(conversation, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    reason = await continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (speech_started, speech_started), last_accepted_at=lambda: 0,
    )
    assert reason == "response_stalled"
    assert now == (112 if second_utterance else 108)
    expected = (
        [2000] * (2 if second_utterance else 1) if nudge_result else
        (list(range(2000, 4000, 250)) if second_utterance else []) + list(range(2000, 8001, 250))
    )
    assert turn.nudge_backend_calls == expected


@pytest.mark.parametrize("last_chunk_at", [0.0, 100.0, 101.0])
@pytest.mark.parametrize("transcript_at, pending, lost, expected", [
    (100.0, False, False, "unanswered_utterance"),
    (0.0, False, False, "response_stalled"),
    (99.0, False, False, "response_stalled"),
    (100.0, True, False, "response_stalled"),
    (100.0, False, True, "connection_lost"),
])
async def test_unanswered_verdict_requires_this_utterance_transcribed_and_backend_idle(
    monkeypatch, transcript_at, pending, lost, expected, last_chunk_at,
):
    now = 107.75
    turn = FakeLiveTurn()
    turn.user_transcript_at = transcript_at
    turn.backend_pending = pending
    turn.turn_lost = lambda: lost
    turn.last_chunk_at = lambda: last_chunk_at

    async def tick(seconds):
        nonlocal now
        now += seconds
        assert now == 108

    monkeypatch.setattr(conversation, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    assert await continuous_watchdog(
        turn, FakeTts(), followup_seconds=5, stall_seconds=120,
        user_activity=lambda: (100, 100), last_accepted_at=lambda: 0,
    ) == expected
    assert turn.nudge_backend_calls == ([] if lost or last_chunk_at >= 100 else [8000])


@pytest.mark.parametrize("input_ended", [False, True])
async def test_unanswered_utterance_ends_with_chirp_and_complete_outcome(input_ended):
    tts = FakeTts()
    loop = answered_loop(tts=tts)
    loop._turns.turn = FakeLiveTurn(bytes_sent=3200)
    loop._turns.playback_report.accepted_audio = False
    loop._turns.input_ended = input_ended
    loop._turns._timeline.anchor_at()
    cue = AsyncMock()
    loop._turns._play_cue = cue
    loop._turns.output_episode = await loop._assistant_output.begin_turn_episode(None)
    await loop._turns.end("unanswered_utterance")
    assert tts.writes == [loop._assistant_output._chirp_off_pcm]
    cue.assert_not_awaited()
    assert loop._turns._timeline.last_turn_ms["outcome"] == "complete"
    assert loop._turns.silent_responses_session == 0
    await loop._cancel_fire_and_forget_tasks()


@pytest.mark.parametrize("reason", ["followup_timeout", "conversation_ended"])
async def test_the_hang_up_chirp_is_written_ahead_of_the_teardown_behind_it(reason):
    """The teardown runs behind the cue, not in front of it."""
    order: list[str] = []
    chirped = asyncio.Event()

    def note(call: str) -> None:
        order.append(call)
        if call == "write_segment":
            chirped.set()

    async def teardown(_reason):
        order.append("teardown")
        try:
            await asyncio.wait_for(chirped.wait(), timeout=0.25)
        except TimeoutError:
            order.append("still_silent")

    async def release():
        order.append("release")

    async def restore():
        order.append("unduck")

    tts = FakeTts(on_call=note)
    loop = answered_loop(tts=tts)
    loop._peering.session_ended = teardown
    loop._turns.turn.release = release
    loop._assistant_output.ducker.restore = restore
    loop._turns.output_episode = await loop._assistant_output.begin_turn_episode(None)
    await loop._turns.end(reason)
    assert tts.writes == [loop._assistant_output._chirp_off_pcm]
    assert "still_silent" not in order
    chirp = order.index("write_segment")
    assert chirp < order.index("release")
    assert chirp < order.index("unduck")
    await loop._cancel_fire_and_forget_tasks()


async def test_playout_acceptance_updates_after_the_first_answer():
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), time.monotonic())
    turn._enqueue_audio(AudioOutChunk(b"\x00\x40" * 120))
    turn._audio_q.put_nowait(None)
    report = PlaybackReport(accepted_audio=True)
    started = time.monotonic()
    await play_responses(turn, FakeTts(), report=report)
    assert report.last_accepted_at >= started


@pytest.mark.parametrize("completion, answer, followup, correction, acknowledged, expected", [
    (10.0, 11.0, 5, False, True, 17.0),
    (10.0, 11.0, 0, False, True, 12.0),
    (10.0, 10.0, 0, False, True, 11.0),
    (10.0, 9.0, 5, False, True, 18.0),
    (10.0, None, 5, False, True, 18.0),
    (28.0, None, 5, False, True, 31.0),
    (10.0, None, 5, True, True, 35.0),
    (10.0, None, 5, False, False, 9.0),
])
async def test_live_backend_handoff_has_a_bounded_grace(
    monkeypatch, completion, answer, followup, correction, acknowledged, expected,
):
    now, accepted, speech_started, last_speech = 2.0, 2.0, 0.5, 1.0
    if not acknowledged:
        accepted = 0.0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(conversation, "time", clock)
    monkeypatch.setattr(openai_live_session, "time", clock)
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), now)

    async def delegate(identity):
        await turn.on_event({"type": "session.delegation.created", "delegation": {"id": identity}})

    await delegate("original")

    async def tick(seconds):
        nonlocal now, accepted, speech_started, last_speech
        now += seconds
        if correction and now == 5.0:
            speech_started, last_speech, accepted = 4.0, 5.0, 5.0
            await delegate("correction")
        if now == completion or now == completion + 1:
            await turn.on_event({
                "type": "response.event", "delegation_id": "original",
                "event": {"type": "response.completed", "response": {"id": "r1"}},
            })
        if now == answer:
            accepted = now
        await turn.on_event({
            "type": "session.output_transcript.delta", "delta": ".", "start_ms": 0, "end_ms": 1,
        })
        assert now <= 36

    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    tts = FakeTts()
    tts.expected_drain_at = lambda: accepted + 1 if answer and now >= answer else 0
    reason = await continuous_watchdog(
        turn, tts, followup_seconds=followup, stall_seconds=120,
        user_activity=lambda: (speech_started, last_speech), last_accepted_at=lambda: accepted,
    )
    assert now == expected
    assert reason == ("response_stalled" if correction or not accepted else "followup_timeout")


@pytest.mark.parametrize("accepted, drain, accepted_age, drain_age", [
    (0, 0, None, None),
    (99, 0, 1000, None),
    (99, 99, 1000, 1000),
    (0, 101, None, -1000),
])
def test_deadline_ages_distinguish_absent_anchors_and_future_playout(
    monkeypatch, accepted, drain, accepted_age, drain_age,
):
    emitted = Mock()
    monkeypatch.setattr(conversation, "log_event", emitted)
    assert conversation._resolved(
        "playout_stalled", 100, 98, accepted, drain, 0, FakeLiveTurn(), None,
    ) == "playout_stalled"
    fields = emitted.call_args.kwargs
    assert fields["accepted_age_ms"] == accepted_age
    assert fields["drain_age_ms"] == drain_age


@pytest.mark.parametrize("finish_write", [True, False])
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_live_first_and_later_writes_survive_response_deadlines_but_are_bounded(
    monkeypatch, finish_write, acknowledged,
):
    now = 100.0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(conversation, "time", clock)
    monkeypatch.setattr(turn_playback, "time", clock)
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), now)
    turn._enqueue_audio(AudioOutChunk(b"\x00\x40" * 120))
    report = PlaybackReport(accepted_audio=acknowledged, last_accepted_at=96 if acknowledged else 0)
    entered, unblock = asyncio.Event(), asyncio.Event()
    tts = FakeTts()
    original_write = tts.write_segment

    async def write(*args, **kwargs):
        entered.set()
        await unblock.wait()
        return await original_write(*args, **kwargs)

    tts.write_segment = write
    playback = asyncio.create_task(play_responses(turn, tts, report=report))
    await wait_signalled(entered, "playout write", producer=playback)
    assert turn.audio_chunks_pending() == 0
    assert report.write_started_at == 100

    async def tick(seconds):
        nonlocal now
        now += seconds
        if now == 101.5 and finish_write:
            unblock.set()
            await wait_until(lambda: report.write_started_at == 0)
        assert now <= 106.5

    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    try:
        reason = await continuous_watchdog(
            turn, tts, followup_seconds=5, stall_seconds=2,
            user_activity=lambda: (92, 93), last_accepted_at=lambda: report.last_accepted_at,
            write_started_at=lambda: report.write_started_at,
        )
        assert reason == ("followup_timeout" if finish_write else "playout_stalled")
        assert now == (106.5 if finish_write else 102)
    finally:
        playback.cancel()
        await asyncio.gather(playback, return_exceptions=True)
    assert report.write_started_at == 0


@pytest.mark.parametrize("lost_before_playback", [True, False])
@pytest.mark.parametrize("stuck", [None, "write", "drain"])
async def test_connection_loss_drains_received_audio_but_bounds_stuck_playout(
    monkeypatch, lost_before_playback, stuck,
):
    now = 100.0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(conversation, "time", clock)
    monkeypatch.setattr(turn_playback, "time", clock)
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), now)
    chunks = [b"\x00\x40" * 120, b"\x00\x20" * 120]
    for pcm in chunks:
        turn._enqueue_audio(AudioOutChunk(pcm))
    if lost_before_playback:
        turn._on_connection_lost()
    report = PlaybackReport()
    writing, unblock, written, drained = (asyncio.Event() for _ in range(4))
    tts = FakeTts(on_drain=drained.wait)
    tts.expected_drain_at = lambda: (1000 if stuck == "drain" else 102) if report.accepted_audio else 0
    original_write = tts.write_segment

    async def write(*args, **kwargs):
        writing.set()
        await unblock.wait()
        accepted = await original_write(*args, **kwargs)
        if len(tts.writes) == len(chunks):
            written.set()
        return accepted

    tts.write_segment = write
    playback = asyncio.create_task(play_responses(turn, tts, continuous=True, report=report))
    try:
        await wait_signalled(writing, "first tail write", producer=playback)
        if not lost_before_playback:
            turn._on_connection_lost()

        async def tick(seconds):
            nonlocal now
            now += seconds
            if now == 101 and stuck != "write":
                unblock.set()
                await wait_signalled(written, "tail accepted", producer=playback)
            if now == 102 and stuck is None:
                drained.set()
            await asyncio.sleep(0)
            assert now <= 103

        monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
        reason = await continuous_watchdog(
            turn, tts, followup_seconds=5, stall_seconds=2,
            user_activity=lambda: (92, 93), last_accepted_at=lambda: report.last_accepted_at,
            write_started_at=lambda: report.write_started_at,
        )
        assert reason == ("connection_lost" if stuck is None else "playout_stalled")
        assert now == (103 if stuck == "drain" else 102)
        assert tts.writes == ([] if stuck == "write" else chunks)
        if stuck is None:
            await playback
            assert turn.audio_chunks_pending() == 0
    finally:
        playback.cancel()
        await asyncio.gather(playback, return_exceptions=True)


@pytest.mark.parametrize("reason", ["response_stalled", "playout_stalled"])
async def test_stalled_conversation_plays_one_failure_cue(reason):
    tts = FakeTts()
    loop = answered_loop(tts=tts)
    cue = AsyncMock()
    loop._turns._play_cue = cue
    loop._turns.output_episode = await loop._assistant_output.begin_turn_episode(None)
    await loop._turns.end(reason)
    cue.assert_awaited_once()
    assert not tts.writes
    assert tts.flush_calls == 1
    await loop._cancel_fire_and_forget_tasks()
