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
but never spoken about — and the NN-6 research confirmation-window cancel
timeout in `_handle_wake_frame` (which must also cue — a dropped wake with
no audible response is a non-negotiable violation, not just a missing log
line).

The turn-acquire catch-all's `wake.refused` is pinned on the driver it
shares with `test_voice_daemon_defects.py::
test_turn_open_failure_cue_is_honest_about_cause`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import pytest

from jasper.research import DONE, ResearchJob
from jasper.voice._base import BaseLiveConnection
from jasper.voice._supervisor import CANT_CONNECT_CUE_SLUG
from jasper.voice_daemon import INTERNAL_ERROR_CUE_SLUG, State, WakeLoop

from tests._live_turn_fake import FakeLiveTurn, silent_frame
from tests._log_events import event_field_maps

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


class _BlockingReleaseTurn(FakeLiveTurn):
    """`LiveTurn.release()` as a real teardown finds it: an await that can
    park. BOTH teardown paths call it after they have read who owns output
    and before they act on the answer, so the surrender lands inside it and
    a single answer read up front is stale when it is used."""

    def __init__(
        self,
        timeline: list[str],
        release_started: asyncio.Event,
        allow_release: asyncio.Event,
    ) -> None:
        super().__init__()
        self._timeline = timeline
        self._release_started = release_started
        self._allow_release = allow_release

    async def release(self) -> None:
        await super().release()
        self._timeline.append("release_start")
        self._release_started.set()
        await asyncio.wait_for(self._allow_release.wait(), timeout=5.0)
        self._timeline.append("release_end")


class _CueDuringSurrender:
    """Stand-in cue manager so the REAL `_play_cue` path runs end to end,
    with the surrendered opener resuming either inside the cue's play window
    or after it: what that teardown does to the cue's episode, its duck and
    the TTS stream is observable either way."""

    def __init__(
        self,
        wl: WakeLoop,
        timeline: list[str],
        allow_release: asyncio.Event,
        opener_done: asyncio.Event,
        *,
        resume_opener_during_play: bool,
    ) -> None:
        self._wl = wl
        self._timeline = timeline
        self._allow_release = allow_release
        self._opener_done = opener_done
        self._resume_opener_during_play = resume_opener_during_play
        self.played: list[str] = []
        self.kind_at_play: str | None = None
        self.turn_episode_field_at_play: bool | None = None
        self.active_at_play_end: bool | None = None
        self.kind_at_play_end: str | None = None

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        self.kind_at_play = self._wl._output_gate.active_kind
        self.turn_episode_field_at_play = (
            self._wl._turn_output_episode is not None
        )
        self._timeline.append("cue_play_start")
        if self._resume_opener_during_play:
            self._allow_release.set()
            await asyncio.wait_for(self._opener_done.wait(), timeout=5.0)
        self.active_at_play_end = self._wl._output_gate.is_active
        self.kind_at_play_end = self._wl._output_gate.active_kind
        self._timeline.append("cue_play_end")
        return True


async def _failed_begin_opener(
    wl: WakeLoop,
    turn: FakeLiveTurn,
    opener_done: asyncio.Event,
) -> None:
    """`_open_confirmation_window`'s opener dying mid-begin, through the
    REAL `_begin_turn`: its `finally` runs `_cleanup_after_failed_begin`
    inside the `_await_output_cleanup_owned` task wrapper, which is where
    the window between reading ownership and acting on it lives."""

    async def _inner(**_kwargs) -> None:
        await wl._begin_turn_output_episode()
        await wl._ducker.duck()
        wl._session_id = "sess-stalled-opener"
        wl._turn = turn
        raise RuntimeError("confirmation turn died mid-begin")

    wl._begin_turn_inner = _inner
    try:
        with pytest.raises(RuntimeError):
            await wl._begin_turn()
    finally:
        opener_done.set()


async def _success_arm_opener(
    wl: WakeLoop,
    turn: FakeLiveTurn,
    opener_done: asyncio.Event,
) -> None:
    """The same opener on its success arm: the turn opened, so the surrender
    lands on a loop that tears down through `_end_turn_inner` — whose "done
    listening" chirp and drain wait are output writes like any other."""
    await wl._begin_turn_output_episode()
    await wl._ducker.duck()
    wl._cfg.active_voice_model = "test-model"
    wl._session_id = "sess-stalled-opener"
    wl._turn = turn
    wl._bg_tasks = set()
    wl._wake_event_store = None
    wl._state = State.SESSION
    try:
        await wl._end_turn_inner("test")
    finally:
        opener_done.set()


_OPENERS = {
    "failed_begin": _failed_begin_opener,
    "success_arm": _success_arm_opener,
}


async def _drive_cancel_timeout(
    monkeypatch: pytest.MonkeyPatch,
    *,
    teardown: str,
    resume_during_cue: bool,
    cues_configured: bool = True,
) -> tuple[WakeLoop, list[str], _CueDuringSurrender | None]:
    """The NN-6 collision on real code: a confirmation-window opener holding
    the turn output episode, the wake that cancels it timing out, and the
    cue that must still be heard taking the gate from under it."""
    import jasper.voice_daemon as voice_daemon_module

    monkeypatch.setattr(
        voice_daemon_module,
        "RESEARCH_CONFIRMATION_OPEN_CANCEL_TIMEOUT_SEC",
        0.01,
    )
    wl = WakeLoop.for_tests()
    wl._state = State.WAKE
    timeline: list[str] = []
    wl._ducker = _OrderedDucker(timeline)
    _record_output_writes(wl, timeline)
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    opener_done = asyncio.Event()
    turn = _BlockingReleaseTurn(timeline, release_started, allow_release)
    cues = (
        _CueDuringSurrender(
            wl,
            timeline,
            allow_release,
            opener_done,
            resume_opener_during_play=resume_during_cue,
        )
        if cues_configured else None
    )
    wl._cues = cues
    opener = asyncio.create_task(_OPENERS[teardown](wl, turn, opener_done))
    try:
        # Parked inside `turn.release()`: past the point where each teardown
        # path reads who owns output, before the point where it acts on it.
        await asyncio.wait_for(release_started.wait(), timeout=5.0)
        # Armed only now: `_end_turn_inner` reads the confirmation window at
        # its top, and the window's own dismissal bookkeeping is another
        # test's subject.
        wl._research_window_active = True
        wl._research_window_job = ResearchJob(
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
        wl._research_window_decided = False
        wl._research_window_cancelled_by_wake = False
        # Never set: the opener never observes the cancellation, forcing the
        # `asyncio.wait_for` in `_handle_wake_frame` to time out.
        wl._research_window_opening_done = asyncio.Event()
        wl._legs["on"].detector.score_frame = lambda _frame: 0.95
        await wl._handle_wake_frame(silent_frame(), leg="on")
        allow_release.set()
        await asyncio.wait_for(opener, timeout=5.0)
    finally:
        opener.cancel()
    return wl, timeline, cues


async def _win(**_kwargs) -> str:
    return "WIN"


async def _never_recovers(_timeout: float) -> bool:
    return False


async def _trigger_spend_cap(_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """(a) The spend cap is reached: refused before any turn opens."""
    wl = WakeLoop.for_tests()
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
    wl = WakeLoop.for_tests()
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

    wl = WakeLoop.for_tests()
    played, rec = _cue_recorder()
    wl._connection = connection
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
    daemon chose. `mic_muted` is in `NO_ANSWER_CUE_SUPPRESSED_REASONS`, so
    it names itself in the record and is neither counted nor spoken about —
    the shape its `input_ended` sibling already emits."""
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
    through the REAL `_play_cue`, against a REAL concurrent opener that
    holds the turn episode `_play_cue`'s own admission cannot preempt and
    that resumes into its own teardown while the cue is still sounding."""
    _wl, _timeline, cues = await _drive_cancel_timeout(
        monkeypatch, teardown="failed_begin", resume_during_cue=True,
    )
    assert cues is not None
    # Surrendering output leaves the opener's "teardown still owed" sentinel
    # standing, so its own guarded cleanup call sites still fire.
    assert cues.turn_episode_field_at_play is True
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


class _ParkedPeeringNotify:
    """`PeeringClient.session_ended` as a real teardown finds it: a write
    to the peering daemon that can park on its socket. It sits between the
    teardown's episode capture and every output action guarded on
    ownership — the END_SEGMENT, the chirp, the drain wait, the duck
    restore, the gate release — so a surrender landing inside it is the
    window a single ownership answer read once at the top would miss."""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.parked = asyncio.Event()
        self.resume = asyncio.Event()

    async def __call__(self, _reason: str) -> None:
        self._timeline.append("peering_notify")
        self.parked.set()
        await asyncio.wait_for(self.resume.wait(), timeout=5.0)


async def _surrender_inside_end_turn_inner() -> tuple[WakeLoop, list[str]]:
    """`_end_turn_inner` losing output ownership after it has begun and
    before it has written anything: the research cancel timeout's handover,
    landing inside the peering notify."""
    wl = WakeLoop.for_tests()
    timeline: list[str] = []
    wl._ducker = _OrderedDucker(timeline)
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
    await wl._begin_turn_output_episode()
    await wl._ducker.duck()
    opener_episode = wl._turn_output_episode
    assert opener_episode is not None

    teardown = asyncio.create_task(wl._end_turn_inner("test"))
    try:
        await asyncio.wait_for(notify.parked.wait(), timeout=5.0)
        cue_episode = await wl._output_gate.hand_over_if_current(
            opener_episode, "admin",
        )
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
    action, so a surrender landing in an await before them stops all of
    them — the END_SEGMENT included, which goes down the shared TTS stream
    and would close the segment of whatever took the gate and bill its
    loudness to this turn. One answer read at the top lets every one
    through."""
    wl, timeline = await _surrender_inside_end_turn_inner()

    # Nothing after the surrender: no end_segment, no chirp write, no drain
    # wait, no duck restore.
    assert timeline == ["duck", "peering_notify", "surrender"]
    # The cue that took the gate still owns it — the teardown released
    # nothing — while the opener still finished the turn it was holding.
    assert wl._output_gate.active_kind == "admin"
    assert wl._turn is None
    assert wl._session_id is None
    assert wl._turn_output_episode is None
    assert wl._state is State.WAKE


@pytest.mark.parametrize("teardown", ["failed_begin", "success_arm"])
@pytest.mark.parametrize(
    "resume_during_cue",
    [True, False],
    ids=["opener_resumes_during_cue", "cue_finishes_first"],
)
async def test_a_surrendered_opener_neither_writes_nor_unducks(
    teardown: str,
    resume_during_cue: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NN-6, both teardown paths and both orderings: once the cue has taken
    the output gate, the opener that lost it must not touch output — no
    chirp mixed into the cue, no drain wait held open for someone else's
    audio, no duck restore, no gate release. Ownership is therefore asked
    again AT each of those actions, not once before the awaits that
    separate them (`turn.release()` is one of those awaits, and the
    surrender lands inside it)."""
    wl, timeline, cues = await _drive_cancel_timeout(
        monkeypatch, teardown=teardown, resume_during_cue=resume_during_cue,
    )
    assert cues is not None
    assert cues.played == [INTERNAL_ERROR_CUE_SLUG]

    play_start = timeline.index("cue_play_start")
    play_end = timeline.index("cue_play_end")
    during_cue = timeline[play_start:play_end]
    assert "restore" not in during_cue
    assert "drain_wait" not in during_cue
    # A surrendered opener writes nothing at all, in either ordering.
    assert [entry for entry in timeline if entry.startswith("write_")] == []
    # The only duck handback in the run is the cue's own, after its audio.
    assert timeline.count("restore") == 1
    assert timeline.index("restore") > play_end

    # The cue owned output for its whole window, and the gate goes idle only
    # when the cue's own drain releases it.
    assert cues.kind_at_play == "admin"
    assert cues.kind_at_play_end == "admin"
    assert cues.active_at_play_end is True
    assert wl._output_gate.is_active is False
    # The opener still finished the turn it was holding.
    assert wl._turn is None
    assert wl._session_id is None
    assert wl._turn_output_episode is None
    assert wl._state is State.WAKE


@pytest.mark.parametrize("teardown", ["failed_begin", "success_arm"])
async def test_the_surrendered_duck_comes_back_once_with_no_cue_manager(
    teardown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-cue arm. Nothing ducks for a cue that cannot play, so the
    timeout path hands back the duck the surrendered opener took and can no
    longer restore — exactly once, and after the surrender. The episode it
    handed `_play_cue` is released on that exit too: leaked, the gate stays
    taken for the rest of the daemon run and every later cue is skipped."""
    wl, timeline, cues = await _drive_cancel_timeout(
        monkeypatch,
        teardown=teardown,
        resume_during_cue=False,
        cues_configured=False,
    )
    assert cues is None
    # One restore, landing while the opener is still parked in `release()`:
    # the timeout path's, after the surrender — not the opener's.
    assert timeline.count("restore") == 1
    assert (
        timeline.index("release_start")
        < timeline.index("restore")
        < timeline.index("release_end")
    )
    assert [entry for entry in timeline if entry.startswith("write_")] == []
    assert wl._output_gate.is_active is False
    assert wl._turn_output_episode is None
    assert wl._state is State.WAKE
