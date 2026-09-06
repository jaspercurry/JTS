# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Every wake or manual-session refusal, and every silent-turn diagnosis,
is a structured `event=` record — never source-text-only.

One row per refusal surface: the spend-cap and paused gates and the
turn-acquire catch-all in `_arbitrate_acquire_drain` (the wake path), the
BUSY guard in `manual_session_start`, the hold-timeout/no-audio-sent/
input-ended diagnoses in `_end_turn_inner`, and the NN-6 research
confirmation-window cancel timeout in `_handle_wake_frame` (which must
also cue — a dropped wake with no audible response is a non-negotiable
violation, not just a missing log line).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import numpy as np
import pytest

from jasper.research import DONE, ResearchJob
from jasper.voice_daemon import INTERNAL_ERROR_CUE_SLUG, State, WakeLoop

from tests._live_turn_fake import FakeLiveTurn
from tests._log_events import event_fields

_Trigger = Callable[[pytest.MonkeyPatch], Awaitable[list[str]]]


def _cue_recorder() -> tuple[list[str], Callable[[str], Awaitable[bool]]]:
    played: list[str] = []

    async def _rec(slug: str) -> bool:
        played.append(slug)
        return True

    return played, _rec


async def _win(**_kwargs) -> str:
    return "WIN"


async def _noop(*_args, **_kwargs) -> None:
    return None


async def _never_recovers(_timeout: float) -> bool:
    return False


async def _trigger_spend_cap(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(a) The spend cap is reached: refused before any turn opens."""
    wl = WakeLoop.for_tests()
    played, rec = _cue_recorder()
    wl._peer_arbitrate = _win
    wl._play_cue = rec
    try:
        await wl._arbitrate_acquire_drain(
            score=0.9, rms_dbfs=-30.0, spend_allowed=False,
            conn_paused=False, can_serve=False,
        )
    finally:
        await wl._cancel_fire_and_forget_tasks()
    return played


async def _trigger_connection_paused(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(b) The live connection is still paused after the bounded wait."""
    wl = WakeLoop.for_tests()
    played, rec = _cue_recorder()
    wl._peer_arbitrate = _win
    wl._play_cue = rec
    wl._await_connection = _never_recovers
    try:
        await wl._arbitrate_acquire_drain(
            score=0.9, rms_dbfs=-30.0, spend_allowed=True,
            conn_paused=True, can_serve=False,
        )
    finally:
        await wl._cancel_fire_and_forget_tasks()
    return played


async def _trigger_acquire_error(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(c) An unexpected local error during turn-open (not a connection
    problem — see `test_turn_open_failure_cue_is_honest_about_cause`)."""
    wl = WakeLoop.for_tests()
    played, rec = _cue_recorder()

    async def _begin_boom(**_kwargs) -> None:
        raise RuntimeError("attempt to write a readonly database")

    wl._peer_arbitrate = _win
    wl._prepare_assistant_loudness_context = _noop
    wl._play_listening_chirp = _noop
    wl._begin_turn_inner = _begin_boom
    wl._play_cue = rec
    try:
        await wl._arbitrate_acquire_drain(
            score=0.9, rms_dbfs=-30.0, spend_allowed=True,
            conn_paused=False, can_serve=True,
        )
    finally:
        await wl._cancel_fire_and_forget_tasks()
    return played


async def _trigger_manual_busy(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(d) manual_session_start while a session is already open."""
    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    result = await wl.manual_session_start()
    assert result == "BUSY"
    return []


def _prepare_teardown(
    wl: WakeLoop,
    *,
    bytes_sent: int,
    chunks_received: int,
    input_ended: bool,
    manual: bool,
    user_speech: bool = False,
) -> FakeLiveTurn:
    """The `_end_turn_inner` surface a real teardown touches, per
    `tests/test_voice_daemon_push_to_talk_endpointer.py::_torn_down_mid_hold`."""
    wl._cfg.active_voice_model = "test-model"
    wl._state = State.SESSION
    turn = FakeLiveTurn(bytes_sent=bytes_sent, chunks_received=chunks_received)
    wl._turn = turn
    wl._bg_tasks = set()
    wl._wake_event_store = None
    wl._session_id = "sess-refusals"
    wl._input_ended = input_ended
    wl._user_speech_seen = user_speech
    wl._manual_endpoint_this_turn = manual
    return turn


async def _trigger_hold_timeout(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(e) A held push-to-talk button: the idle watchdog reaped the turn
    before the model was ever asked to answer."""
    wl = WakeLoop.for_tests()
    _prepare_teardown(
        wl, bytes_sent=4096, chunks_received=0,
        input_ended=False, manual=True,
    )
    await wl._end_turn_inner("test")
    assert wl._state is State.WAKE
    return []


async def _trigger_no_audio_sent(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(e) A turn that opened and closed with zero bytes ever sent — the
    idle watchdog reaping a wake that fired on noise."""
    wl = WakeLoop.for_tests()
    _prepare_teardown(
        wl, bytes_sent=0, chunks_received=0,
        input_ended=False, manual=False,
    )
    await wl._end_turn_inner("test")
    assert wl._state is State.WAKE
    return []


async def _trigger_input_ended_reason(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(e) The pre-existing `input_ended` diagnosis is unchanged by the two
    new sibling branches above."""
    wl = WakeLoop.for_tests()
    _prepare_teardown(
        wl, bytes_sent=4096, chunks_received=0,
        input_ended=True, manual=False, user_speech=True,
    )
    await wl._end_turn_inner("test")
    assert wl._state is State.WAKE
    return []


async def _trigger_research_cancel_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """(f) NN-6: the research confirmation window's cancel-on-wake race
    itself times out. The wake is dropped either way — it must still cue."""
    import jasper.voice_daemon as voice_daemon_module

    monkeypatch.setattr(
        voice_daemon_module,
        "RESEARCH_CONFIRMATION_OPEN_CANCEL_TIMEOUT_SEC",
        0.01,
    )
    wl = WakeLoop.for_tests()
    wl._state = State.WAKE
    job = ResearchJob(
        id="job-cancel-timeout",
        query="q",
        status=DONE,
        result="r",
        error=None,
        created_at=time.time(),
        finished_at=time.time(),
        announced=False,
        read=False,
    )
    wl._research_window_active = True
    wl._research_window_job = job
    wl._research_window_decided = False
    wl._research_window_cancelled_by_wake = False
    # Never set: the opener never observes the cancellation, forcing the
    # `asyncio.wait_for` above to time out instead of resolving.
    wl._research_window_opening_done = asyncio.Event()
    wl._legs["on"].detector.score_frame = lambda _frame: 0.95
    played, rec = _cue_recorder()
    wl._play_cue = rec
    await wl._handle_wake_frame(np.zeros(1280, dtype=np.int16), leg="on")
    return played


@pytest.mark.parametrize(
    "trigger, expected_event, expected_fields",
    [
        pytest.param(
            _trigger_spend_cap, "wake.refused",
            {"reason": "spend_cap_reached"},
            id="spend_cap_reached",
        ),
        pytest.param(
            _trigger_connection_paused, "wake.refused",
            {"reason": "connection_paused"},
            id="connection_paused",
        ),
        pytest.param(
            _trigger_acquire_error, "wake.refused",
            {"reason": "acquire_error", "exc_type": "RuntimeError"},
            id="acquire_error",
        ),
        pytest.param(
            _trigger_manual_busy, "session.manual_refused",
            {"reason": "busy"},
            id="manual_busy",
        ),
        pytest.param(
            _trigger_hold_timeout, "turn.silent_response",
            {
                "provider": "test", "model": "test-model",
                "reason": "hold_timeout", "bytes_sent": "4096",
                "chunks_received": "0", "turn_lost": "false",
                "idle_timeout_sec": "10.0", "endpointer": "push_to_talk",
            },
            id="hold_timeout",
        ),
        pytest.param(
            _trigger_no_audio_sent, "turn.silent_response",
            {
                "provider": "test", "model": "test-model",
                "reason": "no_audio_sent", "bytes_sent": "0",
                "chunks_received": "0", "turn_lost": "false",
                "endpointer": "silero_aec",
            },
            id="no_audio_sent",
        ),
        pytest.param(
            _trigger_input_ended_reason, "turn.silent_response",
            {
                "provider": "test", "model": "test-model",
                "reason": "test", "bytes_sent": "4096",
                "chunks_received": "0", "turn_lost": "false",
                "count": "1", "endpointer": "silero_aec",
            },
            id="input_ended_reason",
        ),
        pytest.param(
            _trigger_research_cancel_timeout,
            "research.confirmation_window_cancel_timeout",
            {"job_id": "job-cancel-timeout"},
            id="research_confirmation_window_cancel_timeout",
        ),
    ],
)
async def test_refusal_is_a_structured_event(
    trigger: _Trigger,
    expected_event: str,
    expected_fields: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        played = await trigger(monkeypatch)

    assert event_fields(caplog, expected_event) == expected_fields

    if expected_event == "research.confirmation_window_cancel_timeout":
        # NN-6: a dropped wake must still be audible.
        assert played == [INTERNAL_ERROR_CUE_SLUG]
