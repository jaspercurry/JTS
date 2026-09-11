# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Turn completion retains ownership until output and cleanup finish."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from jasper.voice.turn_playback import play_responses
from jasper.voice_daemon import State
from jasper.tts_routing import FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET
from tests._async_wait import wait_signalled, wait_until
from tests._live_turn_fake import FakeLiveTurn as _FakeTurn, silent_frame
from tests._log_events import event_fields
from tests._wake_loop import wake_loop_for_tests
from tests.usage_store_fixtures import FakeUsageStore


def _make_wakeloop():
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
    wl = _make_wakeloop()

    asyncio.run(wl._end_turn())
    assert wl._state is State.WAKE
    assert wl._usage_store.close_calls == 1
    assert wl._turn is None

    # Second call: not in a turn anymore — must short-circuit, no crash.
    asyncio.run(wl._end_turn())
    assert wl._usage_store.close_calls == 1


def test_end_turn_reentry_while_teardown_in_flight_short_circuits():
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


async def _response_loop(pcm=bytes(8)):
    wl = _make_wakeloop()
    turn = wl._turn
    turn._bytes_sent, turn._chunks_received = 4096, 1
    wl._input_ended = True
    wl._anchor_turn_timeline()
    wl._turn_output_episode = await wl._output_gate.begin_turn()
    wl._wake_telemetry.outcome = AsyncMock()
    wl._assistant_output.listening_chirp = AsyncMock()

    async def cue(slug):
        assert not wl._output_gate.is_active
        assert wl._state is State.SESSION

    wl._play_cue = AsyncMock(side_effect=cue)

    async def audio():
        yield pcm

    turn.audio_out_chunks = audio
    turn.wait_for_interrupt = asyncio.Event().wait
    return wl, turn


def _start_playback(wl):
    return asyncio.create_task(play_responses(
        wl._turn, wl._tts, report=wl._playback_report,
        admission_refusal=wl._assistant_output.admission_refusal,
        on_response_started=wl._turn_observer("first_response"),
        on_first_write=wl._turn_observer("first_write"),
    ))


@pytest.mark.parametrize("end_path", ["callback", "frame"])
@pytest.mark.parametrize("mode", [
    "refused", "error", "paused_error", "partial_error", "accepted", "empty", "measurement", "interrupt",
    "lost_reply", "lost_after_complete",
])
async def test_shared_playback_result_wins_over_same_tick_watchdog(mode, end_path, caplog):
    wl, turn = await _response_loop(b"" if mode == "empty" else bytes(8))
    failed = mode in {"refused", "error", "paused_error", "partial_error"}
    accepted = mode in {"partial_error", "accepted", "lost_reply", "lost_after_complete"}
    lost_reply = mode == "lost_reply"
    turn.turn_lost = lambda: mode.startswith("lost_")
    turn.server_turn_complete = lambda: mode == "lost_after_complete"
    wl._wake_telemetry.stage = AsyncMock()
    wl._last_turn_ms = previous = {"event_id": "previous"}
    if mode == "paused_error":
        wl._connection.is_paused = lambda: True
    if mode == "measurement":
        await wl._output_gate.pause_admission()
    if mode == "interrupt":
        interrupt = asyncio.Event()
        interrupt.set()
        turn.wait_for_interrupt = interrupt.wait

    async def write(*args, on_first_write, **kwargs):
        if accepted:
            await on_first_write()
        if mode in {"error", "paused_error", "partial_error"}:
            raise OSError("output unavailable")
        return accepted

    wl._tts.write_segment = write
    wl._tts.flush = AsyncMock()
    playback = _start_playback(wl)
    watchdog = asyncio.create_task(asyncio.sleep(0))
    wl._bg_tasks = {playback, watchdog}
    await asyncio.gather(playback, watchdog, return_exceptions=True)
    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        try:
            if end_path == "callback":
                wl._arm_turn_background_end()
                wl._on_turn_background_done(watchdog)
                await wait_until(lambda: wl._state is State.WAKE, timeout=10.0)
            else:
                await wl._handle_session_frame(silent_frame())
        finally:
            await wl._cancel_fire_and_forget_tasks()

    outcome = "session_failed" if failed or lost_reply else "completed"
    reason = "playback_failed" if failed else wl._playback_report.stop_reason or "ended"
    wl._wake_telemetry.outcome.assert_awaited_once_with(outcome, reason)
    assert wl._assistant_output.listening_chirp.await_count == (0 if failed else 1)
    assert wl._play_cue.await_count == (1 if failed or lost_reply or mode == "empty" else 0)
    if wl._play_cue.await_count:
        wl._play_cue.assert_awaited_once_with("internal_error")
    assert wl._tts.flush.await_count == (1 if failed or mode == "interrupt" else 0)
    assert wl.session_status()["silent_responses_session"] == int(
        (failed and not accepted) or lost_reply or mode == "empty",
    )
    timeline = event_fields(caplog, "turn.timeline")
    assert timeline["outcome"] == ("failed" if failed or lost_reply else "complete")
    if failed or lost_reply:
        wl._wake_telemetry.stage.assert_not_awaited()
        assert wl.session_status()["last_turn_ms"] == previous
    else:
        wl._wake_telemetry.stage.assert_awaited_once_with("turn_complete")
        assert wl.session_status()["last_turn_ms"]["outcome"] == "complete"
    assert ("first_write_ms" in timeline) == accepted
    assert turn.release_calls == turn.end_input_calls == wl._usage_store.close_calls == 1
    assert wl._turn is None and wl._state is State.WAKE


@pytest.mark.parametrize("reason", ["ended", "mic_muted", "stopping", "research_window_wake"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_teardown_joins_accepted_prefix_before_its_final_outcome(reason, write_fails, caplog):
    wl, turn = await _response_loop()
    writing = asyncio.Event()

    async def write(*args, on_first_write, **kwargs):
        writing.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await on_first_write()
            if write_fails:
                raise OSError("output failed during cancellation") from None
            raise

    wl._tts.write_segment = write
    playback = _start_playback(wl)
    wl._bg_tasks = {playback}
    await wait_signalled(writing, "pending output write", producer=playback)
    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        await wl._end_turn(reason)
    failed = write_fails and reason == "ended"
    wl._wake_telemetry.outcome.assert_awaited_once_with(
        "session_failed" if failed else "completed", "playback_failed" if failed else reason,
    )
    assert "first_write_ms" in event_fields(caplog, "turn.timeline")
    assert wl._play_cue.await_count == int(failed)
    assert wl._silent_responses_session == 0
    assert turn.release_calls == wl._usage_store.close_calls == 1
    assert playback.done()


@pytest.mark.parametrize("accepted_prefix", [False, True])
async def test_barge_signal_is_kept_when_teardown_cancels_the_pending_write(accepted_prefix):
    wl, turn = await _response_loop()
    writing, interrupt = asyncio.Event(), asyncio.Event()
    turn.wait_for_interrupt = interrupt.wait
    wl._tts.flush = AsyncMock()

    async def write(*args, on_first_write, **kwargs):
        if accepted_prefix:
            await on_first_write()
        writing.set()
        await asyncio.Event().wait()

    wl._tts.write_segment = write
    playback = _start_playback(wl)
    wl._bg_tasks = {playback}
    await wait_signalled(writing, "pending output write", producer=playback)
    interrupt.set()
    await wl._end_turn()
    wl._wake_telemetry.outcome.assert_awaited_once_with("completed", "barge_in")
    wl._play_cue.assert_not_awaited()
    wl._tts.flush.assert_awaited_once()
    assert wl._silent_responses_session == 0
    assert turn.release_calls == 1


def test_simultaneous_background_task_completion_schedules_one_teardown():
    """Multiple completed bg tasks should coalesce to one _end_turn task."""
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


async def test_closure_completes_without_waiting_for_the_provider_release():
    """The user's ending — chirp, duck restore, back to wake listening —
    runs while the provider teardown is still in flight."""
    wl = _make_wakeloop()
    wl._turn_output_episode = await wl._output_gate.begin_turn()
    finish_release = asyncio.Event()
    order: list[str] = []

    async def release():
        try:
            await finish_release.wait()
        finally:
            order.append("release_done")

    async def restore():
        order.append("duck_restore")

    async def chirp(*, going_on):
        order.append(f"chirp_{going_on}")

    wl._turn.release = release
    wl._ducker.restore = restore
    wl._assistant_output.listening_chirp = chirp

    teardown = asyncio.create_task(wl._end_turn("conversation_ended"))
    try:
        await wait_until(lambda: wl._state is State.WAKE)
        assert order == ["chirp_False", "duck_restore"]
        assert teardown.done()
    finally:
        finish_release.set()
        await asyncio.gather(teardown, return_exceptions=True)
    await wl._pending_release
    assert order[-1] == "release_done"


async def test_shutdown_waits_out_a_pending_provider_release():
    """The daemon's task sweep gives an in-flight release time to reach
    `session.close`: a cancelled one leaves the provider session open, and
    a live session bills per connected minute."""
    wl = _make_wakeloop()
    wl._turn_output_episode = await wl._output_gate.begin_turn()
    closed = asyncio.Event()

    async def release():
        await asyncio.sleep(0.05)
        closed.set()

    wl._turn.release = release

    await wl._end_turn("conversation_ended")
    pending = wl._pending_release
    assert pending is not None

    await wl._cancel_fire_and_forget_tasks()

    assert closed.is_set()
    assert not pending.cancelled()


@pytest.mark.parametrize("phase", ["outcome", "peering", "segment", "drain", "restore", "meter"])
@pytest.mark.parametrize("cancellation", ["caller", "operation"])
async def test_end_turn_finishes_owned_cleanup_before_propagating_cancel(
    phase, cancellation,
):
    from unittest.mock import AsyncMock, Mock

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

    # "release" is no longer an owned cleanup phase, so it carries no
    # position in the closure — only the fact that it still ran.
    assert [name for name in calls if name != "release"] == [
        "outcome", "peering", "segment", "drain", "restore", "meter",
    ]
    await wl._pending_release
    assert "release" in calls
    assert wl._usage_store.close_calls == 1
    assert wl._state is State.WAKE
    assert wl._turn is None
    assert not wl._output_gate.is_active
    assert wl._ending is False
    wl._content_activity.resume.assert_called_once()
    await wl._end_turn()
    assert wl._usage_store.close_calls == 1
