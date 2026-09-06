# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Every wake or manual-session refusal, and every silent-turn diagnosis,
is a structured `event=` record — never source-text-only.

One row per refusal surface: the spend-cap and paused gates in
`_arbitrate_acquire_drain` (the wake path, including a connection still in
`IDLE_INIT`), the BUSY guard in `manual_session_start`, the hold-timeout/
recording-timeout/no-audio-sent/input-ended diagnoses in `_end_turn_inner`
— and the reasons the household or the daemon chose, which stay silent —
and the NN-6 research confirmation-window cancel timeout in
`_handle_wake_frame` (which must also cue — a dropped wake with no audible
response is a non-negotiable violation, not just a missing log line).

The turn-acquire catch-all's `wake.refused` is pinned on the driver it
shares with `test_voice_daemon_defects.py::
test_turn_open_failure_cue_is_honest_about_cause`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import numpy as np
import pytest

from jasper.research import DONE, ResearchJob
from jasper.voice._base import BaseLiveConnection
from jasper.voice._supervisor import CANT_CONNECT_CUE_SLUG
from jasper.voice_daemon import INTERNAL_ERROR_CUE_SLUG, State, WakeLoop

from tests._live_turn_fake import FakeLiveTurn
from tests._log_events import event_field_maps

_Trigger = Callable[[pytest.MonkeyPatch], Awaitable[list[str]]]


def _cue_recorder() -> tuple[list[str], Callable[[str], Awaitable[bool]]]:
    played: list[str] = []

    async def _rec(slug: str) -> bool:
        played.append(slug)
        return True

    return played, _rec


class _SpyCues:
    """Stand-in cue manager so the REAL `_play_cue` path runs end to end."""

    def __init__(self) -> None:
        self.played: list[str] = []

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        return True


async def _win(**_kwargs) -> str:
    return "WIN"


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


async def _trigger_idle_init_connection(
    _monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """(b) A wake landing before the provider's first `start()`. The state
    is still `IDLE_INIT`, which `is_paused()` counts as "the first connect
    is still dialling", so the wake gets the honest still-connecting cue
    instead of falling through to a mislabelled `internal_error`."""
    connection = BaseLiveConnection(model="test-model", voice="test-voice")
    assert connection.is_paused() is True

    wl = WakeLoop.for_tests()
    played, rec = _cue_recorder()
    wl._connection = connection
    wl._peer_arbitrate = _win
    wl._play_cue = rec
    wl._await_connection = _never_recovers
    try:
        await wl._arbitrate_acquire_drain(
            score=0.9, rms_dbfs=-30.0, spend_allowed=True,
            conn_paused=connection.is_paused(), can_serve=False,
        )
    finally:
        await wl._cancel_fire_and_forget_tasks()
    assert played == [CANT_CONNECT_CUE_SLUG]
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


async def _trigger_no_audio_sent_suppressed(
    _monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """(e) The same zero-byte teardown under an end the household or the
    daemon chose. `mic_muted` is in `NO_ANSWER_CUE_SUPPRESSED_REASONS`, and
    a mute lands before the first forwarded frame routinely, so it must not
    be diagnosed at all."""
    wl = WakeLoop.for_tests()
    _prepare_teardown(
        wl, bytes_sent=0, chunks_received=0,
        input_ended=False, manual=False,
    )
    await wl._end_turn_inner("mic_muted")
    assert wl._state is State.WAKE
    return []


async def _trigger_recording_timeout(
    _monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """(e) A wake turn whose silence detector never tripped: the idle
    watchdog ended it before the wake loop asked for a response."""
    wl = WakeLoop.for_tests()
    _prepare_teardown(
        wl, bytes_sent=4096, chunks_received=0,
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
    itself times out. The wake is dropped either way — it must still cue,
    through the REAL `_play_cue`, while the window's opener still owns the
    output episode that `_play_cue`'s own admission cannot preempt."""
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
    cues = _SpyCues()
    wl._cues = cues
    # What the opener holds for the whole cancel wait, taken through the
    # real gate: an "admin" cue cannot be admitted against a live episode.
    await wl._begin_turn_output_episode()
    await wl._handle_wake_frame(np.zeros(1280, dtype=np.int16), leg="on")
    # The cue's own drain path released the handed-over episode.
    assert wl._output_gate.is_active is False
    return cues.played


@pytest.mark.parametrize(
    "trigger, expected_event, expected_records",
    [
        pytest.param(
            _trigger_spend_cap, "wake.refused",
            [{"reason": "spend_cap_reached"}],
            id="spend_cap_reached",
        ),
        pytest.param(
            _trigger_connection_paused, "wake.refused",
            [{"reason": "connection_paused"}],
            id="connection_paused",
        ),
        pytest.param(
            _trigger_idle_init_connection, "wake.refused",
            [{"reason": "connection_paused"}],
            id="idle_init_connection",
        ),
        pytest.param(
            _trigger_manual_busy, "session.manual_refused",
            [{"reason": "busy"}],
            id="manual_busy",
        ),
        pytest.param(
            _trigger_hold_timeout, "turn.silent_response",
            [{
                "provider": "test", "model": "test-model",
                "reason": "hold_timeout", "bytes_sent": "4096",
                "chunks_received": "0", "turn_lost": "false",
                "idle_timeout_sec": "10.0", "endpointer": "push_to_talk",
            }],
            id="hold_timeout",
        ),
        pytest.param(
            _trigger_recording_timeout, "turn.silent_response",
            [{
                "provider": "test", "model": "test-model",
                "reason": "recording_timeout", "bytes_sent": "4096",
                "chunks_received": "0", "turn_lost": "false",
                "endpointer": "silero_aec",
            }],
            id="recording_timeout",
        ),
        pytest.param(
            _trigger_no_audio_sent, "turn.silent_response",
            [{
                "provider": "test", "model": "test-model",
                "reason": "no_audio_sent", "bytes_sent": "0",
                "chunks_received": "0", "turn_lost": "false",
                "endpointer": "silero_aec",
            }],
            id="no_audio_sent",
        ),
        pytest.param(
            _trigger_no_audio_sent_suppressed, "turn.silent_response",
            [],
            id="no_audio_sent_suppressed",
        ),
        pytest.param(
            _trigger_input_ended_reason, "turn.silent_response",
            [{
                "provider": "test", "model": "test-model",
                "reason": "test", "bytes_sent": "4096",
                "chunks_received": "0", "turn_lost": "false",
                "count": "1", "endpointer": "silero_aec",
            }],
            id="input_ended_reason",
        ),
        pytest.param(
            _trigger_research_cancel_timeout,
            "research.confirmation_window_cancel_timeout",
            [{"job_id": "job-cancel-timeout"}],
            id="research_confirmation_window_cancel_timeout",
        ),
    ],
)
async def test_refusal_is_a_structured_event(
    trigger: _Trigger,
    expected_event: str,
    expected_records: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        played = await trigger(monkeypatch)

    assert event_field_maps(caplog, expected_event) == expected_records

    if expected_event == "research.confirmation_window_cancel_timeout":
        # NN-6: a dropped wake must still be audible.
        assert played == [INTERNAL_ERROR_CUE_SLUG]
