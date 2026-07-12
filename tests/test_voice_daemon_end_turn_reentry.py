# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression: _end_turn must be idempotent / non-reentrant.

Before the fix, _end_turn ran a multi-second teardown (telemetry,
peering notify, bg-task join, end_input with a 2 s timeout, release,
chirp, duck restore) and only flipped self._state to WAKE at its very
last line, clearing self._session_id just before. A concurrent
mic-mute (control socket) or the next main-loop session frame arriving
inside that window re-entered _end_turn and hit
`assert self._session_id is not None` after it had been cleared — the
main-loop path did not swallow the AssertionError, crashing the daemon
(session drop + corrupted usage row).

The fix guards teardown with a dedicated `self._ending` flag held across
the whole teardown, so a second concurrent entrant short-circuits,
leaving exactly one teardown. The flag is used instead of an early
`_state = WAKE` flip on purpose: `_state` must stay SESSION through the
teardown (which plays a chirp on the single PortAudio stream) so the
supervisor-cue / timer-announce / mic-loop gates that key on SESSION keep
holding and nothing collides with the teardown chirp.
"""

from __future__ import annotations

import asyncio


class _FakeTurn:
    """Minimal LiveTurn stand-in covering the surface _end_turn reads."""

    def __init__(self) -> None:
        self.end_input_calls = 0
        self.release_calls = 0

    def last_chunk_at(self) -> float:
        return 0.0

    def last_activity_at(self) -> float:
        return 0.0

    async def end_input(self) -> None:
        self.end_input_calls += 1

    async def release(self) -> None:
        self.release_calls += 1

    def usage_tokens(self) -> dict[str, int]:
        return {"input_tokens": 0, "output_tokens": 0}

    def usage_breakdown(self):
        return None

    def bytes_sent(self) -> int:
        return 0

    def chunks_received(self) -> int:
        return 0

    def turn_lost(self) -> bool:
        return False


class _FakeUsageStore:
    def __init__(self) -> None:
        self.close_calls = 0

    def close_session(self, session_id, in_tokens, out_tokens, usage=None):
        # Real store asserts a non-None session_id is passed; mirror that
        # so a re-entrant call after _session_id was cleared would blow up
        # exactly like production if the guard were absent.
        assert session_id is not None
        self.close_calls += 1
        return 0.0


def _make_wakeloop():
    from jasper.voice_daemon import State, WakeLoop

    class _Noop:
        def note_voice_session(self, *_a, **_k):
            return None

        def resume(self):
            return None

    class _AsyncNoop:
        async def restore(self):
            return None

        async def resume_content_meter(self):
            return None

        async def end_segment(self):
            return None

        def take_paced_sec(self):
            return 0.0

    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    wl._turn = _FakeTurn()
    wl._session_id = 7
    wl._usage_store = _FakeUsageStore()
    wl._bg_tasks = set()
    wl._peering_current_epoch = "ep-1"
    wl._user_speech_seen = True
    wl._server_vad_this_turn = False
    wl._max_silero_score_in_turn = 0.0
    wl._max_silero_raw_in_turn = 0.0
    wl._silero_aec_armed_at_ms = None
    wl._silero_raw_armed_at_ms = None
    wl._input_ended = False
    wl._ending = False

    # Collaborators with real side effects — stub to async/sync no-ops so
    # only the re-entrancy logic is under test.
    wl._volume_coordinator = _Noop()
    wl._content_activity = _Noop()
    wl._ducker = _AsyncNoop()
    wl._tts = _AsyncNoop()

    async def _noop_stage(_stage):
        # Yield control so a concurrent _end_turn entrant actually gets
        # scheduled mid-teardown — that's the window the race lives in.
        await asyncio.sleep(0)

    async def _noop_outcome(_outcome, _detail=None):
        return None

    async def _noop_peering(_reason):
        return None

    async def _noop_chirp(*, going_on):
        return None

    wl._telemetry_stage = _noop_stage
    wl._telemetry_outcome = _noop_outcome
    wl._notify_peering_session_ended = _noop_peering
    wl._play_listening_chirp = _noop_chirp
    return wl


def test_end_turn_is_idempotent_serial():
    """A second _end_turn call after teardown completes is a no-op."""
    from jasper.voice_daemon import State

    wl = _make_wakeloop()

    asyncio.run(wl._end_turn())
    assert wl._state is State.WAKE
    assert wl._usage_store.close_calls == 1
    assert wl._turn is None

    # Second call: not in a turn anymore — must short-circuit, no crash.
    asyncio.run(wl._end_turn())
    assert wl._usage_store.close_calls == 1


def test_end_turn_reentry_while_teardown_in_flight_short_circuits():
    """A re-entrant call while a teardown is in flight must short-circuit.

    The first teardown is modelled as in-flight by `self._ending = True`
    (the wrapper sets it before the first await and clears it in a
    finally). A concurrent mic-mute / main-loop frame re-enters here.
    Without the guard the body would run again, reach
    `if self._turn is not None:` (turn still set), and trip
    `assert self._session_id is not None` once the first teardown had
    cleared _session_id — the main-loop caller does not swallow that,
    crashing the daemon. With the flag the re-entrant call returns
    immediately and close_session is never re-invoked. State is left
    SESSION (the in-flight teardown owns the WAKE flip) to prove the
    guard does not depend on an early state change.
    """
    from jasper.voice_daemon import State

    wl = _make_wakeloop()
    wl._ending = True  # first teardown is in flight
    wl._state = State.SESSION  # still SESSION — teardown flips it at the end
    # Exact crash window: the in-flight teardown has already cleared
    # _session_id but not yet _turn. Without the guard the body would run,
    # reach `if self._turn is not None:`, and trip the
    # `assert self._session_id is not None` that crashed the daemon.
    wl._session_id = None

    asyncio.run(wl._end_turn())  # must NOT raise and must do nothing

    # Re-entrant call did nothing — the in-flight teardown owns cleanup.
    assert wl._usage_store.close_calls == 0
    assert wl._turn is not None  # untouched by the short-circuited call


def test_end_turn_concurrent_callers_teardown_once():
    """Two _end_turn coroutines racing on one loop tear down exactly once.

    gather() schedules both; the first sets `self._ending = True`
    synchronously before its first await, so the second short-circuits at
    the top guard. Exactly one teardown runs and no AssertionError
    escapes.
    """
    from jasper.voice_daemon import State

    wl = _make_wakeloop()
    turn = wl._turn  # _end_turn clears self._turn on completion

    async def drive():
        await asyncio.gather(wl._end_turn(), wl._end_turn())

    asyncio.run(drive())

    assert wl._state is State.WAKE
    assert wl._turn is None
    assert wl._usage_store.close_calls == 1
    assert turn.end_input_calls == 1
    assert turn.release_calls == 1


def test_background_task_completion_ends_turn_without_new_mic_frame():
    """Manual push-to-talk sources stop producing frames on button release.

    The frame-loop guard still catches completed playback/watchdog tasks for
    always-on mics, but a remote mic must not need a second button press just
    to notice that response playback drained. The background task callback
    should schedule the same teardown path by itself.
    """
    from jasper.voice_daemon import State

    wl = _make_wakeloop()
    turn = wl._turn

    async def drive():
        finished_task = asyncio.create_task(asyncio.sleep(0), name="finished-bg")
        wl._bg_tasks = {finished_task}
        wl._arm_turn_background_end()

        await finished_task
        for _ in range(20):
            if wl._state is State.WAKE:
                return
            pending = list(wl._fire_and_forget)
            if pending:
                await asyncio.gather(*pending)
            else:
                await asyncio.sleep(0)

    asyncio.run(drive())

    assert wl._state is State.WAKE
    assert wl._turn is None
    assert wl._usage_store.close_calls == 1
    assert turn.end_input_calls == 1
    assert turn.release_calls == 1


def test_simultaneous_background_task_completion_schedules_one_teardown():
    """Multiple completed bg tasks should coalesce to one _end_turn task."""
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    wl._turn = object()
    calls = 0

    async def fake_end_turn(reason="ended"):
        nonlocal calls
        calls += 1

    wl._end_turn = fake_end_turn

    async def drive():
        task_a = asyncio.create_task(asyncio.sleep(0), name="bg-a")
        task_b = asyncio.create_task(asyncio.sleep(0), name="bg-b")
        wl._bg_tasks = {task_a, task_b}
        wl._arm_turn_background_end()
        await asyncio.gather(task_a, task_b)
        for _ in range(10):
            pending = list(wl._fire_and_forget)
            if pending:
                await asyncio.gather(*pending)
                return
            await asyncio.sleep(0)

    asyncio.run(drive())

    assert calls == 1
