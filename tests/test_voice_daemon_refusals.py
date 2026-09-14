# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Every wake or manual-session refusal, and every silent-turn diagnosis,
is a structured `event=` record — never source-text-only.

One row per refusal surface: the spend-cap and paused gates in
`_arbitrate_acquire_drain` (the wake path, including a connection still in
`IDLE_INIT`), the BUSY guard in `manual_session_start`, the hold-timeout/
recording-timeout/no-audio-sent/input-ended diagnoses in `_end_turn_inner`
— and the reasons the household or the daemon chose, which are journalled
but never spoken about.

The turn-acquire catch-all's `wake.refused` is pinned on the driver it
shares with `test_voice_daemon_defects.py::
test_turn_open_failure_cue_is_honest_about_cause`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest

from jasper.voice._base import BaseLiveConnection
from jasper.voice._supervisor import CANT_CONNECT_CUE_SLUG
from jasper.voice.turn_lifecycle import State
from jasper.voice_daemon import WakeLoop

from tests._live_turn_fake import FakeLiveTurn
from tests._log_events import event_field_maps
from tests._wake_loop import wake_loop_for_tests

_Trigger = Callable[[pytest.MonkeyPatch], Awaitable[list[str]]]


def _cue_recorder() -> tuple[list[str], Callable[[str], Awaitable[bool]]]:
    played: list[str] = []

    async def _rec(slug: str) -> bool:
        played.append(slug)
        return True

    return played, _rec


class _OrderedDucker:
    """Duck/restore on a shared timeline, so a restore landing inside the
    cue's play window shows up as ordering rather than as a call count."""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.is_ducked = False

    async def duck(self) -> None:
        self.is_ducked = True
        self.timeline.append("duck")

    async def restore(self) -> None:
        self.is_ducked = False
        self.timeline.append("restore")


def _record_output_writes(wl: WakeLoop, timeline: list[str]) -> None:
    """TTS writes and drain waits on the same timeline as the duck, so an
    output write landing inside the cue's play window shows up as ordering
    rather than as a call count."""

    async def _write_segment(*_args, **kwargs) -> bool:
        timeline.append(f"write_{kwargs.get('segment_kind') or 'segment'}")
        return True

    async def _wait_drained() -> None:
        timeline.append("drain_wait")

    wl._tts.write_segment = _write_segment
    wl._tts.wait_drained = _wait_drained


class _ParkedPeeringNotify:
    """`PeeringClient.session_ended` as a real teardown finds it: a write to
    the peering daemon that can park on its socket. BOTH teardown paths call
    it after they have read who owns output and before they act on the
    answer — the END_SEGMENT, the chirp, the drain wait, the duck restore,
    the gate release — so a surrender landing inside it is the window a
    single ownership answer read once at the top would miss."""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.parked = asyncio.Event()
        self.resume = asyncio.Event()

    async def __call__(self, _reason: str) -> None:
        self._timeline.append("peering_notify")
        self.parked.set()
        await asyncio.wait_for(self.resume.wait(), timeout=5.0)
        self._timeline.append("peering_resumed")


async def _win(**_kwargs) -> str:
    return "WIN"


async def _never_recovers(_timeout: float) -> bool:
    return False


async def _trigger_spend_cap(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(a) The spend cap is reached: refused before any turn opens."""
    wl = wake_loop_for_tests()
    played, rec = _cue_recorder()
    wl._peering.arbitrate = _win
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
    wl = wake_loop_for_tests()
    played, rec = _cue_recorder()
    wl._peering.arbitrate = _win
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

    wl = wake_loop_for_tests(connection=connection)
    played, rec = _cue_recorder()
    wl._peering.arbitrate = _win
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
    wl = wake_loop_for_tests()
    wl._turns.state = State.SESSION
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
    wl._turns.state = State.SESSION
    turn = FakeLiveTurn(bytes_sent=bytes_sent, chunks_received=chunks_received)
    wl._turns.turn = turn
    wl._turns.bg_tasks = set()
    wl._wake_telemetry.store = None
    wl._turns.session_id = "sess-refusals"
    wl._turns.input_ended = input_ended
    wl._turns.user_speech_seen = user_speech
    wl._turns.manual_endpoint_this_turn = manual
    return turn


async def _trigger_hold_timeout(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(e) A held push-to-talk button: the idle watchdog reaped the turn
    before the model was ever asked to answer."""
    wl = wake_loop_for_tests()
    _prepare_teardown(
        wl, bytes_sent=4096, chunks_received=0,
        input_ended=False, manual=True,
    )
    await wl._turns._end_turn_inner("test")
    assert wl._turns.state is State.WAKE
    return []


async def _trigger_no_audio_sent(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(e) A turn that opened and closed with zero bytes ever sent — the
    idle watchdog reaping a wake that fired on noise."""
    wl = wake_loop_for_tests()
    _prepare_teardown(
        wl, bytes_sent=0, chunks_received=0,
        input_ended=False, manual=False,
    )
    await wl._turns._end_turn_inner("test")
    assert wl._turns.state is State.WAKE
    return []


async def _trigger_no_audio_sent_suppressed(
    _monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """(e) The same zero-byte teardown under an end the household or the
    daemon chose. `mic_muted` is in `NO_ANSWER_CUE_SUPPRESSED_REASONS`, so
    it names itself in the record and is neither counted nor spoken about —
    the shape its `input_ended` sibling already emits."""
    wl = wake_loop_for_tests()
    _prepare_teardown(
        wl, bytes_sent=0, chunks_received=0,
        input_ended=False, manual=False,
    )
    await wl._turns._end_turn_inner("mic_muted")
    assert wl._turns.state is State.WAKE
    return []


async def _trigger_recording_timeout(
    _monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """(e) A wake turn whose silence detector never tripped: the idle
    watchdog ended it before the wake loop asked for a response."""
    wl = wake_loop_for_tests()
    _prepare_teardown(
        wl, bytes_sent=4096, chunks_received=0,
        input_ended=False, manual=False,
    )
    await wl._turns._end_turn_inner("test")
    assert wl._turns.state is State.WAKE
    return []


async def _trigger_input_ended_reason(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(e) The pre-existing `input_ended` diagnosis is unchanged by the two
    new sibling branches above."""
    wl = wake_loop_for_tests()
    _prepare_teardown(
        wl, bytes_sent=4096, chunks_received=0,
        input_ended=True, manual=False, user_speech=True,
    )
    await wl._turns._end_turn_inner("test")
    assert wl._turns.state is State.WAKE
    return []


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
            [{
                "provider": "test", "model": "test-model",
                "reason": "no_audio_sent", "bytes_sent": "0",
                "chunks_received": "0", "turn_lost": "false",
                "endpointer": "silero_aec", "suppressed": "mic_muted",
            }],
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
        await trigger(monkeypatch)

    assert event_field_maps(caplog, expected_event) == expected_records


async def _surrender_inside_end_turn_inner() -> tuple[WakeLoop, list[str]]:
    """`_end_turn_inner` losing output ownership after it has begun and
    before it has written anything: a cue's episode handover landing inside
    the peering notify."""
    timeline: list[str] = []
    wl = wake_loop_for_tests(ducker=_OrderedDucker(timeline))
    _record_output_writes(wl, timeline)

    async def _end_segment() -> None:
        timeline.append("end_segment")

    wl._tts.end_segment = _end_segment
    notify = _ParkedPeeringNotify(timeline)
    wl._peering.session_ended = notify

    _prepare_teardown(
        wl, bytes_sent=4096, chunks_received=1,
        input_ended=True, manual=False, user_speech=True,
    )
    await wl._turns.begin_output_episode()
    await wl._ducker.duck()
    opener_episode = wl._turns.output_episode
    assert opener_episode is not None

    teardown = asyncio.create_task(wl._turns._end_turn_inner("test"))
    try:
        await asyncio.wait_for(notify.parked.wait(), timeout=5.0)
        await wl._output_gate.end(opener_episode)
        cue_episode = await wl._output_gate.begin_if_idle("admin")
        assert cue_episode is not None
        timeline.append("surrender")
        notify.resume.set()
        await asyncio.wait_for(teardown, timeout=5.0)
    finally:
        notify.resume.set()
        teardown.cancel()
    return wl, timeline


async def test_a_surrender_inside_the_teardown_stops_every_later_write() -> None:
    """NN-6, inside `_end_turn_inner`: ownership is re-asked AT each output
    action, so a surrender landing in an await before them stops every later
    one. The writes this turn owned — its END_SEGMENT and its hang-up chirp —
    happened while it still held the gate; one answer read at the top would
    instead let the ones after the surrender through, down the shared TTS
    stream and into the segment of whatever took the gate."""
    wl, timeline = await _surrender_inside_end_turn_inner()

    # Nothing after the surrender: no drain wait, no duck restore.
    assert timeline == [
        "duck", "end_segment", "peering_notify", "write_chirp", "surrender",
        "peering_resumed",
    ]
    # The cue that took the gate still owns it — the teardown released
    # nothing — while the opener still finished the turn it was holding.
    assert wl._output_gate.active_kind == "admin"
    assert wl._turns.turn is None
    assert wl._turns.session_id is None
    assert wl._turns.output_episode is None
    assert wl._turns.state is State.WAKE
