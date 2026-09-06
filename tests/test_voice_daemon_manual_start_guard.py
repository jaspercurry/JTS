# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""manual_session_start must honor the user-deliberate stop-listening
gates the wake path enforces.

The supported push-to-talk remote / POST /session/start entry opens a
paid LLM turn and ducks music via _begin_turn. The wake path refuses to do that when
the mic is muted or a room-correction measurement sweep is active
(_wake_late_cancelled). These tests pin that manual_session_start does
the same: it returns a refusal code, logs event=session.manual_refused,
and never plays a chirp, primes loudness context, or begins a turn (so
no duck) under either gate.
"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest

from jasper.voice._supervisor import CANT_CONNECT_CUE_SLUG
from jasper.voice_daemon import INTERNAL_ERROR_CUE_SLUG

from ._async_wait import wait_signalled
from ._log_events import event_fields


class _SpyCalls:
    """Records that a side-effecting coroutine was awaited."""

    def __init__(self) -> None:
        self.called = False
        self.args = ()
        self.kwargs = {}

    async def __call__(self, *args, **kwargs) -> None:
        self.called = True
        self.args = args
        self.kwargs = kwargs


class _SpyCues:
    """Stand-in cue manager so the REAL _play_cue path runs end to end."""

    def __init__(self) -> None:
        self.played: list[str] = []

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        return True


def _make_wake_loop():
    """A WakeLoop with only the attributes manual_session_start reads
    plus spies on the side effects we assert must NOT fire.
    """
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._state = State.WAKE
    wl._mic_muted = False
    wl._measurement_active = asyncio.Event()
    wl._fire_and_forget = set()
    # If a guard is skipped, these would be reached — make them visible.
    wl._spend_cap = types.SimpleNamespace(allowed=lambda: True)
    wl._connection = types.SimpleNamespace(
        is_paused=lambda: False, last_failure_detail=lambda: None,
    )
    wl._begin_turn = _SpyCalls()
    wl._prepare_assistant_loudness_context = _SpyCalls()
    wl._play_listening_chirp = _SpyCalls()
    wl._cleanup_after_failed_begin = _SpyCalls()
    return wl


def _assert_no_turn_no_duck(wl) -> None:
    # _begin_turn is what opens the LLM turn AND ducks music
    # (note_voice_session(True) + ducker). Loudness-prime and the
    # listening chirp are the other observable "we started" effects.
    assert wl._begin_turn.called is False
    assert wl._prepare_assistant_loudness_context.called is False
    assert wl._play_listening_chirp.called is False


async def _drain_refusal_cue(wl) -> None:
    """Refusal cues are spawned, not awaited, so the control-socket reply
    is not held behind ~6 s of audio. Assertions on them wait here."""
    await asyncio.gather(*tuple(wl._fire_and_forget))


async def test_manual_start_refused_when_mic_muted(caplog):
    wl = _make_wake_loop()
    wl._mic_muted = True

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.manual_session_start()

    assert result == "MUTED"
    _assert_no_turn_no_duck(wl)
    assert "event=session.manual_refused" in caplog.text
    assert "reason=mic_muted" in caplog.text


async def test_manual_start_refused_when_measurement_active(caplog):
    wl = _make_wake_loop()
    wl._measurement_active.set()

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.manual_session_start()

    assert result == "MEASURING"
    _assert_no_turn_no_duck(wl)
    assert "event=session.manual_refused" in caplog.text
    # Exact reason string shared with the wake path's late-cancel log,
    # so one query covers both refusal surfaces.
    assert "reason=measurement_active" in caplog.text


async def test_manual_start_at_the_spend_cap_cues_and_refuses(caplog):
    """A press at the daily cap answers audibly, like the wake path.

    The cap is not a state the household just asked for, so silence here
    is unexplainable (AGENTS.md's no-silent-deafness rule).
    """
    wl = _make_wake_loop()
    wl._cues = _SpyCues()
    wl._spend_cap = types.SimpleNamespace(allowed=lambda: False)

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.manual_session_start()

    assert result == "CAP"
    assert wl._begin_turn.called is False
    assert wl._play_listening_chirp.called is False
    await _drain_refusal_cue(wl)
    assert wl._cues.played == ["spend_cap_reached"]
    assert event_fields(caplog, "session.manual_refused") == {
        "reason": "spend_cap_reached",
    }


def _paused_connection(wl, *, paused_for_sec: float):
    """Wire a connection that reports paused for `paused_for_sec` of
    loop time, then reports connected. Counts the reconnect nudges."""
    loop = asyncio.get_event_loop()
    clears_at = loop.time() + paused_for_sec
    state = types.SimpleNamespace(nudges=0)

    def _nudge() -> bool:
        state.nudges += 1
        return True

    wl._connection = types.SimpleNamespace(
        is_paused=lambda: loop.time() < clears_at,
        last_failure_detail=lambda: None,
        wake_cue=lambda: "cant_connect",
        request_reconnect_now=_nudge,
    )
    return state


async def test_manual_start_refused_when_paused_asks_for_an_early_retry(
    monkeypatch, caplog,
):
    """A press during a real outage still refuses — with a cue, because a
    press that produces nothing is unexplainable — and shortens the
    supervisor's wait (issue #3855)."""
    from jasper import voice_daemon

    monkeypatch.setattr(voice_daemon, "PAUSED_CONNECTION_WAIT_SEC", 0.2)
    wl = _make_wake_loop()
    wl._play_cue = _SpyCalls()
    state = _paused_connection(wl, paused_for_sec=99.0)

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        assert await wl.manual_session_start() == "PAUSED"

    _assert_no_turn_no_duck(wl)
    assert state.nudges == 1
    await _drain_refusal_cue(wl)
    assert wl._play_cue.called is True
    assert "reason=connection_paused" in caplog.text


@pytest.mark.parametrize(
    ("connection_drops", "expected_slug"),
    [(True, CANT_CONNECT_CUE_SLUG), (False, INTERNAL_ERROR_CUE_SLUG)],
)
async def test_manual_start_failure_cues_the_cause(connection_drops, expected_slug):
    """A press whose turn dies still answers, and names the real cause.

    The idle context reset reopens inside `_begin_turn`, so a reopen that
    never lands raises past the paused gate above and reaches the failure
    handler. It must cue rather than return a silent ERROR (AGENTS.md's
    no-silent-deafness rule) — and an outage cue for a connection that is
    still up would be a lie, so that branch cues `internal_error`.
    """
    wl = _make_wake_loop()
    wl._cues = _SpyCues()
    paused = False

    async def _begin_turn_that_fails(**_kwargs) -> None:
        nonlocal paused
        paused = connection_drops
        raise RuntimeError("live connection: not connected after backoff window")

    wl._connection = types.SimpleNamespace(
        is_paused=lambda: paused,
        last_failure_detail=lambda: None,
        wake_cue=lambda: CANT_CONNECT_CUE_SLUG,
        request_reconnect_now=lambda: True,
    )
    wl._begin_turn = _begin_turn_that_fails

    assert await wl.manual_session_start() == "ERROR"
    await _drain_refusal_cue(wl)
    assert wl._cues.played == [expected_slug]


async def test_manual_refusal_cue_does_not_hold_up_the_reply():
    """The reply must not wait out the cue it just started.

    `manual_session_start` answers a control-socket START whose caller
    times out at 5 s (`control/uds.py`) while a cue runs ~6 s with duck
    and drain: awaiting one answered the button 503 and left the real
    result to be written to a closed socket.
    """
    wl = _make_wake_loop()
    wl._spend_cap = types.SimpleNamespace(allowed=lambda: False)
    played = asyncio.Event()

    class _SlowCues:
        async def play(self, slug: str) -> bool:
            await asyncio.sleep(0.05)
            played.set()
            return True

    wl._cues = _SlowCues()

    assert await wl.manual_session_start() == "CAP"
    assert not played.is_set()

    await _drain_refusal_cue(wl)
    assert played.is_set()


async def test_manual_start_waits_out_a_planned_rotation(monkeypatch):
    """A press landing inside a planned session rotation gets its turn
    once the fresh session is up, instead of a dead PAUSED."""
    from jasper import voice_daemon

    monkeypatch.setattr(voice_daemon, "PAUSED_CONNECTION_WAIT_SEC", 1.2)
    wl = _make_wake_loop()
    wl._play_cue = _SpyCalls()
    _paused_connection(wl, paused_for_sec=0.3)

    assert await wl.manual_session_start() == "OK"

    assert wl._begin_turn.called is True
    assert wl._play_cue.called is False


async def test_manual_start_does_not_repeat_begin_owned_cleanup():
    """A failed begin that released its episode is not cleaned twice."""

    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    cleanup_calls = 0
    cleanup_method = type(wl)._cleanup_after_failed_begin

    async def counted_cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        await cleanup_method(wl)

    async def noop_prepare() -> None:
        return None

    async def failed_inner(**_kwargs) -> None:
        raise RuntimeError("begin failed after owned cleanup")

    def discard_task(coro, **_kwargs):
        coro.close()
        return None

    wl._cleanup_after_failed_begin = counted_cleanup
    wl._prepare_assistant_loudness_context = noop_prepare
    wl._begin_turn_inner = failed_inner
    wl._create_fire_and_forget_task = discard_task

    assert await wl.manual_session_start() == "ERROR"
    assert cleanup_calls == 1
    assert wl._turn_output_episode is None
    assert not wl._output_gate.is_active


async def test_manual_start_cleans_prefix_failure_before_turn_inner():
    """An ordinary loudness-prefix failure is centrally cleaned exactly once."""

    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    cleanup_calls = 0
    inner = _SpyCalls()
    cleanup_method = type(wl)._cleanup_after_failed_begin

    async def counted_cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        await cleanup_method(wl)

    async def fail_prepare() -> None:
        raise RuntimeError("pre-begin loudness preparation failed")

    wl._cleanup_after_failed_begin = counted_cleanup
    wl._prepare_assistant_loudness_context = fail_prepare
    wl._begin_turn_inner = inner

    assert await wl.manual_session_start() == "ERROR"
    assert inner.called is False
    assert cleanup_calls == 1
    assert wl._turn_output_episode is None
    assert not wl._output_gate.is_active


async def test_measurement_pause_and_resume_transfer_volume_ownership():
    """The voice-side gate and volume-owner lease change together."""
    wl = _make_wake_loop()
    ownership: list[bool] = []

    async def note_measurement_active(active):
        ownership.append(bool(active))

    wl._volume_coordinator = types.SimpleNamespace(
        note_measurement_active=note_measurement_active
    )

    assert (await wl.measurement_hold.pause_response())["result"] == "ok"
    assert wl._measurement_active.is_set()
    assert wl._output_gate.admission_paused
    assert ownership == [True]

    assert await wl.measurement_hold.resume() == "ok"
    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused
    assert ownership == [True, False]
    assert wl.session_status()["measurement_active"] is False


async def test_measurement_auto_clear_releases_reconcile_guard(monkeypatch):
    """A crashed correction owner cannot strand the guard active."""
    wl = _make_wake_loop()
    ownership: list[bool] = []

    async def note_measurement_active(active):
        ownership.append(bool(active))

    wl._volume_coordinator = types.SimpleNamespace(
        note_measurement_active=note_measurement_active
    )
    timer_started = asyncio.Event()
    release_timer = asyncio.Event()

    from jasper.voice.measurement_hold import MEASUREMENT_AUTOCLEAR_SEC

    async def fake_sleep(seconds):
        assert seconds == MEASUREMENT_AUTOCLEAR_SEC
        timer_started.set()
        await release_timer.wait()

    monkeypatch.setattr(
        "jasper.voice.measurement_hold._measurement_safety_sleep",
        fake_sleep,
    )

    assert (await wl.measurement_hold.pause_response())["result"] == "ok"
    await wait_signalled(
        timer_started,
        "auto-clear timer started",
        producer=wl.measurement_hold._safety_task,
    )
    release_timer.set()
    await wl.measurement_hold._safety_task

    assert ownership == [True, False]
    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused


async def test_manual_start_begins_turn_when_unguarded():
    # Control: with both stop-listening gates clear (and spend/connection
    # allowed), manual_session_start proceeds normally — proving the new
    # guard is scoped to the muted/measuring conditions only.
    wl = _make_wake_loop()

    result = await wl.manual_session_start()

    assert result == "OK"
    assert wl._begin_turn.called is True
    assert wl._begin_turn.kwargs == {"listening_feedback": True}


async def test_manual_start_unknown_source_refused_before_side_effects(caplog):
    wl = _make_wake_loop()

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.manual_session_start("missing_remote")

    assert result == "UNKNOWN_SOURCE"
    _assert_no_turn_no_duck(wl)
    assert "event=session.manual_refused" in caplog.text
    assert "reason=unknown_source" in caplog.text
    assert "source=missing_remote" in caplog.text


async def test_manual_start_source_uses_source_audio_without_primary_preroll():
    wl = _make_wake_loop()
    wl._manual_mics = {"wiim_remote_2": object()}

    result = await wl.manual_session_start("wiim_remote_2")

    assert result == "OK"
    assert wl._begin_turn.called is True
    assert wl._begin_turn.kwargs == {
        "pre_roll": False,
        "listening_feedback": True,
    }
    assert wl._active_manual_source == "wiim_remote_2"
    assert wl._acquiring is False


async def test_manual_mic_loop_forwards_only_active_source():
    from jasper.voice_daemon import State

    class _FakeMic:
        async def frames(self):
            yield "frame-a"

    wl = _make_wake_loop()
    wl._state = State.SESSION
    wl._manual_mics = {
        "wiim_remote_2": types.SimpleNamespace(mic=_FakeMic()),
    }
    seen = []

    async def handle(frame):
        seen.append(frame)

    wl._handle_session_frame = handle
    await wl._manual_mic_loop("wiim_remote_2")
    assert seen == []

    wl._active_manual_source = "wiim_remote_2"
    await wl._manual_mic_loop("wiim_remote_2")
    assert seen == ["frame-a"]


def _ptt_only_wake_loop():
    """A speaker with NO microphone of its own and one push-to-talk source.

    The shape a full-profile box with no local mic (unplugged, or never
    fitted) has once `_configured_wake_legs` reads the reconciler's
    published "no local mic" verdict: zero wake legs, so `_mic` is None,
    `_push_to_talk_only` derives True, and the only audio path is the
    remote's loop.
    """
    from jasper.voice_daemon import State, WakeLoop, _ManualMicRuntime

    wl = WakeLoop.for_tests(
        legs=[],
        manual_mics=[_ManualMicRuntime("wiim_remote_2", object(), "udp:9892")],
    )
    wl._state = State.WAKE
    wl._mic_muted = False
    wl._measurement_active = asyncio.Event()
    wl._fire_and_forget = set()
    wl._spend_cap = types.SimpleNamespace(allowed=lambda: True)
    wl._connection = types.SimpleNamespace(
        is_paused=lambda: False, last_failure_detail=lambda: None,
    )
    wl._begin_turn = _SpyCalls()
    wl._prepare_assistant_loudness_context = _SpyCalls()
    wl._play_listening_chirp = _SpyCalls()
    wl._cleanup_after_failed_begin = _SpyCalls()
    wl._cues = _SpyCues()
    return wl


async def test_source_less_start_on_a_speaker_with_no_room_mic_cues_and_refuses(
    caplog,
):
    """The silent-turn hole, closed.

    Before this guard, a source-less START on a push-to-talk-only speaker was
    ACCEPTED: `_active_manual_source` stayed None so `_manual_mic_loop`
    dropped every frame, `_pre_roll` was empty because no primary loop fills
    it, and the turn ducked the music, chirped, forwarded zero bytes and died
    to the idle watchdog ~20 s later. `bytes_sent == 0` misses both
    end-of-turn warnings, so the household got silence and the journal got
    nothing at all.
    """
    from jasper.voice_daemon import NO_ROOM_MIC_CUE_SLUG

    wl = _ptt_only_wake_loop()

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.manual_session_start()

    assert result == "NO_ROOM_MIC"
    # NOT `_assert_no_turn_no_duck`: that helper also forbids the loudness
    # prime, and a cue legitimately primes + ducks — that is how it stays
    # audible over music. What must not happen is the paid LLM turn and the
    # "I'm listening" chirp for a turn nothing could ever feed.
    assert wl._begin_turn.called is False
    assert wl._play_listening_chirp.called is False
    # Audible, not just logged: the household pressed something and must not
    # be answered with silence (AGENTS.md's no-silent-deafness rule).
    await _drain_refusal_cue(wl)
    assert wl._cues.played == [NO_ROOM_MIC_CUE_SLUG]
    assert "event=session.manual_refused" in caplog.text
    assert "reason=no_room_microphone" in caplog.text
    # WARNING, not INFO: this is a misconfigured caller on a working speaker.
    assert any(
        rec.levelno >= logging.WARNING
        and "reason=no_room_microphone" in rec.getMessage()
        for rec in caplog.records
    ), caplog.text


def test_no_room_mic_cue_slug_is_registered():
    """A slug the registry does not know bakes no WAV and plays nothing —
    the failure would be as silent as the bug this cue exists to break."""
    from jasper.cues.registry import find
    from jasper.voice_daemon import NO_ROOM_MIC_CUE_SLUG

    cue = find(NO_ROOM_MIC_CUE_SLUG)
    assert cue is not None, NO_ROOM_MIC_CUE_SLUG
    # Provider-agnostic (AGENTS.md): the voice backend is replaceable.
    lowered = cue.template.lower()
    assert "google" not in lowered and "gemini" not in lowered


async def test_ptt_only_speaker_still_serves_a_named_source():
    """Control: the refusal is scoped to the SOURCE-LESS call. Naming the
    remote on the same speaker opens a normal button turn — otherwise the
    guard would have parked the very box it exists to serve."""
    wl = _ptt_only_wake_loop()

    result = await wl.manual_session_start("wiim_remote_2")

    assert result == "OK"
    assert wl._cues.played == []
    assert wl._begin_turn.called is True
    assert wl._begin_turn.kwargs == {
        "pre_roll": False,
        "listening_feedback": True,
    }
    assert wl._active_manual_source == "wiim_remote_2"


async def test_source_less_refusal_reads_the_single_derivation():
    """The refusal consults `_push_to_talk_only`, not its own `_mic is None`.

    Push-to-talk-only is ONE derived fact with one owner; a site that
    re-derives it can drift from run()'s keepalive branch and from
    /state.voice. Forcing the field is therefore enough to move this site:
    with it cleared, the same source-less call falls through to the ordinary
    gates below (here BUSY) instead of refusing — even though `_mic` is still
    None. Nothing else about the speaker changed.
    """
    from jasper.voice_daemon import State

    wl = _ptt_only_wake_loop()
    wl._state = State.SESSION
    assert await wl.manual_session_start() == "NO_ROOM_MIC"
    await _drain_refusal_cue(wl)

    wl2 = _ptt_only_wake_loop()
    wl2._state = State.SESSION
    wl2._push_to_talk_only = False
    assert wl2._mic is None  # unchanged: only the derived fact moved
    assert await wl2.manual_session_start() == "BUSY"
    assert wl2._cues.played == []


async def test_no_room_mic_outranks_the_transient_gates():
    """Permanent shape beats transient state.

    A passing BUSY/MUTED would mask a request that can NEVER succeed on this
    speaker, and the household would keep pressing.
    """
    from jasper.voice_daemon import State

    wl = _ptt_only_wake_loop()
    wl._state = State.SESSION
    wl._mic_muted = True

    assert await wl.manual_session_start() == "NO_ROOM_MIC"
    await _drain_refusal_cue(wl)


async def test_manual_end_is_idempotent_after_input_already_closed():
    from jasper.voice_daemon import State

    wl = _make_wake_loop()
    wl._state = State.SESSION
    wl._turn = object()
    wl._input_ended = True

    result = await wl.manual_session_end()

    assert result == "OK"


async def test_session_task_watcher_ends_manual_turn_without_extra_frame():
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    ended = asyncio.Event()

    async def _end_turn():
        ended.set()

    async def _complete():
        return None

    wl._end_turn = _end_turn
    task = asyncio.create_task(_complete())
    wl._bg_tasks = {task}

    wl._arm_session_task_watcher()
    await asyncio.wait_for(ended.wait(), timeout=1.0)
    await asyncio.gather(*wl._fire_and_forget)


async def test_session_task_watcher_ignores_stale_completed_tasks():
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    ended = False

    async def _end_turn():
        nonlocal ended
        ended = True

    async def _complete():
        return None

    async def _pending():
        await asyncio.Event().wait()

    wl._end_turn = _end_turn
    old_task = asyncio.create_task(_complete())
    new_task = asyncio.create_task(_pending())
    wl._bg_tasks = {new_task}

    await wl._watch_session_tasks((old_task,))

    assert ended is False
    new_task.cancel()
    await asyncio.gather(new_task, return_exceptions=True)
