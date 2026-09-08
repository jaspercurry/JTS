# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Turn completion retains ownership until output and cleanup finish."""

from __future__ import annotations

import asyncio

import pytest

from jasper.tts_routing import FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET
from tests._async_wait import wait_signalled, wait_until
from tests._live_turn_fake import FakeLiveTurn as _FakeTurn
from tests._wake_loop import wake_loop_for_tests
from tests.usage_store_fixtures import FakeUsageStore


def _make_wakeloop():
    from jasper.voice_daemon import State

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

        async def wait_drained(self):
            return None

        def take_paced_sec(self):
            return 0.0

    wl = wake_loop_for_tests()
    wl._state = State.SESSION
    wl._turn = _FakeTurn()
    wl._session_id = 7
    wl._usage_store = FakeUsageStore()
    wl._bg_tasks = set()
    wl._user_speech_seen = True
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

    wl._wake_telemetry.stage = _noop_stage
    wl._wake_telemetry.outcome = _noop_outcome
    wl._peering.session_ended = _noop_peering
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


@pytest.mark.parametrize("completion", ["returned", "failed", "cancelled"])
async def test_background_task_completion_ends_turn_without_new_mic_frame(
    completion,
):
    """Manual mics stop sending frames when the button is released."""
    from jasper.voice_daemon import State

    wl = _make_wakeloop()
    turn = wl._turn

    async def finish():
        if completion == "failed":
            raise RuntimeError("playback failed")

    finished_task = asyncio.create_task(finish())
    wl._bg_tasks = {finished_task}
    wl._arm_turn_background_end()
    if completion == "cancelled":
        finished_task.cancel()
    try:
        await wait_until(lambda: wl._state is State.WAKE, timeout=10.0)
    finally:
        await wl._cancel_fire_and_forget_tasks()
        await asyncio.gather(finished_task, return_exceptions=True)

    assert wl._state is State.WAKE
    assert wl._turn is None
    assert wl._usage_store.close_calls == 1
    assert turn.end_input_calls == 1
    assert turn.release_calls == 1


def test_simultaneous_background_task_completion_schedules_one_teardown():
    """Multiple completed bg tasks should coalesce to one _end_turn task."""
    from jasper.voice_daemon import State

    wl = wake_loop_for_tests()
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


@pytest.mark.parametrize("tts_socket", [FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET])
async def test_turn_ownership_covers_final_chirp_physical_tail(
    tts_socket: str,
) -> None:
    """PAUSE cannot open while the final chirp is still physically audible."""
    from jasper.voice_daemon import State

    wl = _make_wakeloop()
    wl._cfg.tts_outputd_socket = tts_socket
    wl._turn_output_episode = await wl._output_gate.begin_turn()
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()

    async def wait_drained() -> None:
        drain_started.set()
        await wait_signalled(release_drain, "release final chirp drain")

    wl._tts.wait_drained = wait_drained
    teardown = asyncio.create_task(wl._end_turn())
    await wait_signalled(
        drain_started,
        "final chirp physical drain",
        producer=teardown,
    )

    assert wl._state is State.SESSION
    assert wl._output_gate.active_kind == "turn"
    assert (await wl.measurement_hold.pause_response())["result"] == "BUSY"

    release_drain.set()
    await teardown
    assert wl._state is State.WAKE
    assert not wl._output_gate.is_active


@pytest.mark.parametrize("phase", ["outcome", "peering", "segment", "release", "drain", "restore", "meter"])
@pytest.mark.parametrize("cancellation", ["caller", "operation"])
async def test_end_turn_finishes_owned_cleanup_before_propagating_cancel(
    phase, cancellation,
):
    from unittest.mock import AsyncMock, Mock

    from jasper.voice_daemon import State

    wl = _make_wakeloop()
    turn = wl._turn
    wl._turn_output_episode = await wl._output_gate.begin_turn()
    entered, proceed = asyncio.Event(), asyncio.Event()
    calls = []

    async def step(name):
        calls.append(name)
        if name == phase:
            entered.set()
            if cancellation == "operation":
                raise asyncio.CancelledError()
            await proceed.wait()

    wl._wake_telemetry.stage = lambda _stage: step("outcome")
    wl._peering.session_ended = lambda _reason: step("peering")
    wl._tts.end_segment = lambda: step("segment")
    turn.release = lambda: step("release")
    wl._tts.wait_drained = lambda: step("drain")
    wl._ducker.restore = lambda: step("restore")
    wl._tts.resume_content_meter = lambda: step("meter")
    wl._assistant_output.listening_chirp = AsyncMock()
    wl._content_activity.resume = Mock()
    cleanup = asyncio.create_task(wl._end_turn())
    try:
        await wait_signalled(entered, phase, producer=cleanup)
        if cancellation == "caller":
            for _ in range(3):
                cleanup.cancel()
                await asyncio.sleep(0)
            assert not cleanup.done()
            assert wl._state is State.SESSION
            assert wl._output_gate.is_active
            proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
    finally:
        proceed.set()
        await asyncio.gather(cleanup, return_exceptions=True)

    assert calls == ["outcome", "peering", "segment", "release", "drain", "restore", "meter"]
    assert wl._usage_store.close_calls == 1
    assert wl._state is State.WAKE
    assert wl._turn is None
    assert not wl._output_gate.is_active
    assert wl._ending is False
    wl._content_activity.resume.assert_called_once()
    await wl._end_turn()
    assert wl._usage_store.close_calls == 1
