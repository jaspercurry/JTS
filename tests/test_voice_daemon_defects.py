# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import gc
import logging
import time
import weakref

import pytest

from jasper.audio_io import InputDeviceUnavailable
from jasper.voice_daemon import State, idle_watchdog

from ._log_events import event_fields
from ._wake_loop import wake_loop_for_tests


async def test_fire_and_forget_task_survives_gc_until_done():
    """WakeLoop keeps one-shot tasks strongly referenced until completion.

    The acquire/drain task sets `_acquiring=True` before it starts. If
    asyncio's weak task reference let it disappear mid-flight, the daemon
    would keep routing mic frames into the acquire buffer indefinitely.
    """
    wl = wake_loop_for_tests()
    wl._fire_and_forget = set()

    started = asyncio.Event()
    release = asyncio.Event()
    done = asyncio.Event()

    async def _runner() -> None:
        started.set()
        await release.wait()
        done.set()

    task = wl._create_fire_and_forget_task(_runner(), name="gc-proof")
    task_ref = weakref.ref(task)
    del task

    await asyncio.wait_for(started.wait(), timeout=1.0)
    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0)

    assert task_ref() is not None
    assert len(wl._fire_and_forget) == 1

    release.set()
    await asyncio.wait_for(done.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert wl._fire_and_forget == set()


async def test_fire_and_forget_shutdown_cancels_and_awaits_tasks():
    wl = wake_loop_for_tests()
    wl._fire_and_forget = set()

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _never_finishes() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    wl._create_fire_and_forget_task(_never_finishes(), name="cancel-proof")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await wl._cancel_fire_and_forget_tasks()

    assert cancelled.is_set()
    assert wl._fire_and_forget == set()


async def test_run_shutdown_stops_wake_legs_before_sweeping_fire_and_forget():
    wl = wake_loop_for_tests()
    wl._fire_and_forget = set()
    wl._heartbeat = None
    wl._state = State.WAKE
    wl._legs = {"on": object(), "off": object()}
    wl._stop_event = asyncio.Event()
    wl._stop_event.set()

    class _OneFrameMic:
        async def frames(self):
            yield object()

    wl._mic = _OneFrameMic()

    async def _late_shutdown_task() -> None:
        await asyncio.Event().wait()

    async def _wake_leg_loop(_leg_name: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            wl._create_fire_and_forget_task(
                _late_shutdown_task(),
                name="late-shutdown",
            )

    wl._wake_leg_loop = _wake_leg_loop

    try:
        await wl.run()
        assert wl._fire_and_forget == set()
    finally:
        await wl._cancel_fire_and_forget_tasks()


async def test_run_parks_instead_of_crashing_on_the_impossible_no_mic_state():
    """`run()`'s else branch used to dereference `self._mic` unconditionally.

    On the shape where `_push_to_talk.only` is False yet no primary leg was
    ever built, that was a bare AttributeError — main() has no handler for
    it, so the daemon would exit 1 and Restart=on-failure would walk the
    unit into StartLimitAction=reboot, unlike every other input failure,
    which parks cleanly at exit 66 via InputDeviceUnavailable.

    This shape is unreachable on any daemon that actually started —
    `_require_usable_input` (jasper/voice/daemon_main.py) refuses to start
    one with neither a wake leg nor a manual mic — so this test constructs
    it directly, the same way
    test_voice_daemon_manual_start_guard.test_source_less_refusal_reads_the_single_derivation
    does: no legs at all (`_mic` stays None) and no manual mics
    (`_push_to_talk.only` derives False, not True).
    """
    wl = wake_loop_for_tests(legs=[])
    assert wl._mic is None
    assert wl._push_to_talk.only is False

    with pytest.raises(InputDeviceUnavailable):
        await wl.run()


class _StalledTurn:
    def __init__(self, *, last_chunk_delta: float) -> None:
        now = time.monotonic()
        self._last_chunk_at = now - last_chunk_delta
        self._last_activity_at = self._last_chunk_at

    def turn_lost(self) -> bool:
        return False

    def last_activity_at(self) -> float:
        return self._last_activity_at

    def server_turn_complete(self) -> bool:
        return False

    def last_chunk_at(self) -> float:
        return self._last_chunk_at

    def audio_chunks_pending(self) -> int:
        return 0


class _DrainedTts:
    def expected_drain_at(self) -> float:
        return 0.0


async def test_idle_watchdog_caps_mid_response_stall(caplog):
    turn = _StalledTurn(last_chunk_delta=1.0)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await asyncio.wait_for(
            idle_watchdog(
                turn,
                _DrainedTts(),
                timeout=999.0,
                response_stall_timeout=0.01,
            ),
            timeout=1.0,
        )

    assert "response stalled" in caplog.text


class _CompletedTurn:
    """Turn whose only completion signal is ``server_turn_complete()`` — the
    shape an interrupted Gemini turn reaches (it goes interrupted ->
    turn_complete, with no generation_complete)."""

    def turn_lost(self) -> bool:
        return False

    def last_activity_at(self) -> float:
        return time.monotonic()

    def server_turn_complete(self) -> bool:
        return True

    def last_chunk_at(self) -> float:
        return 0.0

    def audio_chunks_pending(self) -> int:
        return 0


async def test_idle_watchdog_returns_on_server_turn_complete():
    """Clean-close path: once the server reports turn_complete and audio has
    drained, the watchdog ends the turn on that signal alone — there is no
    generation_complete to wait on. The generous timers ensure only the
    ``server_turn_complete()`` branch can end this turn, so a provider whose
    interrupted turn goes interrupted -> turn_complete (Gemini) cannot hang
    the watchdog. Complement to ``test_idle_watchdog_caps_mid_response_stall``
    (the no-signal fallback)."""
    await asyncio.wait_for(
        idle_watchdog(
            _CompletedTurn(),
            _DrainedTts(),
            timeout=999.0,
            response_stall_timeout=999.0,
        ),
        timeout=1.0,
    )


async def test_turn_open_failure_cue_is_honest_about_cause(caplog):
    """Regression for the 2026-06-19 incident.

    An UNEXPECTED local error during turn-open (the trigger that day was
    a readonly usage.db write) used to fire the `cant_connect` cue — the
    speaker told the user "I can't connect right now, I'll keep trying"
    when connectivity was fine. The turn-open catch-all must pick the cue
    by the LIVE connection state: `cant_connect` only when the connection
    is genuinely paused, otherwise the honest, low-alarm `internal_error`
    cue. (Layer 2 separately keeps the usage write from reaching here at
    all; this pins the cue honesty regardless of what throws.) The catch-all
    also names itself in the journal, so the refusal is not cue-only."""

    async def _drive(
        *, paused: bool, conn_paused: bool = False, cue: str | None = None,
    ) -> tuple[list[str], int]:
        wl = wake_loop_for_tests()
        played: list[str] = []
        nudges = 0

        async def _rec(slug: str) -> None:
            played.append(slug)

        async def _win(**_kwargs) -> str:
            return "WIN"

        async def _noop(*_args, **_kwargs) -> None:
            return None

        async def _begin_boom(**_kwargs) -> None:
            # Stands in for the real incident: an unexpected local failure
            # on the turn-open hot path (the connection itself is fine).
            raise RuntimeError("attempt to write a readonly database")

        class _Conn:
            def __init__(self, is_paused: bool) -> None:
                self._paused = is_paused

            def is_paused(self) -> bool:
                return self._paused

            def wake_cue(self) -> str:
                return cue or "cant_connect"

            def request_reconnect_now(self) -> bool:
                nonlocal nudges
                nudges += 1
                return True

        wl._wake_late_cancelled = lambda *_a, **_k: False
        wl._peering.arbitrate = _win
        wl._prepare_assistant_loudness_context = _noop
        wl._play_listening_chirp = _noop
        wl._begin_turn_inner = _begin_boom
        wl._play_cue = _rec
        wl._connection = _Conn(paused)

        try:
            await wl._arbitrate_acquire_drain(
                score=0.9,
                rms_dbfs=-30.0,
                spend_allowed=True,
                conn_paused=conn_paused,
                can_serve=True,
            )
        finally:
            await wl._cancel_fire_and_forget_tasks()
        return played, nudges

    # Healthy connection + unexpected local error -> honest internal cue,
    # NOT a false "I can't connect".
    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        assert await _drive(paused=False) == (["internal_error"], 0)
    assert event_fields(caplog, "wake.refused") == {
        "reason": "acquire_error",
        "exc_type": "RuntimeError",
    }

    # Connection genuinely dropped into paused/failed mid-acquire ->
    # cant_connect is the truthful cue.
    assert await _drive(paused=True) == (["cant_connect"], 0)

    # ADR-0215: a TERMINAL outage names its remedy instead — "I'll keep
    # trying" is a false promise when retrying cannot help. Pinned at the
    # wake gate (conn_paused), the path the household actually hits.
    # The refused wake also asks the supervisor to retry now (#3855).
    assert await _drive(
        paused=True, conn_paused=True, cue="provider_out_of_credit",
    ) == (["provider_out_of_credit"], 1)


async def test_turn_open_failure_releases_output_gate_before_cue():
    played: list[tuple[str, str | None]] = []
    async def _win(**_kwargs) -> str:
        return "WIN"
    async def _noop(*_args, **_kwargs) -> None:
        return None
    async def _begin_boom(**_kwargs) -> None:
        raise RuntimeError("turn open failed")
    class _Conn:
        def is_paused(self) -> bool:
            return False
    class _Cues:
        async def play(self, slug: str) -> bool:
            played.append((slug, wl._output_gate.active_kind))
            return True
    wl = wake_loop_for_tests(cues=_Cues())
    wl._wake_late_cancelled = lambda *_a, **_k: False
    wl._peering.arbitrate = _win
    wl._prepare_assistant_loudness_context = _noop
    wl._play_listening_chirp = _noop
    wl._begin_turn_inner = _begin_boom
    wl._connection = _Conn()

    try:
        await wl._arbitrate_acquire_drain(
            score=0.9,
            rms_dbfs=-30.0,
            spend_allowed=True,
            conn_paused=False,
            can_serve=True,
        )
    finally:
        await wl._cancel_fire_and_forget_tasks()

    assert played == [("internal_error", "admin")]
    assert wl._output_gate.active_kind is None


def test_session_status_surfaces_usage_tracking_degraded():
    """session_status() exposes the UsageStore write-health so /state.voice (and
    the spend-cap UI) can show that spend recording is degraded — the S1 signal.
    Defaults False; reflects the store's write_degraded."""
    wl = wake_loop_for_tests()
    assert wl.session_status()["usage_tracking_degraded"] is False

    class _DegradedStore:
        write_degraded = True

        def open_session(self, *_a, **_k):
            return 1

        def close_session(self, *_a, **_k):
            return 0.0

    wl._usage_store = _DegradedStore()
    assert wl.session_status()["usage_tracking_degraded"] is True


def test_session_status_distinguishes_fanin_duck_from_camilla_lock():
    from types import SimpleNamespace

    from jasper.voice_daemon import FanInDucker

    async def program_duck(_on: bool) -> bool:
        return True

    ducker = FanInDucker(SimpleNamespace(program_duck=program_duck))
    ducker._ducked = True
    wl = wake_loop_for_tests(ducker=ducker)

    status = wl.session_status()
    assert status["duck_active"] is True
    assert status["camilla_volume_locked"] is False


def test_capture_gap_resets_wake_history_and_reports_input_age():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from jasper.audio_buffer import InputFrame

    wl = wake_loop_for_tests()
    rt = wl._legs["on"]
    rt.detector.reset = Mock()
    rt.shadow_vad = SimpleNamespace(reset=Mock())
    rt.recent_score = 1.0
    rt.recent_score_at = time.monotonic()
    rt.capture_ring.append(b"old")
    wl._pre_roll.append(b"old")
    wl._barge_in_run_started_at = time.monotonic()
    wl._barge_in_signalled_this_run = True
    rt.mic = SimpleNamespace(
        last_frame=InputFrame(b"new", time.monotonic() - 0.5, True),
        dropped_frames=7,
    )

    _, gap = wl._capture_input(rt.mic, "on")

    assert gap
    rt.detector.reset.assert_called_once()
    rt.shadow_vad.reset.assert_called_once()
    assert rt.recent_score == 0.0
    assert not rt.capture_ring and not wl._pre_roll
    assert wl._barge_in_run_started_at == 0.0
    assert not wl._barge_in_signalled_this_run
    status = wl.session_status()["input_audio"]
    assert status["gaps"] == 1
    assert status["capture_dropped_frames"] == 7
    assert status["last_age_ms"] >= 500


@pytest.mark.parametrize("path", ["wake", "manual"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_acquire_drain_failure_releases_started_resources(monkeypatch, path, cancel):
    from unittest.mock import AsyncMock

    from jasper import voice_daemon
    from jasper.voice_daemon import State
    from tests._async_wait import wait_signalled
    from tests._live_turn_fake import FakeLiveTurn
    from tests.usage_store_fixtures import FakeUsageStore

    wl = wake_loop_for_tests()
    turn = FakeLiveTurn()
    wl._connection.acquire_turn = AsyncMock(return_value=turn)
    wl._content_activity.refresh_now = AsyncMock()
    wl._usage_store = usage = FakeUsageStore()
    usage.open_session = lambda **_kwargs: 7
    wl._assistant_output.listening_chirp = AsyncMock()
    wl._play_cue = AsyncMock(return_value=True)
    workers_started, release_started, finish_release = (asyncio.Event() for _ in range(3))
    workers = []
    peer_commands = []

    async def worker(*_args, **_kwargs):
        workers.append(asyncio.current_task())
        if len(workers) == 2:
            workers_started.set()
        await asyncio.Event().wait()

    async def send(command, **_kwargs):
        peer_commands.append(command)
        if command.startswith("ARBITRATE "):
            return {"result": "WIN", "epoch": "test-wake"}
        return {}

    async def release():
        turn.release_calls += 1
        release_started.set()
        await finish_release.wait()

    async def fail_drain():
        await workers_started.wait()
        if path == "wake":
            assert "SESSION_STARTED test-wake" in peer_commands
        raise RuntimeError("acquired input drain failed")

    monkeypatch.setattr(voice_daemon, "play_responses", worker)
    monkeypatch.setattr(voice_daemon, "idle_watchdog", worker)
    wl._peering._send = send
    wl._drain_acquire_audio = fail_drain
    turn.release = release
    if path == "wake":
        wl._acquiring = True
        starting = wl._arbitrate_acquire_drain(
            score=0.9, rms_dbfs=-30.0, spend_allowed=True,
            conn_paused=False, can_serve=True,
        )
    else:
        starting = wl.manual_session_start()
    task = asyncio.create_task(starting)
    try:
        await wait_signalled(release_started, "failed startup provider release", producer=task)
        assert len(workers) == 2
        assert all(worker.done() for worker in workers)
        if cancel:
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            assert wl._state is State.SESSION
            assert wl._output_gate.active_kind == "turn"
        finish_release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
            await wl._cancel_fire_and_forget_tasks()
            wl._play_cue.assert_awaited_once_with("internal_error")
        assert turn.release_calls == 1
        assert usage.close_calls == 1
        assert wl._state is State.WAKE
        assert not wl._output_gate.is_active
        assert wl._turn is None
        assert not wl._bg_tasks
        assert not wl._acquiring
        assert wl._push_to_talk.active_source is None
        assert wl._assistant_output.listening_chirp.await_count == 1
        if path == "wake":
            assert peer_commands.count("SESSION_ENDED test-wake acquire_error") == 1
    finally:
        finish_release.set()
        await asyncio.gather(task, return_exceptions=True)
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await wl._cancel_fire_and_forget_tasks()
