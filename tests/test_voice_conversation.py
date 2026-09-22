# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jasper.voice.conversation import (
    ACKNOWLEDGED_BACKEND_SEC,
    FIRST_ANSWER_SEC,
    WATCHDOG_POLL_SEC,
    continuous_watchdog,
)
from jasper.voice.openai_live_session import OpenAILiveConnection, OpenAILiveTurn
from jasper.voice.turn_lifecycle import State
from jasper.voice.turn_playback import PlaybackReport, play_responses
from jasper.voice.session import AudioOutChunk
from jasper.voice.speech_activity import SpeechActivity
from jasper.voice import conversation, openai_live_session, turn_playback
from tests._async_wait import wait_signalled, wait_until
from tests._live_turn_fake import FakeLiveTurn, silent_frame
from tests._log_events import event_fields
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


@pytest.mark.parametrize(
    "last_speech, accepted_at, drain_at, backend, followup_seconds, deadline, wait, activity_at",
    [
        (100, 0, 0, False, 2, 100 + FIRST_ANSWER_SEC, "first_answer", 99.5),
        (100, 98, 98.5, False, 2, 100 + FIRST_ANSWER_SEC, "first_answer", 99.5),
        (100, 101, 104, False, 2, 104 + 2, "followup", 99.5),
        (104, 101, 0, False, 2, 104 + 2, "followup", 99.5),
        (100, 101, 0, False, 0, 101, "followup", 99.5),
        (100, 0, 0, True, 2, 100 + ACKNOWLEDGED_BACKEND_SEC, None, 99.5),
        (100, 101, 104, True, 2, 100 + ACKNOWLEDGED_BACKEND_SEC, None, 99.5),
    ],
)
async def test_live_first_answer_followup_and_backend_waits(
    monkeypatch, caplog, last_speech, accepted_at, drain_at, backend, followup_seconds,
    deadline, wait, activity_at,
):
    caplog.set_level(logging.INFO)
    now = deadline - WATCHDOG_POLL_SEC
    turn = FakeLiveTurn()
    monkeypatch.setattr(turn, "last_activity_at", lambda: activity_at)
    turn.backend_pending = backend
    tts = FakeTts()
    tts.expected_drain_at = lambda: drain_at

    async def tick(seconds):
        nonlocal now
        now += seconds
        assert now <= deadline

    monkeypatch.setattr(conversation, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    reason = await continuous_watchdog(
        turn, tts, followup_seconds=followup_seconds, stall_seconds=120,
        speech=SpeechActivity(started_at=99.0, last_at=last_speech),
        playback=PlaybackReport(last_accepted_at=accepted_at, audible_drain_at=max(accepted_at, drain_at)),
    )
    assert now == deadline
    assert reason == ("response_stalled" if backend else "followup_timeout")
    if not backend:
        fields = event_fields(caplog, "voice.turn_deadline")
        assert fields["wait"] == wait
        assert fields["activity_age_ms"] == str(int((deadline - activity_at) * 1000))


@pytest.mark.parametrize("speech_kind, expected", [
    ("confirmed", 107.25), ("noise", 102.0), ("capture_stops", 102.5),
    ("below_peak", 102.5),
])
async def test_speech_at_the_followup_deadline_gets_time_to_qualify(monkeypatch, speech_kind, expected):
    now = 101.75
    loop = answered_loop()
    turn = loop._turns.turn
    turn.continuous_input = turn.owns_interruption = True
    loop._turns.input_ended = True
    loop._turns.speech = SpeechActivity(started_at=89.0, last_at=90.0)
    report = PlaybackReport(last_accepted_at=99.5, audible_drain_at=100.0)
    loop._tts.expected_drain_at = lambda: 100.0
    score = 0.0
    loop._vad = SimpleNamespace(predict=lambda frame: score)
    frames = iter((101.84 + n * 0.08 for n in range(8)))
    next_frame = next(frames, None)

    async def tick(seconds):
        nonlocal now, score, next_frame
        now += seconds
        while next_frame is not None and next_frame <= now:
            score = (0.4 if speech_kind == "below_peak" else 0.95) if next_frame < 102.3 else 0.0
            if speech_kind == "noise" and next_frame > 101.85:
                score = 0.0
            await loop._handle_session_frame(silent_frame(), captured_at=next_frame)
            next_frame = None if speech_kind == "capture_stops" else next(frames, None)
        assert now <= expected

    monkeypatch.setattr(conversation, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    reason = await continuous_watchdog(
        turn, loop._tts, followup_seconds=2, stall_seconds=120,
        speech=loop._turns.speech, playback=report,
    )
    assert reason == "followup_timeout"
    assert now == expected
    assert turn.send_audio_calls > 0
    if speech_kind == "confirmed":
        assert loop._turns.speech.started_at == 101.84


@pytest.mark.parametrize("padding", [False, True])
async def test_followup_counts_from_audible_playout_not_quiet_padding(monkeypatch, padding):
    now, drain_at = 100.0, 0.0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(conversation, "time", clock)
    monkeypatch.setattr(turn_playback, "time", clock)
    monkeypatch.setattr(openai_live_session, "time", clock)
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), now)
    audible, quiet = b"\x00\x40" * 1920, bytes(3840)
    turn._on_output_audio(audible)
    if padding:
        now = 100.75
        turn._on_output_audio(quiet)
    turn._audio_q.put_nowait(None)
    report = PlaybackReport()
    tts = FakeTts()
    tts.expected_drain_at = lambda: drain_at
    original_write = tts.write_segment

    async def write(pcm, **kwargs):
        nonlocal now, drain_at
        # The speaker plays well after receipt; only padding ends at 104.75.
        now, drain_at = (103.92, 104.0) if pcm == audible else (104.67, 104.75)
        return await original_write(pcm, **kwargs)

    tts.write_segment = write
    await play_responses(turn, tts, report=report)
    assert tts.writes == ([audible, quiet] if padding else [audible])
    assert report.last_accepted_at == 103.92
    assert report.audible_drain_at == 104.0
    now = 104.75

    async def tick(seconds):
        nonlocal now
        now += seconds
        assert now <= 106.0

    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    reason = await continuous_watchdog(
        turn, tts, followup_seconds=2, stall_seconds=120,
        speech=SpeechActivity(started_at=89, last_at=90), playback=report,
    )
    assert reason == "followup_timeout"
    assert now == 106.0


@pytest.mark.parametrize("input_ended", [False, True])
async def test_the_hang_up_chirp_is_written_ahead_of_the_teardown_behind_it(input_ended):
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
    loop._turns.turn = FakeLiveTurn(bytes_sent=3200)
    loop._turns.playback_report.accepted_audio = False
    loop._turns.input_ended = input_ended
    loop._turns._play_cue = AsyncMock()
    loop._peering.session_ended = teardown
    loop._turns.turn.release = release
    loop._assistant_output.ducker.restore = restore
    loop._turns.output_episode = await loop._assistant_output.begin_turn_episode(None)
    await loop._turns.end("followup_timeout")
    assert tts.writes == [loop._assistant_output._chirp_off_pcm]
    loop._turns._play_cue.assert_not_awaited()
    assert loop._turns.silent_responses_session == 0
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
    (10.0, 11.0, 2, False, True, 14.0),
    (10.0, 11.0, 0, False, True, 12.0),
    (10.0, 10.0, 0, False, True, 11.0),
    (10.0, 9.0, 2, False, True, 18.0),
    (10.0, None, 2, False, True, 18.0),
    (30.0, None, 2, False, True, 32.0),
    (10.0, None, 2, True, True, 35.0),
    (10.0, None, 2, False, False, 18.0),
])
async def test_live_backend_handoff_has_a_bounded_grace(
    monkeypatch, completion, answer, followup, correction, acknowledged, expected,
):
    now = 2.0
    speech = SpeechActivity(started_at=0.5, last_at=1.0)
    report = PlaybackReport(last_accepted_at=2.0 if acknowledged else 0.0)
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(conversation, "time", clock)
    monkeypatch.setattr(openai_live_session, "time", clock)
    turn = OpenAILiveTurn(OpenAILiveConnection(api_key="test"), now)

    async def delegate(identity):
        await turn.on_event({"type": "session.delegation.created", "delegation": {"id": identity}})

    await delegate("original")

    async def tick(seconds):
        nonlocal now
        now += seconds
        if correction and now == 5.0:
            speech.started_at, speech.last_at, report.last_accepted_at = 4.0, 5.0, 5.0
            await delegate("correction")
        if now == completion or now == completion + 1:
            await turn.on_event({
                "type": "response.event", "delegation_id": "original",
                "event": {"type": "response.completed", "response": {"id": "r1"}},
            })
        if now == answer:
            report.last_accepted_at = now
            report.audible_drain_at = now + 1
        await turn.on_event({
            "type": "session.output_transcript.delta", "delta": ".", "start_ms": 0, "end_ms": 1,
        })
        assert now <= 36

    monkeypatch.setattr(conversation, "asyncio", SimpleNamespace(sleep=tick))
    tts = FakeTts()
    tts.expected_drain_at = lambda: report.audible_drain_at
    reason = await continuous_watchdog(
        turn, tts, followup_seconds=followup, stall_seconds=120,
        speech=speech, playback=report,
    )
    assert now == expected
    assert reason == ("response_stalled" if correction else "followup_timeout")


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
            speech=SpeechActivity(started_at=92, last_at=93), playback=report,
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
            speech=SpeechActivity(started_at=92, last_at=93), playback=report,
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
