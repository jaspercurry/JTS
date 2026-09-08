# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging

import numpy as np
import pytest

from jasper.research import ResearchJob
from jasper.voice.research_announcer import (
    RESEARCH_READY_CONFIRMATION_TEXT,
    ResearchWindow,
)
from tests._live_turn_fake import FakeLiveTurn as _FakeTurn
from tests._log_events import event_records
from tests._turn_host_fake import _job, _MarkingScheduler
from tests._wake_loop import wake_loop_for_tests
from tests.usage_store_fixtures import FakeUsageStore

def _wake_loop():
    from jasper.voice_daemon import State

    wl = wake_loop_for_tests()
    wl._state = State.WAKE
    return wl


def _open_window(wl, job: ResearchJob, *, opening_done=None) -> None:
    """Arm `wl`'s announcer with the window an opener would have left."""
    wl._research._window = (
        ResearchWindow.OPENING if opening_done is not None
        else ResearchWindow.OPEN
    )
    wl._research._window_job = job
    wl._research._window_opening_done = opening_done


def _put_in_session(
    wl,
    *,
    bytes_sent: int = 0,
    chunks_received: int = 0,
) -> _FakeTurn:
    from jasper.voice_daemon import State

    turn = _FakeTurn(bytes_sent=bytes_sent, chunks_received=chunks_received)
    wl._state = State.SESSION
    wl._turn = turn
    wl._session_id = 7
    wl._usage_store = FakeUsageStore()
    wl._user_speech_seen = True
    wl._input_ended = False

    async def _noop(*_args, **_kwargs):
        return None

    async def _noop_chirp(*, going_on):
        return None

    wl._wake_telemetry.stage = _noop
    wl._wake_telemetry.outcome = _noop
    wl._peering.session_ended = _noop
    wl._play_listening_chirp = _noop_chirp
    return turn


async def test_confirmation_silence_dismisses_without_model_commit(caplog):
    wl = _wake_loop()
    turn = _put_in_session(wl, bytes_sent=299_520)
    wl._user_speech_seen = False
    wl._input_ended = False
    _open_window(wl, _job())
    scheduler = _MarkingScheduler()
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await wl._end_turn_inner("no_speech")

    assert turn.end_input_calls == 0
    assert turn.release_calls == 1
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []
    assert wl._research.window_active is False
    assert not event_records(caplog, "turn.silent_response")


async def test_research_announced_in_session_is_spoken_by_end_turn_drain():
    """Pins `_end_turn_inner`'s trailing `await self._research.drain()`
    through the real loop, not the announcer directly: a job announced
    mid-SESSION is held (nothing spoken yet), and only reaches the speaker
    because teardown drains the announcer after flipping back to WAKE.
    Stubbing `drain()` to a no-op would leave this red while every
    announcer-level drain test (which calls `announcer.drain()` directly)
    stays green."""
    wl = _wake_loop()
    _put_in_session(wl)
    scheduler = _MarkingScheduler()
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]
    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    async def _begin_turn(*, pre_roll: bool, text_context: str | None) -> None:
        # The confirmation window this opens is not under test here — only
        # that the drain reaches `_speak` at all.
        return None

    wl._play_dynamic_text = _play
    wl._begin_turn = _begin_turn

    await wl.announce_research_ready(_job())

    assert spoken == []
    assert wl._research.status()["pending_announcements"] == 1

    await wl._end_turn_inner("test")

    assert spoken == [RESEARCH_READY_CONFIRMATION_TEXT]
    assert wl._research.status()["pending_announcements"] == 0


@pytest.mark.parametrize("opening", [False, True])
async def test_real_wake_cancels_research_without_holding_mic_capture(opening):
    wl = _wake_loop()
    turn = None if opening else _put_in_session(wl)
    wl._user_speech_seen = False
    wl._input_ended = False
    opening_done = asyncio.Event() if opening else None
    _open_window(wl, _job(), opening_done=opening_done)
    wl._legs["on"].detector.score_frame = lambda _frame: 0.95
    records = []
    wl._wake_telemetry.store = object()

    async def close_record(*_args):
        records.append("old_closed")

    async def open_record(**_kwargs):
        records.append("new_opened")
        return None

    wl._wake_telemetry.outcome = close_record
    wl._wake_telemetry.on_fire = open_record
    cancellation_started = asyncio.Event()
    cancel_research = wl._research.cancel_for_wake

    async def cancel():
        cancellation_started.set()
        return await cancel_research()

    wl._research.cancel_for_wake = cancel
    arbitrated, release = asyncio.Event(), asyncio.Event()

    async def arbitrate(**_kwargs):
        arbitrated.set()
        await release.wait()
        return "LOSE"

    wl._peering.arbitrate = arbitrate
    try:
        await wl._handle_wake_frame(np.zeros(1280, dtype=np.int16), leg="on")
        assert wl._acquiring
        if opening:
            await asyncio.wait_for(cancellation_started.wait(), timeout=1.0)
            assert wl._research._window is ResearchWindow.CANCELLED
            assert not arbitrated.is_set()
            wl._research._window = ResearchWindow.IDLE
            opening_done.set()
        await asyncio.wait_for(arbitrated.wait(), timeout=1.0)
        assert records == (["new_opened"] if opening else ["old_closed", "new_opened"])
        if turn is not None:
            assert turn.end_input_calls == 0
            assert turn.release_calls == 1
        assert not wl._research.window_active
        assert wl._state.name == "WAKE"
        assert wl._acquiring
    finally:
        release.set()
        await wl._cancel_fire_and_forget_tasks()


async def test_confirmation_open_cancelled_after_begin_ends_turn_without_reading():
    wl = _wake_loop()
    job = _job()
    spoken: list[str] = []

    async def _begin_turn(*, pre_roll: bool, text_context: str | None) -> None:
        assert pre_roll is False
        assert text_context is not None
        _put_in_session(wl)
        wl._research._window = ResearchWindow.CANCELLED

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    wl._begin_turn = _begin_turn
    wl._play_dynamic_text = _play

    await wl._research.open_confirmation_window(job)

    assert spoken == []
    assert wl._research.window_active is False
    assert wl._research._window_opening_done is None
    assert wl._state.name == "WAKE"


async def test_confirmation_open_cancelled_begin_failure_clears_without_reading():
    wl = _wake_loop()
    job = _job()
    spoken: list[str] = []

    async def _begin_turn(*, pre_roll: bool, text_context: str | None) -> None:
        assert pre_roll is False
        assert text_context is not None
        wl._research._window = ResearchWindow.CANCELLED
        raise RuntimeError("turn already cancelled by wake")

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    wl._begin_turn = _begin_turn
    wl._play_dynamic_text = _play

    await wl._research.open_confirmation_window(job)

    assert spoken == []
    assert wl._research.window_active is False
    assert wl._research._window_job is None
    assert wl._research._window_opening_done is None
    assert wl._state.name == "WAKE"


async def test_confirmation_open_unexpected_begin_failure_resets_window_flags():
    wl = _wake_loop()
    job = _job()

    async def _begin_turn(*, pre_roll: bool, text_context: str | None) -> None:
        assert pre_roll is False
        assert text_context is not None
        raise AssertionError("unexpected begin failure")

    wl._begin_turn = _begin_turn

    with pytest.raises(AssertionError):
        await wl._research.open_confirmation_window(job)

    assert wl._research._window is ResearchWindow.IDLE
    assert wl._research._window_job is None
    assert wl._research._window_opening_done is None


def test_system_instruction_includes_research_nudge_when_unconfigured():
    from jasper.voice.prompt import _build_system_instruction

    prompt = _build_system_instruction(
        location="",
        research_configured=False,
        hostname="jts2.local",
    )

    assert "jts2.local/assistant/voice/" in prompt
    assert "If the user asks you to research" in prompt
    assert "Research isn't set up yet" in prompt


def test_system_instruction_omits_research_nudge_when_configured():
    from jasper.voice.prompt import _build_system_instruction

    prompt = _build_system_instruction(location="", research_configured=True)

    assert "Research isn't set up yet" not in prompt


def test_research_failed_cue_is_registered_provider_agnostic():
    from jasper.cues.registry import find

    cue = find("research_failed")

    assert cue is not None
    text = cue.template.lower()
    assert "research" in text
    assert "openai" not in text
    assert "gemini" not in text
    assert "grok" not in text
